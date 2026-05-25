"""
River Vector - Core Constants
Defines system-wide constants for the River Vector autonomy suite.
"""

# System Information
SYSTEM_NAME = "River Vector"
VERSION = "1.0.0"

# ROS2 Configuration
DEFAULT_NODE_NAME = "vector_core"
NAMESPACE = "river"

# Camera Configuration
# The system utilizes 5 cameras for 360-degree coverage and depth estimation.
CAMERA_COUNT = 5
CAMERA_NAMES = ["front", "front_left", "front_right", "rear_left", "rear_right"]
CAMERA_TOPICS = [f"/river/camera/{name}/image_raw" for name in CAMERA_NAMES]

# Hardware / Actuator Constants
MAX_THROTTLE = 1.0
MIN_THROTTLE = 0.0
MAX_STEERING_ANGLE = 45.0  # Degrees

# 7-Speed Clutch Logic Constants
# These constants support the logic for a 7-speed transmission system.
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
