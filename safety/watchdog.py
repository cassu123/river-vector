"""
River Vector - System Watchdog
Monitors the Pi-Pico UART heartbeat and the main autonomy compute loop.
Reports PICO_TIMEOUT (FATAL) and fires registered callbacks on timeout.
"""

import logging
import threading
import time
from typing import Callable, List, Optional

from core.constants import FaultCode, HEARTBEAT_TIMEOUT
from safety.fault_manager import FaultManager, FaultSeverity

logger = logging.getLogger(__name__)


class Watchdog:
    """
    Two-channel watchdog for the River Vector autonomy suite.

    Channel 1 — Pico heartbeat: monitors PicoBridge.is_alive. Reports
    PICO_TIMEOUT (FATAL) and fires callbacks if the Pico goes silent for
    more than 4× the heartbeat interval.

    Channel 2 — Compute loop: requires the main loop to call kick()
    periodically. Reports PICO_TIMEOUT (FATAL) if the loop hangs beyond
    2× the configured timeout.

    Args:
        fault_manager: FaultManager for fault reporting.
        pico_bridge:   PicoBridge for heartbeat monitoring (optional).
        timeout_sec:   Seconds without a kick before compute-loop fault.
    """

    # Poll interval = 1/4 of timeout so we catch failures within one period
    _POLL_DIVISOR: int = 4

    def __init__(
        self,
        fault_manager: FaultManager,
        pico_bridge=None,
        timeout_sec: float = HEARTBEAT_TIMEOUT,
    ) -> None:
        if fault_manager is None:
            raise ValueError("fault_manager must not be None.")
        self._fault_manager = fault_manager
        self._pico = pico_bridge
        self._timeout = timeout_sec
        self._last_kick: float = time.time()
        self._running = False
        self._armed = False
        self._thread: Optional[threading.Thread] = None
        self._callbacks: List[Callable[[], None]] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def arm(self) -> None:
        """Arms the watchdog and starts the monitoring thread."""
        self._last_kick = time.time()
        self._armed = True
        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop, name="Watchdog", daemon=True
        )
        self._thread.start()
        logger.info("Watchdog armed — timeout=%.2fs.", self._timeout)

    def disarm(self) -> None:
        """Stops the watchdog monitoring thread."""
        self._armed = False
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info("Watchdog disarmed.")

    def kick(self) -> None:
        """
        Resets the compute-loop watchdog timer.
        Call this from the main autonomy loop at least once per timeout_sec.
        """
        self._last_kick = time.time()

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def register_timeout_callback(self, callback: Callable[[], None]) -> None:
        """
        Registers a callback invoked when the watchdog fires.

        Args:
            callback: Called with no arguments on timeout. Keep it fast.
        """
        self._callbacks.append(callback)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def kick_age_sec(self) -> float:
        """Seconds since the last kick()."""
        return time.time() - self._last_kick

    @property
    def is_armed(self) -> bool:
        """True if the watchdog is active."""
        return self._armed

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _monitor_loop(self) -> None:
        poll_interval = max(self._timeout / self._POLL_DIVISOR, 0.05)
        while self._running:
            try:
                self._check_pico()
                self._check_compute()
            except Exception as exc:
                logger.error("Watchdog monitor error: %s", exc, exc_info=True)
            time.sleep(poll_interval)

    def _check_pico(self) -> None:
        """Fires PICO_TIMEOUT fault if the Pico has gone silent."""
        if self._pico is None:
            return

        if not self._pico.is_alive:
            age = self._pico.last_heartbeat_age_sec
            if not self._fault_manager.has_active_fault(FaultCode.PICO_TIMEOUT):
                logger.critical(
                    "Watchdog: Pico heartbeat lost (silent for %.2fs).", age
                )
                self._fault_manager.report_fault(
                    FaultCode.PICO_TIMEOUT,
                    detail=f"Pico silent for {age:.2f}s — UART link down.",
                    severity=FaultSeverity.FATAL,
                )
                self._fire_callbacks()
        else:
            if self._fault_manager.has_active_fault(FaultCode.PICO_TIMEOUT):
                self._fault_manager.clear_fault(FaultCode.PICO_TIMEOUT)
                logger.info("Watchdog: Pico heartbeat restored.")

    def _check_compute(self) -> None:
        """Fires PICO_TIMEOUT fault if the main compute loop has hung."""
        age = self.kick_age_sec
        threshold = self._timeout * 2.0
        if age > threshold:
            fault_code = FaultCode.PICO_TIMEOUT  # Reuse PICO_TIMEOUT as the compute-hung signal
            if not self._fault_manager.has_active_fault(fault_code):
                logger.critical(
                    "Watchdog: compute loop hung (no kick for %.2fs).", age
                )
                self._fault_manager.report_fault(
                    fault_code,
                    detail=f"Main loop hung for {age:.2f}s.",
                    severity=FaultSeverity.FATAL,
                )
                self._fire_callbacks()

    def _fire_callbacks(self) -> None:
        for cb in self._callbacks:
            try:
                cb()
            except Exception as exc:
                logger.error("Watchdog callback error: %s", exc, exc_info=True)

    def __repr__(self) -> str:
        return (
            f"Watchdog(armed={self._armed}, "
            f"kick_age={self.kick_age_sec:.2f}s, "
            f"timeout={self._timeout}s)"
        )
