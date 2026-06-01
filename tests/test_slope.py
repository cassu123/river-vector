"""Tests for slope calculation (navigation/terrain_monitor.py) and the
runtime slope safety enforcer (safety/interlocks.py)."""

import unittest

from navigation.terrain_monitor import TerrainMonitor, haversine_m
from safety.interlocks import Interlocks, SlopeAction
from telemetry.alerts import AlertLevel


class _FakeMode:
    def __init__(self, name):
        self.name = name


AUTO = _FakeMode("AUTO")
IDLE = _FakeMode("IDLE")


class _FloorsConfigSync:
    """Minimal config_sync stand-in exposing a fixed max_slope_pct."""
    def __init__(self, max_slope_pct=30.0):
        self._floors = {"max_slope_pct": max_slope_pct, "imu_tilt_cutoff_deg": 15.0}
    def get_safety_floors(self):
        return self._floors


class TestHaversine(unittest.TestCase):
    def test_known_distance(self):
        # ~111.19 m for 0.001° of latitude.
        d = haversine_m(0.0, 0.0, 0.001, 0.0)
        self.assertAlmostEqual(d, 111.19, delta=0.5)


class TestSlopeCalc(unittest.TestCase):
    def test_none_with_fewer_than_two_points(self):
        tm = TerrainMonitor()
        self.assertIsNone(tm.slope_pct)
        tm.update(45.0, -122.0, 100.0)
        self.assertIsNone(tm.slope_pct)  # only one valid-altitude point

    def test_known_sequence_slope(self):
        tm = TerrainMonitor()
        # Two points ~111.19 m apart (0.001° lat), rising 10 m → ~9.0%.
        tm.update(45.0, -122.0, 100.0)
        tm.update(45.001, -122.0, 110.0)
        self.assertIsNotNone(tm.slope_pct)
        self.assertAlmostEqual(tm.slope_pct, 9.0, delta=0.2)

    def test_reports_max_across_buffer(self):
        tm = TerrainMonitor()
        tm.update(45.0, -122.0, 100.0)
        tm.update(45.001, -122.0, 105.0)    # ~4.5%
        tm.update(45.002, -122.0, 120.0)    # ~13.5% — the max
        self.assertAlmostEqual(tm.slope_pct, 13.5, delta=0.3)

    def test_ignores_stationary_points(self):
        tm = TerrainMonitor()
        tm.update(45.0, -122.0, 100.0)
        tm.update(45.0, -122.0, 110.0)  # same spot (horizontal < 0.1 m) → ignored
        self.assertIsNone(tm.slope_pct)

    def test_altitude_none_not_buffered(self):
        tm = TerrainMonitor()
        tm.update(45.0, -122.0, None)
        tm.update(45.001, -122.0, None)
        self.assertIsNone(tm.slope_pct)

    def test_clamped_to_100(self):
        tm = TerrainMonitor()
        # 50 m rise over ~11 m horizontal → >100% raw, clamps to 100.
        tm.update(45.0, -122.0, 100.0)
        tm.update(45.0001, -122.0, 200.0)
        self.assertLessEqual(tm.slope_pct, 100.0)
        self.assertEqual(tm.slope_pct, 100.0)


class TestSlopeEnforcement(unittest.TestCase):
    def _interlocks(self, max_slope=30.0):
        return Interlocks(config_sync=_FloorsConfigSync(max_slope))

    def test_hold_at_limit_not_estop(self):
        il = self._interlocks(30.0)
        r = il.enforce_slope(31.0, AUTO)
        self.assertEqual(r.action, SlopeAction.HOLD)
        self.assertIsNotNone(r.alert)
        self.assertEqual(r.alert.level, AlertLevel.WARNING)
        self.assertEqual(r.alert.title, "Slope limit exceeded")

    def test_estop_at_severe(self):
        il = self._interlocks(30.0)
        r = il.enforce_slope(46.0, AUTO)  # 30 * 1.5 = 45 → severe
        self.assertEqual(r.action, SlopeAction.ESTOP)
        self.assertEqual(r.alert.level, AlertLevel.CRITICAL)
        self.assertEqual(r.alert.title, "Severe slope exceeded")

    def test_no_retrigger_within_hysteresis_band(self):
        il = self._interlocks(30.0)
        first = il.enforce_slope(31.0, AUTO)
        self.assertEqual(first.action, SlopeAction.HOLD)
        # Still elevated but within [0.85*max, severe): no new action.
        again = il.enforce_slope(29.0, AUTO)   # between 25.5 and 30
        self.assertEqual(again.action, SlopeAction.NONE)
        again2 = il.enforce_slope(31.0, AUTO)  # back above limit, still latched
        self.assertEqual(again2.action, SlopeAction.NONE)

    def test_retrigger_after_dropping_below_clear_band(self):
        il = self._interlocks(30.0)
        self.assertEqual(il.enforce_slope(31.0, AUTO).action, SlopeAction.HOLD)
        self.assertEqual(il.enforce_slope(20.0, AUTO).action, SlopeAction.NONE)  # < 0.85*30=25.5 → reset
        self.assertEqual(il.enforce_slope(31.0, AUTO).action, SlopeAction.HOLD)  # re-triggers

    def test_escalation_hold_to_estop(self):
        il = self._interlocks(30.0)
        self.assertEqual(il.enforce_slope(31.0, AUTO).action, SlopeAction.HOLD)
        self.assertEqual(il.enforce_slope(50.0, AUTO).action, SlopeAction.ESTOP)

    def test_hold_only_when_moving(self):
        il = self._interlocks(30.0)
        # Over the limit but idle → no HOLD (graceful stop only applies to motion).
        self.assertEqual(il.enforce_slope(31.0, IDLE).action, SlopeAction.NONE)

    def test_none_when_slope_unknown(self):
        il = self._interlocks(30.0)
        self.assertEqual(il.enforce_slope(None, AUTO).action, SlopeAction.NONE)


if __name__ == "__main__":
    unittest.main()
