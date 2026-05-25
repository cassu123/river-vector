"""
================================================================================
Project:  River Vector — Autonomous Mower Control System
File:     pico/firmware/sensor_read.py
Purpose:  All sensor polling on the Pi Pico. Reads ultrasonic distance,
          battery voltage, fuel level, engine temperature, RPM, and safety
          switch states. Returns raw values — unit conversion happens here.
Author:   [Author Placeholder]
Version:  0.1.0
Date:     2026-05-25
NOTE:     MicroPython only — runs on Pi Pico.
================================================================================
"""

import time
from machine import Pin, ADC, time_pulse_us


# ---------------------------------------------------------------------------
# Pin assignments — must match PicoPins in core/constants.py
# ---------------------------------------------------------------------------
_ULTRASONIC_FRONT_TRIG = 0
_ULTRASONIC_FRONT_ECHO = 1
_ULTRASONIC_REAR_TRIG = 2
_ULTRASONIC_REAR_ECHO = 3
_RPM_PIN = 4
_FUEL_ADC_PIN = 26
_VOLTAGE_ADC_PIN = 27
_TEMP_ADC_PIN = 28
_SEAT_PIN = 16
_ESTOP_PIN = 17
_DECK_LIFT_PIN = 18

# ---------------------------------------------------------------------------
# ADC calibration constants
# ---------------------------------------------------------------------------
ADC_MAX = 65535
VREF = 3.3

# Voltage divider ratio for battery sense (e.g. 10k/3.3k divider for 0–15V → 0–3.3V)
VOLTAGE_DIVIDER_RATIO = 4.545

# Fuel sender resistance range (Ω) — adjust for actual sender
FUEL_SENDER_EMPTY_OHMS = 240.0
FUEL_SENDER_FULL_OHMS = 33.0

# NTC thermistor constants (Steinhart-Hart approximation)
NTC_NOMINAL_OHMS = 10_000
NTC_NOMINAL_TEMP_C = 25.0
NTC_BETA = 3950.0
NTC_SERIES_OHMS = 10_000

# Ultrasonic timeout (μs) — 38ms = ~6.5m max range
ULTRASONIC_TIMEOUT_US = 38_000

# RPM measurement window (ms)
RPM_WINDOW_MS = 200


class SensorReader:
    """
    Reads all sensors connected to the Pi Pico.

    Each read_* method returns calibrated values in engineering units.
    Returns None for any sensor that fails to read rather than raising,
    so a single sensor fault does not crash the firmware loop.
    """

    def __init__(self) -> None:
        # Ultrasonic
        self._us_front_trig = Pin(_ULTRASONIC_FRONT_TRIG, Pin.OUT)
        self._us_front_echo = Pin(_ULTRASONIC_FRONT_ECHO, Pin.IN)
        self._us_rear_trig = Pin(_ULTRASONIC_REAR_TRIG, Pin.OUT)
        self._us_rear_echo = Pin(_ULTRASONIC_REAR_ECHO, Pin.IN)

        # ADC
        self._adc_fuel = ADC(Pin(_FUEL_ADC_PIN))
        self._adc_voltage = ADC(Pin(_VOLTAGE_ADC_PIN))
        self._adc_temp = ADC(Pin(_TEMP_ADC_PIN))

        # RPM (Hall effect — counts pulses per window)
        self._rpm_pin = Pin(_RPM_PIN, Pin.IN, Pin.PULL_UP)
        self._rpm_count = 0
        self._rpm_pin.irq(trigger=Pin.IRQ_FALLING, handler=self._rpm_isr)

        # Safety switches (active-LOW with pull-up)
        self._seat_pin = Pin(_SEAT_PIN, Pin.IN, Pin.PULL_UP)
        self._estop_pin = Pin(_ESTOP_PIN, Pin.IN, Pin.PULL_UP)
        self._deck_pin = Pin(_DECK_LIFT_PIN, Pin.IN, Pin.PULL_UP)

    # ------------------------------------------------------------------
    # Ultrasonic
    # ------------------------------------------------------------------

    def read_ultrasonic(self):
        """
        Reads both ultrasonic sensors.

        Returns:
            Tuple (front_cm, rear_cm). None for any sensor that times out.
        """
        front = self._read_single_ultrasonic(self._us_front_trig, self._us_front_echo)
        rear = self._read_single_ultrasonic(self._us_rear_trig, self._us_rear_echo)
        return front, rear

    def _read_single_ultrasonic(self, trig: Pin, echo: Pin):
        """
        Triggers one ultrasonic sensor and measures echo pulse width.

        Args:
            trig: Trigger output pin.
            echo: Echo input pin.

        Returns:
            Distance in cm, or None on timeout.
        """
        trig.low()
        time.sleep_us(2)
        trig.high()
        time.sleep_us(10)
        trig.low()
        duration = time_pulse_us(echo, 1, ULTRASONIC_TIMEOUT_US)
        if duration < 0:
            return None
        # Speed of sound: 343 m/s → 0.0343 cm/μs, round trip ÷ 2
        return (duration * 0.0343) / 2.0

    # ------------------------------------------------------------------
    # Power
    # ------------------------------------------------------------------

    def read_power(self):
        """
        Reads battery voltage and fuel level.

        Returns:
            Tuple (voltage_v: float, fuel_pct: float).
        """
        voltage = self._read_voltage()
        fuel = self._read_fuel()
        return voltage, fuel

    def _read_voltage(self):
        """Reads battery voltage via ADC voltage divider."""
        raw = self._adc_voltage.read_u16()
        v_adc = (raw / ADC_MAX) * VREF
        return round(v_adc * VOLTAGE_DIVIDER_RATIO, 2)

    def _read_fuel(self):
        """
        Reads fuel level from resistive sender via ADC.
        Returns percentage 0–100.
        """
        raw = self._adc_fuel.read_u16()
        v_adc = (raw / ADC_MAX) * VREF
        # Convert ADC voltage to sender resistance (assuming 3.3V pull-up through series R)
        if v_adc >= VREF:
            return 0.0
        series_r = 1000.0  # Series resistor value in Ω
        if v_adc <= 0:
            return 100.0
        sender_r = series_r * v_adc / (VREF - v_adc)
        pct = (FUEL_SENDER_EMPTY_OHMS - sender_r) / (FUEL_SENDER_EMPTY_OHMS - FUEL_SENDER_FULL_OHMS) * 100.0
        return max(0.0, min(100.0, round(pct, 1)))

    # ------------------------------------------------------------------
    # Temperature
    # ------------------------------------------------------------------

    def read_temperature(self):
        """
        Reads engine temperature from NTC thermistor via ADC.

        Returns:
            Temperature in degrees Celsius.
        """
        raw = self._adc_temp.read_u16()
        v_adc = (raw / ADC_MAX) * VREF
        if v_adc <= 0 or v_adc >= VREF:
            return None
        # Steinhart-Hart NTC calculation
        import math
        r_ntc = NTC_SERIES_OHMS * v_adc / (VREF - v_adc)
        inv_t = (1.0 / (NTC_NOMINAL_TEMP_C + 273.15)) + (1.0 / NTC_BETA) * math.log(r_ntc / NTC_NOMINAL_OHMS)
        temp_k = 1.0 / inv_t
        return round(temp_k - 273.15, 1)

    # ------------------------------------------------------------------
    # RPM
    # ------------------------------------------------------------------

    def _rpm_isr(self, pin) -> None:
        """Interrupt handler — counts Hall effect pulses for RPM."""
        self._rpm_count += 1

    def read_rpm(self):
        """
        Measures engine RPM by counting Hall effect pulses over a window.

        Returns:
            Engine RPM as integer.
        """
        self._rpm_count = 0
        time.sleep_ms(RPM_WINDOW_MS)
        pulses = self._rpm_count
        # Assuming 1 pulse per revolution, scale to per-minute
        rpm = int(pulses * (60_000 / RPM_WINDOW_MS))
        return rpm

    # ------------------------------------------------------------------
    # Safety switches
    # ------------------------------------------------------------------

    def read_switches(self):
        """
        Reads all safety switch states.

        Returns:
            Tuple (seat_occupied: bool, estop_pressed: bool, deck_raised: bool).
            All switches are active-LOW (LOW = triggered).
        """
        seat_occupied = not self._seat_pin.value()    # LOW = seated
        estop_pressed = not self._estop_pin.value()   # LOW = pressed
        deck_raised = not self._deck_pin.value()      # LOW = raised
        return seat_occupied, estop_pressed, deck_raised
