"""
River Vector - Fault Management System
Tracks active faults, severity levels, and safe-to-operate state.
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Dict, List, Optional

from core.constants import FaultCode

logger = logging.getLogger(__name__)


class FaultSeverity(Enum):
    """
    Fault severity levels, ordered from least to most severe.

    WARNING:  Degraded operation. Autonomous continues.
    CRITICAL: Operation blocked until acknowledged.
    FATAL:    Operation blocked. Cannot be acknowledged — requires manual reset.
    """
    WARNING  = 1
    CRITICAL = 2
    FATAL    = 3


@dataclass
class FaultRecord:
    """A single fault record tracked by the FaultManager."""
    code: FaultCode
    detail: str
    severity: FaultSeverity
    active: bool = True
    acknowledged: bool = False
    timestamp: float = field(default_factory=time.time)


class FaultManager:
    """
    Central fault registry for the River Vector autonomy suite.

    Tracks active faults, enforces severity-based operation gates,
    and dispatches callbacks to registered listeners on new faults.

    Severity rules for is_safe_to_operate():
        FATAL    → always blocks (cannot be acknowledged)
        CRITICAL → blocks until acknowledged
        WARNING  → never blocks
    """

    def __init__(self) -> None:
        self._records: Dict[FaultCode, FaultRecord] = {}
        self._lock = threading.Lock()
        self._callbacks: List[Callable[[FaultRecord], None]] = []

    # ------------------------------------------------------------------
    # Fault reporting
    # ------------------------------------------------------------------

    def report_fault(
        self,
        code: FaultCode,
        detail: str = "",
        severity: FaultSeverity = FaultSeverity.CRITICAL,
    ) -> FaultRecord:
        """
        Reports a fault. Idempotent — reporting the same code twice does
        not create a duplicate record; the existing record is returned.

        Args:
            code:     Fault identifier.
            detail:   Human-readable description.
            severity: Fault severity level.

        Returns:
            The active FaultRecord for this code.
        """
        with self._lock:
            if code in self._records and self._records[code].active:
                return self._records[code]

            record = FaultRecord(code=code, detail=detail, severity=severity)
            self._records[code] = record

        logger.warning("Fault reported: [%s] %s — %s", severity.name, code.value, detail)

        for cb in self._callbacks:
            try:
                cb(record)
            except Exception as exc:
                logger.error("Fault callback error: %s", exc, exc_info=True)

        return record

    def clear_fault(self, code: FaultCode) -> bool:
        """
        Clears an active fault.

        Args:
            code: Fault to clear.

        Returns:
            True if the fault existed and was cleared, False if it was never reported.
        """
        with self._lock:
            if code not in self._records:
                return False
            self._records[code].active = False

        logger.info("Fault cleared: %s", code.value)
        return True

    def acknowledge_fault(self, code: FaultCode) -> bool:
        """
        Acknowledges a CRITICAL fault, allowing autonomous operation to resume.
        FATAL faults cannot be acknowledged.

        Args:
            code: Fault to acknowledge.

        Returns:
            True if acknowledged, False if the fault is FATAL or not active.
        """
        with self._lock:
            record = self._records.get(code)
            if record is None or not record.active:
                return False
            if record.severity == FaultSeverity.FATAL:
                return False
            record.acknowledged = True

        logger.info("Fault acknowledged: %s", code.value)
        return True

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------

    def has_active_fault(self, code: FaultCode) -> bool:
        """True if the given fault code is currently active."""
        with self._lock:
            record = self._records.get(code)
            return record is not None and record.active

    def is_safe_to_operate(self) -> bool:
        """
        Returns True if autonomous operation is permitted.

        Blocked by any active FATAL fault or any active, unacknowledged CRITICAL fault.
        """
        with self._lock:
            for record in self._records.values():
                if not record.active:
                    continue
                if record.severity == FaultSeverity.FATAL:
                    return False
                if record.severity == FaultSeverity.CRITICAL and not record.acknowledged:
                    return False
        return True

    @property
    def active_faults(self) -> List[FaultRecord]:
        """List of all currently active fault records."""
        with self._lock:
            return [r for r in self._records.values() if r.active]

    @property
    def highest_severity(self) -> Optional[FaultSeverity]:
        """Highest severity among active faults, or None if no faults are active."""
        active = self.active_faults
        if not active:
            return None
        return max(active, key=lambda r: r.severity.value).severity

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def register_callback(self, callback: Callable[[FaultRecord], None]) -> None:
        """Registers a callback invoked whenever a new fault is reported."""
        self._callbacks.append(callback)

    def __repr__(self) -> str:
        return f"FaultManager(active={len(self.active_faults)}, safe={self.is_safe_to_operate()})"
