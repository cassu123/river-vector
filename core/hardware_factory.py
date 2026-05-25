"""
River Vector - Hardware Factory
Reads a UnitProfile and instantiates the correct concrete hardware drivers.
The rest of the stack only ever sees the abstract interfaces.
"""

import logging
from dataclasses import dataclass

from core.unit_profile import UnitProfile
from hardware.interfaces.drive import AbstractDriveSystem
from hardware.interfaces.deck import AbstractDeckControl
from hardware.interfaces.presence import AbstractOperatorPresence

logger = logging.getLogger(__name__)


@dataclass
class HardwareSuite:
    """All hardware subsystems for one mower unit, accessed through abstract interfaces."""
    drive: AbstractDriveSystem
    presence: AbstractOperatorPresence
    # deck: AbstractDeckControl  — TODO: deck drivers in next pass


class HardwareFactory:
    """
    Constructs a HardwareSuite from a UnitProfile.

    Adding a new platform requires:
      1. A new units/*.json profile with the new drive/presence types.
      2. A new driver class if the hardware is genuinely novel.
      3. A case in the relevant _build_* method below.
    No changes to any autonomy, safety, or navigation code.
    """

    @staticmethod
    def build(profile: UnitProfile, pico_bridge=None) -> HardwareSuite:
        """
        Builds a fully-initialized HardwareSuite for the given unit profile.

        Args:
            profile:     Loaded UnitProfile for this unit.
            pico_bridge: Active PicoBridge instance (may be None in test/sim mode).

        Returns:
            HardwareSuite with concrete driver instances behind abstract interfaces.
        """
        drive    = HardwareFactory._build_drive(profile, pico_bridge)
        presence = HardwareFactory._build_presence(profile)

        logger.info(
            "HardwareFactory: built suite for %s — drive=%s, presence=%s",
            profile.unit_id,
            type(drive).__name__,
            type(presence).__name__,
        )
        return HardwareSuite(drive=drive, presence=presence)

    # ------------------------------------------------------------------
    # Drive system
    # ------------------------------------------------------------------

    @staticmethod
    def _build_drive(profile: UnitProfile, pico_bridge) -> AbstractDriveSystem:
        drive_cfg = profile.hardware.drive
        speed = drive_cfg.max_speed_kmh

        if drive_cfg.type == "clutch":
            from hardware.drivers.clutch_drive import ClutchDrive
            return ClutchDrive(pico_bridge, max_gears=drive_cfg.gears, max_speed_kmh=speed)

        if drive_cfg.type == "differential":
            from hardware.drivers.differential_drive import DifferentialDrive
            return DifferentialDrive(pico_bridge, max_speed_kmh=speed)

        if drive_cfg.type == "direct_electric":
            from hardware.drivers.direct_electric_drive import DirectElectricDrive
            return DirectElectricDrive(pico_bridge, max_speed_kmh=speed)

        if drive_cfg.type == "hydrostatic":
            from hardware.drivers.hydrostatic_drive import HydrostaticDrive
            return HydrostaticDrive(pico_bridge, max_speed_kmh=speed)

        raise ValueError(
            f"No drive driver for type '{drive_cfg.type}'. "
            f"Add a driver in hardware/drivers/ and register it in HardwareFactory._build_drive()."
        )

    # ------------------------------------------------------------------
    # Operator presence
    # ------------------------------------------------------------------

    @staticmethod
    def _build_presence(profile: UnitProfile) -> AbstractOperatorPresence:
        pres_cfg = profile.hardware.operator_presence
        required = pres_cfg.required_for_auto

        if pres_cfg.type == "seat_sensor":
            from hardware.drivers.seat_sensor import SeatSensorPresence
            return SeatSensorPresence(required_for_auto=required)

        if pres_cfg.type == "handle_grip":
            from hardware.drivers.handle_grip import HandleGripPresence
            return HandleGripPresence(required_for_auto=required)

        if pres_cfg.type == "none":
            from hardware.drivers.no_operator import NoOperatorPresence
            return NoOperatorPresence()

        raise ValueError(
            f"No presence driver for type '{pres_cfg.type}'. "
            f"Add a driver in hardware/drivers/ and register it in HardwareFactory._build_presence()."
        )
