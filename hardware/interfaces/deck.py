"""
River Vector - Deck Control Interface
Abstract contract for mowing deck engagement across all platform types.
"""

from abc import ABC, abstractmethod


class AbstractDeckControl(ABC):
    """
    Platform-agnostic mowing deck interface.

    Covers PTO-driven (riding), electric motor (robot/Ryobi), and belt-driven decks.
    The autonomy layer only calls engage/disengage — it never knows the mechanism.
    """

    @abstractmethod
    def engage(self) -> bool:
        """
        Engages the mowing deck (starts blades).

        Returns:
            True if deck was successfully engaged.
        """

    @abstractmethod
    def disengage(self) -> bool:
        """
        Disengages the mowing deck (stops blades).

        Returns:
            True if deck was successfully disengaged.
        """

    @property
    @abstractmethod
    def is_engaged(self) -> bool:
        """True if the deck is currently engaged and blades are spinning."""

    @property
    @abstractmethod
    def width_m(self) -> float:
        """Effective cutting width in meters."""

    @property
    @abstractmethod
    def deck_type(self) -> str:
        """Drive type: 'pto', 'electric', or 'belt'."""
