"""
River Vector - Direct Electric Drive
Single-motor PWM speed control with electronic steering.
Used by: Ryobi electric push mower.
"""

import logging
from hardware.interfaces.drive import AbstractDriveSystem, DriveCommand
from pico.protocol import PicoMessage, PicoMessageType

logger = logging.getLogger(__name__)


class DirectElectricDrive(AbstractDriveSystem):
    """
    Direct electric motor drive for push mower platforms.

    A single brushless motor drives the rear wheels. Steering is electronic
    (assisted front wheels or separate steering servo). There are no gears —
    speed is purely PWM duty cycle on the drive motor.

    Args:
        pico_bridge:   Active PicoBridge for hardware communication.
        max_speed_kmh: Platform top speed.
    """

    def __init__(self, pico_bridge, max_speed_kmh: float = 6.0) -> None:
        self._pico = pico_bridge
        self._max_speed = max_speed_kmh
        self._stopped = True

    def apply(self, cmd: DriveCommand) -> None:
        throttle = max(0.0, min(100.0, cmd.throttle_pct))
        steer    = max(-100.0, min(100.0, cmd.steering_pct))
        brake    = max(0.0, min(100.0, cmd.brake_pct))

        if brake > 5.0:
            throttle = 0.0

        self._pico.send(PicoMessage(PicoMessageType.CMD_THROTTLE, {"value": throttle}))
        self._pico.send(PicoMessage(PicoMessageType.CMD_STEERING, {"value": steer}))
        self._pico.send(PicoMessage(PicoMessageType.CMD_BRAKE,    {"value": brake}))
        self._stopped = throttle == 0.0 and brake > 5.0

    def emergency_stop(self) -> None:
        self._pico.send(PicoMessage(PicoMessageType.CMD_ESTOP, {}))
        self._stopped = True
        logger.critical("DirectElectricDrive: emergency stop executed.")

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
        return f"DirectElectricDrive(max={self._max_speed}km/h, stopped={self._stopped})"
