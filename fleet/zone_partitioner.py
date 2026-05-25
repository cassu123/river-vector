"""
River Vector - Zone Partitioner
Divides a boundary polygon into N sub-zones for multi-unit fleet coverage.
Each unit receives one zone and runs its own PathPlanner on that zone.
"""

import logging
import math
from dataclasses import dataclass, field
from typing import List, Tuple

logger = logging.getLogger(__name__)

Coordinate = Tuple[float, float]  # (latitude, longitude)
EARTH_RADIUS_M: float = 6_371_000.0


@dataclass
class Zone:
    """A sub-polygon representing one unit's assigned mowing area."""
    zone_id: str
    boundary: List[Coordinate]  # Closed polygon (first == last point)
    unit_id: str = ""           # Assigned unit (empty = unassigned)
    estimated_area_m2: float = 0.0


class ZonePartitioner:
    """
    Splits a boundary polygon into N roughly equal sub-zones.

    Strategy: strips approach — the bounding box is divided into N vertical
    strips along the longest axis, then clipped to the boundary polygon.
    Each strip becomes one Zone assigned to one unit.

    This is intentionally simple and correct. Irregular shapes produce
    slightly unequal zones, which is acceptable for the first iteration.

    Args:
        boundary: List of (lat, lng) coordinates defining the outer boundary.
    """

    def __init__(self, boundary: List[Coordinate]) -> None:
        if len(boundary) < 3:
            raise ValueError("Boundary must have at least 3 points.")
        self._boundary = boundary

    def partition(self, n_units: int) -> List[Zone]:
        """
        Partitions the boundary into n_units sub-zones.

        Args:
            n_units: Number of zones to produce. Must be >= 1.

        Returns:
            List of Zone objects, one per unit, ordered left to right.
        """
        if n_units < 1:
            raise ValueError("n_units must be >= 1.")
        if n_units == 1:
            area = self._polygon_area_m2(self._boundary)
            return [Zone(zone_id="zone_0", boundary=list(self._boundary), estimated_area_m2=area)]

        origin_lat, origin_lng = self._boundary[0]
        local_pts = [self._geo_to_local(lat, lng, origin_lat, origin_lng)
                     for lat, lng in self._boundary]

        min_x = min(p[0] for p in local_pts)
        max_x = max(p[0] for p in local_pts)
        min_y = min(p[1] for p in local_pts)
        max_y = max(p[1] for p in local_pts)

        strip_width = (max_x - min_x) / n_units
        zones: List[Zone] = []

        for i in range(n_units):
            x_start = min_x + i * strip_width
            x_end   = min_x + (i + 1) * strip_width

            # Strip corners in local coords
            strip_local = [
                (x_start, min_y),
                (x_end,   min_y),
                (x_end,   max_y),
                (x_start, max_y),
                (x_start, min_y),
            ]

            # Clip strip against boundary polygon
            clipped = self._clip_polygon(strip_local, local_pts)

            if not clipped:
                logger.warning("Zone %d produced empty polygon — skipping.", i)
                continue

            geo_boundary = [
                self._local_to_geo(x, y, origin_lat, origin_lng) for x, y in clipped
            ]
            area = self._polygon_area_m2(geo_boundary)

            zones.append(Zone(
                zone_id=f"zone_{i}",
                boundary=geo_boundary,
                estimated_area_m2=area,
            ))

        logger.info("Partitioned boundary into %d zones.", len(zones))
        return zones

    # ------------------------------------------------------------------
    # Geometry helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _geo_to_local(lat: float, lng: float, origin_lat: float, origin_lng: float) -> Tuple[float, float]:
        x = math.radians(lng - origin_lng) * EARTH_RADIUS_M * math.cos(math.radians(origin_lat))
        y = math.radians(lat - origin_lat) * EARTH_RADIUS_M
        return x, y

    @staticmethod
    def _local_to_geo(x: float, y: float, origin_lat: float, origin_lng: float) -> Coordinate:
        lat = origin_lat + math.degrees(y / EARTH_RADIUS_M)
        lng = origin_lng + math.degrees(x / (EARTH_RADIUS_M * math.cos(math.radians(origin_lat))))
        return lat, lng

    @staticmethod
    def _polygon_area_m2(boundary: List[Coordinate]) -> float:
        """Shoelace formula approximation for small geographic areas."""
        if len(boundary) < 3:
            return 0.0
        origin_lat, origin_lng = boundary[0]
        pts = [ZonePartitioner._geo_to_local(lat, lng, origin_lat, origin_lng)
               for lat, lng in boundary]
        n = len(pts)
        area = 0.0
        for i in range(n):
            j = (i + 1) % n
            area += pts[i][0] * pts[j][1]
            area -= pts[j][0] * pts[i][1]
        return abs(area) / 2.0

    @staticmethod
    def _clip_polygon(
        subject: List[Tuple[float, float]],
        clip: List[Tuple[float, float]],
    ) -> List[Tuple[float, float]]:
        """
        Sutherland-Hodgman polygon clipping.
        Returns the intersection of subject and clip polygons.
        """
        def inside(p, a, b):
            return (b[0] - a[0]) * (p[1] - a[1]) >= (b[1] - a[1]) * (p[0] - a[0])

        def intersection(a, b, c, d):
            ab = (b[0] - a[0], b[1] - a[1])
            cd = (d[0] - c[0], d[1] - c[1])
            denom = ab[0] * cd[1] - ab[1] * cd[0]
            if abs(denom) < 1e-10:
                return a
            t = ((c[0] - a[0]) * cd[1] - (c[1] - a[1]) * cd[0]) / denom
            return (a[0] + t * ab[0], a[1] + t * ab[1])

        output = list(subject)
        if not output:
            return []

        n = len(clip)
        for i in range(n - 1):
            if not output:
                break
            edge_start, edge_end = clip[i], clip[i + 1]
            input_pts = output
            output = []
            for j in range(len(input_pts)):
                current = input_pts[j]
                previous = input_pts[j - 1]
                if inside(current, edge_start, edge_end):
                    if not inside(previous, edge_start, edge_end):
                        output.append(intersection(previous, current, edge_start, edge_end))
                    output.append(current)
                elif inside(previous, edge_start, edge_end):
                    output.append(intersection(previous, current, edge_start, edge_end))

        return output
