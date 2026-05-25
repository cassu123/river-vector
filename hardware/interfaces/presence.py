"""
River Vector - Operator Presence Interface
Abstract contract for detecting operator presence across platform types.
Riding mowers use seat sensors; push mowers use handle grip; robots have none.
"""

from abc import ABC, abstractmethod
from typing import Callable


class AbstractOperatorPresence(ABC):
    """
    Platform-agnostic operator presence detector.

    The safety interlock layer calls is_present() without caring whether
    it's reading a seat pressure switch, a handle grip sensor, or always
    returning True for an unattended robot.
    """

    @abstractmethod
    def is_present(self) -> bool:
        """
        Returns True if an operator is detected in the required position.
        For robots (presence type 'none'), always returns True.
        """

    @property
    @abstractmethod
    def required_for_auto(self) -> bool:
        """True if operator presence is required to enter AUTO mode."""

    @abstractmethod
    def register_absence_callback(self, callback: Callable[[], None]) -> None:
        """
        Registers a callback invoked when operator presence is lost.

        Args:
            callback: Called with no arguments when absence is detected.
        """

    @property
    @abstractmethod
    def presence_type(self) -> str:
        """Source type: 'seat_sensor', 'handle_grip', or 'none'."""
