"""
River Vector - GPS Hardware Interface
Low-level GPS fix data and interface contract used by GPSManager.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from core.constants import RTK_ACCURACY_THRESHOLD_M


class FixQuality(str, Enum):
    NO_FIX    = "NO_FIX"
    GPS       = "GPS"
    DGPS      = "DGPS"
    RTK_FLOAT = "RTK_FLOAT"
    RTK_FIXED = "RTK_FIXED"


@dataclass
class GPSFix:
    """Snapshot of the current GPS fix from hardware."""
    has_fix: bool = False
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    altitude_m: Optional[float] = None             # WGS-84 ellipsoid metres; None until 3D fix
    altitude_accuracy_m: Optional[float] = None    # vertical accuracy estimate (m), None until 3D fix
    heading_deg: Optional[float] = None
    speed_ms: Optional[float] = None
    accuracy_m: float = 999.0
    fix_quality: FixQuality = FixQuality.NO_FIX
    satellites: int = 0

    @property
    def meets_accuracy_requirement(self) -> bool:
        """True if accuracy meets the RTK threshold for autonomous operation."""
        return self.has_fix and self.accuracy_m <= RTK_ACCURACY_THRESHOLD_M


class GPSInterface:
    """
    Base class for GPS hardware drivers.

    Concrete implementations (UART NMEA, UBlox binary, simulated) override
    update() to push new data into _fix. All consumers read via the fix property.
    """

    def __init__(self) -> None:
        self._fix = GPSFix()

    @property
    def fix(self) -> GPSFix:
        """Current GPS fix snapshot."""
        return self._fix

    def update(self) -> None:
        """Polls hardware and refreshes _fix. Override in subclasses."""

    def __repr__(self) -> str:
        f = self._fix
        return f"GPSInterface(has_fix={f.has_fix}, quality={f.fix_quality.value}, acc={f.accuracy_m}m)"
