"""
River Vector - Main Entry Point
Full startup, wiring, and run loop for the River Vector autonomy suite.
Platform-agnostic: the unit profile drives hardware selection at runtime.
All hardware falls back to sim mode when physical devices are unavailable.
"""

import dataclasses
import logging
import os
import signal
import sys
import time

from core.constants import (
    DEFAULT_NODE_NAME,
    HEARTBEAT_TIMEOUT,
    RIVER_SONG_BASE_URL,
    SYSTEM_NAME,
    VERSION,
)
from core.hardware_factory import HardwareFactory
from core.unit_profile import UnitProfile

# Hardware
from hardware.cameras import CameraManager
from hardware.display import DisplayManager
from hardware.lights import LightManager
from hardware.pico_bridge import PicoBridge
from hardware.relays import RelayManager
from hardware.sensors import SensorManager

# Navigation
from navigation.boundary import BoundaryManager
from navigation.gps_manager import GPSManager
from navigation.parking import ParkingController
from navigation.path_planner import PathPlanner

# Autonomy
from autonomy.mode_manager import ModeManager, OperatingMode
from autonomy.mow_session import MowSession
from autonomy.return_home import ReturnHome

# Safety
from safety.estop import EStop
from safety.fault_manager import FaultManager
from safety.interlocks import Interlocks
from safety.watchdog import Watchdog

# Connectivity
from connectivity.api_client import RiverSongClient
from connectivity.meshtastic_beacon import MeshtasticBeacon

# Telemetry
from telemetry.alerts import AlertMonitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(DEFAULT_NODE_NAME)

DEFAULT_PROFILE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "units", "voyager.json"
)

# Heartbeat loop rate
_LOOP_HZ: float = 10.0
_LOOP_PERIOD: float = 1.0 / _LOOP_HZ


class RiverVectorSystem:
    """
    Top-level orchestrator for the River Vector autonomy suite.

    Initialises all subsystems in dependency order, wires sensor callbacks
    to safety consumers, and runs the main status loop. On shutdown, tears
    down in reverse order to ensure safe hardware state.

    Args:
        profile: Loaded UnitProfile for this unit.
    """

    def __init__(self, profile: UnitProfile) -> None:
        self._profile = profile
        self._running = False

        logger.info(
            "=== %s v%s — initialising %s (%s) ===",
            SYSTEM_NAME, VERSION, profile.unit_name, profile.unit_id,
        )

        # ── 1. Safety foundation ─────────────────────────────────────
        self._fault_manager = FaultManager()
        self._estop = EStop(self._fault_manager)
        self._estop.arm()

        # ── 2. Pico bridge (UART link to RP2040) ─────────────────────
        pico_cfg = profile.hardware.pico_bridge
        self._pico = PicoBridge(port=pico_cfg.port, baud_rate=pico_cfg.baud_rate)
        self._pico.connect()

        # ── 3. Hardware subsystems (all sim-safe) ────────────────────
        hardware = HardwareFactory.build(profile, pico_bridge=self._pico)
        self._drive = hardware.drive
        self._presence = hardware.presence

        self._relays = RelayManager(self._pico)
        self._lights = LightManager(self._pico)
        self._sensors = SensorManager(self._pico)
        self._cameras = CameraManager(sim_mode=self._pico.sim_mode)
        self._display = DisplayManager(sim_mode=self._pico.sim_mode)
        self._display.connect()

        # ── 4. Sensor → Pico message wiring ─────────────────────────
        from pico.protocol import PicoMessageType
        self._pico.register_handler(
            PicoMessageType.SENSOR_POWER, self._sensors._handle_power
        )
        self._pico.register_handler(
            PicoMessageType.SENSOR_ULTRASONIC, self._sensors._handle_ultrasonic
        )
        self._pico.register_handler(
            PicoMessageType.SENSOR_THERMAL, self._sensors._handle_thermal
        )
        self._pico.register_handler(
            PicoMessageType.SENSOR_SWITCHES, self._sensors._handle_switches
        )
        self._pico.register_handler(
            PicoMessageType.SENSOR_IMU, self._sensors._handle_imu
        )
        self._pico.register_handler(
            PicoMessageType.SENSOR_RPM, self._sensors._handle_rpm
        )

        # ── 5. GPS ───────────────────────────────────────────────────
        from hardware.gps import GPSInterface
        self._gps_interface = GPSInterface()
        self._gps_manager = GPSManager(self._gps_interface, self._fault_manager)

        # ── 6. Watchdog ──────────────────────────────────────────────
        self._watchdog = Watchdog(
            fault_manager=self._fault_manager,
            pico_bridge=self._pico,
            timeout_sec=profile.safety.watchdog_timeout_ms / 1000.0,
        )
        self._watchdog.register_timeout_callback(self._on_watchdog_timeout)
        self._watchdog.arm()

        # ── 7. Boundary ──────────────────────────────────────────────
        self._boundary = BoundaryManager(self._fault_manager)

        # ── 8. Interlocks ────────────────────────────────────────────
        self._interlocks = Interlocks(
            sensor_manager=self._sensors,
            gps_manager=self._gps_manager,
            presence=self._presence,
            fault_manager=self._fault_manager,
        )

        # ── 9. Mode manager ──────────────────────────────────────────
        self._mode_manager = ModeManager(
            fault_manager=self._fault_manager,
            interlocks=self._interlocks,
        )
        self._mode_manager.register_mode_callback(self._on_mode_change)
        self._mode_manager.start()

        # ── 10. Connectivity & telemetry ─────────────────────────────
        self._cfg_shim = self._build_config_shim()
        self._api = None
        api_key = os.environ.get("RIVER_SONG_API_KEY", "")
        if api_key:
            self._api = RiverSongClient(self._cfg_shim)
            self._api.register()

        self._alert_monitor = AlertMonitor(self._fault_manager, api_client=self._api)
        self._alert_monitor.start()

        # ── 11. Path planner & sessions ──────────────────────────────
        self._path_planner = PathPlanner(
            config=self._cfg_shim,
            gps_manager=self._gps_manager,
            boundary_manager=self._boundary,
        )
        self._mow_session = MowSession(
            config=self._cfg_shim,
            fault_manager=self._fault_manager,
            relay_manager=self._relays,
            path_planner=self._path_planner,
            sensor_manager=self._sensors,
            api_client=self._api,
            light_manager=self._lights,
        )
        self._return_home = ReturnHome(
            config=self._cfg_shim,
            fault_manager=self._fault_manager,
            gps_manager=self._gps_manager,
            camera_manager=self._cameras,
            light_manager=self._lights,
        )

        # ── 12. E-stop wiring ────────────────────────────────────────
        self._estop.register_shutdown_callback(self._on_estop)
        self._fault_manager.register_callback(
            lambda r: self._estop.trigger(r.code) if r.severity.value >= 3 else None
        )

        # ── 13. Meshtastic backup beacon ─────────────────────────────
        meshtastic_port = os.environ.get("MESHTASTIC_PORT", "/dev/ttyUSB1")
        self._beacon = MeshtasticBeacon(
            unit_id=profile.unit_id,
            port=meshtastic_port,
            gps_provider=lambda: (
                (self._gps_manager.latitude, self._gps_manager.longitude)
                if self._gps_manager.has_fix else None
            ),
            battery_provider=lambda: self._sensors.battery_percent,
            mode_provider=lambda: self._mode_manager.current_mode.name,
            on_kill=lambda: self._estop.trigger("MESHTASTIC_KILL"),
            on_where=None,  # beacon fires automatically on WHERE
            cellular_quality=lambda: getattr(self, "_cellular_quality", None),
        )
        self._beacon.start()

        self._lights.indicate_idle()
        logger.info("System initialisation complete — %s ready.", profile.unit_name)

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """
        Runs the main status and telemetry loop until shutdown is requested.

        Kicks the watchdog, updates the display, and polls GPS on every tick.
        The actual mow session and mode transitions are event-driven via
        callbacks and the River Song API command poll.
        """
        self._running = True
        logger.info("Main loop starting at %.0fHz.", _LOOP_HZ)

        while self._running:
            loop_start = time.time()

            try:
                self._watchdog.kick()
                self._gps_interface.update()
                self._update_display()

                if self._api:
                    self._poll_api_commands()

            except Exception as exc:
                logger.error("Main loop error: %s", exc, exc_info=True)

            elapsed = time.time() - loop_start
            sleep_time = max(0.0, _LOOP_PERIOD - elapsed)
            time.sleep(sleep_time)

        logger.info("Main loop exited.")

    def shutdown(self) -> None:
        """Graceful shutdown — stops all subsystems in safe order."""
        logger.info("Shutting down %s...", self._profile.unit_name)
        self._running = False

        if self._mow_session.is_active:
            self._mow_session.abort("SHUTDOWN")

        self._mode_manager.stop()
        self._alert_monitor.stop()
        self._beacon.stop()
        self._watchdog.disarm()
        self._drive.emergency_stop()
        self._relays.emergency_off()
        self._lights.all_off()
        self._cameras.release()
        self._display.disconnect()
        self._pico.disconnect()

        logger.info("Shutdown complete.")

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _on_mode_change(
        self, old_mode: OperatingMode, new_mode: OperatingMode
    ) -> None:
        """Responds to operating mode transitions."""
        logger.info("Mode: %s → %s", old_mode.name, new_mode.name)

        if new_mode == OperatingMode.AUTO:
            self._lights.indicate_auto()
            self._display.update_mode("AUTO")
        elif new_mode == OperatingMode.MANUAL:
            self._lights.indicate_manual()
            self._display.update_mode("MANUAL")
        elif new_mode == OperatingMode.ESTOP:
            self._lights.indicate_estop()
            self._display.update_mode("ESTOP")
        elif new_mode == OperatingMode.FAULT:
            self._lights.indicate_fault()
            self._display.update_mode("FAULT")

        if self._api:
            self._api.push_status({"operating_mode": new_mode.name})

    def _on_estop(self, reason: str) -> None:
        """E-stop hardware callback — cuts drive and relays immediately."""
        logger.critical("E-STOP ENGAGED: %s", reason)
        self._drive.emergency_stop()
        self._relays.emergency_off()
        self._mode_manager.trigger_estop(reason)
        self._display.show_fault(f"ESTOP: {reason[:15]}")

    def _on_watchdog_timeout(self) -> None:
        """Watchdog timeout callback — triggers e-stop."""
        logger.critical("Watchdog timeout — triggering e-stop.")
        self._estop.trigger("WATCHDOG_TIMEOUT")

    # ------------------------------------------------------------------
    # Display update
    # ------------------------------------------------------------------

    def _update_display(self) -> None:
        """Pushes current telemetry to the operator display."""
        snap = self._sensors.snapshot
        fix = self._gps_manager.fix

        self._display.update_fuel(snap.fuel_percent)
        self._display.update_voltage(snap.battery_voltage_v)
        self._display.update_gps(fix.fix_quality.value, fix.accuracy_m)

        faults = self._fault_manager.active_faults
        if faults:
            self._display.show_fault(faults[0].code.value)
        else:
            self._display.clear_fault()

    # ------------------------------------------------------------------
    # API command polling
    # ------------------------------------------------------------------

    def _poll_api_commands(self) -> None:
        """Polls River Song for pending commands and executes them."""
        if not self._api:
            return
        cmd = self._api.poll_commands()
        if cmd is None:
            return

        action = cmd.get("action", "")
        logger.info("Received River Song command: %s", action)

        if action == "mow_start":
            self._mode_manager.request_auto()
            if self._mode_manager.is_autonomous:
                self._mow_session.start()

        elif action == "mow_stop":
            self._mow_session.abort("REMOTE_STOP")
            self._mode_manager.request_manual()

        elif action == "return_home":
            self._mow_session.complete()
            self._return_home.execute()

        elif action == "estop":
            self._estop.trigger("REMOTE_ESTOP")

        elif action == "estop_reset":
            self._estop.reset()
            self._mode_manager.reset_estop()

    # ------------------------------------------------------------------
    # Config shim
    # ------------------------------------------------------------------

    def _build_config_shim(self):
        """
        Builds a simple config shim so legacy components that expect a
        flat config object (with attributes like deck_width_inches) work
        alongside the structured UnitProfile.
        """
        profile = self._profile

        class _Shim:
            unit_id = profile.unit_id
            name = profile.unit_name
            platform = profile.platform
            deck_width_inches = profile.hardware.deck.width_inches
            transmission = profile.hardware.drive.type
            hardware = {}
            features = []
            unit_config = dataclasses.asdict(profile)
            river_song_api_key = os.environ.get("RIVER_SONG_API_KEY", "")
            home_position = {"lat": None, "lng": None}

        return _Shim()


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

_UNITS_DIR = os.path.join(os.path.dirname(__file__), "..", "units")


def _list_units() -> None:
    """Prints all unit profiles found in units/ and exits."""
    import glob
    files = sorted(glob.glob(os.path.join(_UNITS_DIR, "*.json")))
    if not files:
        print("No unit profiles found in units/")
        return
    print(f"{'NAME':<20} {'ID':<14} {'PLATFORM':<10} {'DRIVE':<16} {'CAMERAS'}")
    print("-" * 70)
    for path in files:
        try:
            p = UnitProfile.from_file(path)
            print(
                f"{p.unit_name:<20} {p.unit_id:<14} {p.platform:<10} "
                f"{p.hardware.drive.type:<16} {p.hardware.cameras}"
            )
        except Exception as exc:
            print(f"  {os.path.basename(path)}: (parse error — {exc})")


def _resolve_unit_path(name: str) -> str:
    """
    Resolves a unit name or path to an absolute profile path.

    Accepts:
      - A bare name:  'voyager'  → units/voyager.json
      - A JSON path:  'units/voyager.json' or '/abs/path/to/unit.json'
    """
    if name.endswith(".json"):
        return os.path.abspath(name)
    return os.path.abspath(os.path.join(_UNITS_DIR, f"{name}.json"))


def main(args=None) -> None:
    import argparse
    parser = argparse.ArgumentParser(
        prog="python3 -m core.main",
        description="River Vector Autonomy Suite",
    )
    parser.add_argument(
        "--unit", metavar="NAME",
        help=(
            "Unit to run — bare name (voyager, scout, push_ryobi) or path to "
            "a .json profile. Overrides RIVER_VECTOR_UNIT env var."
        ),
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List all available unit profiles and exit.",
    )
    parsed = parser.parse_args(args)

    if parsed.list:
        _list_units()
        return

    if parsed.unit:
        profile_path = _resolve_unit_path(parsed.unit)
    else:
        profile_path = os.environ.get("RIVER_VECTOR_UNIT", DEFAULT_PROFILE_PATH)

    try:
        profile = UnitProfile.from_file(profile_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Fatal: could not load unit profile: {exc}", file=sys.stderr)
        print("Run with --list to see available units.", file=sys.stderr)
        sys.exit(1)

    system = RiverVectorSystem(profile)

    def _handle_signal(sig, frame):
        logger.info("Signal %s received — shutting down.", sig)
        system.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        system.run()
    except Exception as exc:
        logger.critical("Fatal error in main loop: %s", exc, exc_info=True)
        system.shutdown()
        sys.exit(1)
    finally:
        system.shutdown()


if __name__ == "__main__":
    main()
