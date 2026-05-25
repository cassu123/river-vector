"""
River Vector - Main Entry Point
Orchestrates the River Vector Autonomy Suite using ROS2 Humble.
"""

import sys
import rclpy
from rclpy.node import Node
from core.config import config
from core.constants import DEFAULT_NODE_NAME, CAMERA_COUNT

class RiverVectorCore(Node):
    def __init__(self):
        super().__init__(DEFAULT_NODE_NAME)
        self.get_logger().info(f"Initializing {config.get('unit_name')}...")

        # System State
        self.is_active = True
        self.camera_count = CAMERA_COUNT
        
        # 7-speed clutch logic state placeholder
        # Note: The clutch logic handles transitions across 7 forward speeds.
        self.current_gear = 0  # 0: Neutral

        self.get_logger().info(f"System configured with {self.camera_count} cameras.")
        self.get_logger().info("Clutch controller initialized for 7-speed manual logic.")

        # Timers and Subscribers
        self.create_timer(1.0, self.status_callback)

    def status_callback(self):
        """Periodic status update."""
        if self.is_active:
            self.get_logger().info("System operational. Heartbeat active.")

    def shutdown(self):
        """Safe shutdown of the core node."""
        self.get_logger().info("Shutting down River Vector Core...")
        self.is_active = False

def main(args=None):
    rclpy.init(args=args)
    
    try:
        node = RiverVectorCore()
        try:
            rclpy.spin(node)
        except KeyboardInterrupt:
            node.get_logger().info("Keyboard Interrupt detected.")
        finally:
            node.shutdown()
            node.destroy_node()
            rclpy.shutdown()
    except Exception as e:
        print(f"Fatal error during initialization: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
