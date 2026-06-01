"""Tests for the altitude/slope shared-contract surface:
teach-mode 3D waypoints, telemetry snapshot keys, and config clamping."""

import types
import unittest

from autonomy.teach_mode import TeachSession
from connectivity.config_sync import ConfigSync
from hardware.gps import GPSFix
from telemetry.collector import TelemetryCollector


class _FakeApi:
    def __init__(self):
        self.pushed = []
    def push_teach_waypoints(self, zone_name, waypoints, finalize):
        self.pushed.append((zone_name, list(waypoints), finalize))
        return True


class TestTeachWaypoints3D(unittest.TestCase):
    def test_waypoint_recorded_as_triplet(self):
        session = TeachSession("front_yard", _FakeApi(), gps_provider=lambda: None)
        session._add_waypoint({"lat": 45.1, "lng": -122.2, "alt": 88.5})
        self.assertEqual(session._buffer, [[45.1, -122.2, 88.5]])

    def test_waypoint_alt_none_acceptable(self):
        session = TeachSession("front_yard", _FakeApi(), gps_provider=lambda: None)
        session._add_waypoint({"lat": 45.1, "lng": -122.2, "alt": None})
        self.assertEqual(session._buffer, [[45.1, -122.2, None]])
        # Missing alt key also yields None in the third slot.
        session._add_waypoint({"lat": 1.0, "lng": 2.0})
        self.assertEqual(session._buffer[-1], [1.0, 2.0, None])


class TestTelemetrySnapshotKeys(unittest.TestCase):
    def test_snapshot_includes_altitude_and_slope(self):
        fix = GPSFix(
            has_fix=True, latitude=45.0, longitude=-122.0,
            altitude_m=123.4, altitude_accuracy_m=2.5, fix_quality=1, satellites=8,
        )
        fake_gps = types.SimpleNamespace(fix=fix)
        fake_terrain = types.SimpleNamespace(slope_pct=12.3)
        collector = TelemetryCollector(
            unit_id="RV-TEST", gps_manager=fake_gps, terrain_monitor=fake_terrain,
        )
        d = collector.collect().to_dict()
        for key in ("altitude_m", "altitude_accuracy_m", "slope_pct"):
            self.assertIn(key, d)
        self.assertEqual(d["altitude_m"], 123.4)
        self.assertEqual(d["altitude_accuracy_m"], 2.5)
        self.assertEqual(d["slope_pct"], 12.3)

    def test_snapshot_keys_present_even_when_none(self):
        collector = TelemetryCollector(unit_id="RV-TEST")  # no gps, no terrain
        d = collector.collect().to_dict()
        for key in ("altitude_m", "altitude_accuracy_m", "slope_pct"):
            self.assertIn(key, d)
            self.assertIsNone(d[key])


class TestConfigSlopeClamp(unittest.TestCase):
    def _floors(self, value):
        cfg = {
            "unit_id": "RV", "config_version": 1, "hardware": {},
            "safety_floors": {"max_slope_pct": value},
        }
        return ConfigSync._enforce_absolute_floors(cfg)["safety_floors"]

    def test_default_when_absent(self):
        cfg = {"unit_id": "RV", "config_version": 1, "hardware": {}, "safety_floors": {}}
        floors = ConfigSync._enforce_absolute_floors(cfg)["safety_floors"]
        self.assertEqual(floors["max_slope_pct"], 30.0)

    def test_clamped_low(self):
        self.assertEqual(self._floors(1.0)["max_slope_pct"], 5.0)

    def test_clamped_high(self):
        self.assertEqual(self._floors(95.0)["max_slope_pct"], 60.0)

    def test_passthrough_in_range(self):
        self.assertEqual(self._floors(42.0)["max_slope_pct"], 42.0)


if __name__ == "__main__":
    unittest.main()
