"""
River Vector - Pico Bridge (RP2040)
UART serial link between Raspberry Pi 5 and Pi Pico.
Sends commands to Pico; receives sensor/status messages and dispatches them
to registered handlers. Falls back to sim mode if no serial port is available.
"""

import logging
import threading
import time
from typing import Callable, Dict, List, Optional

from core.constants import FaultCode, HEARTBEAT_TIMEOUT
from pico.protocol import (
    PicoMessage,
    PicoMessageType,
    decode_message,
    encode_message,
)

logger = logging.getLogger(__name__)

MessageHandler = Callable[[PicoMessage], None]


class PicoBridge:
    """
    UART bridge between Raspberry Pi 5 and Pi Pico (RP2040).

    Maintains a background read thread that processes incoming Pico messages
    and dispatches them to registered handlers by message type. Outgoing
    messages are sent synchronously via send(). Automatically falls back to
    sim mode if the serial port cannot be opened (e.g., dev on Chromebook).

    Args:
        port:      Serial device path (e.g. '/dev/ttyACM0').
        baud_rate: UART baud rate.
        sim_mode:  Force simulation mode regardless of port availability.
    """

    HEARTBEAT_INTERVAL_SEC: float = HEARTBEAT_TIMEOUT

    def __init__(
        self,
        port: str = "/dev/ttyACM0",
        baud_rate: int = 115200,
        sim_mode: bool = False,
    ) -> None:
        self._port = port
        self._baud = baud_rate
        self._sim = sim_mode
        self._serial = None
        self._running = False
        self._write_lock = threading.Lock()
        self._read_thread: Optional[threading.Thread] = None
        self._hb_thread: Optional[threading.Thread] = None
        self._handlers: Dict[PicoMessageType, List[MessageHandler]] = {}
        self._last_pico_hb: float = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """
        Opens the serial port and starts read and heartbeat threads.

        Returns:
            True always — falls back to sim mode on serial error.
        """
        if not self._sim:
            try:
                import serial as pyserial
                self._serial = pyserial.Serial(self._port, self._baud, timeout=0.1)
                logger.info(
                    "PicoBridge: connected to %s @ %d baud.", self._port, self._baud
                )
            except Exception as exc:
                logger.warning(
                    "PicoBridge: cannot open %s (%s) — running in sim mode.",
                    self._port, exc,
                )
                self._sim = True

        if self._sim:
            logger.info("PicoBridge: sim mode active — no real serial I/O.")

        self._running = True
        self._read_thread = threading.Thread(
            target=self._read_loop, name="PicoBridge-Read", daemon=True
        )
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, name="PicoBridge-HB", daemon=True
        )
        self._read_thread.start()
        self._hb_thread.start()
        return True

    def disconnect(self) -> None:
        """Stops threads and closes the serial port."""
        self._running = False
        if self._read_thread and self._read_thread.is_alive():
            self._read_thread.join(timeout=2.0)
        if self._hb_thread and self._hb_thread.is_alive():
            self._hb_thread.join(timeout=2.0)
        if self._serial:
            try:
                self._serial.close()
            except Exception:
                pass
        logger.info("PicoBridge disconnected.")

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------

    def send(self, message: PicoMessage) -> None:
        """
        Sends a PicoMessage to the Pico over UART.

        In sim mode, logs the message at DEBUG level instead of transmitting.

        Args:
            message: PicoMessage to transmit.
        """
        if self._sim:
            logger.debug(
                "PicoBridge [SIM TX] %s %s", message.msg_type.value, message.payload
            )
            return
        try:
            data = encode_message(message) + b"\n"
            with self._write_lock:
                if self._serial and self._serial.is_open:
                    self._serial.write(data)
        except Exception as exc:
            logger.error("PicoBridge send error (%s): %s", message.msg_type.value, exc)

    def register_handler(
        self, msg_type: PicoMessageType, handler: MessageHandler
    ) -> None:
        """
        Registers a callback for a specific incoming message type.

        Multiple handlers per type are supported — all are called in order.

        Args:
            msg_type: PicoMessageType to listen for.
            handler:  Callable invoked with the PicoMessage when received.
        """
        self._handlers.setdefault(msg_type, []).append(handler)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def last_heartbeat_age_sec(self) -> float:
        """Seconds since the last HEARTBEAT received from Pico (inf if never)."""
        if self._last_pico_hb == 0.0:
            return float("inf")
        return time.time() - self._last_pico_hb

    @property
    def is_alive(self) -> bool:
        """True if Pico heartbeat is current or sim mode is active."""
        if self._sim:
            return True
        return self.last_heartbeat_age_sec < (self.HEARTBEAT_INTERVAL_SEC * 4)

    @property
    def sim_mode(self) -> bool:
        """True if operating in simulation mode (no real Pico connected)."""
        return self._sim

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _read_loop(self) -> None:
        """Background thread: reads newline-delimited JSON from Pico."""
        while self._running:
            if self._sim:
                time.sleep(0.05)
                continue
            try:
                if self._serial and self._serial.in_waiting:
                    raw = self._serial.readline().rstrip(b"\n")
                    if raw:
                        msg = decode_message(raw)
                        if msg:
                            self._dispatch(msg)
                else:
                    time.sleep(0.01)
            except Exception as exc:
                logger.error("PicoBridge read error: %s", exc)
                time.sleep(0.1)

    def _heartbeat_loop(self) -> None:
        """Sends periodic HEARTBEAT messages to Pico."""
        while self._running:
            self.send(PicoMessage(PicoMessageType.HEARTBEAT, {"ts": time.time()}))
            time.sleep(self.HEARTBEAT_INTERVAL_SEC)

    def _dispatch(self, msg: PicoMessage) -> None:
        """Dispatches a received message to all registered handlers."""
        if msg.msg_type == PicoMessageType.HEARTBEAT:
            self._last_pico_hb = time.time()
            logger.debug("PicoBridge [RX] Pico heartbeat.")

        for handler in self._handlers.get(msg.msg_type, []):
            try:
                handler(msg)
            except Exception as exc:
                logger.error(
                    "PicoBridge handler error for %s: %s",
                    msg.msg_type.value, exc, exc_info=True,
                )

    def __repr__(self) -> str:
        return (
            f"PicoBridge(port={self._port!r}, sim={self._sim}, "
            f"alive={self.is_alive})"
        )
