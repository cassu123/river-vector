"""
River Vector - Clutch Drive
7-speed (or N-speed) manual-sequential clutch transmission.
Used by: Voyager-1 riding mower, future John Deere variants.
"""

import logging
from hardware.interfaces.drive import AbstractDriveSystem, DriveCommand
from pico.protocol import PicoMessage, PicoMessageType

logger = logging.getLogger(__name__)


class ClutchDrive(AbstractDriveSystem):
    """
    Clutch-based sequential transmission drive.

    Translates DriveCommands into Pico throttle, steering, brake, clutch,
    and gear-shift messages. Gear changes are sequential only — no skipping.

    Args:
        pico_bridge: Active PicoBridge for hardware communication.
        max_gears:   Number of forward gears (default 7).
        max_speed_kmh: Platform top speed.
    """

    def __init__(self, pico_bridge, max_gears: int = 7, max_speed_kmh: float = 15.0) -> None:
        self._pico = pico_bridge
        self._max_gears = max_gears
        self._max_speed = max_speed_kmh
        self._gear = 0
        self._stopped = True

    def apply(self, cmd: DriveCommand) -> None:
        target_gear = max(0, min(self._max_gears, cmd.gear))
        if target_gear != self._gear:
            self._shift_to(target_gear)

        self._pico.send(PicoMessage(PicoMessageType.CMD_STEERING,  {"value": cmd.steering_pct}))
        self._pico.send(PicoMessage(PicoMessageType.CMD_THROTTLE,  {"value": cmd.throttle_pct}))
        self._pico.send(PicoMessage(PicoMessageType.CMD_BRAKE,     {"value": cmd.brake_pct}))
        self._stopped = cmd.throttle_pct == 0.0 and cmd.brake_pct >= 5.0

    def emergency_stop(self) -> None:
        self._pico.send(PicoMessage(PicoMessageType.CMD_ESTOP, {}))
        self._pico.send(PicoMessage(PicoMessageType.CMD_THROTTLE, {"value": 0.0}))
        self._pico.send(PicoMessage(PicoMessageType.CMD_BRAKE,    {"value": 100.0}))
        self._gear = 0
        self._stopped = True
        logger.critical("ClutchDrive: emergency stop executed.")

    def _shift_to(self, target: int) -> None:
        while self._gear != target:
            next_gear = self._gear + 1 if target > self._gear else self._gear - 1
            self._pico.send(PicoMessage(PicoMessageType.CMD_SHIFT, {"gear": next_gear}))
            self._gear = next_gear

    @property
    def max_speed_kmh(self) -> float:
        return self._max_speed

    @property
    def current_gear(self) -> int:
        return self._gear

    @property
    def is_stopped(self) -> bool:
        return self._stopped

    def __repr__(self) -> str:
        return f"ClutchDrive(gear={self._gear}/{self._max_gears}, max={self._max_speed}km/h)"
