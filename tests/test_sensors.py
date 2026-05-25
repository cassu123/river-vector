"""
================================================================================
Project:  River Vector — Autonomous Mower Control System
File:     tests/test_sensors.py
Purpose:  Unit tests for the sensor manager — snapshot access, threshold
          callbacks, obstacle detection, and safe-to-operate logic.
          Uses a mock PicoBridge to inject sensor data without hardware.
Author:   [Author Placeholder]
Version:  0.1.0
Date:     2026-05-25
================================================================================
"""

import pytest
from unittest.mock import MagicMock, call

from hardware.sensors import SensorManager
from pico.protocol import PicoMessage, PicoMessageType
from core.constants import (
    FaultCode,
    MIN_VOLTAGE_V,
    CRITICAL_TEMP_C,
    OBSTACLE_STOP_DISTANCE_CM,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_pico():
    """Returns a mock PicoBridge."""
    pico = MagicMock()
    pico.send.return_value = True
    return pico


@pytest.fixture
def sensor_mgr(mock_pico):
    """Returns a SensorManager with a mock Pico bridge."""
    return SensorManager(pico_bridge=mock_pico)


def make_message(msg_type: PicoMessageType, payload: dict) -> PicoMessage:
    """Helper to create a PicoMessage for handler injection."""
    return PicoMessage(msg_type=msg_type, payload=payload)


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

class TestSensorManagerInit:

    def test_requires_pico_bridge(self):
        """SensorManager raises ValueError if pico_bridge is None."""
        with pytest.raises(ValueError, match="pico_bridge"):
            SensorManager(pico_bridge=None)

    def test_initial_snapshot_all_none(self, sensor_mgr):
        """Initial snapshot has all sensor values as None."""
        snap = sensor_mgr.snapshot
        assert snap.battery_voltage_v is None
        assert snap.fuel_percent is None
        assert snap.engine_temp_c is None
        assert snap.ultrasonic_front_cm is None


# ---------------------------------------------------------------------------
# Ultrasonic handler
# ---------------------------------------------------------------------------

class TestUltrasonicHandler:

    def test_ultrasonic_updates_snapshot(self, sensor_mgr):
        """Ultrasonic message updates front and rear distances."""
        msg = make_message(PicoMessageType.SENSOR_ULTRASONIC, {"front": 120.0, "rear": 200.0})
        sensor_mgr._handle_ultrasonic(msg)
        snap = sensor_mgr.snapshot
        assert snap.ultrasonic_front_cm == 120.0
        assert snap.ultrasonic_rear_cm == 200.0

    def test_obstacle_imminent_when_within_stop_distance(self, sensor_mgr):
        """is_obstacle_imminent() returns True when front sensor is within stop distance."""
        msg = make_message(
            PicoMessageType.SENSOR_ULTRASONIC,
            {"front": OBSTACLE_STOP_DISTANCE_CM - 5.0, "rear": 200.0}
        )
        sensor_mgr._handle_ultrasonic(msg)
        assert sensor_mgr.is_obstacle_imminent() is True

    def test_obstacle_not_imminent_when_clear(self, sensor_mgr):
        """is_obstacle_imminent() returns False when sensors show clear path."""
        msg = make_message(
            PicoMessageType.SENSOR_ULTRASONIC,
            {"front": 200.0, "rear": 200.0}
        )
        sensor_mgr._handle_ultrasonic(msg)
        assert sensor_mgr.is_obstacle_imminent() is False


# ---------------------------------------------------------------------------
# Power handler
# ---------------------------------------------------------------------------

class TestPowerHandler:

    def test_power_updates_voltage_and_fuel(self, sensor_mgr):
        """Power message updates voltage and fuel readings."""
        msg = make_message(PicoMessageType.SENSOR_POWER, {"voltage_v": 12.6, "fuel_pct": 75.0})
        sensor_mgr._handle_power(msg)
        snap = sensor_mgr.snapshot
        assert snap.battery_voltage_v == 12.6
        assert snap.fuel_percent == 75.0

    def test_low_voltage_fires_callback(self, sensor_mgr):
        """Low voltage triggers threshold callback with LOW_VOLTAGE fault code."""
        received = []
        sensor_mgr.register_threshold_callback(lambda code, val: received.append(code))
        msg = make_message(
            PicoMessageType.SENSOR_POWER,
            {"voltage_v": MIN_VOLTAGE_V - 0.5, "fuel_pct": 50.0}
        )
        sensor_mgr._handle_power(msg)
        assert FaultCode.LOW_VOLTAGE in received


# ---------------------------------------------------------------------------
# Thermal handler
# ---------------------------------------------------------------------------

class TestThermalHandler:

    def test_thermal_updates_temp(self, sensor_mgr):
        """Thermal message updates engine temperature."""
        msg = make_message(PicoMessageType.SENSOR_THERMAL, {"temp_c": 65.0})
        sensor_mgr._handle_thermal(msg)
        assert sensor_mgr.snapshot.engine_temp_c == 65.0

    def test_over_temp_fires_callback(self, sensor_mgr):
        """Over-temperature triggers threshold callback with OVER_TEMP fault code."""
        received = []
        sensor_mgr.register_threshold_callback(lambda code, val: received.append(code))
        msg = make_message(
            PicoMessageType.SENSOR_THERMAL,
            {"temp_c": CRITICAL_TEMP_C + 5.0}
        )
        sensor_mgr._handle_thermal(msg)
        assert FaultCode.OVER_TEMP in received


# ---------------------------------------------------------------------------
# Switch handler
# ---------------------------------------------------------------------------

class TestSwitchHandler:

    def test_switches_update_snapshot(self, sensor_mgr):
        """Switch message updates seat, estop, and deck states."""
        msg = make_message(
            PicoMessageType.SENSOR_SWITCHES,
            {"seat_occupied": True, "estop_pressed": False, "deck_raised": False}
        )
        sensor_mgr._handle_switches(msg)
        snap = sensor_mgr.snapshot
        assert snap.seat_occupied is True
        assert snap.estop_pressed is False
        assert snap.deck_raised is False


# ---------------------------------------------------------------------------
# Safe to operate
# ---------------------------------------------------------------------------

class TestSafeToOperate:

    def test_safe_when_all_nominal(self, sensor_mgr):
        """is_safe_to_operate() returns True when all values are nominal."""
        sensor_mgr._handle_power(
            make_message(PicoMessageType.SENSOR_POWER, {"voltage_v": 12.6, "fuel_pct": 80.0})
        )
        sensor_mgr._handle_thermal(
            make_message(PicoMessageType.SENSOR_THERMAL, {"temp_c": 60.0})
        )
        sensor_mgr._handle_switches(
            make_message(PicoMessageType.SENSOR_SWITCHES,
                         {"seat_occupied": True, "estop_pressed": False, "deck_raised": False})
        )
        assert sensor_mgr.is_safe_to_operate() is True

    def test_not_safe_when_estop_pressed(self, sensor_mgr):
        """is_safe_to_operate() returns False when e-stop is pressed."""
        sensor_mgr._handle_switches(
            make_message(PicoMessageType.SENSOR_SWITCHES,
                         {"seat_occupied": True, "estop_pressed": True, "deck_raised": False})
        )
        assert sensor_mgr.is_safe_to_operate() is False

    def test_not_safe_when_seat_empty(self, sensor_mgr):
        """is_safe_to_operate() returns False when seat is unoccupied."""
        sensor_mgr._handle_switches(
            make_message(PicoMessageType.SENSOR_SWITCHES,
                         {"seat_occupied": False, "estop_pressed": False, "deck_raised": False})
        )
        assert sensor_mgr.is_safe_to_operate() is False

    def test_not_safe_when_low_voltage(self, sensor_mgr):
        """is_safe_to_operate() returns False when battery voltage is below minimum."""
        sensor_mgr._handle_power(
            make_message(PicoMessageType.SENSOR_POWER,
                         {"voltage_v": MIN_VOLTAGE_V - 1.0, "fuel_pct": 50.0})
        )
        assert sensor_mgr.is_safe_to_operate() is False
