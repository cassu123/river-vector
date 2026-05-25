"""
River Vector - Handle Grip Presence
Operator presence detection via dead-man handle grip switch.
Used by: Ryobi electric push mower.
"""

import logging
from typing import Callable, List, Optional
from hardware.interfaces.presence import AbstractOperatorPresence

logger = logging.getLogger(__name__)


class HandleGripPresence(AbstractOperatorPresence):
    """
    Operator presence via push mower dead-man handle grip switch.

    The operator must maintain grip on the handle bar to keep the
    grip switch closed. Releasing the handle fires absence callbacks
    so the autonomy layer can halt the mow session immediately.

    Args:
        required_for_auto: Whether presence is required to enter AUTO mode.
    """

    def __init__(self, required_for_auto: bool = True) -> None:
        self._required = required_for_auto
        self._gripped: Optional[bool] = None
        self._callbacks: List[Callable[[], None]] = []

    def update_grip_state(self, gripped: bool) -> None:
        """
        Injects grip state from the sensor manager.

        Args:
            gripped: True if the handle grip switch is closed (operator holding).
        """
        was_gripped = self._gripped
        self._gripped = gripped

        if was_gripped is True and not gripped:
            logger.warning("Handle grip released — operator absent.")
            for cb in self._callbacks:
                try:
                    cb()
                except Exception as exc:
                    logger.error("Grip absence callback error: %s", exc)

    def is_present(self) -> bool:
        return bool(self._gripped)

    @property
    def required_for_auto(self) -> bool:
        return self._required

    def register_absence_callback(self, callback: Callable[[], None]) -> None:
        self._callbacks.append(callback)

    @property
    def presence_type(self) -> str:
        return "handle_grip"

    def __repr__(self) -> str:
        return f"HandleGripPresence(gripped={self._gripped}, required={self._required})"
