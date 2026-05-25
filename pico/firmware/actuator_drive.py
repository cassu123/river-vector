"""
================================================================================
Project:  River Vector — Autonomous Mower Control System
File:     pico/firmware/actuator_drive.py
Purpose:  PWM actuator control on the Pi Pico. Drives steering, throttle,
          brake, clutch, and gear shift linear actuators. Controls relay
          outputs for PTO, ignition, and starter. All values are validated
          before being written to hardware.
Author:   [Author Placeholder]
Version:  0.1.0
Date:     2026-05-25
NOTE:     MicroPython only — runs on Pi Pico.
================================================================================
"""

import time
from machine import Pin, PWM


# ---------------------------------------------------------------------------
# Pin assignments — must match PicoPins in core/constants.py
# ---------------------------------------------------------------------------
_STEERING_PWM_PIN = 20
_THROTTLE_PWM_PIN = 21
_BRAKE_PWM_PIN = 22
_CLUTCH_PWM_PIN = 23
_SHIFT_PWM_PIN = 24

_RELAY_PTO_PIN = 15
_RELAY_IGNITION_PIN = 14
_RELAY_STARTER_PIN = 13

# ---------------------------------------------------------------------------
# PWM configuration
# ---------------------------------------------------------------------------
PWM_FREQ_HZ = 50        # Standard servo/actuator frequency
PWM_MIN_US = 1000       # 1ms pulse = 0% / full retract
PWM_MAX_US = 2000       # 2ms pulse = 100% / full extend
PWM_NEUTRAL_US = 1500   # 1.5ms pulse = center / neutral


def _pct_to_duty(percent: float, invert: bool = False) -> int:
    """
    Converts a percentage (0–100) to a PWM duty cycle value.

    Args:
        percent: 0.0 to 100.0.
        invert: If True, 0% maps to PWM_MAX_US and 100% to PWM_MIN_US.

    Returns:
        PWM duty cycle as u16 integer.
    """
    percent = max(0.0, min(100.0, float(percent)))
    if invert:
        percent = 100.0 - percent
    pulse_us = PWM_MIN_US + (percent / 100.0) * (PWM_MAX_US - PWM_MIN_US)
    # Convert μs to u16 duty cycle for 50Hz PWM
    # Period = 1/50Hz = 20ms = 20,000μs
    duty = int((pulse_us / 20_000.0) * 65535)
    return duty


def _steering_pct_to_duty(percent: float) -> int:
    """
    Converts steering percentage (-100 to +100) to PWM duty.
    0% = center (1500μs), -100% = full left (1000μs), +100% = full right (2000μs).

    Args:
        percent: -100.0 to +100.0.

    Returns:
        PWM duty cycle as u16 integer.
    """
    percent = max(-100.0, min(100.0, float(percent)))
    normalized = (percent + 100.0) / 2.0  # Map -100..+100 → 0..100
    return _pct_to_duty(normalized)


class ActuatorDriver:
    """
    Drives all PWM actuators and relay outputs on the Pi Pico.

    Provides a safe interface that validates all inputs before writing
    to hardware. Emergency stop cuts all outputs to safe state immediately.
    """

    def __init__(self) -> None:
        # PWM actuators
        self._steering = PWM(Pin(_STEERING_PWM_PIN), freq=PWM_FREQ_HZ)
        self._throttle = PWM(Pin(_THROTTLE_PWM_PIN), freq=PWM_FREQ_HZ)
        self._brake = PWM(Pin(_BRAKE_PWM_PIN), freq=PWM_FREQ_HZ)
        self._clutch = PWM(Pin(_CLUTCH_PWM_PIN), freq=PWM_FREQ_HZ)
        self._shift = PWM(Pin(_SHIFT_PWM_PIN), freq=PWM_FREQ_HZ)

        # Relay outputs (active-HIGH)
        self._relay_pto = Pin(_RELAY_PTO_PIN, Pin.OUT, value=0)
        self._relay_ignition = Pin(_RELAY_IGNITION_PIN, Pin.OUT, value=0)
        self._relay_starter = Pin(_RELAY_STARTER_PIN, Pin.OUT, value=0)

        # Initialize to safe state
        self.emergency_stop()

    # ------------------------------------------------------------------
    # Actuators
    # ------------------------------------------------------------------

    def set_throttle(self, percent: float) -> None:
        """
        Sets throttle position.

        Args:
            percent: 0.0 (idle) to 100.0 (full throttle).
        """
        self._throttle.duty_u16(_pct_to_duty(percent))

    def set_steering(self, percent: float) -> None:
        """
        Sets steering position.

        Args:
            percent: -100.0 (full left) to +100.0 (full right).
        """
        self._steering.duty_u16(_steering_pct_to_duty(percent))

    def set_brake(self, percent: float) -> None:
        """
        Sets brake actuator position.

        Args:
            percent: 0.0 (released) to 100.0 (full brake).
        """
        self._brake.duty_u16(_pct_to_duty(percent))

    def set_clutch(self, percent: float) -> None:
        """
        Sets clutch actuator position.

        Args:
            percent: 0.0 (engaged) to 100.0 (fully disengaged).
        """
        self._clutch.duty_u16(_pct_to_duty(percent))

    def set_gear(self, gear: int) -> None:
        """
        Moves the gear shift actuator to the position for the requested gear.
        Gear 0 = neutral. Gears 1–7 are evenly distributed across actuator travel.

        Args:
            gear: 0 (neutral) to 7.
        """
        gear = max(0, min(7, int(gear)))
        if gear == 0:
            # Neutral — center position
            self._shift.duty_u16(_pct_to_duty(50.0))
        else:
            # Map gear 1–7 to 0–100% actuator travel
            pct = ((gear - 1) / 6.0) * 100.0
            self._shift.duty_u16(_pct_to_duty(pct))

    # ------------------------------------------------------------------
    # Relays
    # ------------------------------------------------------------------

    def set_relay_ignition(self, active: bool) -> None:
        """
        Controls the ignition relay.

        Args:
            active: True to energize, False to de-energize.
        """
        self._relay_ignition.value(1 if active else 0)

    def set_relay_starter(self, active: bool) -> None:
        """
        Controls the starter relay.

        Args:
            active: True to energize, False to de-energize.
        """
        self._relay_starter.value(1 if active else 0)

    def set_relay_pto(self, active: bool) -> None:
        """
        Controls the PTO relay.

        Args:
            active: True to energize, False to de-energize.
        """
        self._relay_pto.value(1 if active else 0)

    # ------------------------------------------------------------------
    # Emergency stop
    # ------------------------------------------------------------------

    def emergency_stop(self) -> None:
        """
        Immediately sets all actuators to safe state:
        - Throttle: 0%
        - Brake: 100%
        - Clutch: 100% (disengaged)
        - Steering: center
        - All relays: OFF
        """
        self._throttle.duty_u16(_pct_to_duty(0.0))
        self._brake.duty_u16(_pct_to_duty(100.0))
        self._clutch.duty_u16(_pct_to_duty(100.0))
        self._steering.duty_u16(_steering_pct_to_duty(0.0))
        self._relay_pto.value(0)
        self._relay_starter.value(0)
        self._relay_ignition.value(0)
