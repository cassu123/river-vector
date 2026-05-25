"""
River Vector - Unit Registry
Tracks the live state of every unit in a fleet.
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

Coordinate = Tuple[float, float]  # (latitude, longitude)


class UnitStatus(Enum):
    OFFLINE   = auto()   # Not connected / not reporting
    IDLE      = auto()   # Connected, no active session
    MOWING    = auto()   # Active mow session
    RETURNING = auto()   # Returning to home/dock
    FAULT     = auto()   # Fault condition active
    ESTOP     = auto()   # E-stop triggered


@dataclass
class UnitState:
    """Live state record for one fleet unit."""
    unit_id: str
    unit_name: str
    platform: str                          # riding | robot | push
    status: UnitStatus = UnitStatus.OFFLINE
    position: Optional[Coordinate] = None  # Last known GPS position
    battery_pct: Optional[float] = None
    zone_id: Optional[str] = None          # Zone assigned by coordinator
    last_seen: float = field(default_factory=time.time)

    @property
    def is_active(self) -> bool:
        return self.status in (UnitStatus.MOWING, UnitStatus.RETURNING)

    @property
    def seconds_since_seen(self) -> float:
        return time.time() - self.last_seen


class UnitRegistry:
    """
    Thread-safe registry of all units in a fleet.

    Each unit reports its state via update(). The coordinator reads
    the registry to make zone assignments and detect conflicts.

    Args:
        stale_threshold_s: Seconds before a unit is marked OFFLINE (default 10).
    """

    def __init__(self, stale_threshold_s: float = 10.0) -> None:
        self._units: Dict[str, UnitState] = {}
        self._lock = threading.Lock()
        self._stale_threshold = stale_threshold_s

    def register(self, unit_id: str, unit_name: str, platform: str) -> UnitState:
        """
        Registers a new unit. Idempotent — re-registering updates name/platform.

        Args:
            unit_id:   Unique unit identifier (e.g. 'VOY-RV-001').
            unit_name: Human-readable name.
            platform:  'riding', 'robot', or 'push'.

        Returns:
            The UnitState record for this unit.
        """
        with self._lock:
            if unit_id not in self._units:
                self._units[unit_id] = UnitState(
                    unit_id=unit_id, unit_name=unit_name, platform=platform
                )
                logger.info("Fleet: registered unit %s (%s / %s)", unit_id, unit_name, platform)
            else:
                self._units[unit_id].unit_name = unit_name
                self._units[unit_id].platform = platform
            return self._units[unit_id]

    def update(
        self,
        unit_id: str,
        status: Optional[UnitStatus] = None,
        position: Optional[Coordinate] = None,
        battery_pct: Optional[float] = None,
        zone_id: Optional[str] = None,
    ) -> None:
        """
        Updates the live state for a registered unit.

        Args:
            unit_id:     Unit to update.
            status:      New operating status (optional).
            position:    Latest GPS coordinate (optional).
            battery_pct: Battery level 0–100 (optional).
            zone_id:     Assigned zone (optional).
        """
        with self._lock:
            if unit_id not in self._units:
                logger.warning("Fleet: update for unknown unit %s — ignored.", unit_id)
                return
            state = self._units[unit_id]
            if status is not None:
                state.status = status
            if position is not None:
                state.position = position
            if battery_pct is not None:
                state.battery_pct = battery_pct
            if zone_id is not None:
                state.zone_id = zone_id
            state.last_seen = time.time()

    def assign_zone(self, unit_id: str, zone_id: str) -> bool:
        """
        Assigns a zone to a unit. Called by FleetCoordinator.

        Returns:
            True if the unit exists and was updated.
        """
        with self._lock:
            if unit_id not in self._units:
                return False
            self._units[unit_id].zone_id = zone_id
        logger.info("Fleet: assigned zone %s to unit %s", zone_id, unit_id)
        return True

    def get(self, unit_id: str) -> Optional[UnitState]:
        """Returns the UnitState for a unit, or None."""
        with self._lock:
            return self._units.get(unit_id)

    @property
    def all_units(self) -> List[UnitState]:
        """All registered unit states."""
        with self._lock:
            return list(self._units.values())

    @property
    def active_units(self) -> List[UnitState]:
        """Units currently mowing or returning."""
        return [u for u in self.all_units if u.is_active]

    def mark_stale_units_offline(self) -> int:
        """
        Marks any unit that has not reported within stale_threshold_s as OFFLINE.

        Returns:
            Number of units marked offline.
        """
        count = 0
        with self._lock:
            for state in self._units.values():
                if (state.status != UnitStatus.OFFLINE
                        and state.seconds_since_seen > self._stale_threshold):
                    state.status = UnitStatus.OFFLINE
                    logger.warning("Fleet: unit %s marked OFFLINE (stale).", state.unit_id)
                    count += 1
        return count

    def __repr__(self) -> str:
        return f"UnitRegistry(units={len(self._units)}, active={len(self.active_units)})"
