"""
River Vector - Terrain Monitor (slope)

Computes instantaneous ground slope from the recent GPS-fix history, for
consumption by the telemetry assembler and the slope safety enforcer.

Algorithm (per integration spec):
  * Keep a rolling buffer of the last N GPS fixes that have a valid altitude.
  * For each consecutive pair, slope_pct = (|Δalt| / horizontal_m) * 100, where
    horizontal_m is the haversine distance. Pairs with horizontal_m <= 0.1 m
    are ignored (stationary GPS noise would explode the ratio).
  * Report the MAX slope_pct seen across the buffer, clamped to [0, 100].
  * Returns None until at least two valid-altitude fixes exist.

Altitude comes only from the device's own GPS. No external elevation service is
ever consulted here — that enrichment is the server's job.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Deque, Optional, Tuple

from core.constants import SLOPE_BUFFER_SIZE, SLOPE_MIN_HORIZONTAL_M

_EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two lat/lng points, in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return _EARTH_RADIUS_M * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class TerrainMonitor:
    """Rolling-window slope estimator fed one GPS fix at a time."""

    def __init__(self, buffer_size: int = SLOPE_BUFFER_SIZE) -> None:
        # Each entry: (lat, lng, alt_m).
        self._buffer: Deque[Tuple[float, float, float]] = deque(maxlen=buffer_size)
        self._slope_pct: Optional[float] = None

    def update(self, lat: Optional[float], lng: Optional[float],
               alt_m: Optional[float]) -> Optional[float]:
        """
        Records a new fix and recomputes slope. Fixes without lat/lng/alt are
        ignored (they cannot contribute a slope sample). Returns the current
        slope_pct (or None).
        """
        if lat is None or lng is None or alt_m is None:
            return self._slope_pct
        self._buffer.append((float(lat), float(lng), float(alt_m)))
        self._recompute()
        return self._slope_pct

    def _recompute(self) -> None:
        pts = list(self._buffer)
        if len(pts) < 2:
            self._slope_pct = None
            return
        max_slope = 0.0
        seen = False
        for (lat1, lng1, alt1), (lat2, lng2, alt2) in zip(pts, pts[1:]):
            horizontal_m = haversine_m(lat1, lng1, lat2, lng2)
            if horizontal_m <= SLOPE_MIN_HORIZONTAL_M:
                continue
            vertical_m = abs(alt2 - alt1)
            slope = (vertical_m / horizontal_m) * 100.0
            max_slope = max(max_slope, slope)
            seen = True
        # If every pair was stationary, hold the previous reading rather than
        # reporting a misleading 0 (the mower simply wasn't moving).
        if seen:
            self._slope_pct = max(0.0, min(100.0, max_slope))

    @property
    def slope_pct(self) -> Optional[float]:
        """Current slope estimate in %, or None if not enough data."""
        return self._slope_pct

    def reset(self) -> None:
        self._buffer.clear()
        self._slope_pct = None
