"""
================================================================================
Project:  River Vector — Autonomous Mower Control System
File:     pico/firmware/led_control.py
Purpose:  WS2812B LED strip control on the Pi Pico. Implements all named
          lighting patterns for each system state. Animation runs entirely
          on the Pico to avoid blocking the Pi 5.
Author:   [Author Placeholder]
Version:  0.1.0
Date:     2026-05-25
NOTE:     MicroPython only — runs on Pi Pico.
================================================================================
"""

import time
import math
from machine import Pin
from neopixel import NeoPixel


# ---------------------------------------------------------------------------
# Configuration — must match core/constants.py
# ---------------------------------------------------------------------------
LED_PIN = 19
LED_COUNT = 30
LED_BRIGHTNESS = 0.6    # Global scale 0.0–1.0

# Color constants (R, G, B)
_OFF = (0, 0, 0)
_WHITE = (255, 255, 255)
_RED = (255, 0, 0)
_GREEN = (0, 255, 0)
_BLUE = (0, 0, 255)
_YELLOW = (255, 200, 0)
_ORANGE = (255, 80, 0)
_CYAN = (0, 200, 255)
_PURPLE = (120, 0, 255)


def _scale(color: tuple, brightness: float) -> tuple:
    """
    Scales an RGB color tuple by a brightness factor.

    Args:
        color: (R, G, B) tuple.
        brightness: 0.0–1.0 scale factor.

    Returns:
        Scaled (R, G, B) tuple with integer values.
    """
    return (
        int(color[0] * brightness),
        int(color[1] * brightness),
        int(color[2] * brightness),
    )


class LEDController:
    """
    Controls the WS2812B LED strip via NeoPixel driver.

    Pattern animations are driven by calling update() in the main loop.
    Each pattern has its own animation state tracked internally.
    """

    def __init__(
        self,
        pin: int = LED_PIN,
        count: int = LED_COUNT,
        brightness: float = LED_BRIGHTNESS,
    ) -> None:
        self._np = NeoPixel(Pin(pin), count)
        self._count = count
        self._brightness = brightness
        self._pattern = "OFF"
        self._step = 0
        self._last_update_ms = 0
        self.set_pattern("OFF")

    # ------------------------------------------------------------------
    # Pattern control
    # ------------------------------------------------------------------

    def set_pattern(self, pattern: str) -> None:
        """
        Sets the active lighting pattern.

        Args:
            pattern: Pattern name string (matches LightPattern enum names).
        """
        self._pattern = pattern
        self._step = 0
        self._render()

    def set_solid(self, r: int, g: int, b: int) -> None:
        """
        Sets the entire strip to a solid color.

        Args:
            r, g, b: Color channel values 0–255.
        """
        color = _scale((r, g, b), self._brightness)
        for i in range(self._count):
            self._np[i] = color
        self._np.write()

    def update(self) -> None:
        """
        Advances animated patterns by one step.
        Call this from the main loop at ~20Hz for smooth animation.
        """
        now = time.ticks_ms()
        if time.ticks_diff(now, self._last_update_ms) < 50:
            return
        self._last_update_ms = now
        self._step += 1
        self._render()

    # ------------------------------------------------------------------
    # Internal rendering
    # ------------------------------------------------------------------

    def _render(self) -> None:
        """Renders the current pattern at the current animation step."""
        p = self._pattern

        if p == "OFF":
            self._fill(_OFF)

        elif p == "BOOT":
            # Slow blue pulse
            self._pulse(_BLUE, period=40)

        elif p == "MANUAL_IDLE":
            self._fill(_WHITE)

        elif p == "MANUAL_RUNNING":
            self._fill(_GREEN)

        elif p == "AUTO_READY":
            # Slow cyan pulse
            self._pulse(_CYAN, period=30)

        elif p == "AUTO_MOWING":
            # Rotating cyan chase
            self._chase(_CYAN, tail=5)

        elif p == "RETURNING_HOME":
            self._pulse(_PURPLE, period=25)

        elif p == "DOCKING":
            # Fast white flash
            self._flash(_WHITE, on_steps=3, off_steps=3)

        elif p == "ESTOP":
            # Fast red flash — highest urgency
            self._flash(_RED, on_steps=2, off_steps=2)

        elif p == "FAULT":
            # Alternating red/orange
            if self._step % 10 < 5:
                self._fill(_RED)
            else:
                self._fill(_ORANGE)

        elif p == "LOW_FUEL":
            self._pulse(_ORANGE, period=35)

        elif p == "LOW_BATTERY":
            self._pulse(_RED, period=35)

        elif p == "OBSTACLE":
            self._fill(_YELLOW)

        else:
            self._fill(_OFF)

        self._np.write()

    # ------------------------------------------------------------------
    # Animation primitives
    # ------------------------------------------------------------------

    def _fill(self, color: tuple) -> None:
        """Fills the entire strip with one color."""
        c = _scale(color, self._brightness)
        for i in range(self._count):
            self._np[i] = c

    def _pulse(self, color: tuple, period: int = 30) -> None:
        """
        Sinusoidal brightness pulse.

        Args:
            color: Base color.
            period: Steps per full pulse cycle.
        """
        phase = (self._step % period) / period
        brightness = (math.sin(phase * 2 * math.pi) + 1.0) / 2.0
        c = _scale(color, brightness * self._brightness)
        for i in range(self._count):
            self._np[i] = c

    def _flash(self, color: tuple, on_steps: int = 3, off_steps: int = 3) -> None:
        """
        Binary on/off flash.

        Args:
            color: Flash color.
            on_steps: Steps to stay on.
            off_steps: Steps to stay off.
        """
        cycle = on_steps + off_steps
        if self._step % cycle < on_steps:
            self._fill(color)
        else:
            self._fill(_OFF)

    def _chase(self, color: tuple, tail: int = 5) -> None:
        """
        Single-pixel chase with fading tail.

        Args:
            color: Chase color.
            tail: Number of trailing pixels.
        """
        head = self._step % self._count
        for i in range(self._count):
            dist = (head - i) % self._count
            if dist < tail:
                fade = (tail - dist) / tail
                self._np[i] = _scale(color, fade * self._brightness)
            else:
                self._np[i] = _OFF
