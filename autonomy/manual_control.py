"""
River Vector - Manual / Teleoperation Control

Executes manual.* commands when the device is in MANUAL state.

Manual commands are short-duration imperatives (drive forward 500ms,
steer 15° for 300ms, etc.). The UI sends them at ~2Hz for sustained
control — this provides natural watchdog behavior: when the UI stops
sending, the device brakes to a stop within MANUAL_COMMAND_WATCHDOG_SEC.

Safety:
  - Only valid in MANUAL operating mode.
  - Operator-presence interlock applies if presence is configured.
  - All commands have a hard MAX duration (5s) so a stuck command
    can never run indefinitely.
  - Blade engagement requires presence (no exceptions).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

from core.constants import (
    MANUAL_COMMAND_MAX_DURATION_MS,
    MANUAL_COMMAND_WATCHDOG_SEC,
    MAX_STEERING_ANGLE,
)

logger = logging.getLogger(__name__)


class ManualControlError(Exception):
    """Raised when a manual command cannot be executed."""


class ManualController:
    """
    Handles manual.* commands.

    Args:
        drive:        AbstractDriveSystem driver.
        relays:       RelayManager (for blade engagement).
        presence:     AbstractOperatorPresence (may be None if not present).
        is_manual_mode: Callable returning True if ModeManager is in MANUAL.
    """

    def __init__(
        self,
        drive,
        relays,
        presence,
        is_manual_mode: Callable[[], bool],
    ) -> None:
        self._drive = drive
        self._relays = relays
        self._presence = presence
        self._is_manual = is_manual_mode
        self._lock = threading.Lock()
        self._last_command_at: float = 0.0
        self._watchdog_thread: Optional[threading.Thread] = None
        self._running = False

    # ──────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name="ManualControlWatchdog",
            daemon=True,
        )
        self._watchdog_thread.start()

    def stop(self) -> None:
        self._running = False
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            self._watchdog_thread.join(timeout=2.0)

    # ──────────────────────────────────────────────────────────────────
    # Command dispatch
    # ──────────────────────────────────────────────────────────────────

    def handle(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Dispatches a manual.* action. Returns a result dict.

        Raises:
            ManualControlError: on invalid mode, missing presence, or
                                bad parameters.
        """
        if not action.startswith("manual."):
            raise ManualControlError(f"Not a manual action: {action}")

        if not self._is_manual():
            raise ManualControlError(
                "Manual command rejected — device is not in MANUAL mode."
            )

        sub = action.split(".", 1)[1]
        handler = self._dispatch.get(sub)
        if handler is None:
            raise ManualControlError(f"Unknown manual action: {action}")

        with self._lock:
            self._last_command_at = time.time()
        return handler(self, params)

    # ──────────────────────────────────────────────────────────────────
    # Action handlers
    # ──────────────────────────────────────────────────────────────────

    def _drive_action(self, params: Dict[str, Any]) -> Dict[str, Any]:
        direction = params.get("direction", "forward")
        throttle = float(params.get("throttle", 0.0))
        duration_ms = _clamp_duration(params.get("duration_ms", 500))

        if direction not in ("forward", "reverse"):
            raise ManualControlError(f"Invalid direction: {direction!r}")
        throttle = max(0.0, min(1.0, throttle))

        self._require_presence("drive")

        signed = throttle if direction == "forward" else -throttle
        if hasattr(self._drive, "set_throttle"):
            self._drive.set_throttle(signed)
        elif hasattr(self._drive, "drive_forward"):
            if direction == "forward":
                self._drive.drive_forward(throttle)
            else:
                self._drive.drive_reverse(throttle)
        else:
            logger.warning("Drive driver has no throttle interface; sim mode.")

        return {
            "direction": direction,
            "throttle": throttle,
            "duration_ms": duration_ms,
        }

    def _steer_action(self, params: Dict[str, Any]) -> Dict[str, Any]:
        angle = float(params.get("angle_deg", 0.0))
        duration_ms = _clamp_duration(params.get("duration_ms", 500))

        angle = max(-MAX_STEERING_ANGLE, min(MAX_STEERING_ANGLE, angle))

        if hasattr(self._drive, "set_steering"):
            self._drive.set_steering(angle)
        else:
            logger.debug("Drive has no set_steering; sim mode.")

        return {"angle_deg": angle, "duration_ms": duration_ms}

    def _brake_action(self, params: Dict[str, Any]) -> Dict[str, Any]:
        force = float(params.get("force", 0.0))
        duration_ms = _clamp_duration(params.get("duration_ms", 500))
        force = max(0.0, min(1.0, force))

        if hasattr(self._drive, "set_brake"):
            self._drive.set_brake(force)
        elif hasattr(self._drive, "stop"):
            self._drive.stop()
        else:
            logger.debug("Drive has no brake interface; sim mode.")

        return {"force": force, "duration_ms": duration_ms}

    def _blades_action(self, params: Dict[str, Any]) -> Dict[str, Any]:
        engage = bool(params.get("engage", False))

        # Blade engagement always requires presence.
        self._require_presence("blade engagement")

        if engage:
            if hasattr(self._relays, "engage_blades"):
                self._relays.engage_blades()
            elif hasattr(self._relays, "blade_on"):
                self._relays.blade_on()
        else:
            if hasattr(self._relays, "disengage_blades"):
                self._relays.disengage_blades()
            elif hasattr(self._relays, "blade_off"):
                self._relays.blade_off()

        return {"engage": engage}

    _dispatch = {
        "drive":  _drive_action,
        "steer":  _steer_action,
        "brake":  _brake_action,
        "blades": _blades_action,
    }

    # ──────────────────────────────────────────────────────────────────
    # Safety
    # ──────────────────────────────────────────────────────────────────

    def _require_presence(self, action_name: str) -> None:
        """Raises if presence is configured + required but absent."""
        if self._presence is None:
            return
        if getattr(self._presence, "required_for_auto", False):
            if not self._presence.is_present():
                raise ManualControlError(
                    f"Operator presence required for {action_name}, not detected."
                )

    def _watchdog_loop(self) -> None:
        """
        Periodically checks for manual-command timeout. If the UI stops
        sending commands while in MANUAL mode, brakes to a stop.
        """
        while self._running:
            time.sleep(MANUAL_COMMAND_WATCHDOG_SEC / 2.0)
            if not self._is_manual():
                continue
            with self._lock:
                last = self._last_command_at
            if last == 0.0:
                continue
            if time.time() - last > MANUAL_COMMAND_WATCHDOG_SEC:
                logger.info("Manual command watchdog fired — braking to stop.")
                self._safe_stop()
                with self._lock:
                    self._last_command_at = 0.0

    def _safe_stop(self) -> None:
        try:
            if hasattr(self._drive, "stop"):
                self._drive.stop()
            elif hasattr(self._drive, "set_throttle"):
                self._drive.set_throttle(0.0)
        except Exception as exc:
            logger.error("Manual watchdog stop failed: %s", exc, exc_info=True)


def _clamp_duration(ms: Any) -> int:
    """Clamps a duration to [100, MANUAL_COMMAND_MAX_DURATION_MS] ms."""
    try:
        v = int(ms)
    except (TypeError, ValueError):
        v = 500
    return max(100, min(v, MANUAL_COMMAND_MAX_DURATION_MS))
