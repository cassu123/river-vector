"""
River Vector - Drive System Interface
Abstract contract for all drive system types (clutch, differential, direct electric, hydrostatic).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class DriveCommand:
    """Normalized drive command. Values are always in the same units regardless of platform."""
    throttle_pct: float = 0.0   # 0–100
    steering_pct: float = 0.0   # -100 (full left) to +100 (full right)
    brake_pct: float = 0.0      # 0–100
    gear: int = 0               # 0=neutral; only meaningful for clutch drives


class AbstractDriveSystem(ABC):
    """
    Platform-agnostic drive system interface.

    Autonomy code issues DriveCommands. Concrete implementations translate
    those into the appropriate hardware signals (Pico commands, CAN frames,
    PWM duty cycles, etc.) for their specific platform.
    """

    @abstractmethod
    def apply(self, cmd: DriveCommand) -> None:
        """
        Applies a drive command to the hardware.

        Args:
            cmd: Normalized drive command.
        """

    @abstractmethod
    def emergency_stop(self) -> None:
        """Immediately halts all motion. Must be safe to call from any state."""

    @property
    @abstractmethod
    def max_speed_kmh(self) -> float:
        """Maximum achievable speed for this platform in km/h."""

    @property
    @abstractmethod
    def current_gear(self) -> int:
        """Current gear (0 = neutral). Always 0 for non-clutch drives."""

    @property
    @abstractmethod
    def is_stopped(self) -> bool:
        """True if the drive system is confirmed stationary."""
