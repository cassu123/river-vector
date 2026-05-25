"""
================================================================================
Project:  River Vector — Autonomous Mower Control System
File:     pico/protocol.py
Purpose:  Defines the UART message protocol between Raspberry Pi 5 and
          Pi Pico. All messages are newline-terminated JSON with a type
          field and a payload dict. Both sides use this module as the
          single source of truth for message format.
Author:   [Author Placeholder]
Version:  0.1.0
Date:     2026-05-25
================================================================================

Message format (JSON, newline-terminated):
    {"t": "<message_type>", "p": {<payload>}}

Example — Pi sends throttle command:
    {"t": "CMD_THROTTLE", "p": {"value": 45.0}}

Example — Pico sends sensor data:
    {"t": "SENSOR_POWER", "p": {"voltage_v": 12.6, "fuel_pct": 72.0}}
"""

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Maximum message size in bytes — reject anything larger to prevent buffer overflow
MAX_MESSAGE_BYTES: int = 512


class PicoMessageType(str, Enum):
    """
    All valid message type identifiers for the Pi ↔ Pico UART protocol.

    CMD_* messages are sent Pi → Pico (commands).
    SENSOR_* and STATUS_* messages are sent Pico → Pi (telemetry/status).
    HEARTBEAT is bidirectional.
    """

    # ── Commands (Pi → Pico) ──────────────────────────────────────────
    CMD_THROTTLE = "CMD_THROTTLE"           # {"value": 0.0–100.0}
    CMD_STEERING = "CMD_STEERING"           # {"value": -100.0–100.0}
    CMD_BRAKE = "CMD_BRAKE"                 # {"value": 0.0–100.0}
    CMD_CLUTCH = "CMD_CLUTCH"               # {"value": 0.0–100.0}
    CMD_SHIFT = "CMD_SHIFT"                 # {"gear": 0–7}
    CMD_RELAY_IGNITION = "CMD_RELAY_IGNITION"   # {"active": bool}
    CMD_RELAY_STARTER = "CMD_RELAY_STARTER"     # {"active": bool}
    CMD_RELAY_PTO = "CMD_RELAY_PTO"             # {"active": bool}
    CMD_LED_PATTERN = "CMD_LED_PATTERN"     # {"pattern": str, "brightness": float}
    CMD_LED_SOLID = "CMD_LED_SOLID"         # {"r": int, "g": int, "b": int, "brightness": float}
    CMD_ESTOP = "CMD_ESTOP"                 # {} — immediate hardware stop

    # ── Sensor data (Pico → Pi) ───────────────────────────────────────
    SENSOR_ULTRASONIC = "SENSOR_ULTRASONIC"     # {"front": float, "rear": float} (cm)
    SENSOR_POWER = "SENSOR_POWER"               # {"voltage_v": float, "fuel_pct": float}
    SENSOR_THERMAL = "SENSOR_THERMAL"           # {"temp_c": float}
    SENSOR_RPM = "SENSOR_RPM"                   # {"rpm": int}
    SENSOR_IMU = "SENSOR_IMU"                   # {"pitch": float, "roll": float, "heading": float}
    SENSOR_SWITCHES = "SENSOR_SWITCHES"         # {"seat_occupied": bool, "estop_pressed": bool, "deck_raised": bool}

    # ── Status (Pico → Pi) ────────────────────────────────────────────
    STATUS_ACK = "STATUS_ACK"               # {"cmd": str, "ok": bool}
    STATUS_FAULT = "STATUS_FAULT"           # {"code": str, "detail": str}
    STATUS_READY = "STATUS_READY"           # {} — Pico boot complete

    # ── Heartbeat (bidirectional) ─────────────────────────────────────
    HEARTBEAT = "HEARTBEAT"                 # {"ts": float}


@dataclass
class PicoMessage:
    """
    A single protocol message exchanged between Pi 5 and Pi Pico.

    Args:
        msg_type: Message type from PicoMessageType enum.
        payload: Dict of message-specific parameters.
    """
    msg_type: PicoMessageType
    payload: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.msg_type, PicoMessageType):
            raise ValueError(
                f"msg_type must be a PicoMessageType, got {type(self.msg_type).__name__}."
            )
        if not isinstance(self.payload, dict):
            raise ValueError(
                f"payload must be a dict, got {type(self.payload).__name__}."
            )


def encode_message(message: PicoMessage) -> bytes:
    """
    Serializes a PicoMessage to a UTF-8 JSON byte string.

    The wire format is compact JSON with keys "t" (type) and "p" (payload).
    No newline is appended — the caller adds the terminator.

    Args:
        message: PicoMessage to encode.

    Returns:
        UTF-8 encoded JSON bytes.

    Raises:
        ValueError: If the encoded message exceeds MAX_MESSAGE_BYTES.
    """
    wire = {"t": message.msg_type.value, "p": message.payload}
    encoded = json.dumps(wire, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ValueError(
            f"Encoded message size {len(encoded)} bytes exceeds "
            f"MAX_MESSAGE_BYTES ({MAX_MESSAGE_BYTES})."
        )
    return encoded


def decode_message(raw: bytes) -> Optional[PicoMessage]:
    """
    Deserializes a raw UART byte string into a PicoMessage.

    Returns None (with a warning log) if the message is malformed,
    has an unknown type, or exceeds the size limit.

    Args:
        raw: Raw bytes received from UART (without newline terminator).

    Returns:
        PicoMessage on success, None on any parse failure.
    """
    if not raw:
        return None

    if len(raw) > MAX_MESSAGE_BYTES:
        logger.warning(
            "Received oversized message (%d bytes) — discarding.", len(raw)
        )
        return None

    try:
        wire = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("Failed to decode UART message: %s | raw=%r", exc, raw)
        return None

    if "t" not in wire:
        logger.warning("Message missing 't' (type) field: %r", wire)
        return None

    try:
        msg_type = PicoMessageType(wire["t"])
    except ValueError:
        logger.warning("Unknown message type: %r", wire["t"])
        return None

    payload = wire.get("p", {})
    if not isinstance(payload, dict):
        logger.warning("Message payload is not a dict: %r", payload)
        return None

    return PicoMessage(msg_type=msg_type, payload=payload)
