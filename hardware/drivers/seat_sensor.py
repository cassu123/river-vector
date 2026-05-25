"""
River Vector - Seat Sensor Presence
Operator presence detection via seat pressure switch.
Used by: Voyager-1 riding mower, John Deere variants.
"""

import logging
from typing import Callable, List, Optional
from hardware.interfaces.presence import AbstractOperatorPresence

logger = logging.getLogger(__name__)


class SeatSensorPresence(AbstractOperatorPresence):
    """
    Operator presence via riding mower seat pressure switch.

    The seat state is injected from SensorManager switch callbacks.
    Absence triggers registered callbacks so the safety layer can
    immediately halt autonomous operation.

    Args:
        required_for_auto: Whether presence is required to enter AUTO mode.
    """

    def __init__(self, required_for_auto: bool = True) -> None:
        self._required = required_for_auto
        self._occupied: Optional[bool] = None
        self._callbacks: List[Callable[[], None]] = []

    def update_seat_state(self, occupied: bool) -> None:
        """
        Injects seat state from the sensor manager.

        Args:
            occupied: True if seat pressure switch is closed (operator seated).
        """
        was_occupied = self._occupied
        self._occupied = occupied

        if was_occupied is True and not occupied:
            logger.warning("Seat vacated — operator absent.")
            for cb in self._callbacks:
                try:
                    cb()
                except Exception as exc:
                    logger.error("Seat absence callback error: %s", exc)

    def is_present(self) -> bool:
        return bool(self._occupied)

    @property
    def required_for_auto(self) -> bool:
        return self._required

    def register_absence_callback(self, callback: Callable[[], None]) -> None:
        self._callbacks.append(callback)

    @property
    def presence_type(self) -> str:
        return "seat_sensor"

    def __repr__(self) -> str:
        return f"SeatSensorPresence(occupied={self._occupied}, required={self._required})"
