"""
================================================================================
Project:  River Vector — Autonomous Mower Control System
File:     autonomy/shift_controller.py
Purpose:  Gear sequencing logic for the Voyager 7-speed manual transmission.
          Handles clutch-in, shift, clutch-out sequencing with proper timing
          delays. Enforces sequential shifting — no skipping gears.
Author:   [Author Placeholder]
Version:  0.1.0
Date:     2026-05-25
================================================================================
"""

import logging
import time
from typing import Optional

from core.constants import (
    CLUTCH_ENGAGE_DELAY_SEC,
    CLUTCH_RELEASE_DELAY_SEC,
    GEAR_MAX,
    GEAR_MIN,
    GEAR_NEUTRAL,
    SHIFT_SETTLE_DELAY_SEC,
    FaultCode,
)
from safety.fault_manager import FaultManager, FaultSeverity

logger = logging.getLogger(__name__)


class ShiftError(Exception):
    """Raised when a gear shift cannot be completed."""


class ShiftController:
    """
    Controls gear shifting for the Voyager 7-speed manual transmission.

    Shift sequence:
    1. Cut throttle to idle
    2. Disengage clutch (100%)
    3. Wait CLUTCH_ENGAGE_DELAY_SEC
    4. Command gear shift actuator to target gear position
    5. Wait SHIFT_SETTLE_DELAY_SEC for actuator to reach position
    6. Re-engage clutch (0%)
    7. Wait CLUTCH_RELEASE_DELAY_SEC
    8. Restore throttle

    Args:
        actuator_manager: ActuatorManager for clutch and shift commands.
        fault_manager: FaultManager for fault reporting.
    """

    def __init__(self, actuator_manager, fault_manager: FaultManager) -> None:
        if actuator_manager is None:
            raise ValueError("actuator_manager must not be None.")
        if fault_manager is None:
            raise ValueError("fault_manager must not be None.")
        self._actuators = actuator_manager
        self._fault_manager = fault_manager
        self._current_gear: int = GEAR_NEUTRAL
        self._target_gear: int = GEAR_NEUTRAL
        self._shifting: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def shift_to(self, target_gear: int) -> bool:
        """
        Shifts to the specified gear using the full clutch sequence.

        Enforces sequential shifting — will shift through intermediate gears
        if the target is more than one step away.

        Args:
            target_gear: Desired gear (0=neutral, 1–7).

        Returns:
            True if the shift completed successfully.

        Raises:
            ShiftError: If the shift cannot be initiated.
        """
        if not isinstance(target_gear, int) or not (GEAR_NEUTRAL <= target_gear <= GEAR_MAX):
            raise ShiftError(
                f"Invalid target gear {target_gear}. Must be {GEAR_NEUTRAL}–{GEAR_MAX}."
            )

        if self._shifting:
            raise ShiftError("Shift already in progress — cannot initiate another shift.")

        if target_gear == self._current_gear:
            logger.debug("Already in gear %d — no shift needed.", target_gear)
            return True

        logger.info(
            "Shifting: gear %d → gear %d", self._current_gear, target_gear
        )

        # Shift sequentially through intermediate gears
        direction = 1 if target_gear > self._current_gear else -1
        while self._current_gear != target_gear:
            next_gear = self._current_gear + direction
            success = self._execute_single_shift(next_gear)
            if not success:
                return False

        return True

    def shift_up(self) -> bool:
        """
        Shifts up one gear.

        Returns:
            True if shift succeeded, False if already at max gear.
        """
        if self._current_gear >= GEAR_MAX:
            logger.debug("Already in top gear (%d).", GEAR_MAX)
            return False
        return self.shift_to(self._current_gear + 1)

    def shift_down(self) -> bool:
        """
        Shifts down one gear.

        Returns:
            True if shift succeeded, False if already in neutral.
        """
        if self._current_gear <= GEAR_NEUTRAL:
            logger.debug("Already in neutral.")
            return False
        return self.shift_to(self._current_gear - 1)

    def shift_to_neutral(self) -> bool:
        """
        Shifts to neutral (gear 0).

        Returns:
            True if shift succeeded.
        """
        return self.shift_to(GEAR_NEUTRAL)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def current_gear(self) -> int:
        """Currently engaged gear (0=neutral, 1–7)."""
        return self._current_gear

    @property
    def is_shifting(self) -> bool:
        """True if a shift sequence is currently in progress."""
        return self._shifting

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _execute_single_shift(self, target_gear: int) -> bool:
        """
        Executes the full clutch-shift-clutch sequence for a single gear step.

        Args:
            target_gear: The gear to shift into (must be adjacent to current).

        Returns:
            True if the shift completed successfully.
        """
        self._shifting = True
        pre_throttle = self._actuators.state.throttle_pct

        try:
            # 1. Cut throttle
            self._actuators.set_throttle(0.0)
            logger.debug("Throttle cut for shift.")

            # 2. Disengage clutch
            self._actuators.set_clutch(100.0)
            time.sleep(CLUTCH_ENGAGE_DELAY_SEC)
            logger.debug("Clutch disengaged.")

            # 3. Command shift actuator
            # Shift actuator position is sent via the actuator manager
            # which forwards to the Pico CMD_SHIFT message
            self._send_shift_command(target_gear)
            time.sleep(SHIFT_SETTLE_DELAY_SEC)
            logger.debug("Shift actuator moved to gear %d.", target_gear)

            # 4. Re-engage clutch
            self._actuators.set_clutch(0.0)
            time.sleep(CLUTCH_RELEASE_DELAY_SEC)
            logger.debug("Clutch re-engaged.")

            # 5. Restore throttle
            self._actuators.set_throttle(pre_throttle)

            self._current_gear = target_gear
            logger.info("Shift complete — now in gear %d.", self._current_gear)
            return True

        except Exception as exc:
            logger.error("Shift to gear %d failed: %s", target_gear, exc, exc_info=True)
            self._fault_manager.report_fault(
                code=FaultCode.SHIFT_FAILURE,
                detail=f"Shift to gear {target_gear} failed: {exc}",
                severity=FaultSeverity.CRITICAL,
            )
            # Safe recovery — cut throttle, disengage clutch
            try:
                self._actuators.set_throttle(0.0)
                self._actuators.set_clutch(100.0)
            except Exception:
                pass
            return False

        finally:
            self._shifting = False

    def _send_shift_command(self, gear: int) -> None:
        """
        Sends the gear shift command to the actuator manager.

        The actuator manager forwards this to the Pico as CMD_SHIFT.

        Args:
            gear: Target gear number.
        """
        from pico.protocol import PicoMessage, PicoMessageType
        msg = PicoMessage(
            msg_type=PicoMessageType.CMD_SHIFT,
            payload={"gear": gear},
        )
        self._actuators._pico.send(msg)

    def __repr__(self) -> str:
        return (
            f"ShiftController(gear={self._current_gear}, "
            f"shifting={self._shifting})"
        )
