# River Vector

The **River Vector** Autonomy Suite is a Python-based autonomous mower control system that runs on Raspberry Pi hardware. It handles navigation, safety, telemetry, multi-unit fleet coordination, and connectivity back to River Song.

River Vector is a sub-program of the **River Song AI Ecosystem** — it registers with River Song on boot, reports telemetry, and accepts commands from it.

---

## Fleet — Current Units

| Unit | ID | Platform | Drive | Cameras | Power |
|---|---|---|---|---|---|
| Voyager-1 | VOY-RV-001 | Riding mower | 7-speed clutch | 5 | Gas |
| Scout-1 | SCT-RV-001 | Autonomous robot | Differential | 2 | Electric |
| Ryobi-Push-1 | RYO-RV-001 | Push mower | Direct electric | 1 | Electric |

Each unit loads its own JSON profile from `units/` at startup. All hardware falls back to sim mode when physical devices are unavailable, so the system runs on any machine for testing.

---

## Repository Structure

```
core/           Main orchestrator, config, constants, hardware factory
hardware/
  drivers/      Drive implementations: clutch, differential, direct electric, hydrostatic
  interfaces/   Abstract interfaces: drive, deck, operator presence
  actuators.py  Actuator control
  cameras.py    Camera management (OpenCV)
  display.py    Nextion 3.5" touchscreen operator panel (UART)
  gps.py        GPS interface
  lights.py     Status lights
  pico_bridge.py  UART link to RP2040 co-processor
  relays.py     Power relay control
  sensors.py    Sensor aggregation (voltage, temp, fuel, ultrasonic, IMU, RPM)
safety/         E-stop, fault manager, interlocks, watchdog
navigation/     Path planner, boundary enforcement, GPS manager, parking/docking
autonomy/       Mode manager, mow session, return home, shift controller
telemetry/      Collector, logger, alert monitor
connectivity/
  api_client.py   River Song REST API client (WireGuard VPN)
  cellular.py     Cellular connectivity management
  meshtastic_beacon.py  LoRa mesh backup beacon (Meshtastic)
  stream_manager.py     Video/telemetry streaming
  vpn.py          WireGuard VPN management
pico/
  firmware/     RP2040 MicroPython firmware (sensor read, actuator drive, LED)
  protocol.py   Host-side Pico message protocol
calibration/    Camera calibration suite (intrinsic + extrinsic, multi-camera stitch)
fleet/          Multi-unit coordinator, unit registry, zone partitioner
units/          Unit profile JSON files (voyager.json, scout.json, push_ryobi.json)
fleets/         Zone and boundary definitions
tests/          Unit tests
```

---

## Hardware

### Per mower unit (Pi 5 recommended)
- **Raspberry Pi 5** — main compute: navigation, cameras, autonomy
- **Raspberry Pi Pico (RP2040)** — low-level bridge: sensor read, actuator drive, LEDs (UART to Pi)
- **GPS module** — RTK-capable recommended (2 cm accuracy target for Voyager)
- **Cameras** — USB or CSI, one per configured camera slot
- **Nextion 3.5" display** — operator panel, UART via `/dev/ttyUSB0` (optional — sim mode if absent)
- **Meshtastic LoRa module** — backup beacon and kill switch, UART via `/dev/ttyUSB1` (optional)

### System deps (install on the Pi)
```bash
sudo apt install python3 python3-pip python3-venv python3-opencv libopencv-dev
sudo usermod -aG dialout $USER   # serial port access for Pico/display/Meshtastic
```

---

## Running on Raspberry Pi

### 1. Clone and install
```bash
git clone https://github.com/yourusername/river-vector.git
cd river-vector
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> **Note on OpenCV:** If `opencv-python` fails to build on your Pi, replace it with the system package instead:
> ```bash
> pip install --no-deps opencv-python  # or skip it — the apt package above covers it
> ```

### 2. Configure environment
```bash
export RIVER_SONG_API_KEY="your-api-key"          # from River Song admin panel
export RIVER_VECTOR_UNIT="units/voyager.json"     # which unit profile to load
export MESHTASTIC_PORT="/dev/ttyUSB1"             # LoRa module port (optional)
```

Create a `.env` file to persist these across reboots, or add them to the systemd service below.

### 3. Select a unit and run

List all available units:
```bash
python3 -m core.main --list
```
```
NAME                 ID             PLATFORM   DRIVE            CAMERAS
----------------------------------------------------------------------
Ryobi-Push-1         RYO-RV-001     push       direct_electric  1
Scout-1              SCT-RV-001     robot      differential     2
Voyager-1            VOY-RV-001     riding     clutch           5
```

Run a specific unit:
```bash
python3 -m core.main --unit voyager
python3 -m core.main --unit scout
python3 -m core.main --unit push_ryobi
```

You can also point directly at a profile file:
```bash
python3 -m core.main --unit units/voyager.json
python3 -m core.main --unit /path/to/my_custom_unit.json
```

The `--unit` flag overrides the `RIVER_VECTOR_UNIT` env var. If neither is set, Voyager is the default.

The system loads the unit profile, initialises all subsystems (hardware falls back to sim mode if not present), registers with River Song, and enters the main loop at 10 Hz.

### 4. Run as a systemd service (auto-start on boot)
Create `/etc/systemd/system/river-vector.service`:
```ini
[Unit]
Description=River Vector Autonomy Suite
After=network.target

[Service]
User=pi
WorkingDirectory=/home/pi/river-vector
EnvironmentFile=/home/pi/river-vector/.env
ExecStart=/home/pi/river-vector/.venv/bin/python3 -m core.main --unit voyager
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable river-vector
sudo systemctl start river-vector
sudo journalctl -u river-vector -f   # follow logs
```

---

## Unit Profiles

Select which unit to run by setting `RIVER_VECTOR_UNIT`:

```bash
RIVER_VECTOR_UNIT=units/voyager.json python3 -m core.main    # Voyager-1 (default)
RIVER_VECTOR_UNIT=units/scout.json python3 -m core.main      # Scout-1
RIVER_VECTOR_UNIT=units/push_ryobi.json python3 -m core.main # Ryobi-Push-1
```

The profile drives hardware selection at runtime — drive type, camera count, operator presence, power type, and safety timeouts are all unit-specific.

---

## Adding a New Unit

1. Copy an existing profile from `units/` and edit it:
   ```bash
   cp units/voyager.json units/my_mower.json
   ```
2. Set the fields for your hardware — `drive.type`, `platform`, camera count, power type, etc.
3. Run `python3 -m core.main --list` to confirm it appears.
4. Run it: `python3 -m core.main --unit my_mower`

**Valid values** (the loader validates these and rejects unknown ones):
- `drive.type`: `clutch`, `differential`, `direct_electric`, `hydrostatic`
- `platform`: `riding`, `robot`, `push`
- `deck.type`: `pto`, `electric`, `belt`
- `operator_presence.type`: `seat_sensor`, `handle_grip`, `none`
- `power.type`: `gas`, `electric`

If your hardware uses a genuinely different drive mechanism not in that list, add a driver class in `hardware/drivers/` and register it in `HardwareFactory._build_drive()` — no other code needs to change.

---

## Camera Calibration

Run the interactive calibration tool (display required or sim mode):

```bash
python3 -m calibration
```

Calibration data is saved to `calibration_data/`. Results persist across reboots and are loaded automatically by the camera manager.

---

## Tests

```bash
pytest tests/
```

---

## Safety

River Vector contains autonomous control logic connected to physical actuators. Before deploying on any unit:

- Ensure a physical E-stop is accessible and tested before every run.
- The watchdog timeout is 500 ms — if the main loop hangs, the system triggers an automatic E-stop.
- Remote E-stop is available via River Song command (`action: estop`) and via Meshtastic kill command over LoRa mesh.
- Never bypass interlocks for testing — use sim mode instead (`RIVER_VECTOR_UNIT` pointing to a unit profile with `required_for_auto: false`).
