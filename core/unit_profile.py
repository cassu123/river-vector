"""
River Vector - Unit Profile
Strongly-typed representation of a unit's JSON configuration.
Loaded once at startup by HardwareFactory and passed throughout the stack.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)

# Valid string values for each capability field
DRIVE_TYPES = {"clutch", "differential", "direct_electric", "hydrostatic"}
DECK_TYPES = {"pto", "electric", "belt"}
PRESENCE_TYPES = {"seat_sensor", "handle_grip", "none"}
PLATFORM_TYPES = {"riding", "robot", "push"}
POWER_TYPES = {"gas", "electric"}


# ------------------------------------------------------------------
# Sub-profile dataclasses
# ------------------------------------------------------------------

@dataclass
class DriveProfile:
    type: str               # clutch | differential | direct_electric | hydrostatic
    max_speed_kmh: float
    gears: int = 1          # 1 for non-clutch drives
    turn_radius_m: float = 0.5


@dataclass
class DeckProfile:
    width_inches: float
    type: str               # pto | electric | belt
    height_adjustable: bool = True


@dataclass
class CameraConfig:
    name: str
    id: int
    fov: int


@dataclass
class OperatorPresenceProfile:
    type: str               # seat_sensor | handle_grip | none
    required_for_auto: bool = True


@dataclass
class PicoBridgeProfile:
    port: str
    baud_rate: int


@dataclass
class PowerProfile:
    type: str               # gas | electric
    min_voltage_v: float
    nominal_voltage_v: Optional[float] = None
    battery_cells: Optional[int] = None


@dataclass
class HardwareProfile:
    drive: DriveProfile
    deck: DeckProfile
    operator_presence: OperatorPresenceProfile
    pico_bridge: PicoBridgeProfile
    power: PowerProfile
    cameras: int = 0
    camera_config: List[CameraConfig] = field(default_factory=list)


@dataclass
class NavigationProfile:
    max_speed_kmh: float
    gps_accuracy_threshold_m: float
    turn_radius_m: float = 0.5


@dataclass
class SafetyProfile:
    estop_physical: bool = True
    estop_remote: bool = True
    watchdog_timeout_ms: int = 500


# ------------------------------------------------------------------
# Top-level UnitProfile
# ------------------------------------------------------------------

@dataclass
class UnitProfile:
    """
    Complete capability description for one mower unit.

    Loaded from a units/*.json file. HardwareFactory reads this to
    instantiate the correct drivers without any platform-specific
    branching in the autonomy or safety layers.
    """
    unit_name: str
    unit_id: str
    platform: str           # riding | robot | push
    hardware: HardwareProfile
    navigation: NavigationProfile
    safety: SafetyProfile

    @classmethod
    def from_file(cls, path: str) -> "UnitProfile":
        """
        Loads and validates a UnitProfile from a JSON file.

        Args:
            path: Absolute or relative path to the unit JSON.

        Returns:
            Validated UnitProfile instance.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError: If required fields are missing or values are invalid.
        """
        if not os.path.exists(path):
            raise FileNotFoundError(f"Unit profile not found: {path}")

        with open(path, "r") as f:
            data = json.load(f)

        return cls._from_dict(data, path)

    @classmethod
    def _from_dict(cls, d: dict, source: str = "") -> "UnitProfile":
        try:
            hw = d["hardware"]
            drv = hw["drive"]
            deck = hw["deck"]
            pres = hw["operator_presence"]
            pico = hw["pico_bridge"]
            pwr = hw["power"]
            nav = d["navigation"]
            saf = d.get("safety", {})

            drive = DriveProfile(
                type=drv["type"],
                max_speed_kmh=drv.get("max_speed_kmh", nav.get("max_speed_kmh", 10.0)),
                gears=drv.get("gears", 1),
                turn_radius_m=drv.get("turn_radius_m", nav.get("turn_radius_m", 0.5)),
            )
            if drive.type not in DRIVE_TYPES:
                raise ValueError(f"Unknown drive type '{drive.type}'. Valid: {DRIVE_TYPES}")

            deck_p = DeckProfile(
                width_inches=deck["width_inches"],
                type=deck["type"],
                height_adjustable=deck.get("height_adjustable", True),
            )
            if deck_p.type not in DECK_TYPES:
                raise ValueError(f"Unknown deck type '{deck_p.type}'. Valid: {DECK_TYPES}")

            presence = OperatorPresenceProfile(
                type=pres["type"],
                required_for_auto=pres.get("required_for_auto", True),
            )
            if presence.type not in PRESENCE_TYPES:
                raise ValueError(f"Unknown presence type '{presence.type}'. Valid: {PRESENCE_TYPES}")

            pico_p = PicoBridgeProfile(port=pico["port"], baud_rate=pico["baud_rate"])

            power = PowerProfile(
                type=pwr["type"],
                min_voltage_v=pwr["min_voltage_v"],
                nominal_voltage_v=pwr.get("nominal_voltage_v"),
                battery_cells=pwr.get("battery_cells"),
            )
            if power.type not in POWER_TYPES:
                raise ValueError(f"Unknown power type '{power.type}'. Valid: {POWER_TYPES}")

            cam_configs = [
                CameraConfig(name=c["name"], id=c["id"], fov=c["fov"])
                for c in hw.get("camera_config", [])
            ]

            hardware = HardwareProfile(
                drive=drive,
                deck=deck_p,
                operator_presence=presence,
                pico_bridge=pico_p,
                power=power,
                cameras=hw.get("cameras", len(cam_configs)),
                camera_config=cam_configs,
            )

            navigation = NavigationProfile(
                max_speed_kmh=nav["max_speed_kmh"],
                gps_accuracy_threshold_m=nav["gps_accuracy_threshold_m"],
                turn_radius_m=nav.get("turn_radius_m", 0.5),
            )

            safety = SafetyProfile(
                estop_physical=saf.get("estop_physical", True),
                estop_remote=saf.get("estop_remote", True),
                watchdog_timeout_ms=saf.get("watchdog_timeout_ms", 500),
            )

            platform = d["platform"]
            if platform not in PLATFORM_TYPES:
                raise ValueError(f"Unknown platform '{platform}'. Valid: {PLATFORM_TYPES}")

            profile = cls(
                unit_name=d["unit_name"],
                unit_id=d["unit_id"],
                platform=platform,
                hardware=hardware,
                navigation=navigation,
                safety=safety,
            )
            logger.info("Loaded unit profile: %s (%s)", profile.unit_name, profile.unit_id)
            return profile

        except KeyError as exc:
            raise ValueError(f"Unit profile {source!r} missing required field: {exc}") from exc

    def __repr__(self) -> str:
        return (
            f"UnitProfile(id={self.unit_id!r}, platform={self.platform!r}, "
            f"drive={self.hardware.drive.type!r}, "
            f"deck={self.hardware.deck.width_inches}in)"
        )
