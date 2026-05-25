"""
================================================================================
Project:  River Vector — Autonomous Mower Control System
File:     autonomy/mode_manager.py
Purpose:  AUTO/MANUAL state machine. Reads the physical mode toggle switch
          and manages transitions between operating modes. Manual mode is
          always available — the physical switch overrides software state.
          Autonomous mode requires all safety interlocks to pass.
Author:   [Author Placeholder]
Version:  0.1.0
Date:     2026-05-25
================================================================================
"""

import logging
import threading
import time
from enum import Enum, auto
from typing import Callable, List, Optional

from core.constants import FaultCode
from safety.fault_manager import FaultManager

logger = logging.getLogger(__name__)


class OperatingMode(Enum):
    """
    Operating modes for River Vector.

    MANUAL:   Human operator has full control. Autonomous systems are idle.
    AUTO:     Autonomous mowing session active. Safety layer monitors continuously.
    ESTOP:    Emergency stop active. All motion halted. Requires manual reset.
    FAULT:    Non-fatal fault state. Autonomous paused. Manual still available.
    SHUTDOWN: System is shutting down.
    """
    MANUAL = auto()
    AUTO = auto()
    ESTOP = auto()
    FAULT = auto()
    SHUTDOWN = auto()


class ModeTransitionError(Exception):
    """Raised when a mode transition is not permitted."""


class ModeManager:
    """
    Manages the AUTO/MANUAL operating mode state machine.

    The physical toggle switch is the authoritative source for mode selection.
    Software can request AUTO mode, but the switch must be in the AUTO position
    and all interlocks must pass. The switch returning to MANUAL immediately
    overrides any autonomous operation.

    Args:
        fault_manager: FaultManager for fault state queries.
        interlocks: Interlocks instance for pre-flight checks.
        poll_interval: Seconds between mode switch polls.
    """

    POLL_INTERVAL_SEC: float = 0.1

    def __init__(
        self,
        fault_manager: FaultManager,
        interlocks=None,
        poll_interval: float = POLL_INTERVAL_SEC,
    ) -> None:
        if fault_manager is None:
            raise ValueError("fault_manager must not be None.")
        self._fault_manager = fault_manager
        self._interlocks = interlocks
        self._poll_interval = poll_interval
        self._mode = OperatingMode.MANUAL
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._mode_callbacks: List[Callable[[OperatingMode, OperatingMode], None]] = []

        # Hardware switch state — injected by sensor callbacks
        # True = switch in AUTO position, False = MANUAL
        self._switch_in_auto: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Starts the mode polling thread."""
        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="ModeManager",
            daemon=True,
        )
        self._thread.start()
        logger.info("Mode manager started — current mode: %s", self._mode.name)

    def stop(self) -> None:
        """Stops the mode polling thread."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info("Mode manager stopped.")

    # ------------------------------------------------------------------
    # Mode transitions
    # ------------------------------------------------------------------

    def request_auto(self) -> bool:
        """
        Requests a transition to AUTO mode.

        Runs pre-flight interlock checks. Transition is denied if the
        physical switch is not in AUTO position, any FATAL fault is active,
        or interlocks fail.

        Returns:
            True if AUTO mode was entered successfully.
        """
        if not self._switch_in_auto:
            logger.warning("AUTO mode requested but physical switch is in MANUAL position.")
            return False

        if not self._fault_manager.is_safe_to_operate():
            active = [f.code for f in self._fault_manager.active_faults]
            logger.warning("AUTO mode denied — active faults: %s", active)
            return False

        if self._interlocks:
            try:
                self._interlocks.verify()
            except Exception as exc:
                logger.warning("AUTO mode denied — interlock failure: %s", exc)
                return False

        self._transition_to(OperatingMode.AUTO)
        return True

    def request_manual(self) -> None:
        """
        Requests a transition to MANUAL mode.
        Always succeeds — manual mode cannot be blocked by software.
        """
        self._transition_to(OperatingMode.MANUAL)

    def trigger_estop(self, reason: str = "") -> None:
        """
        Transitions to ESTOP mode.

        Args:
            reason: Description of why the e-stop was triggered.
        """
        logger.critical("Mode manager: ESTOP triggered. Reason: %s", reason)
        self._transition_to(OperatingMode.ESTOP)

    def reset_estop(self) -> bool:
        """
        Attempts to clear ESTOP mode and return to MANUAL.

        Returns:
            True if reset was successful.
        """
        with self._lock:
            if self._mode != OperatingMode.ESTOP:
                return True
        self._transition_to(OperatingMode.MANUAL)
        logger.info("ESTOP cleared — returned to MANUAL mode.")
        return True

    # ------------------------------------------------------------------
    # Switch state injection
    # ------------------------------------------------------------------

    def update_switch_state(self, in_auto: bool) -> None:
        """
        Updates the physical mode switch state.

        Called by the sensor manager when the mode toggle switch changes.
        If the switch moves to MANUAL while in AUTO, immediately transitions.

        Args:
            in_auto: True if the physical switch is in the AUTO position.
        """
        self._switch_in_auto = in_auto
        if not in_auto and self._mode == OperatingMode.AUTO:
            logger.info("Physical switch moved to MANUAL — overriding AUTO mode.")
            self.request_manual()

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def mode(self) -> OperatingMode:
        """Current operating mode."""
        with self._lock:
            return self._mode

    @property
    def is_autonomous(self) -> bool:
        """True if currently in AUTO mode."""
        return self.mode == OperatingMode.AUTO

    @property
    def is_manual(self) -> bool:
        """True if currently in MANUAL mode."""
        return self.mode == OperatingMode.MANUAL

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def register_mode_callback(
        self, callback: Callable[[OperatingMode, OperatingMode], None]
    ) -> None:
        """
        Registers a callback invoked on every mode transition.

        Args:
            callback: Called with (old_mode, new_mode).
        """
        self._mode_callbacks.append(callback)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _transition_to(self, new_mode: OperatingMode) -> None:
        """
        Executes a mode transition and fires callbacks.

        Args:
            new_mode: Target operating mode.
        """
        with self._lock:
            old_mode = self._mode
            if old_mode == new_mode:
                return
            self._mode = new_mode

        logger.info("Mode transition: %s → %s", old_mode.name, new_mode.name)
        for cb in self._mode_callbacks:
            try:
                cb(old_mode, new_mode)
            except Exception as exc:
                logger.error("Mode callback error: %s", exc, exc_info=True)

    def _poll_loop(self) -> None:
        """
        Polls for fault state changes that require mode transitions.
        The physical switch is handled via update_switch_state() callbacks.
        """
        while self._running:
            try:
                # If a FATAL fault appears while in AUTO, drop to ESTOP
                if self._mode == OperatingMode.AUTO:
                    if not self._fault_manager.is_safe_to_operate():
                        self.trigger_estop("FAULT_DETECTED_IN_AUTO")
            except Exception as exc:
                logger.error("Mode poll error: %s", exc, exc_info=True)
            time.sleep(self._poll_interval)

    def __repr__(self) -> str:
        return f"ModeManager(mode={self._mode.name}, switch_auto={self._switch_in_auto})"
