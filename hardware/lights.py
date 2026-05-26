"""
River Vector - Lighting Control
RGB LED status strips (front/rear), amber SAE J845 beacon, and audible buzzer.
All output goes through Pico CMD_LED_PATTERN / CMD_LED_SOLID / CMD_RELAY messages.
"""

import logging

from pico.protocol import PicoMessage, PicoMessageType

logger = logging.getLogger(__name__)

# RGB tuples for each operating mode
_COLOR_AUTO       = (0, 255, 0)      # Green — autonomous mowing
_COLOR_MANUAL     = (0, 0, 255)      # Blue — manual operator control
_COLOR_OBSTACLE   = (255, 165, 0)    # Amber — obstacle detected, paused
_COLOR_FAULT      = (255, 0, 0)      # Red — fault state
_COLOR_ESTOP      = (255, 0, 0)      # Red flash — emergency stop
_COLOR_RETURNING  = (0, 255, 255)    # Cyan — returning to home
_COLOR_DOCKING    = (255, 255, 0)    # Yellow — precision docking
_COLOR_IDLE       = (64, 64, 64)     # Dim white — idle/standby
_COLOR_OFF        = (0, 0, 0)

_BRIGHTNESS_FULL  = 1.0
_BRIGHTNESS_DIM   = 0.4


class LightManager:
    """
    Controls the RGB LED strips and amber beacon on the mower.

    LED strips provide at-a-glance status indication. The amber beacon
    is activated during all autonomous operation per SAE J845. All commands
    are forwarded to the Pico via CMD_LED_PATTERN and CMD_LED_SOLID messages.

    Args:
        pico_bridge: PicoBridge for hardware communication.
    """

    def __init__(self, pico_bridge) -> None:
        if pico_bridge is None:
            raise ValueError("pico_bridge must not be None.")
        self._pico = pico_bridge
        self._current_pattern = "idle"

    # ------------------------------------------------------------------
    # Operating mode indicators
    # ------------------------------------------------------------------

    def indicate_auto(self) -> None:
        """Solid green — autonomous mowing active. Beacon ON."""
        self._set_solid(_COLOR_AUTO)
        self._set_beacon(True)
        self._current_pattern = "auto"
        logger.debug("Lights: AUTO (green, beacon on).")

    def indicate_mowing(self) -> None:
        """Alias for indicate_auto() — same visual state."""
        self.indicate_auto()

    def indicate_manual(self) -> None:
        """Solid blue — manual operator control. Beacon OFF."""
        self._set_solid(_COLOR_MANUAL)
        self._set_beacon(False)
        self._current_pattern = "manual"
        logger.debug("Lights: MANUAL (blue, beacon off).")

    def indicate_obstacle(self) -> None:
        """Flashing amber — obstacle detected, session paused. Beacon ON."""
        self._set_pattern("obstacle_flash", _COLOR_OBSTACLE)
        self._set_beacon(True)
        self._current_pattern = "obstacle"
        logger.debug("Lights: OBSTACLE (amber flash, beacon on).")

    def indicate_fault(self) -> None:
        """Flashing red — non-fatal fault, autonomous blocked."""
        self._set_pattern("fault_flash", _COLOR_FAULT)
        self._set_beacon(False)
        self._current_pattern = "fault"
        logger.debug("Lights: FAULT (red flash).")

    def indicate_estop(self) -> None:
        """Rapid red flash — emergency stop active."""
        self._set_pattern("estop_flash", _COLOR_ESTOP)
        self._set_beacon(False)
        self._current_pattern = "estop"
        logger.debug("Lights: ESTOP (rapid red flash).")

    def indicate_returning_home(self) -> None:
        """Cycling cyan — returning to home dock. Beacon ON."""
        self._set_pattern("return_cycle", _COLOR_RETURNING)
        self._set_beacon(True)
        self._current_pattern = "returning"
        logger.debug("Lights: RETURNING HOME (cyan cycle, beacon on).")

    def indicate_docking(self) -> None:
        """Slow yellow pulse — precision ArUco docking in progress."""
        self._set_pattern("dock_pulse", _COLOR_DOCKING)
        self._set_beacon(True)
        self._current_pattern = "docking"
        logger.debug("Lights: DOCKING (yellow pulse, beacon on).")

    def indicate_idle(self) -> None:
        """Dim white — powered but idle. Beacon OFF."""
        self._set_solid(_COLOR_IDLE, brightness=_BRIGHTNESS_DIM)
        self._set_beacon(False)
        self._current_pattern = "idle"
        logger.debug("Lights: IDLE (dim white).")

    def all_off(self) -> None:
        """Turns off all LEDs and beacon."""
        self._set_solid(_COLOR_OFF)
        self._set_beacon(False)
        self._current_pattern = "off"

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def current_pattern(self) -> str:
        """Name of the currently active light pattern."""
        return self._current_pattern

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _set_solid(
        self, color: tuple, brightness: float = _BRIGHTNESS_FULL
    ) -> None:
        """Sets both LED strips to a solid color."""
        r, g, b = color
        self._pico.send(PicoMessage(
            PicoMessageType.CMD_LED_SOLID,
            {"r": r, "g": g, "b": b, "brightness": brightness},
        ))

    def _set_pattern(self, pattern: str, color: tuple) -> None:
        """Sends a named LED pattern with a base color to the Pico."""
        r, g, b = color
        self._pico.send(PicoMessage(
            PicoMessageType.CMD_LED_PATTERN,
            {"pattern": pattern, "r": r, "g": g, "b": b, "brightness": _BRIGHTNESS_FULL},
        ))

    def _set_beacon(self, active: bool) -> None:
        """Turns the amber SAE J845 rotating beacon on or off."""
        # Beacon is wired through a dedicated Pico GPIO (mapped as pattern "beacon")
        self._pico.send(PicoMessage(
            PicoMessageType.CMD_LED_PATTERN,
            {"pattern": "beacon", "active": active},
        ))

    def __repr__(self) -> str:
        return f"LightManager(pattern={self._current_pattern!r})"
