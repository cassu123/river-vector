"""
River Vector - Emergency Stop (E-Stop)
Immediate, latching halt. Any trigger source locks the system until reset.
"""

import logging
import threading
import time
from typing import Callable, List, Optional

from core.constants import FaultCode
from safety.fault_manager import FaultManager, FaultSeverity

logger = logging.getLogger(__name__)


class EStop:
    """
    Latching emergency stop controller.

    Once triggered, the system is halted and locked until reset() is called.
    The first trigger reason is preserved — subsequent trigger() calls are no-ops.
    Callbacks are fired synchronously on trigger so shutdown logic runs immediately.

    Args:
        fault_manager: FaultManager for recording the ESTOP_TRIGGERED fault.
    """

    def __init__(self, fault_manager: FaultManager) -> None:
        if fault_manager is None:
            raise ValueError("fault_manager must not be None.")
        self._fault_manager = fault_manager
        self._lock = threading.Lock()
        self._triggered = False
        self._armed = False
        self._reason: Optional[str] = None
        self._trigger_time: Optional[float] = None
        self._callbacks: List[Callable[[str], None]] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def arm(self) -> None:
        """Arms the e-stop so it can be triggered. Called once at startup."""
        with self._lock:
            self._armed = True
        logger.info("E-Stop armed.")

    # ------------------------------------------------------------------
    # Trigger / Reset
    # ------------------------------------------------------------------

    def trigger(self, reason: str = "") -> None:
        """
        Triggers the e-stop. Idempotent — first caller sets the reason.

        Args:
            reason: Description of why the e-stop was triggered.
        """
        with self._lock:
            if self._triggered:
                return
            self._triggered = True
            self._reason = reason
            self._trigger_time = time.time()

        logger.critical("E-STOP TRIGGERED — reason: %s", reason)

        self._fault_manager.report_fault(
            FaultCode.ESTOP_TRIGGERED,
            detail=reason,
            severity=FaultSeverity.FATAL,
        )

        for cb in self._callbacks:
            try:
                cb(reason)
            except Exception as exc:
                logger.error("E-Stop callback error: %s", exc, exc_info=True)

    def reset(self) -> bool:
        """
        Clears the triggered state after the situation is resolved.

        Returns:
            True always (reset is always permitted).
        """
        with self._lock:
            if not self._triggered:
                return True
            self._triggered = False
            self._reason = None
            self._trigger_time = None

        self._fault_manager.clear_fault(FaultCode.ESTOP_TRIGGERED)
        logger.info("E-Stop reset — system cleared.")
        return True

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def register_shutdown_callback(self, callback: Callable[[str], None]) -> None:
        """
        Registers a callback invoked immediately when the e-stop is triggered.

        Args:
            callback: Called with the trigger reason string.
        """
        self._callbacks.append(callback)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def is_triggered(self) -> bool:
        """True if the e-stop is currently active."""
        with self._lock:
            return self._triggered

    @property
    def trigger_reason(self) -> Optional[str]:
        """The reason the e-stop was triggered, or None if not triggered."""
        with self._lock:
            return self._reason

    @property
    def trigger_time(self) -> Optional[float]:
        """Unix timestamp of when the e-stop was triggered, or None."""
        with self._lock:
            return self._trigger_time

    def __repr__(self) -> str:
        return f"EStop(triggered={self._triggered}, reason={self._reason!r})"
