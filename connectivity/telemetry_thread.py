"""
River Vector - Telemetry Thread

Pushes telemetry snapshots to River Song at a state-dependent cadence.

Cadence (from spec §8):
  IDLE              → 30s
  MANUAL            → 15s
  AUTO              → 5s
  RETURNING_HOME    → 5s
  FAULT             → 1s
  ESTOP             → 1s
  SETUP_PENDING     → none
  UNCLAIMED/CLAIMING → none
  OFFLINE_REPLAY    → queue locally, batch-replay when online

The thread runs on its own timer. It does NOT block the main autonomy
loop. On offline, snapshots are buffered in a deque (capacity 500,
oldest evicted on overflow) and replayed in batches up to 50 per call
when connectivity is restored.
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from typing import Any, Callable, Deque, Dict, List, Optional

from core.constants import TELEMETRY_BATCH_MAX, TELEMETRY_QUEUE_MAX

logger = logging.getLogger(__name__)


# Cadence table (seconds between pushes per state).
# A value of None means "do not push in this state".
CADENCE: Dict[str, Optional[float]] = {
    "UNCLAIMED": None,
    "CLAIMING": None,
    "SETUP_PENDING": None,
    "IDLE": 30.0,
    "MANUAL": 15.0,
    "AUTO": 5.0,
    "RETURNING_HOME": 5.0,
    "FAULT": 1.0,
    "ESTOP": 1.0,
    "OFFLINE_REPLAY": 5.0,
    "TEACH": 5.0,
}


# Builder signature: returns one snapshot dict ready for POST.
# Provided by main.py — wraps TelemetryCollector + extra fields.
SnapshotBuilder = Callable[[], Dict[str, Any]]
# State accessor: returns the current operating mode name string.
StateAccessor = Callable[[], str]


class TelemetryThread:
    """
    Background thread that pushes telemetry on the state-driven cadence.

    Args:
        api_client:     RiverSongClient.
        probe:          ConnectivityProbe.
        build_snapshot: Returns a snapshot dict to push.
        get_state:      Returns the current operating-mode name.
    """

    def __init__(
        self,
        api_client,
        probe,
        build_snapshot: SnapshotBuilder,
        get_state: StateAccessor,
    ) -> None:
        self._api = api_client
        self._probe = probe
        self._build = build_snapshot
        self._get_state = get_state
        self._queue: Deque[Dict[str, Any]] = collections.deque(maxlen=TELEMETRY_QUEUE_MAX)
        self._dropped_count = 0
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # ──────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            name="TelemetryThread",
            daemon=True,
        )
        self._thread.start()
        logger.info("TelemetryThread started.")

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    # ──────────────────────────────────────────────────────────────────
    # State
    # ──────────────────────────────────────────────────────────────────

    @property
    def queue_depth(self) -> int:
        with self._lock:
            return len(self._queue)

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    # ──────────────────────────────────────────────────────────────────
    # Loop
    # ──────────────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        last_push = 0.0
        while self._running:
            state = self._get_state() or "IDLE"
            cadence = CADENCE.get(state)
            if cadence is None:
                time.sleep(1.0)
                continue

            now = time.time()
            if now - last_push < cadence:
                time.sleep(min(0.5, cadence - (now - last_push)))
                continue

            self._tick()
            last_push = now

    def _tick(self) -> None:
        """Builds one snapshot, attempts to push (with any queued backlog)."""
        try:
            snap = self._build()
        except Exception as exc:
            logger.error("Telemetry snapshot build failed: %s", exc, exc_info=True)
            return

        if not self._probe.is_online:
            self._enqueue(snap)
            return

        # Online: flush any backlog first, then this snapshot.
        backlog: List[Dict[str, Any]] = []
        with self._lock:
            while self._queue and len(backlog) < TELEMETRY_BATCH_MAX - 1:
                backlog.append(self._queue.popleft())
        backlog.append(snap)

        if not self._api.push_telemetry_batch(backlog):
            # Failure → re-queue the snapshots we tried to send.
            for s in backlog:
                self._enqueue(s)

    def _enqueue(self, snap: Dict[str, Any]) -> None:
        with self._lock:
            if len(self._queue) >= TELEMETRY_QUEUE_MAX:
                self._dropped_count += 1
                # deque has maxlen, so old entries are evicted automatically;
                # this branch is just for counting.
            self._queue.append(snap)
