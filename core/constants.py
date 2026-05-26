"""
River Vector - Core Constants
Defines system-wide constants for the River Vector autonomy suite.
"""

from enum import Enum, auto

# System Information
SYSTEM_NAME = "River Vector"
VERSION = "1.0.0"

# ROS2 Configuration
DEFAULT_NODE_NAME = "vector_core"
NAMESPACE = "river"

# Camera Configuration
CAMERA_COUNT = 5
CAMERA_NAMES = ["front", "front_left", "front_right", "rear_left", "rear_right"]
CAMERA_TOPICS = [f"/river/camera/{name}/image_raw" for name in CAMERA_NAMES]
CAMERA_RESOLUTION: tuple = (640, 480)  # Width x Height pixels

# Hardware / Actuator Constants
MAX_THROTTLE = 1.0
MIN_THROTTLE = 0.0
MAX_STEERING_ANGLE = 45.0  # Degrees

# 7-Speed Clutch Logic Constants
GEAR_RATIOS = [0, 1, 2, 3, 4, 5, 6, 7]  # 0 is Neutral
MAX_GEARS = 7

# Safety Constants
ESTOP_TOPIC = "/river/safety/estop"
HEARTBEAT_TIMEOUT = 0.5  # Seconds
MAX_FAULT_COUNT = 3

# Navigation Constants
GPS_TOPIC = "/river/nav/gps"
IMU_TOPIC = "/river/nav/imu"
BOUNDARY_MARGIN = 0.5  # Meters

# GPS / RTK accuracy required for autonomous operation
RTK_ACCURACY_THRESHOLD_M: float = 0.02  # 2 cm

# ArUco docking marker
ARUCO_HOME_MARKER_ID: int = 0  # ID of the home dock ArUco marker

# Sensor thresholds
MIN_VOLTAGE_V: float = 11.0          # Below this → LOW_VOLTAGE fault
CRITICAL_TEMP_C: float = 95.0        # Above this → OVER_TEMP fault
MIN_FUEL_PERCENT: float = 10.0       # Below this → LOW_FUEL fault
OBSTACLE_STOP_DISTANCE_CM: float = 40.0  # Front ultrasonic stop threshold

# River Song API
RIVER_SONG_BASE_URL: str = "https://riversongai.com"
RIVER_SONG_API_PREFIX: str = "/api/vector"
API_TIMEOUT_SEC: float = 10.0
API_RETRY_ATTEMPTS: int = 3
API_RETRY_BACKOFF_SEC: float = 1.0

# 7-speed clutch shift timing (seconds)
GEAR_NEUTRAL: int = 0
GEAR_MIN: int = 1
GEAR_MAX: int = 7
CLUTCH_ENGAGE_DELAY_SEC: float = 0.30    # Time to fully disengage clutch
SHIFT_SETTLE_DELAY_SEC: float = 0.15     # Time for shift actuator to reach position
CLUTCH_RELEASE_DELAY_SEC: float = 0.25  # Time to re-engage clutch smoothly


class FaultCode(str, Enum):
    """System-wide fault identifiers shared across all subsystems."""
    NONE             = "NONE"
    LOW_VOLTAGE      = "LOW_VOLTAGE"
    OVER_TEMP        = "OVER_TEMP"
    LOW_FUEL         = "LOW_FUEL"
    CAMERA_FAILURE   = "CAMERA_FAILURE"
    GPS_SIGNAL_LOST  = "GPS_SIGNAL_LOST"
    GPS_ACCURACY_LOW = "GPS_ACCURACY_LOW"
    OBSTACLE_DETECTED = "OBSTACLE_DETECTED"
    ESTOP_TRIGGERED  = "ESTOP_TRIGGERED"
    PICO_TIMEOUT     = "PICO_TIMEOUT"
    TILT_EXCEEDED    = "TILT_EXCEEDED"
    OPERATOR_ABSENT  = "OPERATOR_ABSENT"
    SHIFT_FAILURE    = "SHIFT_FAILURE"
    BOUNDARY_BREACH  = "BOUNDARY_BREACH"
