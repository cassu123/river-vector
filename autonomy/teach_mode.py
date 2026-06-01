"""
River Vector - Boundary Teach Mode

Captures GPS waypoints while an operator manually drives the perimeter
of a yard. The captured polygon is pushed to River Song as a new zone.

Flow:
  1. Operator clicks "Teach Boundary" in the UI, names the zone, picks
     this unit.
  2. River Song issues teach.start command. Device transitions to
     TEACH state, starts a TeachSession.
  3. Operator drives the perimeter. The TeachSession captures GPS
     waypoints at TEACH_CAPTURE_HZ Hz.
  4. Every 5 seconds, the accumulated batch is pushed to
     POST /api/vector/zones/teach.
  5. Operator clicks "End Boundary" (or River Song issues teach.end).
     The session finalizes — closes the polygon if needed, pushes
     the final batch with finalize=true, transitions out of TEACH.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from core.constants import TEACH_CAPTURE_HZ, TEACH_WAYPOINT_BUFFER_MAX

logger = logging.getLogger(__name__)


# GPS provider: returns {"lat","lng","alt"} (alt may be None) or None if no fix.
GPSProvider = Callable[[], Optional[Dict[str, float]]]

# A captured waypoint is a [lat, lng, alt_m] triplet; alt_m is None without a 3-D fix.
Waypoint = List[Optional[float]]


class TeachSession:
    """
    One active boundary-teach session.

    Args:
        zone_name:    The name the operator gave the zone.
        api_client:   RiverSongClient (for waypoint pushes).
        gps_provider: Callable returning {"lat","lng"} or None.
    """

    PUSH_INTERVAL_SEC: float = 5.0

    def __init__(
        self,
        zone_name: str,
        api_client,
        gps_provider: GPSProvider,
    ) -> None:
        if not zone_name:
            raise ValueError("zone_name must not be empty.")
        self._zone_name = zone_name
        self._api = api_client
        self._gps = gps_provider
        self._lock = threading.Lock()
        self._buffer: List[Waypoint] = []
        self._unpushed: List[Waypoint] = []
        self._running = False
        self._capture_thread: Optional[threading.Thread] = None
        self._push_thread: Optional[threading.Thread] = None

    # ──────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._capture_thread = threading.Thread(
            target=self._capture_loop,
            name="TeachCapture",
            daemon=True,
        )
        self._push_thread = threading.Thread(
            target=self._push_loop,
            name="TeachPush",
            daemon=True,
        )
        self._capture_thread.start()
        self._push_thread.start()
        logger.info("TeachSession started: zone_name=%s", self._zone_name)

    def end(self, save: bool = True) -> Dict[str, Any]:
        """
        Ends the session. If save=True, pushes the final batch with
        finalize=True so River Song stores the zone.

        Returns a summary dict.
        """
        self._running = False
        if self._capture_thread:
            self._capture_thread.join(timeout=2.0)
        if self._push_thread:
            self._push_thread.join(timeout=2.0)

        with self._lock:
            unpushed = list(self._unpushed)
            self._unpushed.clear()
            total = len(self._buffer)

        if save:
            ok = self._api.push_teach_waypoints(
                zone_name=self._zone_name,
                waypoints=unpushed,
                finalize=True,
            )
            logger.info(
                "TeachSession ended (saved=%s) — %d total waypoints captured.",
                ok, total,
            )
            return {"saved": ok, "total_waypoints": total}
        logger.info("TeachSession ended (discarded) — %d waypoints.", total)
        return {"saved": False, "total_waypoints": total}

    # ──────────────────────────────────────────────────────────────────
    # State
    # ──────────────────────────────────────────────────────────────────

    @property
    def zone_name(self) -> str:
        return self._zone_name

    @property
    def waypoint_count(self) -> int:
        with self._lock:
            return len(self._buffer)

    # ──────────────────────────────────────────────────────────────────
    # Internal
    # ──────────────────────────────────────────────────────────────────

    def _capture_loop(self) -> None:
        interval = 1.0 / TEACH_CAPTURE_HZ
        while self._running:
            try:
                point = self._gps()
                if point and "lat" in point and "lng" in point:
                    self._add_waypoint(point)
            except Exception as exc:
                logger.error("TeachSession capture error: %s", exc, exc_info=True)
            time.sleep(interval)

    def _push_loop(self) -> None:
        while self._running:
            time.sleep(self.PUSH_INTERVAL_SEC)
            self._flush_batch()

    def _add_waypoint(self, point: Dict[str, float]) -> None:
        # Record a [lat, lng, alt_m] triplet. alt_m is None when the GPS has no
        # 3-D fix; the server stores the triplet either way and handles None.
        triplet: Waypoint = [
            point.get("lat"),
            point.get("lng"),
            point.get("alt"),
        ]
        with self._lock:
            if len(self._buffer) >= TEACH_WAYPOINT_BUFFER_MAX:
                logger.warning(
                    "TeachSession buffer full (%d) — dropping waypoint.",
                    TEACH_WAYPOINT_BUFFER_MAX,
                )
                return
            self._buffer.append(triplet)
            self._unpushed.append(triplet)

    def _flush_batch(self) -> None:
        with self._lock:
            batch = list(self._unpushed)
            self._unpushed.clear()

        if not batch:
            return
        ok = self._api.push_teach_waypoints(
            zone_name=self._zone_name,
            waypoints=batch,
            finalize=False,
        )
        if not ok:
            # Push failed — re-queue.
            with self._lock:
                self._unpushed = batch + self._unpushed
            logger.warning("TeachSession push failed; %d waypoints re-queued.", len(batch))


class TeachManager:
    """
    Singleton-style holder for the current teach session.

    The main loop calls handle_command() for teach.* actions; this
    starts, drives, and ends one TeachSession at a time.
    """

    def __init__(self, api_client, gps_provider: GPSProvider) -> None:
        self._api = api_client
        self._gps = gps_provider
        self._session: Optional[TeachSession] = None

    @property
    def active(self) -> bool:
        return self._session is not None

    def handle(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatches teach.* actions. Returns a result dict."""
        if action == "teach.start":
            zone_name = str(params.get("zone_name", "")).strip()
            if not zone_name:
                raise ValueError("teach.start requires zone_name")
            if self._session is not None:
                raise RuntimeError(
                    "A teach session is already in progress. End it first."
                )
            self._session = TeachSession(zone_name, self._api, self._gps)
            self._session.start()
            return {"zone_name": zone_name, "status": "started"}

        if action == "teach.waypoint":
            if self._session is None:
                raise RuntimeError("No teach session in progress.")
            pt = self._gps()
            if not pt:
                return {"status": "no_fix"}
            self._session._add_waypoint(pt)  # internal but explicit add
            return {"status": "added", "count": self._session.waypoint_count}

        if action == "teach.end":
            if self._session is None:
                raise RuntimeError("No teach session in progress.")
            save = bool(params.get("save", True))
            result = self._session.end(save=save)
            self._session = None
            return result

        raise ValueError(f"Unknown teach action: {action}")
