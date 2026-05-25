"""Hardware abstraction interfaces for River Vector platform-agnostic operation."""

from hardware.interfaces.drive import AbstractDriveSystem, DriveCommand
from hardware.interfaces.deck import AbstractDeckControl
from hardware.interfaces.presence import AbstractOperatorPresence

__all__ = [
    "AbstractDriveSystem",
    "DriveCommand",
    "AbstractDeckControl",
    "AbstractOperatorPresence",
]
