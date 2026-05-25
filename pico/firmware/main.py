"""
================================================================================
Project:  River Vector — Autonomous Mower Control System
File:     pico/firmware/main.py
Purpose:  Pi Pico MicroPython entry point. Initializes all I/O subsystems,
          starts the UART listener loop, and dispatches incoming commands
          from the Pi 5. Runs the watchdog timer — if the Pi 5 heartbeat
          stops, all actuators are cut immediately.
Author:   [Author Placeholder]
Version:  0.1.0
Date:     2026-05-25
NOTE:     This file runs on MicroPython on the Pi Pico. Standard CPython
          libraries are NOT available. Use only MicroPython builtins and
          the modules in this firmware directory.
================================================================================
"""

import json
import time
import sys

from machine import UART, Pin, Timer
import uasyncio as asyncio

from sensor_read import SensorReader
from actuator_drive import ActuatorDriver
from led_control import LEDController

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
UART_ID = 0
UART_TX_PIN = 0
UART_RX_PIN = 1
UART_BAUD = 115_200

HEARTBEAT_PIN = 5           # Input from Pi 5 — must pulse every 500ms
WATCHDOG_TIMEOUT_MS = 600   # 600ms — slightly longer than Pi's 500ms interval
MODE_TOGGLE_PIN = 6         # Physical AUTO/MANUAL switch

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
_last_heartbeat_ms: int = 0
_watchdog_tripped: bool = False
_uart: UART = None
_sensors: SensorReader = None
_actuators: ActuatorDriver = None
_leds: LEDController = None


# ---------------------------------------------------------------------------
# UART helpers
# ---------------------------------------------------------------------------

def uart_send(msg_type: str, payload: dict) -> None:
    """
    Sends a JSON message to the Pi 5 over UART.

    Args:
        msg_type: Message type string (matches PicoMessageType values).
        payload: Dict of message parameters.
    """
    wire = {"t": msg_type, "p": payload}
    line = json.dumps(wire) + "\n"
    _uart.write(line.encode("utf-8"))


def uart_send_ack(cmd: str, ok: bool) -> None:
    """Sends a STATUS_ACK response for a received command."""
    uart_send("STATUS_ACK", {"cmd": cmd, "ok": ok})


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------

def _heartbeat_isr(pin) -> None:
    """
    Interrupt handler for the Pi 5 heartbeat signal.
    Resets the watchdog timer on each rising edge.
    """
    global _last_heartbeat_ms
    _last_heartbeat_ms = time.ticks_ms()


def _check_watchdog() -> None:
    """
    Called periodically to check if the Pi 5 heartbeat has timed out.
    If timed out, triggers an emergency stop.
    """
    global _watchdog_tripped
    if _watchdog_tripped:
        return
    elapsed = time.ticks_diff(time.ticks_ms(), _last_heartbeat_ms)
    if elapsed > WATCHDOG_TIMEOUT_MS:
        _watchdog_tripped = True
        _emergency_stop("WATCHDOG_TIMEOUT")


def _emergency_stop(reason: str) -> None:
    """
    Cuts all actuators and relays immediately.
    Called by watchdog timeout or CMD_ESTOP command.

    Args:
        reason: String description of why the stop was triggered.
    """
    if _actuators:
        _actuators.emergency_stop()
    uart_send("STATUS_FAULT", {"code": "F004", "detail": reason})
    _leds.set_pattern("ESTOP") if _leds else None


# ---------------------------------------------------------------------------
# Command dispatcher
# ---------------------------------------------------------------------------

def _dispatch_command(msg_type: str, payload: dict) -> None:
    """
    Routes an incoming command from the Pi 5 to the appropriate handler.

    Args:
        msg_type: Command type string.
        payload: Command parameters dict.
    """
    try:
        if msg_type == "HEARTBEAT":
            global _last_heartbeat_ms, _watchdog_tripped
            _last_heartbeat_ms = time.ticks_ms()
            _watchdog_tripped = False
            uart_send("HEARTBEAT", {"ts": time.ticks_ms()})

        elif msg_type == "CMD_THROTTLE":
            _actuators.set_throttle(float(payload.get("value", 0.0)))
            uart_send_ack("CMD_THROTTLE", True)

        elif msg_type == "CMD_STEERING":
            _actuators.set_steering(float(payload.get("value", 0.0)))
            uart_send_ack("CMD_STEERING", True)

        elif msg_type == "CMD_BRAKE":
            _actuators.set_brake(float(payload.get("value", 0.0)))
            uart_send_ack("CMD_BRAKE", True)

        elif msg_type == "CMD_CLUTCH":
            _actuators.set_clutch(float(payload.get("value", 0.0)))
            uart_send_ack("CMD_CLUTCH", True)

        elif msg_type == "CMD_SHIFT":
            _actuators.set_gear(int(payload.get("gear", 0)))
            uart_send_ack("CMD_SHIFT", True)

        elif msg_type == "CMD_RELAY_IGNITION":
            _actuators.set_relay_ignition(bool(payload.get("active", False)))
            uart_send_ack("CMD_RELAY_IGNITION", True)

        elif msg_type == "CMD_RELAY_STARTER":
            _actuators.set_relay_starter(bool(payload.get("active", False)))
            uart_send_ack("CMD_RELAY_STARTER", True)

        elif msg_type == "CMD_RELAY_PTO":
            _actuators.set_relay_pto(bool(payload.get("active", False)))
            uart_send_ack("CMD_RELAY_PTO", True)

        elif msg_type == "CMD_LED_PATTERN":
            _leds.set_pattern(payload.get("pattern", "OFF"))
            uart_send_ack("CMD_LED_PATTERN", True)

        elif msg_type == "CMD_LED_SOLID":
            _leds.set_solid(
                int(payload.get("r", 0)),
                int(payload.get("g", 0)),
                int(payload.get("b", 0)),
            )
            uart_send_ack("CMD_LED_SOLID", True)

        elif msg_type == "CMD_ESTOP":
            _emergency_stop("CMD_ESTOP_RECEIVED")
            uart_send_ack("CMD_ESTOP", True)

        else:
            uart_send("STATUS_FAULT", {"code": "UNKNOWN_CMD", "detail": msg_type})

    except Exception as exc:
        uart_send("STATUS_FAULT", {"code": "CMD_ERROR", "detail": str(exc)})


# ---------------------------------------------------------------------------
# Sensor broadcast loop
# ---------------------------------------------------------------------------

async def _sensor_broadcast_loop() -> None:
    """
    Periodically reads all sensors and broadcasts data to the Pi 5.
    Runs as an asyncio task.
    """
    while True:
        try:
            # Ultrasonic
            front, rear = _sensors.read_ultrasonic()
            uart_send("SENSOR_ULTRASONIC", {"front": front, "rear": rear})

            # Power
            voltage, fuel = _sensors.read_power()
            uart_send("SENSOR_POWER", {"voltage_v": voltage, "fuel_pct": fuel})

            # Thermal
            temp = _sensors.read_temperature()
            uart_send("SENSOR_THERMAL", {"temp_c": temp})

            # RPM
            rpm = _sensors.read_rpm()
            uart_send("SENSOR_RPM", {"rpm": rpm})

            # Safety switches
            seat, estop, deck = _sensors.read_switches()
            uart_send("SENSOR_SWITCHES", {
                "seat_occupied": seat,
                "estop_pressed": estop,
                "deck_raised": deck,
            })

            # Watchdog check
            _check_watchdog()

        except Exception as exc:
            uart_send("STATUS_FAULT", {"code": "SENSOR_ERROR", "detail": str(exc)})

        await asyncio.sleep_ms(100)  # 10 Hz sensor broadcast


# ---------------------------------------------------------------------------
# UART receive loop
# ---------------------------------------------------------------------------

async def _uart_receive_loop() -> None:
    """
    Reads incoming UART lines from the Pi 5 and dispatches commands.
    Runs as an asyncio task.
    """
    buf = b""
    while True:
        if _uart.any():
            chunk = _uart.read(64)
            if chunk:
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        wire = json.loads(line.decode("utf-8"))
                        _dispatch_command(wire.get("t", ""), wire.get("p", {}))
                    except Exception as exc:
                        uart_send("STATUS_FAULT", {
                            "code": "PARSE_ERROR",
                            "detail": str(exc),
                        })
        await asyncio.sleep_ms(10)


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------

def main() -> None:
    """
    Pi Pico boot sequence.
    Initializes UART, sensors, actuators, LEDs, and starts async loops.
    """
    global _uart, _sensors, _actuators, _leds, _last_heartbeat_ms

    # UART
    _uart = UART(UART_ID, baudrate=UART_BAUD, tx=Pin(UART_TX_PIN), rx=Pin(UART_RX_PIN))

    # Subsystems
    _sensors = SensorReader()
    _actuators = ActuatorDriver()
    _leds = LEDController()

    # Heartbeat interrupt
    hb_pin = Pin(HEARTBEAT_PIN, Pin.IN, Pin.PULL_DOWN)
    hb_pin.irq(trigger=Pin.IRQ_RISING, handler=_heartbeat_isr)
    _last_heartbeat_ms = time.ticks_ms()

    # Boot complete — notify Pi 5
    _leds.set_pattern("BOOT")
    uart_send("STATUS_READY", {})

    # Start async event loop
    loop = asyncio.get_event_loop()
    loop.create_task(_uart_receive_loop())
    loop.create_task(_sensor_broadcast_loop())
    loop.run_forever()


main()
