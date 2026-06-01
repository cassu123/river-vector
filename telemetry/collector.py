"""
================================================================================
Project:  River Vector — Autonomous Mower Control System
File:     telemetry/collector.py
Purpose:  Aggregates all sensor and system data into a unified telemetry
          snapshot. Provides the single source of truth for current mower
          state consumed by the InfluxDB logger, River Song API, and
          alert monitor.
Author:   [Author Placeholder]
Version:  0.1.0
Date:     2026-05-25
================================================================================
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class TelemetrySnapshot:
    """
    Complete mower state snapshot at a point in time.
    All fields are Optional — None means data is not yet available.
    """
    timestamp: float = field(default_factory=time.time)
    unit_id: str = ""

    # Position
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    heading_deg: Optional[float] = None
    speed_mps: Optional[float] = None
    gps_fix_quality: int = 0
    gps_accuracy_m: Optional[float] = None
    altitude_m: Optional[float] = None
    altitude_accuracy_m: Optional[float] = None
    slope_pct: Optional[float] = None

    # Power
    battery_voltage_v: Optional[float] = None
    fuel_percent: Optional[float] = None

    # Thermal / mechanical
    engine_temp_c: Optional[float] = None
    engine_rpm: Optional[int] = None

    # IMU
    pitch_deg: Optional[float] = None
    roll_deg: Optional[float] = None

    # Obstacle
    ultrasonic_front_cm: Optional[float] = None
    ultrasonic_rear_cm: Optional[float] = None

    # System state
    operating_mode: str = "MANUAL"
    session_state: str = "IDLE"
    current_gear: int = 0
    pto_active: bool = False
    estop_active: bool = False
    active_fault_codes: list = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Converts the snapshot to a flat dictionary for serialization."""
        return {
            "timestamp": self.timestamp,
            "unit_id": self.unit_id,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "heading_deg": self.heading_deg,
            "speed_mps": self.speed_mps,
            "gps_fix_quality": self.gps_fix_quality,
            "gps_accuracy_m": self.gps_accuracy_m,
            "altitude_m": self.altitude_m,
            "altitude_accuracy_m": self.altitude_accuracy_m,
            "slope_pct": self.slope_pct,
            "battery_voltage_v": self.battery_voltage_v,
            "fuel_percent": self.fuel_percent,
            "engine_temp_c": self.engine_temp_c,
            "engine_rpm": self.engine_rpm,
            "pitch_deg": self.pitch_deg,
            "roll_deg": self.roll_deg,
            "ultrasonic_front_cm": self.ultrasonic_front_cm,
            "ultrasonic_rear_cm": self.ultrasonic_rear_cm,
            "operating_mode": self.operating_mode,
            "session_state": self.session_state,
            "current_gear": self.current_gear,
            "pto_active": self.pto_active,
            "estop_active": self.estop_active,
            "active_fault_codes": self.active_fault_codes,
        }


class TelemetryCollector:
    """
    Aggregates data from all subsystems into a unified telemetry snapshot.

    Subsystems call update_* methods to push their latest data. The
    collector merges all updates into a single TelemetrySnapshot that
    is consumed by the logger and API client.

    Args:
        unit_id: Unit identifier string from config.
        sensor_manager: SensorManager instance (optional).
        gps_manager: GPSManager instance (optional).
        mode_manager: ModeManager instance (optional).
        fault_manager: FaultManager instance (optional).
    """

    def __init__(
        self,
        unit_id: str,
        sensor_manager=None,
        gps_manager=None,
        mode_manager=None,
        fault_manager=None,
        terrain_monitor=None,
    ) -> None:
        self._unit_id = unit_id
        self._sensors = sensor_manager
        self._gps = gps_manager
        self._mode_manager = mode_manager
        self._fault_manager = fault_manager
        self._terrain = terrain_monitor
        self._lock = threading.Lock()
        self._snapshot = TelemetrySnapshot(unit_id=unit_id)

    # ------------------------------------------------------------------
    # Snapshot collection
    # ------------------------------------------------------------------

    def collect(self) -> TelemetrySnapshot:
        """
        Collects the latest data from all registered subsystems and
        returns an updated snapshot.

        Returns:
            Current TelemetrySnapshot.
        """
        snap = TelemetrySnapshot(unit_id=self._unit_id)

        # Sensor data
        if self._sensors:
            sensor_snap = self._sensors.snapshot
            snap.battery_voltage_v = sensor_snap.battery_voltage_v
            snap.fuel_percent = sensor_snap.fuel_percent
            snap.engine_temp_c = sensor_snap.engine_temp_c
            snap.engine_rpm = sensor_snap.engine_rpm
            snap.pitch_deg = sensor_snap.pitch_deg
            snap.roll_deg = sensor_snap.roll_deg
            snap.ultrasonic_front_cm = sensor_snap.ultrasonic_front_cm
            snap.ultrasonic_rear_cm = sensor_snap.ultrasonic_rear_cm
            snap.estop_active = bool(sensor_snap.estop_pressed)

        # GPS data
        if self._gps:
            fix = self._gps.fix
            snap.latitude = fix.latitude
            snap.longitude = fix.longitude
            snap.heading_deg = fix.heading_deg
            snap.speed_mps = fix.speed_ms
            snap.gps_fix_quality = fix.fix_quality
            snap.gps_accuracy_m = fix.accuracy_m
            snap.altitude_m = fix.altitude_m
            snap.altitude_accuracy_m = fix.altitude_accuracy_m

        # Terrain (slope) — device-calculated from GPS altitude history.
        if self._terrain is not None:
            snap.slope_pct = self._terrain.slope_pct

        # Mode
        if self._mode_manager:
            snap.operating_mode = self._mode_manager.mode.name

        # Faults
        if self._fault_manager:
            snap.active_fault_codes = [
                f.code for f in self._fault_manager.active_faults
            ]

        with self._lock:
            self._snapshot = snap

        return snap

    @property
    def latest(self) -> TelemetrySnapshot:
        """Returns the most recently collected snapshot without re-collecting."""
        with self._lock:
            return self._snapshot

    def __repr__(self) -> str:
        snap = self.latest
        return (
            f"TelemetryCollector(unit={self._unit_id}, "
            f"mode={snap.operating_mode}, "
            f"faults={snap.active_fault_codes})"
        )
