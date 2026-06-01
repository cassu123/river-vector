"""
River Vector - Universal Constants

ONLY universal values live here. Anything that varies per unit (camera
count, gear count, deck width, battery thresholds) is in the per-unit
configuration pulled from River Song.

What belongs here:
  - Absolute safety floors (true for any mower under any configuration)
  - Filesystem paths (same on every device)
  - Protocol versions
  - Network timing constants

What does NOT belong here:
  - Voyager-specific values (use config_sync.get_config())
  - Per-platform values (use HardwareCapabilities)
"""

from enum import Enum

# ──────────────────────────────────────────────────────────────────────────
# System Identity
# ──────────────────────────────────────────────────────────────────────────

SYSTEM_NAME = "River Vector"
VERSION = "0.2.0"
PROTOCOL_VERSION = 1
DEFAULT_NODE_NAME = "vector_core"

# ──────────────────────────────────────────────────────────────────────────
# Filesystem Paths (universal — same on every device)
# ──────────────────────────────────────────────────────────────────────────

BOOTSTRAP_PATH = "/etc/river-vector/bootstrap.json"
CONFIG_CACHE_PATH = "/var/lib/river-vector/config_cache.json"
CLAIM_CODE_PATH = "/var/lib/river-vector/claim_code.txt"
KEYSTORE_PATH = "/etc/river-vector/keystore"
LOG_DIR = "/var/log/river-vector"
LOG_PATH = f"{LOG_DIR}/river-vector.log"

# ──────────────────────────────────────────────────────────────────────────
# Network / Protocol
# ──────────────────────────────────────────────────────────────────────────

# Long-poll: server holds 30s; client adds 5s margin before considering timeout.
LONG_POLL_SERVER_HOLD_SEC = 30
LONG_POLL_CLIENT_MARGIN_SEC = 5
LONG_POLL_CLIENT_TIMEOUT_SEC = LONG_POLL_SERVER_HOLD_SEC + LONG_POLL_CLIENT_MARGIN_SEC

# Telemetry queue: in-memory ring buffer for offline replay.
TELEMETRY_QUEUE_MAX = 500
TELEMETRY_BATCH_MAX = 50

# Backoff on connection errors (exponential, capped).
BACKOFF_INITIAL_SEC = 1.0
BACKOFF_MAX_SEC = 60.0

# Standard API request timeout (non-streaming endpoints).
API_TIMEOUT_SEC = 10.0
API_RETRY_ATTEMPTS = 3
API_RETRY_BACKOFF_SEC = 1.0

RIVER_SONG_API_PREFIX = "/api/vector"

# mDNS service advertisement during the CLAIMING phase.
MDNS_SERVICE_TYPE = "_rivervector._tcp.local."
MDNS_PORT = 8765

# Local HTTP server used during claim handshake.
CLAIM_SERVER_HOST = "0.0.0.0"
CLAIM_SERVER_PORT = 8765
CLAIM_CODE_LENGTH = 6

# NTP sync requirement on boot.
NTP_SYNC_TIMEOUT_SEC = 30
NTP_DRIFT_FAULT_SEC = 5.0

# ──────────────────────────────────────────────────────────────────────────
# Absolute Safety Floors (universal — server CANNOT override these)
# ──────────────────────────────────────────────────────────────────────────

ABSOLUTE_MIN_OBSTACLE_CLEARANCE_M = 0.10
ABSOLUTE_MIN_IMU_TILT_CUTOFF_DEG = 10.0
ABSOLUTE_MAX_IMU_TILT_CUTOFF_DEG = 25.0
ABSOLUTE_MIN_WATCHDOG_TIMEOUT_MS = 250
ABSOLUTE_MAX_WATCHDOG_TIMEOUT_MS = 2000
ABSOLUTE_MIN_SLOPE_PCT = 5.0
ABSOLUTE_MAX_SLOPE_PCT = 60.0

# Default safety floor values applied when no per-unit config is present.
DEFAULT_OBSTACLE_CLEARANCE_M = 0.20
DEFAULT_IMU_TILT_CUTOFF_DEG = 15.0
DEFAULT_WATCHDOG_TIMEOUT_MS = 500
DEFAULT_MAX_SLOPE_PCT = 30.0

# Slope (terrain) monitoring + enforcement.
SLOPE_BUFFER_SIZE = 5             # rolling GPS-fix buffer for slope calc
SLOPE_MIN_HORIZONTAL_M = 0.1      # ignore sub-decimetre moves (stationary noise)
SLOPE_SEVERE_FACTOR = 1.5         # severe (e-stop) threshold = max * 1.5
SLOPE_HYSTERESIS_FACTOR = 0.85    # must drop below max * 0.85 before re-trigger

# Manual control: each manual.* command has a max effective duration so
# the device fails safe when UI heartbeats stop.
MANUAL_COMMAND_MAX_DURATION_MS = 5000
MANUAL_COMMAND_WATCHDOG_SEC = 1.0

# Teach mode: capture rate and buffer cap.
TEACH_CAPTURE_HZ = 1.0
TEACH_WAYPOINT_BUFFER_MAX = 5000

# ──────────────────────────────────────────────────────────────────────────
# Fault Codes (universal across all platforms)
# ──────────────────────────────────────────────────────────────────────────


class FaultCode(str, Enum):
    """System-wide fault identifiers shared across all River Vector units."""

    NONE = "NONE"

    # Power / thermal
    LOW_VOLTAGE = "LOW_VOLTAGE"
    OVER_TEMP = "OVER_TEMP"
    LOW_FUEL = "LOW_FUEL"

    # Sensors
    CAMERA_FAILURE = "CAMERA_FAILURE"
    GPS_SIGNAL_LOST = "GPS_SIGNAL_LOST"
    GPS_ACCURACY_LOW = "GPS_ACCURACY_LOW"
    OBSTACLE_DETECTED = "OBSTACLE_DETECTED"
    TILT_EXCEEDED = "TILT_EXCEEDED"
    SLOPE_EXCEEDED = "SLOPE_EXCEEDED"
    OPERATOR_ABSENT = "OPERATOR_ABSENT"
    BOUNDARY_BREACH = "BOUNDARY_BREACH"

    # Control / actuation
    ESTOP_TRIGGERED = "ESTOP_TRIGGERED"
    PICO_TIMEOUT = "PICO_TIMEOUT"
    SHIFT_FAILURE = "SHIFT_FAILURE"

    # System / connectivity
    CLOCK_NOT_SYNCED = "CLOCK_NOT_SYNCED"
    CLOCK_DRIFT = "CLOCK_DRIFT"
    CONFIG_INVALID = "CONFIG_INVALID"
    AUTH_FAILURE = "AUTH_FAILURE"
    SERVER_UNREACHABLE = "SERVER_UNREACHABLE"


# ──────────────────────────────────────────────────────────────────────────
# Connectivity tiers (reported with telemetry)
# ──────────────────────────────────────────────────────────────────────────


class ConnectivityTier(str, Enum):
    """How the device is currently reaching River Song."""

    INTERNET = "internet"
    LAN = "lan"
    OFFLINE = "offline"
    MESHTASTIC_ONLY = "meshtastic_only"


# ──────────────────────────────────────────────────────────────────────────
# Legacy constants
#
# These are values that previously lived here but now belong in per-unit
# configuration pulled from River Song. They remain accessible to keep
# existing callers building during the migration. Each will be removed as
# its consumers are refactored to read from config_sync.get_config().
#
# DO NOT add new references to these. New code reads per-unit values from
# config_sync.get_config()["hardware"] or HardwareCapabilities.
# ──────────────────────────────────────────────────────────────────────────

# Per-unit safety thresholds — moved to safety_floors / hardware.power.
MIN_VOLTAGE_V = 11.0
CRITICAL_TEMP_C = 95.0
MIN_FUEL_PERCENT = 10.0
OBSTACLE_STOP_DISTANCE_CM = 40.0

# Per-unit drive parameters — moved to hardware.drive.
MAX_THROTTLE = 1.0
MIN_THROTTLE = 0.0
MAX_STEERING_ANGLE = 45.0
GEAR_RATIOS = [0, 1, 2, 3, 4, 5, 6, 7]
MAX_GEARS = 7
GEAR_NEUTRAL = 0
GEAR_MIN = 1
GEAR_MAX = 7
CLUTCH_ENGAGE_DELAY_SEC = 0.30
SHIFT_SETTLE_DELAY_SEC = 0.15
CLUTCH_RELEASE_DELAY_SEC = 0.25

# Per-unit camera config — moved to hardware.cameras.
CAMERA_COUNT = 5
CAMERA_NAMES = ["front", "front_left", "front_right", "rear_left", "rear_right"]
CAMERA_TOPICS = [f"/river/camera/{name}/image_raw" for name in CAMERA_NAMES]
CAMERA_RESOLUTION: tuple = (640, 480)

# Per-unit GPS config — moved to hardware.sensors.gps.
RTK_ACCURACY_THRESHOLD_M = 0.02

# Per-unit navigation params — moved to hardware.navigation.
BOUNDARY_MARGIN = 0.5
ARUCO_HOME_MARKER_ID = 0

# ROS topic strings — kept for ROS2 callers (will go when ROS2 layer is removed).
ESTOP_TOPIC = "/river/safety/estop"
GPS_TOPIC = "/river/nav/gps"
IMU_TOPIC = "/river/nav/imu"

# General system tuning.
HEARTBEAT_TIMEOUT = 0.5
MAX_FAULT_COUNT = 3
RIVER_SONG_BASE_URL = "https://riversongai.com"
