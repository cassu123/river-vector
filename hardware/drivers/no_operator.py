"""
River Vector - No Operator Presence
Null presence implementation for fully autonomous robot platforms.
Used by: Scout robot mower (no human operator required).
"""

from typing import Callable
from hardware.interfaces.presence import AbstractOperatorPresence


class NoOperatorPresence(AbstractOperatorPresence):
    """
    Presence stub for unattended robot platforms.

    is_present() always returns True — there is no operator to detect,
    and the platform is designed to operate without one.
    Absence callbacks are never fired.
    """

    def is_present(self) -> bool:
        return True

    @property
    def required_for_auto(self) -> bool:
        return False

    def register_absence_callback(self, callback: Callable[[], None]) -> None:
        pass  # Robots have no operator to go absent

    @property
    def presence_type(self) -> str:
        return "none"

    def __repr__(self) -> str:
        return "NoOperatorPresence()"
