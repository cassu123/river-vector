"""
River Vector - Sensor Manager
Receives Pico telemetry, maintains a live snapshot, and fires threshold callbacks.
"""

import logging
import threading
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from core.constants import (
    FaultCode,
    MIN_VOLTAGE_V,
    CRITICAL_TEMP_C,
    OBSTACLE_STOP_DISTANCE_CM,
)
from pico.protocol import PicoMessage, PicoMessageType

logger = logging.getLogger(__name__)

ThresholdCallback = Callable[[FaultCode, float], None]


@dataclass
class SensorSnapshot:
    """Point-in-time snapshot of all sensor values. None = not yet received."""
    battery_voltage_v: Optional[float] = None
    fuel_percent: Optional[float] = None
    engine_temp_c: Optional[float] = None
    ultrasonic_front_cm: Optional[float] = None
    ultrasonic_rear_cm: Optional[float] = None
    seat_occupied: Optional[bool] = None
    estop_pressed: Optional[bool] = None
    deck_raised: Optional[bool] = None
    pitch_deg: Optional[float] = None
    roll_deg: Optional[float] = None
    rpm: Optional[int] = None


class SensorManager:
    """
    Processes incoming Pico sensor messages and exposes a live snapshot.

    Threshold callbacks are fired when sensor values cross safety limits.
    is_safe_to_operate() consolidates all critical checks for the interlock layer.

    Args:
        pico_bridge: PicoBridge instance — used to register message handlers.
    """

    def __init__(self, pico_bridge) -> None:
        if pico_bridge is None:
            raise ValueError("pico_bridge must not be None.")
        self._pico = pico_bridge
        self._lock = threading.Lock()
        self._snapshot = SensorSnapshot()
        self._threshold_callbacks: List[ThresholdCallback] = []

    # ------------------------------------------------------------------
    # Message handlers (called by PicoBridge on incoming messages)
    # ------------------------------------------------------------------

    def _handle_ultrasonic(self, msg: PicoMessage) -> None:
        with self._lock:
            self._snapshot.ultrasonic_front_cm = msg.payload.get("front")
            self._snapshot.ultrasonic_rear_cm = msg.payload.get("rear")

    def _handle_power(self, msg: PicoMessage) -> None:
        voltage = msg.payload.get("voltage_v")
        fuel = msg.payload.get("fuel_pct")
        with self._lock:
            self._snapshot.battery_voltage_v = voltage
            self._snapshot.fuel_percent = fuel

        if voltage is not None and voltage < MIN_VOLTAGE_V:
            self._fire_threshold(FaultCode.LOW_VOLTAGE, voltage)

    def _handle_thermal(self, msg: PicoMessage) -> None:
        temp = msg.payload.get("temp_c")
        with self._lock:
            self._snapshot.engine_temp_c = temp

        if temp is not None and temp > CRITICAL_TEMP_C:
            self._fire_threshold(FaultCode.OVER_TEMP, temp)

    def _handle_switches(self, msg: PicoMessage) -> None:
        with self._lock:
            self._snapshot.seat_occupied = msg.payload.get("seat_occupied")
            self._snapshot.estop_pressed = msg.payload.get("estop_pressed")
            self._snapshot.deck_raised = msg.payload.get("deck_raised")

    def _handle_imu(self, msg: PicoMessage) -> None:
        with self._lock:
            self._snapshot.pitch_deg = msg.payload.get("pitch")
            self._snapshot.roll_deg = msg.payload.get("roll")

    def _handle_rpm(self, msg: PicoMessage) -> None:
        with self._lock:
            self._snapshot.rpm = msg.payload.get("rpm")

    # ------------------------------------------------------------------
    # Derived safety checks
    # ------------------------------------------------------------------

    def is_obstacle_imminent(self) -> bool:
        """True if front ultrasonic reading is within the stop threshold."""
        with self._lock:
            front = self._snapshot.ultrasonic_front_cm
        return front is not None and front < OBSTACLE_STOP_DISTANCE_CM

    def is_safe_to_operate(self) -> bool:
        """
        Consolidated safety gate for autonomous operation.

        Returns False if any of the following are true:
        - E-stop button is pressed
        - Seat is unoccupied (operator absent)
        - Battery voltage is below minimum
        """
        with self._lock:
            snap = self._snapshot

        if snap.estop_pressed:
            return False
        if snap.seat_occupied is False:
            return False
        if snap.battery_voltage_v is not None and snap.battery_voltage_v < MIN_VOLTAGE_V:
            return False
        return True

    # ------------------------------------------------------------------
    # Snapshot access
    # ------------------------------------------------------------------

    @property
    def snapshot(self) -> SensorSnapshot:
        """Current sensor snapshot (thread-safe copy)."""
        with self._lock:
            import copy
            return copy.copy(self._snapshot)

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def register_threshold_callback(self, callback: ThresholdCallback) -> None:
        """
        Registers a callback fired when a sensor crosses a safety threshold.

        Args:
            callback: Called with (FaultCode, value).
        """
        self._threshold_callbacks.append(callback)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _fire_threshold(self, code: FaultCode, value: float) -> None:
        for cb in self._threshold_callbacks:
            try:
                cb(code, value)
            except Exception as exc:
                logger.error("Threshold callback error: %s", exc, exc_info=True)

    def __repr__(self) -> str:
        snap = self.snapshot
        return (
            f"SensorManager(voltage={snap.battery_voltage_v}V, "
            f"temp={snap.engine_temp_c}°C, "
            f"front_cm={snap.ultrasonic_front_cm})"
        )
