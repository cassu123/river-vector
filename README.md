# River Vector

The **River Vector** Autonomy Suite is the universal control program for any autonomous mower in the River Song fleet. One codebase, configured per-unit via the River Song web interface — no `.json` files to edit on the device.

River Vector is a sub-program of the **River Song AI Ecosystem**. It claims with River Song on first boot, pulls its operational config, registers with River Song, reports telemetry, and accepts commands.

---

## How it works

```
┌──────────────────────┐                       ┌─────────────────────┐
│  River Song          │ ◄── HTTP POST (5–30s) │ River Vector (Pi)   │
│  riversongai.com     │     telemetry         │  one universal code │
│                      │ ◄── long-poll (~100ms)│  per unit:          │
│  Setup wizard        │     commands          │   - Bootstrap +     │
│  Zone editor         │ ──► claim handshake   │     unit identity   │
│  Program builder     │     (mDNS + code)     │   - Config pulled   │
│  Live fleet view     │ ──► config bundle     │     from server     │
│  Schedules           │     (hardware,        │   - Hardware built  │
│                      │     safety floors,    │     from config     │
│                      │     zones, program)   │   - Autonomy local  │
└──────────────────────┘                       └─────────────────────┘
```

- **Hardware is declared, not assumed.** Cameras, GPS, IMU, fuel sensor — all optional. Missing hardware degrades gracefully.
- **Safety floors are enforced on-device.** River Song stores what the operator configured; the device enforces absolute minimums regardless of what the server pushes.
- **Mowing survives a server outage.** The last-pulled config is cached locally; telemetry is queued and replayed on reconnect.

For the complete protocol, data model, and per-component design, see [`docs/RIVER_VECTOR_INTEGRATION_SPEC.md`](docs/RIVER_VECTOR_INTEGRATION_SPEC.md).

---

## Provisioning a new mower

A freshly flashed Pi runs through this sequence:

1. `sudo ./scripts/install.sh install` — creates `/etc/river-vector/`, `/var/lib/river-vector/`, `/var/log/river-vector/`, the service user, and the systemd unit.
2. `sudo ./scripts/install.sh add-wifi <ssid> <password>` — adds a WiFi network (PSK encrypted at rest). Repeat for each network the mower should know (home WiFi, phone hotspot, etc.).
3. `sudo systemctl start river-vector` — service starts.
4. On boot, the device auto-generates a `unit_id`, broadcasts mDNS, and displays a 6-digit claim code.
5. Open `https://riversongai.com` → Fleet → Discovered Devices → Claim → enter the code.
6. The setup wizard prompts for: identity, drive system, deck, hardware present (cameras, GPS type, sensors), power, safety floors, and home position.
7. Once saved, the device pulls its config, builds hardware, and enters `IDLE`.

All settings are editable post-setup from the unit detail page in River Song.

---

## Operating states

| State | Meaning |
|---|---|
| `UNCLAIMED` | First boot — has identity, no River Song association. |
| `CLAIMING` | mDNS broadcasting, awaiting claim code verification. |
| `SETUP_PENDING` | Claimed but no operational config yet. |
| `IDLE` | Configured. Ready to accept commands. |
| `MANUAL` | Operator-driven or teleoperated. |
| `AUTO` | Autonomous mowing session in progress. |
| `RETURNING_HOME` | Autonomous return-to-home navigation. |
| `ESTOP` | Emergency stop active. All motion halted. |
| `FAULT` | Critical fault preventing operation. |
| `OFFLINE_REPLAY` | Server unreachable; running cached config. |
| `TEACH` | Boundary teach mode active. |

---

## Repository structure

```
core/
  bootstrap.py      Device-local bootstrap (/etc/river-vector/bootstrap.json)
  identity.py       unit_id generation + claim state machine
  hardware_factory.py  Reads per-unit config, builds the right drivers
  main.py           Entry point, boot sequence, run loop
  constants.py      ONLY universal constants (absolute floors, paths, protocol)

connectivity/
  api_client.py        River Song REST client (token-authenticated)
  config_sync.py       Pulls + caches operational config
  command_stream.py    Long-poll command receiver (sub-100ms latency)
  telemetry_thread.py  State-cadenced telemetry pusher with offline replay
  connectivity_probe.py Active server URL + tier reporting
  wifi_manager.py      Pre-agreed SSID list, joins highest-priority available
  mdns_advertise.py    Broadcasts presence during CLAIMING
  claim_server.py      HTTP endpoint for the claim handshake

safety/                E-stop, fault manager, interlocks (read per-unit floors), watchdog
navigation/            Path planner, boundary, GPS manager, docking
autonomy/
  mode_manager.py      Full 12-state operating mode machine
  mow_session.py       One mowing session lifecycle
  return_home.py       Autonomous return-to-home
  manual_control.py    Manual teleop with watchdog
  teach_mode.py        GPS waypoint capture for boundary definition
hardware/              Drivers (drive systems, sensors, cameras, Pico bridge, ...)
telemetry/             Collector, alerts
docs/
  RIVER_VECTOR_INTEGRATION_SPEC.md  Full system specification (READ THIS FIRST)
scripts/
  install.sh           Provisioning, WiFi setup, reset
units/
  example.json         Bootstrap template
tests/                 unittest + pytest
```

---

## Bootstrap file format

A device's `/etc/river-vector/bootstrap.json` contains *only* what the device cannot derive itself:

```json
{
  "protocol_version": 1,
  "unit_id": "RV-A1B2C3D4-9F2E",
  "claim_state": "CLAIMED",
  "unit_token": "<issued at claim>",
  "firmware_version": "0.2.0",
  "server": {
    "url_primary":  "https://riversongai.com",
    "url_fallback": "http://192.168.1.221:8000"
  },
  "wifi_networks": [
    {"ssid": "<home>",       "psk_encrypted": "...", "priority": 1},
    {"ssid": "<phone-hotspot>", "psk_encrypted": "...", "priority": 2}
  ]
}
```

Everything else — hardware specs, safety floors, zones, programs — lives in River Song and is pulled via `GET /api/vector/config/{unit_id}`.

---

## Connectivity hierarchy

1. **Primary** — `url_primary` (internet → Cloudflare Tunnel → River Song).
2. **Fallback** — `url_fallback` (LAN direct, used when internet is down but WiFi LAN is up).
3. **Offline** — Last cached config used; telemetry queued.
4. **Meshtastic** (optional) — LoRa beacon for GPS broadcast + remote kill only.

The active tier is reported on every telemetry push (`connectivity_tier` field).

---

## Running tests

```
pip install -r requirements.txt
python3 -m pytest tests/
```

All hardware falls back to sim mode on dev machines, so the suite runs without a Pi.

---

## Integration with River Song

The River Song side of this integration is implemented in the [RiverSongAI](https://github.com/cassu123/RiverSongAI) repository per the same spec at `docs/RIVER_VECTOR_INTEGRATION_SPEC.md`. The device-facing API surface (`/api/vector/*`) and the operator-facing fleet UI (`/fleet`, `/fleet/zones`, `/fleet/programs`, etc.) are documented there.
