"""
River Vector - Boundary Enforcement
Manages the GPS geofence polygon for the mowing area.
Provides point-in-polygon checks, proximity warnings, and breach detection.
Polygon coordinates are (latitude, longitude) decimal degree tuples.
"""

import logging
import math
from typing import List, Optional, Tuple

from core.constants import BOUNDARY_MARGIN, FaultCode
from safety.fault_manager import FaultManager, FaultSeverity

logger = logging.getLogger(__name__)

Coordinate = Tuple[float, float]   # (latitude, longitude)

# Earth radius for local metric approximation
_EARTH_R_M: float = 6_371_000.0


class BoundaryManager:
    """
    GPS geofence boundary for autonomous mowing.

    Stores the property boundary polygon and provides:
    - is_inside(lat, lng): strict containment check
    - is_near_boundary(lat, lng, margin_m): proximity warning zone
    - breach detection with fault reporting

    The polygon must be closed — first and last point should be equal,
    but closure is handled internally if omitted.

    Args:
        fault_manager: FaultManager for breach fault reporting (optional).
    """

    def __init__(self, fault_manager: Optional[FaultManager] = None) -> None:
        self._fault_manager = fault_manager
        self._boundary: List[Coordinate] = []   # (lat, lng) tuples

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load_from_coords(self, coords: List[Coordinate]) -> None:
        """
        Loads the boundary polygon from a list of (lat, lng) tuples.

        Args:
            coords: Ordered list of (latitude, longitude) vertices.

        Raises:
            ValueError: If fewer than 3 vertices are provided.
        """
        if len(coords) < 3:
            raise ValueError(
                f"Boundary requires at least 3 vertices, got {len(coords)}."
            )
        # Ensure polygon is closed
        self._boundary = list(coords)
        if self._boundary[0] != self._boundary[-1]:
            self._boundary.append(self._boundary[0])

        logger.info(
            "Boundary loaded: %d vertices, approx area=%.0fm².",
            len(self._boundary) - 1,
            self.approximate_area_m2(),
        )

    def load_from_dicts(self, coord_dicts: List[dict]) -> None:
        """
        Loads the boundary from a list of {"lat": ..., "lng": ...} dicts.

        This matches the format used in fleets/*.json files.

        Args:
            coord_dicts: List of dicts with 'lat' and 'lng' keys.
        """
        coords = [(d["lat"], d["lng"]) for d in coord_dicts]
        self.load_from_coords(coords)

    # ------------------------------------------------------------------
    # Containment checks
    # ------------------------------------------------------------------

    @property
    def is_defined(self) -> bool:
        """True if a valid boundary polygon has been loaded."""
        return len(self._boundary) >= 4  # At least 3 unique + closure point

    def is_inside(self, lat: float, lng: float) -> bool:
        """
        Returns True if the coordinate is inside the boundary polygon.

        Uses the ray-casting (even-odd rule) algorithm.

        Args:
            lat: Latitude in decimal degrees.
            lng: Longitude in decimal degrees.

        Returns:
            True if inside or on the boundary edge.
        """
        if not self.is_defined:
            return True  # No boundary defined — all positions permitted

        return self._ray_cast(lat, lng)

    def is_near_boundary(self, lat: float, lng: float, margin_m: float = BOUNDARY_MARGIN) -> bool:
        """
        Returns True if the coordinate is within margin_m of the boundary edge.

        Used for slowing down before reaching the hard geofence.

        Args:
            lat:      Latitude in decimal degrees.
            lng:      Longitude in decimal degrees.
            margin_m: Warning margin in meters.

        Returns:
            True if within the warning margin of any boundary edge.
        """
        if not self.is_defined:
            return False

        for i in range(len(self._boundary) - 1):
            seg_a = self._boundary[i]
            seg_b = self._boundary[i + 1]
            dist = self._point_to_segment_distance_m(lat, lng, seg_a, seg_b)
            if dist <= margin_m:
                return True
        return False

    def check_and_report(self, lat: float, lng: float) -> bool:
        """
        Checks containment and reports a BOUNDARY_BREACH fault if outside.

        Args:
            lat: Current latitude.
            lng: Current longitude.

        Returns:
            True if inside boundary (safe), False if breach detected.
        """
        inside = self.is_inside(lat, lng)
        if not inside and self._fault_manager:
            if not self._fault_manager.has_active_fault(FaultCode.BOUNDARY_BREACH):
                logger.critical(
                    "BOUNDARY BREACH — position (%.6f, %.6f) is outside geofence.",
                    lat, lng,
                )
                self._fault_manager.report_fault(
                    FaultCode.BOUNDARY_BREACH,
                    detail=f"Position ({lat:.6f}, {lng:.6f}) outside geofence.",
                    severity=FaultSeverity.FATAL,
                )
        elif inside and self._fault_manager:
            if self._fault_manager.has_active_fault(FaultCode.BOUNDARY_BREACH):
                self._fault_manager.clear_fault(FaultCode.BOUNDARY_BREACH)
                logger.info("Boundary breach cleared — position back inside geofence.")
        return inside

    # ------------------------------------------------------------------
    # Geometry
    # ------------------------------------------------------------------

    def approximate_area_m2(self) -> float:
        """
        Estimates the polygon area using the Shoelace formula in local meters.

        Returns:
            Approximate area in square meters.
        """
        if not self.is_defined:
            return 0.0

        origin_lat, origin_lng = self._boundary[0]
        local = [self._geo_to_local(lat, lng, origin_lat, origin_lng)
                 for lat, lng in self._boundary]

        n = len(local)
        area = 0.0
        for i in range(n - 1):
            x1, y1 = local[i]
            x2, y2 = local[i + 1]
            area += x1 * y2 - x2 * y1
        return abs(area) / 2.0

    # ------------------------------------------------------------------
    # Internal geometry helpers
    # ------------------------------------------------------------------

    def _ray_cast(self, lat: float, lng: float) -> bool:
        """Even-odd ray casting: counts crossings of boundary edges."""
        inside = False
        poly = self._boundary
        n = len(poly)
        j = n - 1
        for i in range(n):
            lat_i, lng_i = poly[i]
            lat_j, lng_j = poly[j]
            if ((lng_i > lng) != (lng_j > lng)) and (
                lat < (lat_j - lat_i) * (lng - lng_i) / (lng_j - lng_i + 1e-15) + lat_i
            ):
                inside = not inside
            j = i
        return inside

    @staticmethod
    def _geo_to_local(
        lat: float, lng: float, origin_lat: float, origin_lng: float
    ) -> Tuple[float, float]:
        """Converts GPS to local Cartesian meters from origin."""
        x = math.radians(lng - origin_lng) * _EARTH_R_M * math.cos(math.radians(origin_lat))
        y = math.radians(lat - origin_lat) * _EARTH_R_M
        return x, y

    @staticmethod
    def _point_to_segment_distance_m(
        lat: float, lng: float,
        seg_a: Coordinate, seg_b: Coordinate,
    ) -> float:
        """
        Computes the approximate distance in meters from a point to a line segment.
        """
        origin_lat, origin_lng = seg_a
        px, py = BoundaryManager._geo_to_local(lat, lng, origin_lat, origin_lng)
        bx, by = BoundaryManager._geo_to_local(seg_b[0], seg_b[1], origin_lat, origin_lng)

        seg_len2 = bx * bx + by * by
        if seg_len2 == 0.0:
            return math.hypot(px, py)

        t = max(0.0, min(1.0, (px * bx + py * by) / seg_len2))
        proj_x = bx * t
        proj_y = by * t
        return math.hypot(px - proj_x, py - proj_y)

    def __repr__(self) -> str:
        return (
            f"BoundaryManager(defined={self.is_defined}, "
            f"vertices={max(0, len(self._boundary) - 1)}, "
            f"area≈{self.approximate_area_m2():.0f}m²)"
        )
