"""
River Vector - Config Sync

Pulls the full operational configuration bundle from River Song and
caches it to disk. The rest of the system reads from this cache so that
a network outage during a session does not disrupt operation.

Cache file: /var/lib/river-vector/config_cache.json

Schema (from spec §6.4):
  {
    "unit_id": "...",
    "name": "...",
    "config_version": <int>,
    "hardware": { ... HardwareConfig ... },
    "safety_floors": { ... },
    "home_position": { lat, lng, heading_deg },
    "assigned_program": { ... } | null,
    "absolute_floors": { ... }
  }

The device's safety code defensively enforces absolute floors on every
read — config_sync just stores what the server provided.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional

from core.constants import (
    ABSOLUTE_MAX_IMU_TILT_CUTOFF_DEG,
    ABSOLUTE_MAX_WATCHDOG_TIMEOUT_MS,
    ABSOLUTE_MIN_IMU_TILT_CUTOFF_DEG,
    ABSOLUTE_MIN_OBSTACLE_CLEARANCE_M,
    ABSOLUTE_MIN_WATCHDOG_TIMEOUT_MS,
    CONFIG_CACHE_PATH,
    DEFAULT_IMU_TILT_CUTOFF_DEG,
    DEFAULT_OBSTACLE_CLEARANCE_M,
    DEFAULT_WATCHDOG_TIMEOUT_MS,
)

logger = logging.getLogger(__name__)


class ConfigUnavailableError(Exception):
    """Raised when no config is available — neither server nor cache."""


class ConfigInvalidError(Exception):
    """Raised when a config (cached or fetched) fails schema validation."""


# ──────────────────────────────────────────────────────────────────────────
# Hardware capability summary (derived from config)
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class HardwareCapabilities:
    """
    Universal capability descriptor derived from per-unit hardware config.

    Autonomy code gates on these flags rather than peeking into the
    hardware blob directly. A unit with cameras=0 simply has
    has_cameras=False — no platform-specific branches required.
    """

    has_cameras: bool
    camera_count: int
    has_gps: bool
    gps_grade: str          # 'none' | 'standard' | 'rtk'
    has_imu: bool
    has_obstacle_sensors: bool
    obstacle_sensor_type: str  # 'none' | 'ultrasonic' | 'lidar' | 'camera_based'
    has_fuel_sensor: bool
    has_temperature_sensor: bool
    has_rpm_sensor: bool
    has_operator_presence: bool
    presence_type: str         # 'none' | 'seat_sensor' | 'handle_grip'
    drive_type: str            # 'clutch' | 'differential' | 'direct_electric' | 'hydrostatic'
    deck_engagement: str       # 'pto_lever' | 'electric_pto' | 'belt'
    power_type: str            # 'gas' | 'electric'

    @property
    def supports_autonomous(self) -> bool:
        """RTK-grade GPS is the minimum for safe autonomous stripe mowing."""
        return self.gps_grade == "rtk" and self.has_imu

    @classmethod
    def from_hardware(cls, hw: Dict[str, Any]) -> "HardwareCapabilities":
        sensors = hw.get("sensors", {}) or {}
        cameras = hw.get("cameras", {}) or {}
        drive = hw.get("drive", {}) or {}
        deck = hw.get("deck", {}) or {}
        power = hw.get("power", {}) or {}
        gps_val = sensors.get("gps", "none") or "none"
        obstacle_val = sensors.get("obstacle", "none") or "none"
        presence_val = sensors.get("operator_presence", "none") or "none"
        cam_count = int(cameras.get("count", 0) or 0)
        return cls(
            has_cameras=cam_count > 0,
            camera_count=cam_count,
            has_gps=gps_val != "none",
            gps_grade=gps_val,
            has_imu=bool(sensors.get("imu", False)),
            has_obstacle_sensors=obstacle_val != "none",
            obstacle_sensor_type=obstacle_val,
            has_fuel_sensor=bool(sensors.get("fuel", False)),
            has_temperature_sensor=bool(sensors.get("temperature", False)),
            has_rpm_sensor=bool(sensors.get("rpm", False)),
            has_operator_presence=presence_val != "none",
            presence_type=presence_val,
            drive_type=drive.get("type", "clutch"),
            deck_engagement=deck.get("engagement", "pto_lever"),
            power_type=power.get("type", "gas"),
        )


# ──────────────────────────────────────────────────────────────────────────
# ConfigSync
# ──────────────────────────────────────────────────────────────────────────


class ConfigSync:
    """
    Owns the device's operational config. Pulls from server, caches to disk.

    Args:
        api_client:  RiverSongClient instance.
        cache_path:  Override for the cache file location.
    """

    def __init__(self, api_client, cache_path: str = CONFIG_CACHE_PATH) -> None:
        self._api = api_client
        self._cache_path = cache_path
        self._lock = threading.Lock()
        self._config: Optional[Dict[str, Any]] = None

    # ──────────────────────────────────────────────────────────────────
    # Pull / load
    # ──────────────────────────────────────────────────────────────────

    def pull(self) -> bool:
        """
        Pulls config from server, validates, writes cache.

        Returns True on success. On failure, leaves any existing cached
        config in place untouched.
        """
        fresh = self._api.pull_config()
        if fresh is None:
            logger.warning("Config pull returned no data.")
            return False
        try:
            self._validate(fresh)
        except ConfigInvalidError as exc:
            logger.error("Pulled config failed validation: %s", exc)
            return False

        sanitized = self._enforce_absolute_floors(fresh)
        with self._lock:
            self._config = sanitized
        self._write_cache(sanitized)
        logger.info(
            "Config pulled and cached. version=%s",
            sanitized.get("config_version"),
        )
        return True

    def load_cache(self) -> bool:
        """
        Loads config from the local cache file, if any.

        Returns True if a valid cached config was loaded.
        """
        if not os.path.exists(self._cache_path):
            return False
        try:
            with open(self._cache_path, "r") as f:
                data = json.load(f)
            self._validate(data)
        except (OSError, json.JSONDecodeError, ConfigInvalidError) as exc:
            logger.error("Config cache invalid: %s", exc)
            return False
        sanitized = self._enforce_absolute_floors(data)
        with self._lock:
            self._config = sanitized
        logger.info(
            "Loaded cached config. version=%s",
            sanitized.get("config_version"),
        )
        return True

    def ensure(self) -> Dict[str, Any]:
        """
        Returns a config dict, attempting fresh pull then cache fallback.

        Raises ConfigUnavailableError if neither is available.
        """
        if self.pull():
            return self.get_config()
        if self.load_cache():
            return self.get_config()
        raise ConfigUnavailableError(
            "Config unavailable: neither server nor cache could provide one."
        )

    # ──────────────────────────────────────────────────────────────────
    # Accessors
    # ──────────────────────────────────────────────────────────────────

    def get_config(self) -> Dict[str, Any]:
        """Returns the current config dict, or raises if not loaded."""
        with self._lock:
            if self._config is None:
                raise ConfigUnavailableError("Config not loaded.")
            return self._config

    def get_revision(self) -> int:
        """Returns the current config_version, or 0 if not loaded."""
        with self._lock:
            if self._config is None:
                return 0
            return int(self._config.get("config_version", 0) or 0)

    def get_hardware(self) -> Dict[str, Any]:
        """Returns the hardware sub-dict."""
        return self.get_config().get("hardware", {})

    def get_safety_floors(self) -> Dict[str, Any]:
        """Returns the safety_floors sub-dict, with absolute floors enforced."""
        return self.get_config().get("safety_floors", {})

    def get_assigned_program(self) -> Optional[Dict[str, Any]]:
        """Returns the assigned program dict, or None if no program assigned."""
        return self.get_config().get("assigned_program")

    def get_capabilities(self) -> HardwareCapabilities:
        """Returns the derived HardwareCapabilities object."""
        return HardwareCapabilities.from_hardware(self.get_hardware())

    def get_home_position(self) -> Dict[str, Any]:
        """Returns {lat, lng, heading_deg}."""
        return self.get_config().get(
            "home_position",
            {"lat": 0.0, "lng": 0.0, "heading_deg": 0.0},
        )

    # ──────────────────────────────────────────────────────────────────
    # Validation + enforcement
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _validate(config: Dict[str, Any]) -> None:
        """Lightweight schema validation. Raises ConfigInvalidError on failure."""
        required_top = {"unit_id", "config_version", "hardware", "safety_floors"}
        missing = required_top - set(config.keys())
        if missing:
            raise ConfigInvalidError(f"Missing required top-level fields: {missing}")

        if not isinstance(config["config_version"], int):
            raise ConfigInvalidError(
                f"config_version must be int, got {type(config['config_version']).__name__}"
            )

        hw = config["hardware"]
        if not isinstance(hw, dict):
            raise ConfigInvalidError("hardware must be a dict")
        for sub in ("drive", "deck", "power"):
            if sub not in hw or not isinstance(hw[sub], dict):
                raise ConfigInvalidError(f"hardware.{sub} missing or invalid")

    @staticmethod
    def _enforce_absolute_floors(config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Defensive enforcement of absolute floors on the safety_floors block.

        If the server pushed a value outside the absolute bounds, we clamp
        to the nearest valid bound. This guarantees that even a buggy or
        malicious server cannot make the device unsafe.
        """
        floors = dict(config.get("safety_floors", {}))

        clearance = float(floors.get(
            "min_obstacle_clearance_m", DEFAULT_OBSTACLE_CLEARANCE_M
        ))
        if clearance < ABSOLUTE_MIN_OBSTACLE_CLEARANCE_M:
            logger.warning(
                "Server pushed clearance=%.2f below absolute min %.2f; clamping.",
                clearance, ABSOLUTE_MIN_OBSTACLE_CLEARANCE_M,
            )
            clearance = ABSOLUTE_MIN_OBSTACLE_CLEARANCE_M
        floors["min_obstacle_clearance_m"] = clearance

        tilt = float(floors.get("imu_tilt_cutoff_deg", DEFAULT_IMU_TILT_CUTOFF_DEG))
        if tilt < ABSOLUTE_MIN_IMU_TILT_CUTOFF_DEG:
            tilt = ABSOLUTE_MIN_IMU_TILT_CUTOFF_DEG
        elif tilt > ABSOLUTE_MAX_IMU_TILT_CUTOFF_DEG:
            tilt = ABSOLUTE_MAX_IMU_TILT_CUTOFF_DEG
        floors["imu_tilt_cutoff_deg"] = tilt

        wd = int(floors.get("watchdog_timeout_ms", DEFAULT_WATCHDOG_TIMEOUT_MS))
        if wd < ABSOLUTE_MIN_WATCHDOG_TIMEOUT_MS:
            wd = ABSOLUTE_MIN_WATCHDOG_TIMEOUT_MS
        elif wd > ABSOLUTE_MAX_WATCHDOG_TIMEOUT_MS:
            wd = ABSOLUTE_MAX_WATCHDOG_TIMEOUT_MS
        floors["watchdog_timeout_ms"] = wd

        sanitized = dict(config)
        sanitized["safety_floors"] = floors
        return sanitized

    def _write_cache(self, config: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self._cache_path), exist_ok=True)
        tmp = f"{self._cache_path}.tmp"
        with open(tmp, "w") as f:
            json.dump(config, f, indent=2)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, self._cache_path)

    def __repr__(self) -> str:
        rev = self.get_revision() if self._config else "(none)"
        return f"ConfigSync(version={rev}, cache={self._cache_path})"
