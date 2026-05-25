"""
River Vector - Hydrostatic Drive
Continuous variable transmission via hydrostatic pump.
Used by: Future John Deere and commercial riding mower variants.
"""

import logging
from hardware.interfaces.drive import AbstractDriveSystem, DriveCommand
from pico.protocol import PicoMessage, PicoMessageType

logger = logging.getLogger(__name__)


class HydrostaticDrive(AbstractDriveSystem):
    """
    Hydrostatic CVT drive for commercial riding mower platforms.

    Speed is controlled by a hydraulic pump position (0–100% displacement).
    No discrete gears — throttle maps directly to pump displacement.
    Reverse is supported natively (negative displacement).

    Args:
        pico_bridge:   Active PicoBridge for hardware communication.
        max_speed_kmh: Platform top speed (forward).
    """

    def __init__(self, pico_bridge, max_speed_kmh: float = 18.0) -> None:
        self._pico = pico_bridge
        self._max_speed = max_speed_kmh
        self._stopped = True

    def apply(self, cmd: DriveCommand) -> None:
        throttle = max(0.0, min(100.0, cmd.throttle_pct))
        steer    = max(-100.0, min(100.0, cmd.steering_pct))
        brake    = max(0.0, min(100.0, cmd.brake_pct))

        if brake > 5.0:
            throttle = 0.0

        # Hydrostatic pump position maps 1:1 to throttle percentage
        self._pico.send(PicoMessage(PicoMessageType.CMD_THROTTLE, {"value": throttle}))
        self._pico.send(PicoMessage(PicoMessageType.CMD_STEERING, {"value": steer}))
        self._pico.send(PicoMessage(PicoMessageType.CMD_BRAKE,    {"value": brake}))
        self._stopped = throttle == 0.0 and brake > 5.0

    def emergency_stop(self) -> None:
        self._pico.send(PicoMessage(PicoMessageType.CMD_ESTOP, {}))
        self._stopped = True
        logger.critical("HydrostaticDrive: emergency stop executed.")

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
        return f"HydrostaticDrive(max={self._max_speed}km/h, stopped={self._stopped})"
