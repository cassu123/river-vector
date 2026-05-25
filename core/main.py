"""
River Vector - Main Entry Point
Orchestrates the River Vector Autonomy Suite using ROS2 Humble.
Platform-agnostic: the unit profile drives hardware selection at startup.
"""

import os
import sys
import rclpy
from rclpy.node import Node

from core.unit_profile import UnitProfile
from core.hardware_factory import HardwareFactory
from core.constants import DEFAULT_NODE_NAME

# Default unit profile — override with RIVER_VECTOR_UNIT env var
DEFAULT_PROFILE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "units", "voyager.json"
)


class RiverVectorCore(Node):
    def __init__(self, profile: UnitProfile, hardware_suite):
        super().__init__(DEFAULT_NODE_NAME)
        self._profile = profile
        self._hw = hardware_suite

        self.get_logger().info(
            "Initializing %s (%s) — platform=%s, drive=%s",
            profile.unit_name,
            profile.unit_id,
            profile.platform,
            profile.hardware.drive.type,
        )

        self.is_active = True
        self.create_timer(1.0, self._status_callback)
        self.get_logger().info("System operational.")

    def _status_callback(self):
        if self.is_active:
            self.get_logger().info(
                "Heartbeat — %s | drive: %s | gear: %d",
                self._profile.unit_id,
                type(self._hw.drive).__name__,
                self._hw.drive.current_gear,
            )

    def shutdown(self):
        self.get_logger().info("Shutting down %s...", self._profile.unit_name)
        self._hw.drive.emergency_stop()
        self.is_active = False


def main(args=None):
    profile_path = os.environ.get("RIVER_VECTOR_UNIT", DEFAULT_PROFILE_PATH)

    try:
        profile = UnitProfile.from_file(profile_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Fatal: could not load unit profile: {exc}", file=sys.stderr)
        sys.exit(1)

    hardware = HardwareFactory.build(profile, pico_bridge=None)

    rclpy.init(args=args)
    try:
        node = RiverVectorCore(profile, hardware)
        try:
            rclpy.spin(node)
        except KeyboardInterrupt:
            node.get_logger().info("Keyboard interrupt.")
        finally:
            node.shutdown()
            node.destroy_node()
            rclpy.shutdown()
    except Exception as exc:
        print(f"Fatal error during initialization: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
