"""
================================================================================
Project:  River Vector — Autonomous Mower Control System
File:     telemetry/logger.py
Purpose:  InfluxDB time-series telemetry writer. Consumes TelemetrySnapshots
          from the collector and writes them to InfluxDB at a configured
          interval. Handles connection failures gracefully with a local
          write buffer.
Author:   [Author Placeholder]
Version:  0.1.0
Date:     2026-05-25
================================================================================
"""

import logging
import threading
import time
from collections import deque
from typing import Optional

from influxdb_client import InfluxDBClient, WriteOptions
from influxdb_client.client.write_api import SYNCHRONOUS
from influxdb_client.domain.write_precision import WritePrecision

from core.constants import INFLUX_WRITE_INTERVAL_SEC
from telemetry.collector import TelemetryCollector, TelemetrySnapshot

logger = logging.getLogger(__name__)

# Maximum number of snapshots to buffer when InfluxDB is unreachable
BUFFER_MAX_SIZE: int = 1000


class InfluxLogger:
    """
    Writes telemetry snapshots to InfluxDB.

    Runs a background write thread that collects snapshots at
    INFLUX_WRITE_INTERVAL_SEC and writes them as InfluxDB line protocol
    points. Failed writes are buffered and retried on the next cycle.

    Args:
        config: MowerConfig with InfluxDB connection settings.
        collector: TelemetryCollector to pull snapshots from.
    """

    MEASUREMENT = "mower_telemetry"

    def __init__(self, config, collector: TelemetryCollector) -> None:
        if config is None:
            raise ValueError("config must not be None.")
        if collector is None:
            raise ValueError("collector must not be None.")
        self._config = config
        self._collector = collector
        self._client: Optional[InfluxDBClient] = None
        self._write_api = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._buffer: deque = deque(maxlen=BUFFER_MAX_SIZE)
        self._write_count = 0
        self._error_count = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """
        Connects to InfluxDB and starts the write thread.
        Connection failure is non-fatal — buffering begins immediately.
        """
        self._connect()
        self._running = True
        self._thread = threading.Thread(
            target=self._write_loop,
            name="InfluxLogger",
            daemon=True,
        )
        self._thread.start()
        logger.info("InfluxDB logger started.")

    def stop(self) -> None:
        """Stops the write thread and closes the InfluxDB connection."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        if self._client:
            self._client.close()
        logger.info(
            "InfluxDB logger stopped. Writes: %d, Errors: %d.",
            self._write_count,
            self._error_count,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        """Attempts to connect to InfluxDB."""
        token = self._config.influx_token
        if not token:
            logger.warning(
                "INFLUX_TOKEN not set — InfluxDB writes will fail. "
                "Set INFLUX_TOKEN in .env file."
            )
        try:
            self._client = InfluxDBClient(
                url=self._config.influx_url,
                token=token or "",
                org=self._config.influx_org,
            )
            self._write_api = self._client.write_api(write_options=SYNCHRONOUS)
            logger.info(
                "InfluxDB connected: %s / %s",
                self._config.influx_url,
                self._config.influx_bucket,
            )
        except Exception as exc:
            logger.warning("InfluxDB connection failed (will retry): %s", exc)

    def _write_loop(self) -> None:
        """Background thread that collects and writes telemetry snapshots."""
        while self._running:
            try:
                snapshot = self._collector.collect()
                self._buffer.append(snapshot)
                self._flush_buffer()
            except Exception as exc:
                logger.error("InfluxDB write loop error: %s", exc, exc_info=True)
            time.sleep(INFLUX_WRITE_INTERVAL_SEC)

    def _flush_buffer(self) -> None:
        """Writes all buffered snapshots to InfluxDB."""
        if not self._write_api or not self._buffer:
            return

        points = []
        while self._buffer:
            snap = self._buffer.popleft()
            points.append(self._snapshot_to_point(snap))

        try:
            self._write_api.write(
                bucket=self._config.influx_bucket,
                org=self._config.influx_org,
                record=points,
                precision=WritePrecision.SECONDS,
            )
            self._write_count += len(points)
        except Exception as exc:
            logger.warning("InfluxDB write failed (%d points): %s", len(points), exc)
            self._error_count += 1
            # Re-buffer failed points (up to buffer limit)
            for p in points:
                self._buffer.appendleft(p)

    def _snapshot_to_point(self, snap: TelemetrySnapshot) -> dict:
        """
        Converts a TelemetrySnapshot to an InfluxDB point dict.

        Args:
            snap: TelemetrySnapshot to convert.

        Returns:
            InfluxDB point dictionary.
        """
        fields = {k: v for k, v in snap.to_dict().items()
                  if v is not None
                  and k not in ("timestamp", "unit_id", "active_fault_codes")}

        return {
            "measurement": self.MEASUREMENT,
            "tags": {
                "unit_id": snap.unit_id,
                "operating_mode": snap.operating_mode,
            },
            "fields": fields,
            "time": int(snap.timestamp),
        }

    def __repr__(self) -> str:
        return (
            f"InfluxLogger(running={self._running}, "
            f"writes={self._write_count}, "
            f"errors={self._error_count}, "
            f"buffered={len(self._buffer)})"
        )
