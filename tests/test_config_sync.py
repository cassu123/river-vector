"""Tests for connectivity/config_sync.py."""

import json
import os
import tempfile
import unittest
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

from connectivity.config_sync import (
    ConfigInvalidError,
    ConfigSync,
    ConfigUnavailableError,
    HardwareCapabilities,
)
from core.constants import (
    ABSOLUTE_MIN_OBSTACLE_CLEARANCE_M,
    ABSOLUTE_MAX_IMU_TILT_CUTOFF_DEG,
)


def _good_config(version: int = 1) -> Dict[str, Any]:
    return {
        "unit_id": "RV-TEST-0001",
        "name": "Test Unit",
        "config_version": version,
        "hardware": {
            "drive": {"type": "clutch", "gears": 7, "max_speed_kmh": 15.0},
            "deck": {"width_inches": 42, "engagement": "pto_lever"},
            "pico_bridge": {"port": "/dev/ttyACM0", "baud_rate": 115200},
            "power": {"type": "gas", "min_battery_v": 11.0},
            "sensors": {
                "gps": "rtk",
                "imu": True,
                "fuel": True,
                "temperature": True,
                "rpm": True,
                "obstacle": "ultrasonic",
                "operator_presence": "seat_sensor",
            },
            "cameras": {"count": 5, "config": []},
        },
        "safety_floors": {
            "min_obstacle_clearance_m": 0.20,
            "imu_tilt_cutoff_deg": 15.0,
            "watchdog_timeout_ms": 500,
            "min_battery_v_cutoff": 11.0,
            "operator_presence_required_for_auto": True,
        },
        "home_position": {"lat": 0.0, "lng": 0.0, "heading_deg": 0.0},
        "assigned_program": None,
    }


class _MockApi:
    def __init__(self, payload: Optional[Dict[str, Any]] = None) -> None:
        self.payload = payload
        self.call_count = 0

    def pull_config(self) -> Optional[Dict[str, Any]]:
        self.call_count += 1
        return self.payload


class TestConfigSync(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.cache = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(self.cache)

    def tearDown(self) -> None:
        try:
            os.remove(self.cache)
        except OSError:
            pass

    def test_pull_then_load_cache(self) -> None:
        api = _MockApi(_good_config())
        sync = ConfigSync(api, cache_path=self.cache)
        self.assertTrue(sync.pull())
        self.assertEqual(sync.get_revision(), 1)

        # New instance reads cache successfully.
        sync2 = ConfigSync(_MockApi(None), cache_path=self.cache)
        self.assertTrue(sync2.load_cache())
        self.assertEqual(sync2.get_revision(), 1)

    def test_ensure_falls_back_to_cache(self) -> None:
        # First, populate cache.
        api = _MockApi(_good_config(7))
        sync = ConfigSync(api, cache_path=self.cache)
        sync.pull()
        # Then fail to pull, ensure cache is used.
        sync2 = ConfigSync(_MockApi(None), cache_path=self.cache)
        config = sync2.ensure()
        self.assertEqual(config["config_version"], 7)

    def test_ensure_raises_when_no_cache_and_no_server(self) -> None:
        sync = ConfigSync(_MockApi(None), cache_path=self.cache)
        with self.assertRaises(ConfigUnavailableError):
            sync.ensure()

    def test_invalid_config_rejected(self) -> None:
        bad = _good_config()
        del bad["hardware"]
        api = _MockApi(bad)
        sync = ConfigSync(api, cache_path=self.cache)
        self.assertFalse(sync.pull())

    def test_clearance_below_floor_clamped(self) -> None:
        cfg = _good_config()
        cfg["safety_floors"]["min_obstacle_clearance_m"] = 0.05  # below absolute
        api = _MockApi(cfg)
        sync = ConfigSync(api, cache_path=self.cache)
        sync.pull()
        floors = sync.get_safety_floors()
        self.assertGreaterEqual(
            floors["min_obstacle_clearance_m"],
            ABSOLUTE_MIN_OBSTACLE_CLEARANCE_M,
        )

    def test_tilt_above_max_clamped(self) -> None:
        cfg = _good_config()
        cfg["safety_floors"]["imu_tilt_cutoff_deg"] = 45.0  # above max
        api = _MockApi(cfg)
        sync = ConfigSync(api, cache_path=self.cache)
        sync.pull()
        self.assertLessEqual(
            sync.get_safety_floors()["imu_tilt_cutoff_deg"],
            ABSOLUTE_MAX_IMU_TILT_CUTOFF_DEG,
        )

    def test_capabilities_extracted(self) -> None:
        api = _MockApi(_good_config())
        sync = ConfigSync(api, cache_path=self.cache)
        sync.pull()
        caps = sync.get_capabilities()
        self.assertTrue(caps.has_cameras)
        self.assertEqual(caps.camera_count, 5)
        self.assertEqual(caps.gps_grade, "rtk")
        self.assertTrue(caps.has_imu)
        self.assertTrue(caps.supports_autonomous)

    def test_capabilities_no_gps_no_autonomous(self) -> None:
        cfg = _good_config()
        cfg["hardware"]["sensors"]["gps"] = "none"
        api = _MockApi(cfg)
        sync = ConfigSync(api, cache_path=self.cache)
        sync.pull()
        caps = sync.get_capabilities()
        self.assertFalse(caps.supports_autonomous)
        self.assertFalse(caps.has_gps)

    def test_capabilities_no_imu_no_autonomous(self) -> None:
        cfg = _good_config()
        cfg["hardware"]["sensors"]["imu"] = False
        api = _MockApi(cfg)
        sync = ConfigSync(api, cache_path=self.cache)
        sync.pull()
        caps = sync.get_capabilities()
        self.assertFalse(caps.supports_autonomous)

    def test_camera_zero_count_supported(self) -> None:
        cfg = _good_config()
        cfg["hardware"]["cameras"] = {"count": 0, "config": []}
        api = _MockApi(cfg)
        sync = ConfigSync(api, cache_path=self.cache)
        sync.pull()
        caps = sync.get_capabilities()
        self.assertFalse(caps.has_cameras)
        self.assertEqual(caps.camera_count, 0)


if __name__ == "__main__":
    unittest.main()
