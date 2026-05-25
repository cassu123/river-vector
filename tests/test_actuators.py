"""
================================================================================
Project:  River Vector — Autonomous Mower Control System
File:     tests/test_actuators.py
Purpose:  Unit tests for the actuator manager — input validation, clamping,
          safety interlocks (brake/throttle conflict), and emergency stop.
          Uses a mock PicoBridge to avoid hardware dependency.
Author:   [Author Placeholder]
Version:  0.1.0
Date:     2026-05-25
================================================================================
"""

import pytest
from unittest.mock import MagicMock, patch

from hardware.actuators import ActuatorManager, ActuatorError
from pico.protocol import PicoMessage, PicoMessageType


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_pico():
    """Returns a mock PicoBridge that always succeeds on send()."""
    pico = MagicMock()
    pico.send.return_value = True
    return pico


@pytest.fixture
def actuators(mock_pico):
    """Returns an enabled ActuatorManager with a mock Pico bridge."""
    mgr = ActuatorManager(pico_bridge=mock_pico)
    mgr.enable()
    return mgr


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

class TestActuatorManagerInit:

    def test_requires_pico_bridge(self):
        """ActuatorManager raises ValueError if pico_bridge is None."""
        with pytest.raises(ValueError, match="pico_bridge"):
            ActuatorManager(pico_bridge=None)

    def test_disabled_by_default(self, mock_pico):
        """ActuatorManager is disabled until enable() is called."""
        mgr = ActuatorManager(pico_bridge=mock_pico)
        assert mgr._enabled is False

    def test_enable_sets_enabled(self, mock_pico):
        """enable() sets the enabled flag."""
        mgr = ActuatorManager(pico_bridge=mock_pico)
        mgr.enable()
        assert mgr._enabled is True


# ---------------------------------------------------------------------------
# Steering
# ---------------------------------------------------------------------------

class TestSteering:

    def test_set_steering_center(self, actuators, mock_pico):
        """Setting steering to 0.0 sends the correct command."""
        actuators.set_steering(0.0)
        assert actuators.state.steering_pct == 0.0
        mock_pico.send.assert_called()

    def test_set_steering_full_left(self, actuators):
        """Steering accepts -100.0 (full left)."""
        actuators.set_steering(-100.0)
        assert actuators.state.steering_pct == -100.0

    def test_set_steering_full_right(self, actuators):
        """Steering accepts +100.0 (full right)."""
        actuators.set_steering(100.0)
        assert actuators.state.steering_pct == 100.0

    def test_set_steering_clamps_over_max(self, actuators):
        """Steering value above 100 is clamped to 100."""
        actuators.set_steering(150.0)
        assert actuators.state.steering_pct == 100.0

    def test_set_steering_clamps_under_min(self, actuators):
        """Steering value below -100 is clamped to -100."""
        actuators.set_steering(-150.0)
        assert actuators.state.steering_pct == -100.0

    def test_set_steering_requires_enabled(self, mock_pico):
        """Steering raises ActuatorError when manager is disabled."""
        mgr = ActuatorManager(pico_bridge=mock_pico)
        with pytest.raises(ActuatorError):
            mgr.set_steering(0.0)


# ---------------------------------------------------------------------------
# Throttle
# ---------------------------------------------------------------------------

class TestThrottle:

    def test_set_throttle_valid(self, actuators):
        """Throttle accepts values 0–100."""
        actuators.set_throttle(50.0)
        assert actuators.state.throttle_pct == 50.0

    def test_set_throttle_clamps_over_max(self, actuators):
        """Throttle above 100 is clamped."""
        actuators.set_throttle(120.0)
        assert actuators.state.throttle_pct == 100.0

    def test_set_throttle_blocked_when_braking(self, actuators):
        """Throttle raises ActuatorError when brake is applied."""
        actuators.set_brake(50.0)
        with pytest.raises(ActuatorError, match="brake"):
            actuators.set_throttle(30.0)

    def test_set_throttle_requires_enabled(self, mock_pico):
        """Throttle raises ActuatorError when manager is disabled."""
        mgr = ActuatorManager(pico_bridge=mock_pico)
        with pytest.raises(ActuatorError):
            mgr.set_throttle(50.0)


# ---------------------------------------------------------------------------
# Brake
# ---------------------------------------------------------------------------

class TestBrake:

    def test_set_brake_cuts_throttle(self, actuators):
        """Applying brake cuts throttle to zero."""
        actuators.set_throttle(50.0)
        actuators.set_brake(100.0)
        assert actuators.state.throttle_pct == 0.0
        assert actuators.state.brake_pct == 100.0

    def test_set_brake_sets_braking_flag(self, actuators):
        """Brake > 5% sets is_braking flag."""
        actuators.set_brake(10.0)
        assert actuators.state.is_braking is True

    def test_release_brake_clears_braking_flag(self, actuators):
        """Brake <= 5% clears is_braking flag."""
        actuators.set_brake(10.0)
        actuators.set_brake(0.0)
        assert actuators.state.is_braking is False


# ---------------------------------------------------------------------------
# Emergency stop
# ---------------------------------------------------------------------------

class TestEmergencyStop:

    def test_emergency_stop_cuts_throttle(self, actuators):
        """Emergency stop sets throttle to 0."""
        actuators.set_throttle(80.0)
        actuators.emergency_stop()
        assert actuators.state.throttle_pct == 0.0

    def test_emergency_stop_applies_full_brake(self, actuators):
        """Emergency stop applies 100% brake."""
        actuators.emergency_stop()
        assert actuators.state.brake_pct == 100.0

    def test_emergency_stop_works_when_disabled(self, mock_pico):
        """Emergency stop works even when the manager is disabled."""
        mgr = ActuatorManager(pico_bridge=mock_pico)
        # Should not raise
        mgr.emergency_stop()
        assert mgr.state.throttle_pct == 0.0
        assert mgr.state.brake_pct == 100.0

    def test_disable_applies_safe_neutral(self, actuators):
        """disable() applies safe neutral state."""
        actuators.set_throttle(60.0)
        actuators.disable()
        assert actuators.state.throttle_pct == 0.0
        assert actuators.state.brake_pct == 100.0
        assert actuators._enabled is False
