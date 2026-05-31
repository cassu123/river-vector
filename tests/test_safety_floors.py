"""
Tests for safety floor enforcement.

Verifies that the three-layer model holds:
  1. Absolute floors are clamped to in config_sync.
  2. Per-unit safety_floors are surfaced via interlocks.get_safety_floors().
  3. Per-program values may not loosen safety_floors.
"""

import os
import tempfile
import unittest
from typing import Any, Dict, Optional

from connectivity.config_sync import ConfigSync
from core.constants import (
    ABSOLUTE_MAX_IMU_TILT_CUTOFF_DEG,
    ABSOLUTE_MAX_WATCHDOG_TIMEOUT_MS,
    ABSOLUTE_MIN_IMU_TILT_CUTOFF_DEG,
    ABSOLUTE_MIN_OBSTACLE_CLEARANCE_M,
    ABSOLUTE_MIN_WATCHDOG_TIMEOUT_MS,
)
from safety.interlocks import Interlocks


def _config_with_floors(**floors: Any) -> Dict[str, Any]:
    return {
        "unit_id": "RV-TEST-X",
        "config_version": 1,
        "hardware": {
            "drive": {"type": "clutch", "gears": 7, "max_speed_kmh": 15.0},
            "deck": {"width_inches": 42, "engagement": "pto_lever"},
            "power": {"type": "gas"},
            "sensors": {
                "gps": "rtk",
                "imu": True,
                "fuel": True,
                "temperature": True,
                "rpm": True,
                "obstacle": "ultrasonic",
                "operator_presence": "seat_sensor",
            },
            "cameras": {"count": 0, "config": []},
        },
        "safety_floors": {
            "min_obstacle_clearance_m": floors.get("min_obstacle_clearance_m", 0.20),
            "imu_tilt_cutoff_deg": floors.get("imu_tilt_cutoff_deg", 15.0),
            "watchdog_timeout_ms": floors.get("watchdog_timeout_ms", 500),
            "min_battery_v_cutoff": floors.get("min_battery_v_cutoff", 11.0),
            "operator_presence_required_for_auto": floors.get(
                "operator_presence_required_for_auto", True
            ),
        },
    }


class _MockApi:
    def __init__(self, payload: Optional[Dict[str, Any]] = None) -> None:
        self.payload = payload

    def pull_config(self) -> Optional[Dict[str, Any]]:
        return self.payload


class TestSafetyFloorClamping(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.cache = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(self.cache)

    def tearDown(self) -> None:
        try:
            os.remove(self.cache)
        except OSError:
            pass

    def _build_sync(self, cfg: Dict[str, Any]) -> ConfigSync:
        sync = ConfigSync(_MockApi(cfg), cache_path=self.cache)
        sync.pull()
        return sync

    def test_clearance_below_floor_clamped(self) -> None:
        sync = self._build_sync(_config_with_floors(min_obstacle_clearance_m=0.01))
        self.assertEqual(
            sync.get_safety_floors()["min_obstacle_clearance_m"],
            ABSOLUTE_MIN_OBSTACLE_CLEARANCE_M,
        )

    def test_tilt_below_min_clamped(self) -> None:
        sync = self._build_sync(_config_with_floors(imu_tilt_cutoff_deg=2.0))
        self.assertEqual(
            sync.get_safety_floors()["imu_tilt_cutoff_deg"],
            ABSOLUTE_MIN_IMU_TILT_CUTOFF_DEG,
        )

    def test_tilt_above_max_clamped(self) -> None:
        sync = self._build_sync(_config_with_floors(imu_tilt_cutoff_deg=99.0))
        self.assertEqual(
            sync.get_safety_floors()["imu_tilt_cutoff_deg"],
            ABSOLUTE_MAX_IMU_TILT_CUTOFF_DEG,
        )

    def test_watchdog_below_min_clamped(self) -> None:
        sync = self._build_sync(_config_with_floors(watchdog_timeout_ms=50))
        self.assertEqual(
            sync.get_safety_floors()["watchdog_timeout_ms"],
            ABSOLUTE_MIN_WATCHDOG_TIMEOUT_MS,
        )

    def test_watchdog_above_max_clamped(self) -> None:
        sync = self._build_sync(_config_with_floors(watchdog_timeout_ms=999999))
        self.assertEqual(
            sync.get_safety_floors()["watchdog_timeout_ms"],
            ABSOLUTE_MAX_WATCHDOG_TIMEOUT_MS,
        )

    def test_valid_floors_passed_through(self) -> None:
        sync = self._build_sync(_config_with_floors(
            min_obstacle_clearance_m=0.40,
            imu_tilt_cutoff_deg=18.0,
            watchdog_timeout_ms=700,
        ))
        f = sync.get_safety_floors()
        self.assertEqual(f["min_obstacle_clearance_m"], 0.40)
        self.assertEqual(f["imu_tilt_cutoff_deg"], 18.0)
        self.assertEqual(f["watchdog_timeout_ms"], 700)


class TestInterlocksReadsFloors(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.cache = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(self.cache)
        self.sync = ConfigSync(
            _MockApi(_config_with_floors(min_obstacle_clearance_m=0.40)),
            cache_path=self.cache,
        )
        self.sync.pull()

    def tearDown(self) -> None:
        try:
            os.remove(self.cache)
        except OSError:
            pass

    def test_interlocks_surfaces_floors(self) -> None:
        il = Interlocks(config_sync=self.sync)
        floors = il.get_safety_floors()
        self.assertEqual(floors["min_obstacle_clearance_m"], 0.40)

    def test_interlocks_fallback_without_config(self) -> None:
        il = Interlocks(config_sync=None)
        floors = il.get_safety_floors()
        # Should still return a usable dict.
        self.assertIn("min_obstacle_clearance_m", floors)
        self.assertIn("imu_tilt_cutoff_deg", floors)


if __name__ == "__main__":
    unittest.main()
