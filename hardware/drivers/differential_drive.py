"""
River Vector - Differential Drive
Left/right independent motor control for skid-steer robot platforms.
Used by: Scout robot mower.
"""

import logging
from hardware.interfaces.drive import AbstractDriveSystem, DriveCommand
from pico.protocol import PicoMessage, PicoMessageType

logger = logging.getLogger(__name__)


class DifferentialDrive(AbstractDriveSystem):
    """
    Skid-steer differential drive for robot platforms.

    Translates a unified throttle/steering command into independent
    left and right motor PWM values. Steering mixes into the motor speeds:
    turning reduces speed on the inside wheel proportionally.

    Args:
        pico_bridge:   Active PicoBridge for hardware communication.
        max_speed_kmh: Platform top speed.
    """

    def __init__(self, pico_bridge, max_speed_kmh: float = 5.0) -> None:
        self._pico = pico_bridge
        self._max_speed = max_speed_kmh
        self._stopped = True

    def apply(self, cmd: DriveCommand) -> None:
        # Mix throttle and steering into left/right motor values
        throttle = max(0.0, min(100.0, cmd.throttle_pct))
        steer = max(-100.0, min(100.0, cmd.steering_pct))

        left  = throttle * (1.0 - max(0.0,  steer / 100.0))
        right = throttle * (1.0 - max(0.0, -steer / 100.0))

        if cmd.brake_pct > 5.0:
            left = right = 0.0

        # Reuse CMD_STEERING for left motor, CMD_THROTTLE for right motor
        # (Pico firmware interprets these as differential channels when in DIFF mode)
        self._pico.send(PicoMessage(PicoMessageType.CMD_STEERING, {"value": left}))
        self._pico.send(PicoMessage(PicoMessageType.CMD_THROTTLE, {"value": right}))
        self._pico.send(PicoMessage(PicoMessageType.CMD_BRAKE,    {"value": cmd.brake_pct}))
        self._stopped = left == 0.0 and right == 0.0

    def emergency_stop(self) -> None:
        self._pico.send(PicoMessage(PicoMessageType.CMD_ESTOP, {}))
        self._stopped = True
        logger.critical("DifferentialDrive: emergency stop executed.")

    @property
    def max_speed_kmh(self) -> float:
        return self._max_speed

    @property
    def current_gear(self) -> int:
        return 0

    @property
    def is_stopped(self) -> bool:
        return self._stopped

    def __repr__(self) -> str:
        return f"DifferentialDrive(max={self._max_speed}km/h, stopped={self._stopped})"
