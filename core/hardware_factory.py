"""
River Vector - Hardware Factory

Reads the per-unit hardware config (pulled from River Song via
config_sync) and instantiates the correct concrete hardware drivers.

The rest of the stack only ever sees abstract interfaces. Missing
hardware degrades gracefully — components are substituted with sim-mode
implementations rather than crashing.

Architecture:
  HardwareConfig (dict from config_sync.get_hardware())
      ↓
  HardwareFactory.build()
      ↓
  HardwareSuite (concrete drivers behind abstract interfaces)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from connectivity.config_sync import HardwareCapabilities
from hardware.interfaces.drive import AbstractDriveSystem
from hardware.interfaces.presence import AbstractOperatorPresence

logger = logging.getLogger(__name__)


@dataclass
class HardwareSuite:
    """
    All instantiated hardware subsystems for one unit.

    Components that are absent on this unit's hardware are set to None.
    Callers must check for None before use OR rely on capabilities
    (HardwareCapabilities) to gate access.
    """

    drive: AbstractDriveSystem
    presence: AbstractOperatorPresence
    capabilities: HardwareCapabilities


class HardwareFactory:
    """
    Constructs a HardwareSuite from a hardware config dict.

    The hardware dict matches the schema in spec §5.1 — a flat-ish
    structure with sub-dicts for drive, deck, pico_bridge, power, sensors,
    cameras.
    """

    @staticmethod
    def build(
        hardware: Dict[str, Any],
        pico_bridge=None,
    ) -> HardwareSuite:
        """
        Builds a HardwareSuite from a hardware dict.

        Args:
            hardware:    The hardware config dict (config_sync.get_hardware()).
            pico_bridge: Optional PicoBridge instance.

        Returns:
            A populated HardwareSuite.
        """
        capabilities = HardwareCapabilities.from_hardware(hardware)
        drive = HardwareFactory._build_drive(hardware, pico_bridge)
        presence = HardwareFactory._build_presence(hardware, capabilities)

        logger.info(
            "HardwareFactory: built suite — drive=%s, presence=%s, "
            "cameras=%d, gps=%s, imu=%s",
            type(drive).__name__,
            type(presence).__name__,
            capabilities.camera_count,
            capabilities.gps_grade,
            capabilities.has_imu,
        )
        return HardwareSuite(
            drive=drive,
            presence=presence,
            capabilities=capabilities,
        )

    # ──────────────────────────────────────────────────────────────────
    # Drive
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _build_drive(
        hardware: Dict[str, Any], pico_bridge
    ) -> AbstractDriveSystem:
        drive_cfg = hardware.get("drive", {}) or {}
        drive_type = drive_cfg.get("type", "clutch")
        max_speed = float(drive_cfg.get("max_speed_kmh", 10.0))
        gears = int(drive_cfg.get("gears", 1))

        if drive_type == "clutch":
            from hardware.drivers.clutch_drive import ClutchDrive
            return ClutchDrive(
                pico_bridge,
                max_gears=gears,
                max_speed_kmh=max_speed,
            )

        if drive_type == "differential":
            from hardware.drivers.differential_drive import DifferentialDrive
            return DifferentialDrive(pico_bridge, max_speed_kmh=max_speed)

        if drive_type == "direct_electric":
            from hardware.drivers.direct_electric_drive import DirectElectricDrive
            return DirectElectricDrive(pico_bridge, max_speed_kmh=max_speed)

        if drive_type == "hydrostatic":
            from hardware.drivers.hydrostatic_drive import HydrostaticDrive
            return HydrostaticDrive(pico_bridge, max_speed_kmh=max_speed)

        raise ValueError(
            f"No drive driver for type '{drive_type}'. "
            "Add a driver in hardware/drivers/ and register it here."
        )

    # ──────────────────────────────────────────────────────────────────
    # Operator presence
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _build_presence(
        hardware: Dict[str, Any],
        capabilities: HardwareCapabilities,
    ) -> AbstractOperatorPresence:
        presence_type = capabilities.presence_type

        # Honor the safety_floor flag in addition to the hardware
        # declaration — the operator can require presence on a unit that
        # has the sensor.
        required = capabilities.has_operator_presence

        if presence_type == "seat_sensor":
            from hardware.drivers.seat_sensor import SeatSensorPresence
            return SeatSensorPresence(required_for_auto=required)

        if presence_type == "handle_grip":
            from hardware.drivers.handle_grip import HandleGripPresence
            return HandleGripPresence(required_for_auto=required)

        # presence_type == "none" — robot/autonomous units have no operator.
        from hardware.drivers.no_operator import NoOperatorPresence
        return NoOperatorPresence()
