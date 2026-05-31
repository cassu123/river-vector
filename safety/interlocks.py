"""
River Vector - Safety Interlocks

Pre-flight checks that must pass before AUTO mode is entered.

Safety thresholds come from THREE sources, applied in this order:

  1. Absolute floors (universal, hardcoded in core/constants.py) — these
     are clamped by config_sync on every config receipt; the device
     never operates below them.
  2. Per-unit safety_floors (server-configured, stored in
     vector_units.safety_floors). Tighter than absolute floors.
  3. Per-program values (server-configured, in vector_programs).
     Tighter than per-unit safety_floors. Validated server-side AND
     defensively re-checked here.

The interlock layer reads from config_sync.get_safety_floors() at
verify() time so the latest values always apply.
"""

from __future__ import annotations

import logging
from typing import Optional

from core.constants import (
    ABSOLUTE_MIN_OBSTACLE_CLEARANCE_M,
    DEFAULT_OBSTACLE_CLEARANCE_M,
    DEFAULT_IMU_TILT_CUTOFF_DEG,
    FaultCode,
    MIN_FUEL_PERCENT,
    MIN_VOLTAGE_V,
)

logger = logging.getLogger(__name__)


class InterlockError(Exception):
    """Raised by Interlocks.verify() when a pre-flight check fails."""


class Interlocks:
    """
    Pre-flight safety interlock checks for AUTO mode entry.

    Args:
        sensor_manager:  SensorManager for e-stop, voltage, fuel, deck state.
        gps_manager:     GPSManager for fix quality and RTK accuracy.
        presence:        AbstractOperatorPresence driver.
        fault_manager:   FaultManager — queried for pre-existing faults.
        config_sync:     ConfigSync — provides safety_floors and capabilities.
    """

    def __init__(
        self,
        sensor_manager=None,
        gps_manager=None,
        presence=None,
        fault_manager=None,
        config_sync=None,
    ) -> None:
        self._sensors = sensor_manager
        self._gps = gps_manager
        self._presence = presence
        self._fault_manager = fault_manager
        self._config_sync = config_sync

    # ──────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────

    def verify(self) -> None:
        """
        Runs all interlock checks.

        Raises:
            InterlockError: If any check fails.
        """
        self._check_fatal_faults()
        self._check_estop()
        self._check_operator_presence()
        self._check_voltage()
        self._check_fuel()
        self._check_deck()
        self._check_gps()
        self._check_tilt()
        self._check_capabilities()
        logger.info("All interlocks passed — AUTO mode permitted.")

    def get_safety_floors(self) -> dict:
        """
        Returns the active safety floors. Falls back to absolute defaults
        when no config is loaded.
        """
        if self._config_sync is None:
            return {
                "min_obstacle_clearance_m": DEFAULT_OBSTACLE_CLEARANCE_M,
                "imu_tilt_cutoff_deg": DEFAULT_IMU_TILT_CUTOFF_DEG,
                "min_battery_v_cutoff": MIN_VOLTAGE_V,
                "operator_presence_required_for_auto": True,
            }
        try:
            return self._config_sync.get_safety_floors()
        except Exception:
            return {
                "min_obstacle_clearance_m": DEFAULT_OBSTACLE_CLEARANCE_M,
                "imu_tilt_cutoff_deg": DEFAULT_IMU_TILT_CUTOFF_DEG,
                "min_battery_v_cutoff": MIN_VOLTAGE_V,
                "operator_presence_required_for_auto": True,
            }

    # ──────────────────────────────────────────────────────────────────
    # Individual checks
    # ──────────────────────────────────────────────────────────────────

    def _check_fatal_faults(self) -> None:
        if self._fault_manager is None:
            return
        if not self._fault_manager.is_safe_to_operate():
            active = [f.code for f in self._fault_manager.active_faults]
            raise InterlockError(f"Active faults block AUTO mode: {active}")

    def _check_estop(self) -> None:
        if self._sensors is None:
            return
        snap = self._sensors.snapshot
        if snap.estop_pressed:
            raise InterlockError(
                "E-stop button is pressed. Release and reset before entering AUTO."
            )

    def _check_operator_presence(self) -> None:
        if self._presence is None:
            return
        floors = self.get_safety_floors()
        required = floors.get("operator_presence_required_for_auto", True)
        if not required:
            return
        if not getattr(self._presence, "required_for_auto", False):
            return
        if not self._presence.is_present():
            raise InterlockError(
                f"Operator presence required ({self._presence.presence_type}) "
                "but not detected."
            )

    def _check_voltage(self) -> None:
        if self._sensors is None:
            return
        snap = self._sensors.snapshot
        if snap.battery_voltage_v is None:
            return
        floors = self.get_safety_floors()
        min_v = float(floors.get("min_battery_v_cutoff", MIN_VOLTAGE_V))
        if snap.battery_voltage_v < min_v:
            raise InterlockError(
                f"Battery voltage {snap.battery_voltage_v:.1f}V "
                f"below minimum {min_v:.1f}V."
            )

    def _check_fuel(self) -> None:
        if self._sensors is None:
            return
        snap = self._sensors.snapshot
        # Only enforce if a fuel sensor is configured.
        if self._config_sync is not None:
            try:
                caps = self._config_sync.get_capabilities()
                if not caps.has_fuel_sensor:
                    return
            except Exception:
                pass
        if snap.fuel_percent is not None and snap.fuel_percent < MIN_FUEL_PERCENT:
            raise InterlockError(
                f"Fuel level {snap.fuel_percent:.1f}% below minimum {MIN_FUEL_PERCENT}%."
            )

    def _check_deck(self) -> None:
        if self._sensors is None:
            return
        snap = self._sensors.snapshot
        if snap.deck_raised:
            raise InterlockError(
                "Cutting deck is raised. Lower the deck before entering AUTO."
            )

    def _check_gps(self) -> None:
        if self._gps is None:
            return
        # Skip if device has no GPS.
        if self._config_sync is not None:
            try:
                caps = self._config_sync.get_capabilities()
                if not caps.has_gps:
                    return
            except Exception:
                pass
        if not self._gps.is_ready_for_autonomous:
            fix = self._gps.fix
            if not fix.has_fix:
                raise InterlockError("No GPS fix available.")
            raise InterlockError(
                f"GPS accuracy {fix.accuracy_m:.3f}m does not meet RTK threshold."
            )

    def _check_tilt(self) -> None:
        """Refuses AUTO if current tilt already exceeds the cutoff."""
        if self._sensors is None:
            return
        snap = self._sensors.snapshot
        floors = self.get_safety_floors()
        cutoff = float(floors.get("imu_tilt_cutoff_deg", DEFAULT_IMU_TILT_CUTOFF_DEG))
        for axis_name, value in (("pitch", snap.pitch_deg), ("roll", snap.roll_deg)):
            if value is None:
                continue
            if abs(value) >= cutoff:
                raise InterlockError(
                    f"Current {axis_name} {value:.1f}° exceeds tilt cutoff {cutoff:.1f}°."
                )

    def _check_capabilities(self) -> None:
        """Refuses AUTO on units that do not support autonomous operation."""
        if self._config_sync is None:
            return
        try:
            caps = self._config_sync.get_capabilities()
        except Exception:
            return
        if not caps.supports_autonomous:
            raise InterlockError(
                "Unit does not support autonomous operation "
                f"(gps_grade={caps.gps_grade}, has_imu={caps.has_imu}). "
                "RTK GPS and IMU are required."
            )

    def __repr__(self) -> str:
        return (
            f"Interlocks("
            f"sensors={'yes' if self._sensors else 'none'}, "
            f"gps={'yes' if self._gps else 'none'}, "
            f"presence={'yes' if self._presence else 'none'}, "
            f"config={'yes' if self._config_sync else 'none'})"
        )
