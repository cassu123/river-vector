# River Vector

The **River Vector** Autonomy Suite is a robust, ROS2 Humble-based navigation and control system designed for high-performance autonomous platforms.

## Key Features
- **ROS2 Humble Integration**: Built on the industry-standard robotics middleware.
- **5-Camera Vision System**: 360-degree situational awareness with front, flank, and rear coverage.
- **7-Speed Clutch Logic**: Advanced transmission control for precision power delivery.
- **RP2040 (Pico) Integration**: Dedicated low-level bridge for sensor acquisition and actuator drive.
- **Defensive Design**: Extensive error handling, watchdog monitoring, and safety interlocks.

## Repository Structure
- `core/`: Main orchestration, configuration, and constants.
- `hardware/`: Drivers for sensors, cameras, and the Pico bridge.
- `safety/`: E-Stop logic, interlocks, and fault management.
- `navigation/`: Path planning, boundary enforcement, and GPS management.
- `autonomy/`: High-level mode management and mission control.
- `telemetry/`: Logging, data collection, and remote alerts.
- `connectivity/`: Cellular, VPN, and API management.
- `pico/`: RP2040 firmware and communication protocol.
- `units/`: Unit-specific configuration profiles (e.g., Voyager).

## Getting Started
1. Install ROS2 Humble.
2. Install dependencies: `pip install -r requirements.txt`.
3. Launch the core node: `python3 -m core.main`.

## Safety Disclaimer
River Vector contains powerful autonomous control logic. Always ensure a physical E-Stop is accessible during operation.
