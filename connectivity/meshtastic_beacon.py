"""
River Vector — Meshtastic Backup Communication Beacon

Provides an off-grid backup channel via LoRa mesh radio (Meshtastic).
Runs independently of 4G LTE. When cellular drops, this is the only way
to send commands to the mower or find out where it stopped.

Hardware
--------
One Meshtastic-compatible LoRa node connected via USB serial to the Pi.
Tested with: Heltec LoRa32 V3, LILYGO T-Beam, RAK WisBlock 4631.
Typical range: 1–5 km line of sight, 300–800 m suburban.

Packet format  (pipe-delimited, kept tiny to respect LoRa duty-cycle)
----------------------------------------------------------------------
Outbound beacon (broadcast every BEACON_INTERVAL_S):
    RV|<unit_id>|<lat>|<lng>|<bat%>|<mode>
    e.g.  RV|VOY-RV-001|40.71298|-74.00618|85|AUTO

Inbound commands (anyone on the mesh can send to the mower node):
    KILL <unit_id>    → triggers emergency stop callback
    WHERE <unit_id>   → sends an immediate position beacon reply
    STATUS <unit_id>  → sends extended status reply

All commands are checked against unit_id so stray packets from other
River units on the same mesh are ignored.

Falls back to sim/log-only mode if pyserial or meshtastic are not
installed, or if no Meshtastic hardware is detected on the serial port.
"""

import logging
import threading
import time
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)

# How often to broadcast a position beacon (seconds)
BEACON_INTERVAL_S: float = 60.0

# If cellular signal drops below this quality, halve the beacon interval
LOW_SIGNAL_THRESHOLD: int = 20   # percent (0–100)

# Serial port where the Meshtastic node is connected
DEFAULT_PORT: str = "/dev/ttyUSB1"


class MeshtasticBeacon:
    """
    Meshtastic LoRa backup beacon for River Vector.

    Runs a background thread that:
    1. Broadcasts GPS + battery + mode every BEACON_INTERVAL_S seconds.
    2. Listens for KILL / WHERE / STATUS commands from the mesh.

    All callbacks are invoked from the background thread — keep them
    short or hand off to a queue.

    Args:
        unit_id:          Mower unit identifier (e.g. 'VOY-RV-001').
        port:             Serial port of the Meshtastic node.
        gps_provider:     Callable returning (lat, lng) or None.
        battery_provider: Callable returning battery % float or None.
        mode_provider:    Callable returning current mode string.
        on_kill:          Called when a KILL command is received.
        on_where:         Called when a WHERE command is received
                          (beacon fires automatically; this is optional extra).
        cellular_quality: Callable returning signal quality 0–100 or None.
        sim_mode:         Force sim/log-only mode.
    """

    def __init__(
        self,
        unit_id: str,
        port: str = DEFAULT_PORT,
        gps_provider: Optional[Callable[[], Optional[Tuple[float, float]]]] = None,
        battery_provider: Optional[Callable[[], Optional[float]]] = None,
        mode_provider: Optional[Callable[[], str]] = None,
        on_kill: Optional[Callable[[], None]] = None,
        on_where: Optional[Callable[[], None]] = None,
        cellular_quality: Optional[Callable[[], Optional[int]]] = None,
        sim_mode: bool = False,
    ) -> None:
        self._unit_id = unit_id
        self._port = port
        self._gps = gps_provider
        self._bat = battery_provider
        self._mode = mode_provider
        self._on_kill = on_kill
        self._on_where = on_where
        self._cellular_quality = cellular_quality
        self._sim = sim_mode

        self._iface = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._force_beacon = threading.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """
        Connect to the Meshtastic node and start the background thread.
        Falls back to sim mode if hardware is unavailable.
        """
        if not self._sim:
            self._sim = not self._connect()

        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, name="meshtastic-beacon", daemon=True
        )
        self._thread.start()
        logger.info(
            "MeshtasticBeacon started for '%s' (sim=%s, port=%s).",
            self._unit_id, self._sim, self._port,
        )

    def stop(self) -> None:
        """Stop the beacon thread and close the Meshtastic connection."""
        self._running = False
        self._force_beacon.set()
        if self._thread:
            self._thread.join(timeout=5.0)
        self._disconnect()
        logger.info("MeshtasticBeacon stopped.")

    def request_beacon(self) -> None:
        """Trigger an immediate beacon broadcast (e.g. on e-stop)."""
        self._force_beacon.set()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _connect(self) -> bool:
        """Open the Meshtastic serial interface. Returns True on success."""
        try:
            import meshtastic.serial_interface
            from pubsub import pub

            self._iface = meshtastic.serial_interface.SerialInterface(self._port)
            pub.subscribe(self._on_receive, "meshtastic.receive.text")
            logger.info("Meshtastic node connected on %s.", self._port)
            return True
        except Exception as exc:
            logger.warning(
                "Meshtastic hardware not found on %s (%s) — running in sim mode.",
                self._port, exc,
            )
            return False

    def _disconnect(self) -> None:
        if self._iface:
            try:
                self._iface.close()
            except Exception:
                pass
            self._iface = None

    def _run_loop(self) -> None:
        """Background loop: periodic beacon + forced-beacon event."""
        while self._running:
            self._send_beacon()

            # Adaptive interval: shorter when cellular is weak
            interval = BEACON_INTERVAL_S
            if self._cellular_quality:
                quality = self._cellular_quality()
                if quality is not None and quality < LOW_SIGNAL_THRESHOLD:
                    interval = BEACON_INTERVAL_S / 2

            # Wait for either the interval or a forced-beacon request
            self._force_beacon.wait(timeout=interval)
            self._force_beacon.clear()

    def _send_beacon(self) -> None:
        """Broadcast a compact position + status packet over the mesh."""
        lat, lng = (None, None)
        if self._gps:
            pos = self._gps()
            if pos:
                lat, lng = pos

        bat = self._bat() if self._bat else None
        mode = self._mode() if self._mode else "UNKNOWN"

        lat_s  = f"{lat:.5f}"  if lat  is not None else "?"
        lng_s  = f"{lng:.5f}"  if lng  is not None else "?"
        bat_s  = f"{bat:.0f}"  if bat  is not None else "?"

        packet = f"RV|{self._unit_id}|{lat_s}|{lng_s}|{bat_s}|{mode}"

        if self._sim:
            logger.info("Meshtastic [SIM] TX: %s", packet)
            return

        try:
            self._iface.sendText(packet, destinationId="^all")
            logger.debug("Meshtastic TX: %s", packet)
        except Exception as exc:
            logger.error("Meshtastic send error: %s", exc)

    def _on_receive(self, packet, interface=None) -> None:
        """
        Handle an incoming Meshtastic text message.

        Parses KILL / WHERE / STATUS commands addressed to this unit.
        """
        try:
            decoded = packet.get("decoded", {})
            text: str = decoded.get("text", "").strip()
        except Exception:
            return

        if not text:
            return

        parts = text.split()
        if len(parts) < 2:
            return

        command = parts[0].upper()
        target  = parts[1]

        if target != self._unit_id:
            return  # Not for us

        logger.warning("Meshtastic RX command: '%s' for '%s'", command, target)

        if command == "KILL":
            logger.critical("Meshtastic KILL command received — triggering e-stop.")
            if self._on_kill:
                self._on_kill()
            # Immediately broadcast a confirmation beacon
            self._force_beacon.set()

        elif command == "WHERE":
            logger.info("Meshtastic WHERE command — sending position beacon.")
            if self._on_where:
                self._on_where()
            self._force_beacon.set()

        elif command == "STATUS":
            self._send_status_reply()

    def _send_status_reply(self) -> None:
        """Send a verbose status reply (triggered by STATUS command)."""
        lat, lng = (None, None)
        if self._gps:
            pos = self._gps()
            if pos:
                lat, lng = pos

        bat  = self._bat()  if self._bat  else None
        mode = self._mode() if self._mode else "UNKNOWN"

        lat_s = f"{lat:.5f}" if lat is not None else "?"
        lng_s = f"{lng:.5f}" if lng is not None else "?"
        bat_s = f"{bat:.0f}" if bat is not None else "?"

        reply = (
            f"STATUS|{self._unit_id}|"
            f"pos={lat_s},{lng_s}|"
            f"bat={bat_s}%|"
            f"mode={mode}"
        )

        if self._sim:
            logger.info("Meshtastic [SIM] STATUS reply: %s", reply)
            return

        try:
            self._iface.sendText(reply, destinationId="^all")
        except Exception as exc:
            logger.error("Meshtastic status reply error: %s", exc)

    def __repr__(self) -> str:
        return (
            f"MeshtasticBeacon(unit={self._unit_id!r}, "
            f"port={self._port!r}, sim={self._sim})"
        )
