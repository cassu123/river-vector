"""
River Vector - Fleet Coordinator
Central authority for multi-unit fleet operations.
Assigns zones, monitors unit health, and detects inter-unit conflicts.
"""

import json
import logging
import os
import threading
import time
from typing import Dict, List, Optional, Tuple

from fleet.unit_registry import UnitRegistry, UnitState, UnitStatus
from fleet.zone_partitioner import Zone, ZonePartitioner

logger = logging.getLogger(__name__)

Coordinate = Tuple[float, float]

# Distance threshold below which two units are considered on a collision course
CONFLICT_DISTANCE_M: float = 3.0


class FleetCoordinator:
    """
    Manages a fleet of River Vector units operating on the same property.

    Responsibilities:
    - Load fleet manifest (units, boundary, home positions)
    - Partition the boundary into per-unit zones
    - Detect proximity conflicts between active units
    - Mark stale units offline
    - Provide a unified fleet status view

    Args:
        registry:        UnitRegistry tracking all unit states.
        poll_interval_s: Seconds between health/conflict checks.
    """

    def __init__(
        self,
        registry: UnitRegistry,
        poll_interval_s: float = 2.0,
    ) -> None:
        self._registry = registry
        self._poll_interval = poll_interval_s
        self._boundary: List[Coordinate] = []
        self._zones: Dict[str, Zone] = {}          # zone_id → Zone
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Fleet manifest
    # ------------------------------------------------------------------

    @classmethod
    def from_manifest(cls, manifest_path: str, registry: UnitRegistry) -> "FleetCoordinator":
        """
        Loads a fleet manifest JSON and registers all units in the registry.

        Args:
            manifest_path: Path to a fleets/*.json file.
            registry:      UnitRegistry to populate.

        Returns:
            Configured FleetCoordinator (not yet started).
        """
        if not os.path.exists(manifest_path):
            raise FileNotFoundError(f"Fleet manifest not found: {manifest_path}")

        with open(manifest_path, "r") as f:
            manifest = json.load(f)

        coordinator = cls(registry)
        coordinator._boundary = [
            (pt["lat"], pt["lng"]) for pt in manifest.get("boundary", [])
        ]

        for unit_def in manifest.get("units", []):
            registry.register(
                unit_id=unit_def["unit_id"],
                unit_name=unit_def["unit_name"],
                platform=unit_def["platform"],
            )

        logger.info(
            "Fleet manifest loaded: %s — %d units, boundary=%d pts",
            manifest.get("fleet_name", "unnamed"),
            len(manifest.get("units", [])),
            len(coordinator._boundary),
        )
        return coordinator

    # ------------------------------------------------------------------
    # Zone assignment
    # ------------------------------------------------------------------

    def assign_zones(self) -> Dict[str, Zone]:
        """
        Partitions the fleet boundary into zones and assigns one per unit.

        Only IDLE or MOWING units are assigned zones. OFFLINE/FAULT units
        are skipped — remaining units cover more area each.

        Returns:
            Dict of unit_id → Zone for all assigned units.
        """
        if not self._boundary:
            raise ValueError("No boundary defined — load a fleet manifest first.")

        eligible = [
            u for u in self._registry.all_units
            if u.status not in (UnitStatus.OFFLINE, UnitStatus.FAULT, UnitStatus.ESTOP)
        ]

        if not eligible:
            logger.warning("No eligible units for zone assignment.")
            return {}

        partitioner = ZonePartitioner(self._boundary)
        zones = partitioner.partition(len(eligible))

        assignments: Dict[str, Zone] = {}
        for unit, zone in zip(eligible, zones):
            zone.unit_id = unit.unit_id
            self._zones[zone.zone_id] = zone
            self._registry.assign_zone(unit.unit_id, zone.zone_id)
            assignments[unit.unit_id] = zone
            logger.info(
                "Zone %s (%.0fm²) assigned to %s",
                zone.zone_id, zone.estimated_area_m2, unit.unit_id,
            )

        return assignments

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Starts the background health and conflict monitor thread."""
        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop, name="FleetCoordinator", daemon=True
        )
        self._thread.start()
        logger.info("Fleet coordinator started.")

    def stop(self) -> None:
        """Stops the background thread."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        logger.info("Fleet coordinator stopped.")

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def zones(self) -> Dict[str, Zone]:
        """Currently assigned zones keyed by zone_id."""
        return dict(self._zones)

    @property
    def registry(self) -> UnitRegistry:
        return self._registry

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        while self._running:
            try:
                self._registry.mark_stale_units_offline()
                self._check_conflicts()
            except Exception as exc:
                logger.error("Fleet coordinator poll error: %s", exc, exc_info=True)
            time.sleep(self._poll_interval)

    def _check_conflicts(self) -> None:
        """Logs a warning when two active units are within CONFLICT_DISTANCE_M."""
        active = [u for u in self._registry.active_units if u.position is not None]
        for i in range(len(active)):
            for j in range(i + 1, len(active)):
                a, b = active[i], active[j]
                dist = self._haversine_m(a.position, b.position)
                if dist < CONFLICT_DISTANCE_M:
                    logger.warning(
                        "CONFLICT: %s and %s are %.1fm apart (threshold %.1fm)",
                        a.unit_id, b.unit_id, dist, CONFLICT_DISTANCE_M,
                    )

    @staticmethod
    def _haversine_m(a: Coordinate, b: Coordinate) -> float:
        import math
        lat1, lng1 = map(math.radians, a)
        lat2, lng2 = map(math.radians, b)
        dlat = lat2 - lat1
        dlng = lng2 - lng1
        h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
        return 6_371_000.0 * 2 * math.atan2(math.sqrt(h), math.sqrt(1 - h))

    def __repr__(self) -> str:
        return (
            f"FleetCoordinator("
            f"units={len(self._registry.all_units)}, "
            f"zones={len(self._zones)})"
        )
