"""
================================================================================
Project:  River Vector — Autonomous Mower Control System
File:     navigation/path_planner.py
Purpose:  Mowing pattern generator. Takes the boundary polygon and generates
          an ordered list of GPS waypoints for complete coverage. Supports
          parallel stripe and spiral patterns. Accounts for deck width and
          overlap percentage.
Author:   [Author Placeholder]
Version:  0.1.0
Date:     2026-05-25
================================================================================
"""

import logging
import math
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List, Optional, Tuple

from core.constants import FaultCode
from navigation.gps_manager import GPSManager, EARTH_RADIUS_M

logger = logging.getLogger(__name__)

Coordinate = Tuple[float, float]  # (latitude, longitude)


class MowPattern(Enum):
    """Available mowing path patterns."""
    PARALLEL_STRIPES = auto()   # Back-and-forth parallel rows
    SPIRAL_INWARD = auto()      # Spiral from boundary inward (future)
    PERIMETER_FIRST = auto()    # Perimeter pass then fill (future)


@dataclass
class Waypoint:
    """A single navigation waypoint."""
    lat: float
    lng: float
    heading_deg: Optional[float] = None     # Desired heading at this waypoint
    is_turn: bool = False                   # True if this is a row-end turn point
    index: int = 0


@dataclass
class MowPlan:
    """A complete mowing plan with ordered waypoints."""
    waypoints: List[Waypoint] = field(default_factory=list)
    pattern: MowPattern = MowPattern.PARALLEL_STRIPES
    deck_width_m: float = 1.067             # 42 inches in meters
    overlap_pct: float = 10.0              # 10% overlap between stripes
    estimated_area_m2: float = 0.0
    stripe_count: int = 0

    @property
    def total_waypoints(self) -> int:
        return len(self.waypoints)


class PathPlanner:
    """
    Generates GPS waypoint paths for complete mowing coverage.

    Takes the boundary polygon from BoundaryManager and produces an
    ordered list of Waypoints that cover the entire area with the
    configured deck width and overlap.

    Args:
        config: MowerConfig for deck width.
        gps_manager: GPSManager for current position reference.
        boundary_manager: BoundaryManager for the mowing polygon.
    """

    DEFAULT_OVERLAP_PCT: float = 10.0

    def __init__(
        self,
        config,
        gps_manager: GPSManager,
        boundary_manager,
    ) -> None:
        self._config = config
        self._gps = gps_manager
        self._boundary = boundary_manager
        self._deck_width_m = (config.deck_width_inches * 0.0254)  # inches → meters
        self._current_plan: Optional[MowPlan] = None

    # ------------------------------------------------------------------
    # Plan generation
    # ------------------------------------------------------------------

    def generate_plan(
        self,
        pattern: MowPattern = MowPattern.PARALLEL_STRIPES,
        overlap_pct: float = DEFAULT_OVERLAP_PCT,
        heading_deg: float = 0.0,
    ) -> MowPlan:
        """
        Generates a complete mowing plan for the defined boundary.

        Args:
            pattern: Mowing pattern to use.
            overlap_pct: Stripe overlap percentage (0–50).
            heading_deg: Stripe orientation in degrees (0 = North-South stripes).

        Returns:
            MowPlan with ordered waypoints.

        Raises:
            ValueError: If no boundary is defined or overlap is out of range.
        """
        if not self._boundary.is_defined:
            raise ValueError("No boundary defined — cannot generate mowing plan.")
        if not 0.0 <= overlap_pct <= 50.0:
            raise ValueError(f"overlap_pct must be 0–50, got {overlap_pct}.")

        logger.info(
            "Generating %s plan — deck=%.2fm, overlap=%.1f%%, heading=%.1f°",
            pattern.name,
            self._deck_width_m,
            overlap_pct,
            heading_deg,
        )

        if pattern == MowPattern.PARALLEL_STRIPES:
            plan = self._generate_parallel_stripes(overlap_pct, heading_deg)
        else:
            logger.warning("Pattern %s not yet implemented — using PARALLEL_STRIPES.", pattern.name)
            plan = self._generate_parallel_stripes(overlap_pct, heading_deg)

        self._current_plan = plan
        logger.info(
            "Plan generated: %d waypoints, %d stripes, ~%.0fm² coverage.",
            plan.total_waypoints,
            plan.stripe_count,
            plan.estimated_area_m2,
        )
        return plan

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def current_plan(self) -> Optional[MowPlan]:
        """The most recently generated mowing plan."""
        return self._current_plan

    # ------------------------------------------------------------------
    # Internal — parallel stripe generation
    # ------------------------------------------------------------------

    def _generate_parallel_stripes(
        self, overlap_pct: float, heading_deg: float
    ) -> MowPlan:
        """
        Generates parallel stripe waypoints covering the boundary polygon.

        Computes a bounding box aligned to the requested heading, then
        generates stripes spaced by (deck_width * (1 - overlap/100)).

        Args:
            overlap_pct: Stripe overlap percentage.
            heading_deg: Stripe direction in degrees.

        Returns:
            MowPlan with waypoints.
        """
        boundary_coords = self._boundary._boundary
        if not boundary_coords:
            return MowPlan()

        # Effective stripe spacing
        stripe_spacing_m = self._deck_width_m * (1.0 - overlap_pct / 100.0)

        # Compute bounding box in local Cartesian coordinates
        # Use the first boundary point as origin
        origin_lat, origin_lng = boundary_coords[0]
        local_points = [
            self._geo_to_local(lat, lng, origin_lat, origin_lng)
            for lat, lng in boundary_coords
        ]

        # Rotate points to align with heading
        angle_rad = math.radians(heading_deg)
        rotated = [self._rotate(x, y, -angle_rad) for x, y in local_points]

        min_x = min(p[0] for p in rotated)
        max_x = max(p[0] for p in rotated)
        min_y = min(p[1] for p in rotated)
        max_y = max(p[1] for p in rotated)

        # Generate stripe centerlines
        waypoints: List[Waypoint] = []
        stripe_count = 0
        x = min_x + self._deck_width_m / 2.0
        left_to_right = True

        while x <= max_x:
            # Stripe endpoints in rotated frame
            y_start = min_y if left_to_right else max_y
            y_end = max_y if left_to_right else min_y

            # Rotate back and convert to geo
            rx_start, ry_start = self._rotate(x, y_start, angle_rad)
            rx_end, ry_end = self._rotate(x, y_end, angle_rad)

            lat_start, lng_start = self._local_to_geo(rx_start, ry_start, origin_lat, origin_lng)
            lat_end, lng_end = self._local_to_geo(rx_end, ry_end, origin_lat, origin_lng)

            stripe_heading = heading_deg if left_to_right else (heading_deg + 180) % 360

            waypoints.append(Waypoint(
                lat=lat_start, lng=lng_start,
                heading_deg=stripe_heading,
                is_turn=False,
                index=len(waypoints),
            ))
            waypoints.append(Waypoint(
                lat=lat_end, lng=lng_end,
                heading_deg=stripe_heading,
                is_turn=True,
                index=len(waypoints),
            ))

            x += stripe_spacing_m
            left_to_right = not left_to_right
            stripe_count += 1

        area = (max_x - min_x) * (max_y - min_y)

        return MowPlan(
            waypoints=waypoints,
            pattern=MowPattern.PARALLEL_STRIPES,
            deck_width_m=self._deck_width_m,
            overlap_pct=overlap_pct,
            estimated_area_m2=area,
            stripe_count=stripe_count,
        )

    # ------------------------------------------------------------------
    # Coordinate utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _geo_to_local(
        lat: float, lng: float, origin_lat: float, origin_lng: float
    ) -> Tuple[float, float]:
        """Converts GPS coordinates to local Cartesian meters from origin."""
        x = math.radians(lng - origin_lng) * EARTH_RADIUS_M * math.cos(math.radians(origin_lat))
        y = math.radians(lat - origin_lat) * EARTH_RADIUS_M
        return x, y

    @staticmethod
    def _local_to_geo(
        x: float, y: float, origin_lat: float, origin_lng: float
    ) -> Tuple[float, float]:
        """Converts local Cartesian meters back to GPS coordinates."""
        lat = origin_lat + math.degrees(y / EARTH_RADIUS_M)
        lng = origin_lng + math.degrees(x / (EARTH_RADIUS_M * math.cos(math.radians(origin_lat))))
        return lat, lng

    @staticmethod
    def _rotate(x: float, y: float, angle_rad: float) -> Tuple[float, float]:
        """Rotates a 2D point by angle_rad radians."""
        cos_a = math.cos(angle_rad)
        sin_a = math.sin(angle_rad)
        return (x * cos_a - y * sin_a, x * sin_a + y * cos_a)

    def __repr__(self) -> str:
        plan = self._current_plan
        return (
            f"PathPlanner(deck={self._deck_width_m:.2f}m, "
            f"waypoints={plan.total_waypoints if plan else 0})"
        )
