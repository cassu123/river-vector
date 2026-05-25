"""
================================================================================
Project:  River Vector — Autonomous Mower Control System
File:     telemetry/alerts.py
Purpose:  Threshold monitoring and push notification dispatch. Watches
          telemetry values and fault states, then pushes alerts to the
          River Song API when conditions warrant operator notification.
Author:   [Author Placeholder]
Version:  0.1.0
Date:     2026-05-25
================================================================================
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Dict, List, Optional

from core.constants import (
    CRITICAL_TEMP_C,
    MIN_FUEL_PERCENT,
    MIN_VOLTAGE_V,
    FaultCode,
)
from safety.fault_manager import FaultManager, FaultRecord, FaultSeverity

logger = logging.getLogger(__name__)


class AlertLevel(Enum):
    """Alert severity levels for push notifications."""
    INFO = auto()
    WARNING = auto()
    CRITICAL = auto()


@dataclass
class Alert:
    """A single alert event."""
    level: AlertLevel
    title: str
    message: str
    fault_code: str = FaultCode.NONE
    timestamp: float = field(default_factory=time.time)
    sent: bool = False


class AlertMonitor:
    """
    Monitors fault states and telemetry thresholds, dispatching push
    alerts to registered handlers (River Song API, local log, etc.).

    Deduplicates alerts — the same alert is not re-sent until the
    condition clears and re-triggers.

    Args:
        fault_manager: FaultManager to monitor for fault events.
        api_client: RiverSongClient for push notifications (optional).
        alert_interval_sec: Minimum seconds between repeated alerts for the same fault.
    """

    DEFAULT_ALERT_INTERVAL_SEC: float = 60.0

    def __init__(
        self,
        fault_manager: FaultManager,
        api_client=None,
        alert_interval_sec: float = DEFAULT_ALERT_INTERVAL_SEC,
    ) -> None:
        if fault_manager is None:
            raise ValueError("fault_manager must not be None.")
        self._fault_manager = fault_manager
        self._api = api_client
        self._alert_interval = alert_interval_sec
        self._sent_alerts: Dict[str, float] = {}  # fault_code → last sent timestamp
        self._handlers: List[Callable[[Alert], None]] = []
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Register as fault callback
        self._fault_manager.register_callback(self._on_fault)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Starts the alert monitoring thread."""
        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop,
            name="AlertMonitor",
            daemon=True,
        )
        self._thread.start()
        logger.info("Alert monitor started.")

    def stop(self) -> None:
        """Stops the alert monitoring thread."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info("Alert monitor stopped.")

    # ------------------------------------------------------------------
    # Handler registration
    # ------------------------------------------------------------------

    def register_handler(self, handler: Callable[[Alert], None]) -> None:
        """
        Registers a callback invoked when an alert is dispatched.

        Args:
            handler: Callable accepting an Alert instance.
        """
        self._handlers.append(handler)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_fault(self, record: FaultRecord) -> None:
        """
        Fault manager callback — converts fault records to alerts.

        Args:
            record: FaultRecord from the fault manager.
        """
        level_map = {
            FaultSeverity.WARNING: AlertLevel.WARNING,
            FaultSeverity.CRITICAL: AlertLevel.CRITICAL,
            FaultSeverity.FATAL: AlertLevel.CRITICAL,
        }
        level = level_map.get(record.severity, AlertLevel.WARNING)

        alert = Alert(
            level=level,
            title=f"Fault: {record.code}",
            message=record.detail or record.code,
            fault_code=record.code,
        )
        self._dispatch(alert)

    def _monitor_loop(self) -> None:
        """Periodically checks for active faults that need re-alerting."""
        while self._running:
            try:
                for fault in self._fault_manager.active_faults:
                    last_sent = self._sent_alerts.get(fault.code, 0.0)
                    if time.time() - last_sent > self._alert_interval:
                        self._on_fault(fault)
            except Exception as exc:
                logger.error("Alert monitor loop error: %s", exc, exc_info=True)
            time.sleep(30.0)

    def _dispatch(self, alert: Alert) -> None:
        """
        Dispatches an alert to all registered handlers and the API.

        Args:
            alert: Alert to dispatch.
        """
        self._sent_alerts[alert.fault_code] = time.time()
        alert.sent = True

        log_fn = {
            AlertLevel.INFO: logger.info,
            AlertLevel.WARNING: logger.warning,
            AlertLevel.CRITICAL: logger.error,
        }.get(alert.level, logger.warning)

        log_fn("ALERT [%s] %s: %s", alert.level.name, alert.title, alert.message)

        # Push to River Song API
        if self._api:
            try:
                self._api.post_alert(alert)
            except Exception as exc:
                logger.warning("Failed to push alert to API: %s", exc)

        # Fire local handlers
        for handler in self._handlers:
            try:
                handler(alert)
            except Exception as exc:
                logger.error("Alert handler error: %s", exc, exc_info=True)

    def __repr__(self) -> str:
        return (
            f"AlertMonitor(running={self._running}, "
            f"sent={len(self._sent_alerts)})"
        )
