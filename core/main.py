"""
River Vector - Main Entry Point

Full startup, claim flow, configuration sync, and autonomy run loop.

Boot sequence (per spec §13.10):
  1. Load bootstrap (/etc/river-vector/bootstrap.json)
  2. Initialize logging
  3. Identity ready (auto-generate unit_id on first boot)
  4. Connect to WiFi from pre-agreed SSID list
  5. Wait for NTP clock sync (up to 30s)
  6. Start ConnectivityProbe
  7. If UNCLAIMED: run claim flow (mDNS + claim_server) until claimed
  8. Register with River Song
  9. Pull operational config (config_sync.ensure())
  10. Build hardware from config
  11. Start telemetry thread
  12. Start command stream (long-poll)
  13. Enter IDLE
  14. Run main loop (drains command queue, kicks watchdog, polls GPS)

This module is universal — it works for any unit running River Vector,
regardless of what hardware that unit has.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import sys
import time
from typing import Any, Dict, Optional

# Core
from core.bootstrap import BootstrapNotFoundError, BootstrapInvalidError, load_bootstrap
from core.compute_topology import ComputeTopologyError, ROLE_CONTROL, ROLE_VISION
from core.constants import (
    BOOTSTRAP_PATH,
    DEFAULT_NODE_NAME,
    LOG_DIR,
    LOG_PATH,
    NTP_SYNC_TIMEOUT_SEC,
    SYSTEM_NAME,
    VERSION,
)
from core.hardware_factory import HardwareFactory, HardwareSuite
from core.identity import Identity

# Connectivity
from connectivity.api_client import RiverSongClient
from connectivity.command_stream import CommandStream
from connectivity.config_sync import ConfigSync, ConfigUnavailableError
from connectivity.connectivity_probe import ConnectivityProbe
from connectivity.telemetry_thread import TelemetryThread
from connectivity.wifi_manager import WifiAssociationError, WifiManager

# Hardware
from hardware.pico_bridge import PicoBridge
from hardware.sensors import SensorManager
from hardware.relays import RelayManager
from hardware.lights import LightManager
from hardware.cameras import CameraManager
from hardware.display import DisplayManager

# Autonomy
from autonomy.manual_control import ManualController, ManualControlError
from autonomy.mode_manager import ModeManager, OperatingMode
from autonomy.mow_session import MowSession
from autonomy.return_home import ReturnHome
from autonomy.teach_mode import TeachManager

# Safety
from safety.estop import EStop
from safety.fault_manager import FaultManager
from safety.interlocks import Interlocks, SlopeAction
from safety.watchdog import Watchdog

# Navigation
from navigation.boundary import BoundaryManager
from navigation.gps_manager import GPSManager
from navigation.path_planner import PathPlanner
from navigation.terrain_monitor import TerrainMonitor

# Telemetry
from telemetry.alerts import AlertMonitor
from telemetry.collector import TelemetryCollector

logger = logging.getLogger(DEFAULT_NODE_NAME)

# Main loop rate (Hz). Telemetry and command streaming are on their own threads.
_LOOP_HZ: float = 10.0
_LOOP_PERIOD: float = 1.0 / _LOOP_HZ


# ──────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────


def _setup_logging() -> None:
    """
    Sets up logging to journald (via stderr) AND to a rotating file at
    /var/log/river-vector/river-vector.log.
    """
    os.makedirs(LOG_DIR, exist_ok=True) if os.access(os.path.dirname(LOG_DIR), os.W_OK) else None

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Clear default handlers — systemd captures stderr.
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    stderr = logging.StreamHandler(stream=sys.stderr)
    stderr.setLevel(logging.INFO)
    stderr.setFormatter(fmt)
    root.addHandler(stderr)

    try:
        file_h = logging.handlers.RotatingFileHandler(
            LOG_PATH,
            maxBytes=50 * 1024 * 1024,
            backupCount=5,
        )
        file_h.setLevel(logging.DEBUG)
        file_h.setFormatter(fmt)
        root.addHandler(file_h)
    except (OSError, PermissionError) as exc:
        # If we can't write to /var/log on dev machines, that's fine.
        logger.warning("Could not open log file at %s: %s", LOG_PATH, exc)


# ──────────────────────────────────────────────────────────────────────────
# NTP sync
# ──────────────────────────────────────────────────────────────────────────


def _wait_for_ntp_sync(timeout_sec: int = NTP_SYNC_TIMEOUT_SEC) -> bool:
    """
    Waits up to timeout_sec for systemd-timesyncd to report NTP sync.

    Returns True if synced, False on timeout. On dev machines without
    timesyncd, returns True immediately (assumed correct).
    """
    if not os.path.exists("/run/systemd/system"):
        logger.info("Non-systemd environment — skipping NTP sync wait.")
        return True

    import subprocess
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            result = subprocess.run(
                ["timedatectl", "show", "--property=NTPSynchronized", "--value"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip().lower() == "yes":
                logger.info("NTP clock synchronized.")
                return True
        except (subprocess.SubprocessError, FileNotFoundError):
            return True  # timedatectl unavailable — assume OK.
        time.sleep(1.0)
    logger.error("NTP sync timeout after %ds.", timeout_sec)
    return False


# ──────────────────────────────────────────────────────────────────────────
# Claim flow
# ──────────────────────────────────────────────────────────────────────────


def _run_claim_flow(identity: Identity) -> None:
    """
    Runs the mDNS + claim_server handshake.

    Blocks until the device is claimed (no timeout — operator-driven).
    Restarts mDNS if needed and prints the claim code repeatedly so
    the operator can see it.
    """
    from connectivity.claim_server import ClaimServer
    from connectivity.mdns_advertise import MdnsAdvertiser

    code = identity.begin_claiming()
    logger.warning("=" * 60)
    logger.warning("DEVICE UNCLAIMED — pairing required")
    logger.warning("unit_id:    %s", identity.unit_id)
    logger.warning("claim_code: %s", code)
    logger.warning("Open the River Song fleet page to claim this device.")
    logger.warning("=" * 60)

    advertiser = MdnsAdvertiser(unit_id=identity.unit_id)
    claim_server = ClaimServer(identity=identity)

    advertiser.start()
    claim_server.start()
    try:
        # Block forever until claimed; the claim_server signals success.
        while not identity.is_claimed:
            claim_server.wait_for_claim(timeout=30.0)
            if not identity.is_claimed:
                logger.info(
                    "Still awaiting claim... unit_id=%s claim_code=%s",
                    identity.unit_id, identity.claim_code,
                )
    finally:
        claim_server.stop()
        advertiser.stop()

    logger.info("Device claimed successfully.")


# ──────────────────────────────────────────────────────────────────────────
# RiverVectorSystem
# ──────────────────────────────────────────────────────────────────────────


class RiverVectorSystem:
    """
    Top-level orchestrator for the River Vector autonomy suite.

    Owns the boot sequence, all subsystems, and the main run loop.
    Subsystems are initialized in dependency order; shutdown is in
    reverse order with safe hardware state.
    """

    def __init__(self) -> None:
        logger.info("=" * 60)
        logger.info("%s v%s starting", SYSTEM_NAME, VERSION)
        logger.info("=" * 60)

        # ── 1. Bootstrap ──────────────────────────────────────────────
        self._bootstrap = load_bootstrap()
        self._identity = Identity(self._bootstrap)
        logger.info("Identity: %s", self._identity)

        # ── 1a. Compute topology ──────────────────────────────────────
        # This node's place in the unit's compute layout (solo vs split).
        # core.main IS the control process, so it must own the control role
        # locally — the control/safety loop is never reached over the network.
        # Vision-only nodes run `python -m vision.node`, not this entrypoint.
        self._compute = self._bootstrap.compute.validated()
        logger.info("Compute topology: %s", self._compute.describe())
        if not self._compute.owns_role(ROLE_CONTROL):
            raise ComputeTopologyError(
                "This node does not own the 'control' role, but core.main is the "
                "control process. Vision-only nodes must run `python -m vision.node`. "
                f"Profile: {self._compute.describe()}"
            )

        # ── 2. Safety foundation ──────────────────────────────────────
        self._fault_manager = FaultManager()
        self._estop = EStop(self._fault_manager)
        self._estop.arm()

        # ── 3. Mode manager (starts in UNCLAIMED) ─────────────────────
        initial_mode = (
            OperatingMode.SETUP_PENDING if self._identity.is_claimed
            else OperatingMode.UNCLAIMED
        )
        self._mode_manager = ModeManager(
            fault_manager=self._fault_manager,
            initial_mode=initial_mode,
        )

        # Network / connectivity placeholders — populated in run().
        self._wifi: Optional[WifiManager] = None
        self._probe: Optional[ConnectivityProbe] = None
        self._api: Optional[RiverSongClient] = None
        self._config_sync: Optional[ConfigSync] = None
        self._command_stream: Optional[CommandStream] = None
        self._telemetry_thread: Optional[TelemetryThread] = None

        # Hardware / autonomy placeholders — populated post-config.
        self._pico: Optional[PicoBridge] = None
        self._suite: Optional[HardwareSuite] = None
        self._sensors: Optional[SensorManager] = None
        self._relays: Optional[RelayManager] = None
        self._lights: Optional[LightManager] = None
        self._cameras: Optional[CameraManager] = None
        self._display: Optional[DisplayManager] = None
        self._gps_manager: Optional[GPSManager] = None
        self._gps_interface = None
        self._terrain = TerrainMonitor()
        self._watchdog: Optional[Watchdog] = None
        self._boundary: Optional[BoundaryManager] = None
        self._interlocks: Optional[Interlocks] = None
        self._path_planner: Optional[PathPlanner] = None
        self._mow_session: Optional[MowSession] = None
        self._return_home: Optional[ReturnHome] = None
        self._alert_monitor: Optional[AlertMonitor] = None
        self._telemetry_collector: Optional[TelemetryCollector] = None
        self._manual: Optional[ManualController] = None
        self._teach: Optional[TeachManager] = None

        # Session bookkeeping.
        self._current_session_id: Optional[str] = None

        self._running = False

    # ──────────────────────────────────────────────────────────────────
    # Boot
    # ──────────────────────────────────────────────────────────────────

    def boot(self) -> None:
        """
        Runs the full boot sequence. Blocks until the device is online,
        claimed, configured, and IDLE — or it raises.
        """
        # ── Connectivity foundation ───────────────────────────────────
        self._wifi = WifiManager(self._bootstrap)
        try:
            self._wifi.connect()
        except WifiAssociationError as exc:
            logger.critical("WiFi association failed: %s", exc)
            raise

        _wait_for_ntp_sync()

        self._probe = ConnectivityProbe(self._bootstrap)
        self._probe.start()

        # ── API client (requires identity + probe) ────────────────────
        self._api = RiverSongClient(self._identity, self._probe)

        # ── Claim flow if needed ──────────────────────────────────────
        if not self._identity.is_claimed:
            self._mode_manager.set_mode(OperatingMode.CLAIMING)
            _run_claim_flow(self._identity)

        # ── Register with River Song ──────────────────────────────────
        self._mode_manager.set_mode(OperatingMode.SETUP_PENDING)
        self._api.register(
            firmware_version=VERSION,
            auto_detected_hardware=self._auto_detect_hardware(),
        )

        # ── Pull operational config ───────────────────────────────────
        self._config_sync = ConfigSync(self._api)
        try:
            config = self._config_sync.ensure()
        except ConfigUnavailableError as exc:
            logger.error("No config available: %s", exc)
            logger.info("Staying in SETUP_PENDING; will retry.")
            self._wait_for_config()
            config = self._config_sync.get_config()

        # ── Build hardware from config ────────────────────────────────
        self._build_hardware(config)

        # ── Wire interlocks with config_sync ──────────────────────────
        self._interlocks = Interlocks(
            sensor_manager=self._sensors,
            gps_manager=self._gps_manager,
            presence=self._suite.presence if self._suite else None,
            fault_manager=self._fault_manager,
            config_sync=self._config_sync,
            terrain=self._terrain,
        )
        self._mode_manager._interlocks = self._interlocks  # late wiring
        self._mode_manager.start()

        # ── Start background threads ──────────────────────────────────
        self._start_telemetry_thread()
        self._start_command_stream()
        self._start_alert_monitor()

        # ── Autonomy adapters ─────────────────────────────────────────
        self._manual = ManualController(
            drive=self._suite.drive if self._suite else None,
            relays=self._relays,
            presence=self._suite.presence if self._suite else None,
            is_manual_mode=lambda: self._mode_manager.mode == OperatingMode.MANUAL,
        )
        self._manual.start()
        self._teach = TeachManager(
            api_client=self._api,
            gps_provider=self._gps_provider,
        )

        # ── Ready ─────────────────────────────────────────────────────
        self._mode_manager.set_mode(OperatingMode.IDLE)
        logger.info("Boot complete — entering main loop.")

    # ──────────────────────────────────────────────────────────────────
    # Main loop
    # ──────────────────────────────────────────────────────────────────

    def run(self) -> None:
        """
        Main loop. Drains the command queue, kicks watchdog, updates display.

        Telemetry and command receipt are on background threads — this
        loop's only job is to dispatch received commands and maintain
        real-time invariants.
        """
        self._running = True
        last_state = self._mode_manager.mode

        while self._running:
            loop_start = time.time()

            try:
                if self._watchdog is not None:
                    self._watchdog.kick()
                if self._gps_interface is not None and hasattr(self._gps_interface, "update"):
                    self._gps_interface.update()
                self._update_terrain_and_enforce_slope()
                self._update_display()
                self._drain_command_queue()
                self._check_config_version()
            except Exception as exc:
                logger.error("Main loop error: %s", exc, exc_info=True)

            elapsed = time.time() - loop_start
            time.sleep(max(0.0, _LOOP_PERIOD - elapsed))

    def shutdown(self) -> None:
        """Graceful shutdown — stops all subsystems in safe order."""
        logger.info("Shutting down %s...", SYSTEM_NAME)
        self._running = False

        try:
            if self._mow_session and self._mow_session.is_active:
                self._mow_session.abort("SHUTDOWN")
        except Exception:
            pass

        for stopper in (
            self._mode_manager.stop if self._mode_manager else None,
            self._command_stream.stop if self._command_stream else None,
            self._telemetry_thread.stop if self._telemetry_thread else None,
            self._alert_monitor.stop if self._alert_monitor else None,
            self._manual.stop if self._manual else None,
            self._probe.stop if self._probe else None,
            self._watchdog.disarm if self._watchdog else None,
        ):
            try:
                if stopper:
                    stopper()
            except Exception as exc:
                logger.error("Shutdown stopper raised: %s", exc, exc_info=True)

        try:
            if self._suite and self._suite.drive:
                self._suite.drive.emergency_stop()
        except Exception:
            pass
        try:
            if self._relays:
                self._relays.emergency_off()
        except Exception:
            pass
        try:
            if self._cameras:
                self._cameras.release()
        except Exception:
            pass
        try:
            if self._display:
                self._display.disconnect()
        except Exception:
            pass
        try:
            if self._pico:
                self._pico.disconnect()
        except Exception:
            pass

        logger.info("Shutdown complete.")

    # ──────────────────────────────────────────────────────────────────
    # Boot helpers
    # ──────────────────────────────────────────────────────────────────

    def _auto_detect_hardware(self) -> Dict[str, Any]:
        """
        Returns a dict of hardware the device can detect on its own.
        Used as a hint for the setup wizard to pre-fill answers.
        """
        return {
            "pico_present": os.path.exists("/dev/ttyACM0"),
            "rpi_serial_present": os.path.exists("/proc/cpuinfo"),
        }

    def _wait_for_config(self) -> None:
        """
        Polls River Song for a config until one becomes available.

        Used when the device boots claimed but the operator has not
        yet completed the setup wizard server-side.
        """
        logger.info("Waiting for operator to complete setup wizard...")
        while self._running or True:
            time.sleep(15.0)
            if self._config_sync.pull():
                return

    def _build_cameras(self, caps):
        """
        Picks the camera implementation based on this node's compute role.

        If this (control) node owns the `vision` role, use the local
        CameraManager. If vision lives on a peer node (split topology), use a
        RemoteCameraManager pointed at it. Either way, absent/failed hardware
        degrades to invalid frames — the rest of the stack is unaffected.
        """
        if self._compute.owns_role(ROLE_VISION):
            sim_cameras = (not caps.has_cameras) or (self._pico and self._pico.sim_mode)
            return CameraManager(sim_mode=bool(sim_cameras))

        peer_url = self._compute.peer_url(ROLE_VISION)
        if not peer_url:
            logger.warning(
                "Vision role is neither local nor has a peer URL; cameras disabled (sim)."
            )
            return CameraManager(sim_mode=True)

        from hardware.remote_camera import RemoteCameraManager
        logger.info("Cameras: delegating vision to peer node at %s.", peer_url)
        return RemoteCameraManager(peer_url)

    def _build_hardware(self, config: Dict[str, Any]) -> None:
        """Builds all hardware subsystems from the pulled config."""
        hardware = config.get("hardware", {})
        pico_cfg = hardware.get("pico_bridge", {}) or {}
        port = pico_cfg.get("port", "/dev/ttyACM0")
        baud = int(pico_cfg.get("baud_rate", 115200))

        self._pico = PicoBridge(port=port, baud_rate=baud)
        try:
            self._pico.connect()
        except Exception as exc:
            logger.warning("PicoBridge connect failed: %s — sim mode.", exc)

        self._suite = HardwareFactory.build(hardware, pico_bridge=self._pico)
        caps = self._suite.capabilities

        self._sensors = SensorManager(self._pico) if self._pico else None
        self._relays = RelayManager(self._pico) if self._pico else None
        self._lights = LightManager(self._pico) if self._pico else None
        self._cameras = self._build_cameras(caps)
        self._display = DisplayManager(sim_mode=(self._pico is None or self._pico.sim_mode))
        try:
            self._display.connect()
        except Exception:
            pass

        # GPS only if hardware declares it.
        if caps.has_gps:
            from hardware.gps import GPSInterface
            self._gps_interface = GPSInterface()
            self._gps_manager = GPSManager(self._gps_interface, self._fault_manager)
        else:
            self._gps_interface = None
            self._gps_manager = None

        # Watchdog timeout from safety_floors.
        wd_ms = int(config["safety_floors"].get("watchdog_timeout_ms", 500))
        self._watchdog = Watchdog(
            fault_manager=self._fault_manager,
            pico_bridge=self._pico,
            timeout_sec=wd_ms / 1000.0,
        )
        self._watchdog.register_timeout_callback(self._on_watchdog_timeout)
        self._watchdog.arm()

        self._boundary = BoundaryManager(self._fault_manager)

        if self._sensors and self._pico:
            self._wire_pico_handlers()

        # Telemetry collector — universal, takes whatever sensors exist.
        self._telemetry_collector = TelemetryCollector(
            unit_id=self._identity.unit_id,
            sensor_manager=self._sensors,
            gps_manager=self._gps_manager,
            mode_manager=self._mode_manager,
            fault_manager=self._fault_manager,
            terrain_monitor=self._terrain,
        )

        # Path planner + sessions (only meaningful with GPS).
        if self._gps_manager is not None:
            self._path_planner = PathPlanner(
                config=_PathPlannerShim(config),
                gps_manager=self._gps_manager,
                boundary_manager=self._boundary,
            )
            self._mow_session = MowSession(
                config=_MowSessionShim(config),
                fault_manager=self._fault_manager,
                relay_manager=self._relays,
                path_planner=self._path_planner,
                sensor_manager=self._sensors,
                api_client=self._api,
                light_manager=self._lights,
            )
            self._return_home = ReturnHome(
                config=_ReturnHomeShim(config),
                fault_manager=self._fault_manager,
                gps_manager=self._gps_manager,
                camera_manager=self._cameras,
                light_manager=self._lights,
            )

    def _wire_pico_handlers(self) -> None:
        from pico.protocol import PicoMessageType
        self._pico.register_handler(PicoMessageType.SENSOR_POWER, self._sensors._handle_power)
        self._pico.register_handler(PicoMessageType.SENSOR_ULTRASONIC, self._sensors._handle_ultrasonic)
        self._pico.register_handler(PicoMessageType.SENSOR_THERMAL, self._sensors._handle_thermal)
        self._pico.register_handler(PicoMessageType.SENSOR_SWITCHES, self._sensors._handle_switches)
        self._pico.register_handler(PicoMessageType.SENSOR_IMU, self._sensors._handle_imu)
        self._pico.register_handler(PicoMessageType.SENSOR_RPM, self._sensors._handle_rpm)

    # ──────────────────────────────────────────────────────────────────
    # Telemetry, command stream, alerts
    # ──────────────────────────────────────────────────────────────────

    def _start_telemetry_thread(self) -> None:
        self._telemetry_thread = TelemetryThread(
            api_client=self._api,
            probe=self._probe,
            build_snapshot=self._build_telemetry_snapshot,
            get_state=lambda: self._mode_manager.mode.name,
        )
        self._telemetry_thread.start()

    def _start_command_stream(self) -> None:
        self._command_stream = CommandStream(
            identity=self._identity,
            probe=self._probe,
            api_client=self._api,
            on_command=self._on_command,
            on_auth_failure=self._on_auth_failure,
            on_config_version=self._on_config_version,
        )
        self._command_stream.start()

    def _start_alert_monitor(self) -> None:
        self._alert_monitor = AlertMonitor(self._fault_manager, api_client=self._api)
        self._alert_monitor.start()

    def _build_telemetry_snapshot(self) -> Dict[str, Any]:
        """Builds a single telemetry snapshot for posting."""
        if self._telemetry_collector is None:
            return {
                "timestamp": time.time(),
                "unit_id": self._identity.unit_id,
                "operating_mode": self._mode_manager.mode.name,
            }
        snap = self._telemetry_collector.collect()
        d = snap.to_dict()
        d["session_id"] = self._current_session_id
        d["connectivity_tier"] = self._probe.tier.value if self._probe else "offline"
        return d

    def _gps_provider(self) -> Optional[Dict[str, float]]:
        """Returns current {lat, lng, alt} for teach mode (alt None without 3-D fix)."""
        if self._gps_manager is None:
            return None
        pos = self._gps_manager.position
        if pos is None:
            return None
        return {
            "lat": float(pos[0]),
            "lng": float(pos[1]),
            "alt": self._gps_manager.fix.altitude_m,
        }

    def _update_terrain_and_enforce_slope(self) -> None:
        """Feeds the latest GPS fix to the terrain monitor and enforces slope limits."""
        if self._gps_manager is None or self._interlocks is None:
            return
        fix = self._gps_manager.fix
        self._terrain.update(fix.latitude, fix.longitude, fix.altitude_m)
        result = self._interlocks.enforce_slope(
            self._terrain.slope_pct, self._mode_manager.mode
        )
        if result.alert is not None and self._alert_monitor is not None:
            self._alert_monitor.emit(result.alert)
        if result.action == SlopeAction.ESTOP:
            self._estop.trigger("SLOPE_SEVERE")
            self._mode_manager.trigger_estop("SLOPE_SEVERE")
        elif result.action == SlopeAction.HOLD:
            self._mode_manager.request_hold("SLOPE_LIMIT")

    # ──────────────────────────────────────────────────────────────────
    # Command dispatch
    # ──────────────────────────────────────────────────────────────────

    def _on_command(self, cmd: Dict[str, Any]) -> None:
        """Pushes a received command onto the queue. Drained in main loop."""
        # The command_stream already has a queue; we use it directly.
        pass  # No-op — main loop reads from command_stream.queue.

    def _drain_command_queue(self) -> None:
        """Drains the command stream queue and dispatches each command."""
        if self._command_stream is None:
            return
        q = self._command_stream.queue
        while not q.empty():
            try:
                cmd = q.get_nowait()
            except Exception:
                break
            self._dispatch(cmd)

    def _dispatch(self, cmd: Dict[str, Any]) -> None:
        """Routes a command to the right subsystem."""
        action = cmd.get("action", "")
        params = cmd.get("params", {}) or {}
        command_id = cmd.get("command_id", "")
        logger.info("Dispatching command: %s (id=%s)", action, command_id)

        try:
            if action == "mow_start":
                self._handle_mow_start(params)
            elif action == "mow_stop":
                self._handle_mow_stop(params)
            elif action == "return_home":
                self._handle_return_home()
            elif action == "estop":
                self._handle_estop(params)
            elif action == "estop_reset":
                self._handle_estop_reset()
            elif action == "config.refresh":
                self._config_sync.pull()
            elif action.startswith("manual."):
                self._manual.handle(action, params)
            elif action.startswith("teach."):
                self._teach.handle(action, params)
            else:
                logger.warning("Unknown command action: %s", action)
                if command_id:
                    self._api.complete_command(command_id, success=False,
                                               result={"error": "unknown_action"})
                return

            if command_id:
                self._api.complete_command(command_id, success=True)
        except ManualControlError as exc:
            logger.warning("Manual command rejected: %s", exc)
            if command_id:
                self._api.complete_command(command_id, success=False,
                                           result={"error": str(exc)})
        except Exception as exc:
            logger.error("Command dispatch failed: %s", exc, exc_info=True)
            if command_id:
                self._api.complete_command(command_id, success=False,
                                           result={"error": str(exc)})

    def _handle_mow_start(self, params: Dict[str, Any]) -> None:
        if self._mow_session is None or self._mode_manager is None:
            raise RuntimeError("Autonomy not initialized (no GPS / path planner).")
        if not self._mode_manager.request_auto():
            raise RuntimeError("AUTO mode denied — interlock or fault.")
        # Begin session bookkeeping.
        program_id = params.get("program_id")
        version = self._config_sync.get_revision()
        self._current_session_id = self._api.session_start(
            program_id=program_id,
            config_version=version,
        )
        logger.info("Session started: id=%s program=%s",
                    self._current_session_id, program_id)
        self._mow_session.start()

    def _handle_mow_stop(self, params: Dict[str, Any]) -> None:
        if self._mow_session and self._mow_session.is_active:
            self._mow_session.abort(params.get("reason", "REMOTE_STOP"))
        self._end_session("aborted", abort_reason=params.get("reason"))
        self._mode_manager.request_manual()
        self._mode_manager.set_mode(OperatingMode.IDLE)

    def _handle_return_home(self) -> None:
        if self._mow_session and self._mow_session.is_active:
            self._mow_session.complete()
            self._end_session("completed")
        if self._return_home:
            self._mode_manager.set_mode(OperatingMode.RETURNING_HOME)
            self._return_home.execute()
            self._mode_manager.set_mode(OperatingMode.IDLE)

    def _handle_estop(self, params: Dict[str, Any]) -> None:
        reason = params.get("reason", "REMOTE_ESTOP")
        self._estop.trigger(reason)
        self._end_session("aborted", abort_reason=f"estop:{reason}")

    def _handle_estop_reset(self) -> None:
        self._estop.reset()
        self._mode_manager.reset_estop()

    def _end_session(
        self,
        status: str,
        abort_reason: Optional[str] = None,
    ) -> None:
        if not self._current_session_id:
            return
        self._api.session_end(
            session_id=self._current_session_id,
            status=status,
            abort_reason=abort_reason,
        )
        self._current_session_id = None

    # ──────────────────────────────────────────────────────────────────
    # Stream event hooks
    # ──────────────────────────────────────────────────────────────────

    def _on_auth_failure(self) -> None:
        """Drops to UNCLAIMED on persistent auth failure."""
        logger.critical("Auth failure — resetting claim state.")
        try:
            self._identity.reset_claim()
        finally:
            self._mode_manager.set_mode(OperatingMode.UNCLAIMED)
            self._running = False

    def _on_config_version(self, new_version: int) -> None:
        """Triggered when X-Config-Version header changes."""
        current = self._config_sync.get_revision()
        if new_version <= current:
            return
        if self._mow_session and self._mow_session.is_active:
            # Mid-session: only pull safety_floors changes immediately.
            # Spec §9: tightenings apply immediately. We simplify by
            # always re-pulling; the device clamps anyway.
            logger.info(
                "Config version bumped mid-session (%d → %d); pulling for safety floors.",
                current, new_version,
            )
        else:
            logger.info(
                "Config version bumped (%d → %d); pulling new config.",
                current, new_version,
            )
        self._config_sync.pull()

    def _on_watchdog_timeout(self) -> None:
        logger.critical("Watchdog timeout — e-stop.")
        self._estop.trigger("WATCHDOG_TIMEOUT")

    def _update_display(self) -> None:
        if self._display is None or self._sensors is None:
            return
        try:
            snap = self._sensors.snapshot
            if snap.fuel_percent is not None:
                self._display.update_fuel(snap.fuel_percent)
            if snap.battery_voltage_v is not None:
                self._display.update_voltage(snap.battery_voltage_v)
            if self._gps_manager is not None:
                fix = self._gps_manager.fix
                self._display.update_gps(fix.fix_quality.value, fix.accuracy_m)
            faults = self._fault_manager.active_faults
            if faults:
                self._display.show_fault(faults[0].code.value)
            else:
                self._display.clear_fault()
        except Exception as exc:
            logger.debug("Display update failed: %s", exc)

    def _check_config_version(self) -> None:
        """No-op placeholder — command_stream invokes on_config_version directly."""
        return


# ──────────────────────────────────────────────────────────────────────────
# Legacy config shims
#
# The autonomy modules (MowSession, ReturnHome, PathPlanner) were written
# against a flat attribute-style config object. Until they are refactored
# to read from config_sync directly, these shims adapt the new
# dict-based config to the old attribute interface.
# ──────────────────────────────────────────────────────────────────────────


class _ConfigShimBase:
    def __init__(self, config: Dict[str, Any]) -> None:
        hw = config.get("hardware", {})
        program = config.get("assigned_program") or {}
        home = config.get("home_position") or {}
        self.unit_id = config.get("unit_id", "")
        self.name = config.get("name", "")
        self.platform = config.get("platform", "riding")
        self.deck_width_inches = float(hw.get("deck", {}).get("width_inches", 42))
        self.transmission = hw.get("drive", {}).get("type", "clutch")
        self.hardware = hw
        self.features = []
        self.home_position = {"lat": home.get("lat"), "lng": home.get("lng")}
        self.assigned_program = program


class _PathPlannerShim(_ConfigShimBase):
    pass


class _MowSessionShim(_ConfigShimBase):
    pass


class _ReturnHomeShim(_ConfigShimBase):
    pass


# ──────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────


def main(argv=None) -> None:
    _setup_logging()

    try:
        load_bootstrap()
    except BootstrapNotFoundError:
        print(
            f"Fatal: bootstrap not found at {BOOTSTRAP_PATH}.\n"
            "Provision this device first — see scripts/install.sh.",
            file=sys.stderr,
        )
        sys.exit(1)
    except BootstrapInvalidError as exc:
        print(f"Fatal: bootstrap invalid: {exc}", file=sys.stderr)
        sys.exit(1)

    system = RiverVectorSystem()

    def _handle_signal(sig, _frame):
        logger.info("Signal %s received — shutting down.", sig)
        system.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        system.boot()
        system.run()
    except Exception as exc:
        logger.critical("Fatal error: %s", exc, exc_info=True)
        system.shutdown()
        sys.exit(1)
    finally:
        system.shutdown()


if __name__ == "__main__":
    main()
