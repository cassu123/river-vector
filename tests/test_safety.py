"""
================================================================================
Project:  River Vector — Autonomous Mower Control System
File:     tests/test_safety.py
Purpose:  Unit tests for the safety subsystem — fault manager, e-stop,
          interlocks, and watchdog behavior.
Author:   [Author Placeholder]
Version:  0.1.0
Date:     2026-05-25
================================================================================
"""

import time
import pytest

from safety.fault_manager import FaultManager, FaultSeverity, FaultRecord
from safety.estop import EStop
from core.constants import FaultCode


# ---------------------------------------------------------------------------
# FaultManager tests
# ---------------------------------------------------------------------------

class TestFaultManager:

    def setup_method(self):
        self.fm = FaultManager()

    def test_report_fault_creates_record(self):
        """Reporting a fault creates an active record."""
        record = self.fm.report_fault(FaultCode.LOW_VOLTAGE, "Battery at 11.2V")
        assert record.code == FaultCode.LOW_VOLTAGE
        assert record.active is True
        assert record.detail == "Battery at 11.2V"

    def test_report_same_fault_twice_is_idempotent(self):
        """Reporting the same fault twice does not create duplicate records."""
        self.fm.report_fault(FaultCode.LOW_VOLTAGE, "first")
        self.fm.report_fault(FaultCode.LOW_VOLTAGE, "second")
        active = [f for f in self.fm.active_faults if f.code == FaultCode.LOW_VOLTAGE]
        assert len(active) == 1

    def test_clear_fault(self):
        """Clearing a fault marks it inactive."""
        self.fm.report_fault(FaultCode.LOW_VOLTAGE, "test")
        result = self.fm.clear_fault(FaultCode.LOW_VOLTAGE)
        assert result is True
        assert not self.fm.has_active_fault(FaultCode.LOW_VOLTAGE)

    def test_clear_nonexistent_fault_returns_false(self):
        """Clearing a fault that was never reported returns False."""
        result = self.fm.clear_fault(FaultCode.CAMERA_FAILURE)
        assert result is False

    def test_fatal_fault_blocks_operation(self):
        """A FATAL fault makes is_safe_to_operate() return False."""
        self.fm.report_fault(FaultCode.ESTOP_TRIGGERED, "test", FaultSeverity.FATAL)
        assert self.fm.is_safe_to_operate() is False

    def test_warning_fault_does_not_block_operation(self):
        """A WARNING fault does not block autonomous operation."""
        self.fm.report_fault(FaultCode.LOW_FUEL, "10%", FaultSeverity.WARNING)
        assert self.fm.is_safe_to_operate() is True

    def test_critical_fault_blocks_until_acknowledged(self):
        """An unacknowledged CRITICAL fault blocks operation."""
        self.fm.report_fault(FaultCode.OBSTACLE_DETECTED, "40cm", FaultSeverity.CRITICAL)
        assert self.fm.is_safe_to_operate() is False
        self.fm.acknowledge_fault(FaultCode.OBSTACLE_DETECTED)
        assert self.fm.is_safe_to_operate() is True

    def test_fatal_fault_cannot_be_acknowledged(self):
        """FATAL faults cannot be acknowledged — require manual reset."""
        self.fm.report_fault(FaultCode.ESTOP_TRIGGERED, "test", FaultSeverity.FATAL)
        result = self.fm.acknowledge_fault(FaultCode.ESTOP_TRIGGERED)
        assert result is False
        assert self.fm.is_safe_to_operate() is False

    def test_callback_fired_on_new_fault(self):
        """Registered callbacks are invoked when a new fault is reported."""
        received = []
        self.fm.register_callback(lambda r: received.append(r.code))
        self.fm.report_fault(FaultCode.LOW_VOLTAGE, "test")
        assert FaultCode.LOW_VOLTAGE in received

    def test_highest_severity_with_multiple_faults(self):
        """highest_severity returns the most severe active fault level."""
        self.fm.report_fault(FaultCode.LOW_FUEL, "warning", FaultSeverity.WARNING)
        self.fm.report_fault(FaultCode.GPS_SIGNAL_LOST, "critical", FaultSeverity.CRITICAL)
        assert self.fm.highest_severity == FaultSeverity.CRITICAL

    def test_no_active_faults_returns_none_severity(self):
        """highest_severity returns None when no faults are active."""
        assert self.fm.highest_severity is None


# ---------------------------------------------------------------------------
# EStop tests
# ---------------------------------------------------------------------------

class TestEStop:

    def setup_method(self):
        self.fm = FaultManager()
        self.estop = EStop(self.fm)
        self.estop.arm()

    def test_initial_state_not_triggered(self):
        """E-stop is not triggered after arming."""
        assert self.estop.is_triggered is False

    def test_trigger_sets_triggered(self):
        """Triggering the e-stop sets is_triggered to True."""
        self.estop.trigger("TEST")
        assert self.estop.is_triggered is True

    def test_trigger_records_reason(self):
        """Trigger reason is stored."""
        self.estop.trigger("SEAT_VACATED")
        assert self.estop.trigger_reason == "SEAT_VACATED"

    def test_trigger_is_idempotent(self):
        """Calling trigger() twice does not change the reason."""
        self.estop.trigger("FIRST")
        self.estop.trigger("SECOND")
        assert self.estop.trigger_reason == "FIRST"

    def test_trigger_fires_callbacks(self):
        """Shutdown callbacks are invoked on trigger."""
        reasons = []
        self.estop.register_shutdown_callback(lambda r: reasons.append(r))
        self.estop.trigger("TEST_CALLBACK")
        assert "TEST_CALLBACK" in reasons

    def test_trigger_reports_fault(self):
        """Triggering e-stop reports ESTOP_TRIGGERED fault."""
        self.estop.trigger("TEST")
        assert self.fm.has_active_fault(FaultCode.ESTOP_TRIGGERED)

    def test_reset_clears_triggered(self):
        """Resetting e-stop clears the triggered state."""
        self.estop.trigger("TEST")
        result = self.estop.reset()
        assert result is True
        assert self.estop.is_triggered is False

    def test_reset_when_not_triggered_returns_true(self):
        """Resetting when not triggered is a no-op and returns True."""
        result = self.estop.reset()
        assert result is True

    def test_trigger_time_recorded(self):
        """Trigger timestamp is recorded."""
        before = time.time()
        self.estop.trigger("TEST")
        after = time.time()
        assert self.estop.trigger_time is not None
        assert before <= self.estop.trigger_time <= after
