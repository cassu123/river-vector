"""
River Vector - Safety Interlocks
Pre-flight checks that must all pass before AUTO mode is permitted.
Called by ModeManager.request_auto() — raises InterlockError on any failure.
"""

import logging
from typing import Optional

from core.constants import FaultCode, MIN_FUEL_PERCENT, MIN_VOLTAGE_V

logger = logging.getLogger(__name__)


class InterlockError(Exception):
    """Raised by Interlocks.verify() when a pre-flight check fails."""


class Interlocks:
    """
    Pre-flight safety interlock checks for AUTO mode entry.

    Each check is independent. The first failure raises InterlockError
    with a human-readable message. All dependencies are optional — if
    None, that check is skipped (simulation / partial-hardware mode).

    Args:
        sensor_manager:  SensorManager for e-stop, voltage, fuel, deck state.
        gps_manager:     GPSManager for fix quality and RTK accuracy.
        presence:        AbstractOperatorPresence driver for seat/grip detection.
        fault_manager:   FaultManager — queried for pre-existing fatal faults.
    """

    def __init__(
        self,
        sensor_manager=None,
        gps_manager=None,
        presence=None,
        fault_manager=None,
    ) -> None:
        self._sensors = sensor_manager
        self._gps = gps_manager
        self._presence = presence
        self._fault_manager = fault_manager

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def verify(self) -> None:
        """
        Runs all interlock checks.

        Raises:
            InterlockError: If any check fails. Message describes which check
                            failed and why.
        """
        self._check_fatal_faults()
        self._check_estop()
        self._check_operator_presence()
        self._check_voltage()
        self._check_fuel()
        self._check_deck()
        self._check_gps()
        logger.info("All interlocks passed — AUTO mode permitted.")

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_fatal_faults(self) -> None:
        """Blocks AUTO if any FATAL fault is already active."""
        if self._fault_manager is None:
            return
        if not self._fault_manager.is_safe_to_operate():
            active = [f.code for f in self._fault_manager.active_faults]
            raise InterlockError(f"Active faults block AUTO mode: {active}")

    def _check_estop(self) -> None:
        """Blocks AUTO if the physical e-stop button is pressed."""
        if self._sensors is None:
            return
        snap = self._sensors.snapshot
        if snap.estop_pressed:
            raise InterlockError(
                "E-stop button is pressed. Release and reset before entering AUTO."
            )

    def _check_operator_presence(self) -> None:
        """Blocks AUTO if operator presence is required but not detected."""
        if self._presence is None:
            return
        if self._presence.required_for_auto and not self._presence.is_present():
            raise InterlockError(
                f"Operator presence required ({self._presence.presence_type}) "
                "but not detected. Occupy the operating position before AUTO."
            )

    def _check_voltage(self) -> None:
        """Blocks AUTO if battery voltage is below the minimum threshold."""
        if self._sensors is None:
            return
        snap = self._sensors.snapshot
        if snap.battery_voltage_v is not None and snap.battery_voltage_v < MIN_VOLTAGE_V:
            raise InterlockError(
                f"Battery voltage {snap.battery_voltage_v:.1f}V is below "
                f"minimum {MIN_VOLTAGE_V}V."
            )

    def _check_fuel(self) -> None:
        """Blocks AUTO if fuel level is below the minimum threshold."""
        if self._sensors is None:
            return
        snap = self._sensors.snapshot
        if snap.fuel_percent is not None and snap.fuel_percent < MIN_FUEL_PERCENT:
            raise InterlockError(
                f"Fuel level {snap.fuel_percent:.1f}% is below "
                f"minimum {MIN_FUEL_PERCENT}%."
            )

    def _check_deck(self) -> None:
        """Blocks AUTO if the cutting deck is raised (blades exposed, no ground contact)."""
        if self._sensors is None:
            return
        snap = self._sensors.snapshot
        if snap.deck_raised:
            raise InterlockError(
                "Cutting deck is raised. Lower the deck before entering AUTO."
            )

    def _check_gps(self) -> None:
        """Blocks AUTO if GPS fix quality or RTK accuracy is insufficient."""
        if self._gps is None:
            return
        if not self._gps.is_ready_for_autonomous:
            fix = self._gps.fix
            if not fix.has_fix:
                raise InterlockError("No GPS fix available. Waiting for satellite lock.")
            raise InterlockError(
                f"GPS accuracy {fix.accuracy_m:.3f}m does not meet RTK threshold "
                "required for autonomous operation."
            )

    def __repr__(self) -> str:
        return (
            f"Interlocks("
            f"sensors={'yes' if self._sensors else 'none'}, "
            f"gps={'yes' if self._gps else 'none'}, "
            f"presence={'yes' if self._presence else 'none'}, "
            f"faults={'yes' if self._fault_manager else 'none'})"
        )
