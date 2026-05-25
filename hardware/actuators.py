"""
River Vector - Actuator Manager
Translates high-level motion commands into Pico bridge messages.
Enforces safety constraints (throttle/brake conflict, enable gate).
"""

import logging
from dataclasses import dataclass

from pico.protocol import PicoMessage, PicoMessageType

logger = logging.getLogger(__name__)

_BRAKE_THRESHOLD_PCT: float = 5.0  # brake_pct above this sets is_braking


class ActuatorError(Exception):
    """Raised when an actuator command is rejected due to safety or state."""


@dataclass
class ActuatorState:
    """Live snapshot of all actuator positions."""
    throttle_pct: float = 0.0   # 0–100
    steering_pct: float = 0.0   # -100 to +100
    brake_pct: float = 100.0    # 0–100 (starts applied)
    is_braking: bool = True


class ActuatorManager:
    """
    Manages throttle, steering, and brake commands for the mower platform.

    All commands are forwarded to the RP2040 Pico via the pico_bridge.
    The manager is disabled by default — call enable() before commanding.
    emergency_stop() bypasses the enable gate and always works.

    Args:
        pico_bridge: PicoBridge instance for hardware communication.
    """

    def __init__(self, pico_bridge) -> None:
        if pico_bridge is None:
            raise ValueError("pico_bridge must not be None.")
        self._pico = pico_bridge
        self._enabled = False
        self._state = ActuatorState()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def enable(self) -> None:
        """Enables the actuator manager. Releases brake to allow motion commands."""
        self._enabled = True
        self._state.brake_pct = 0.0
        self._state.is_braking = False
        self._pico.send(PicoMessage(PicoMessageType.CMD_BRAKE, {"value": 0.0}))
        logger.info("Actuator manager enabled.")

    def disable(self) -> None:
        """Disables the manager and applies safe neutral state."""
        self._apply_safe_neutral()
        self._enabled = False
        logger.info("Actuator manager disabled — safe neutral applied.")

    # ------------------------------------------------------------------
    # Motion commands
    # ------------------------------------------------------------------

    def set_steering(self, value: float) -> None:
        """
        Sets steering position.

        Args:
            value: Steering percentage, -100 (full left) to +100 (full right).

        Raises:
            ActuatorError: If the manager is disabled.
        """
        self._require_enabled()
        clamped = max(-100.0, min(100.0, value))
        self._state.steering_pct = clamped
        self._pico.send(PicoMessage(PicoMessageType.CMD_STEERING, {"value": clamped}))

    def set_throttle(self, value: float) -> None:
        """
        Sets throttle level. Rejected if brake is applied.

        Args:
            value: Throttle percentage, 0–100.

        Raises:
            ActuatorError: If the manager is disabled or brake is applied.
        """
        self._require_enabled()
        if self._state.is_braking:
            raise ActuatorError("Cannot apply throttle while brake is engaged.")
        clamped = max(0.0, min(100.0, value))
        self._state.throttle_pct = clamped
        self._pico.send(PicoMessage(PicoMessageType.CMD_THROTTLE, {"value": clamped}))

    def set_brake(self, value: float) -> None:
        """
        Sets brake pressure. Automatically cuts throttle when brake is applied.

        Args:
            value: Brake percentage, 0–100.
        """
        clamped = max(0.0, min(100.0, value))
        if clamped > _BRAKE_THRESHOLD_PCT:
            self._state.throttle_pct = 0.0
            self._state.is_braking = True
            self._pico.send(PicoMessage(PicoMessageType.CMD_THROTTLE, {"value": 0.0}))
        else:
            self._state.is_braking = False

        self._state.brake_pct = clamped
        self._pico.send(PicoMessage(PicoMessageType.CMD_BRAKE, {"value": clamped}))

    def emergency_stop(self) -> None:
        """Immediate halt — throttle to 0, full brake. Works even when disabled."""
        self._apply_safe_neutral()
        logger.critical("Actuator manager: EMERGENCY STOP applied.")

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def state(self) -> ActuatorState:
        """Current actuator state snapshot."""
        return self._state

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise ActuatorError("ActuatorManager is disabled — call enable() first.")

    def _apply_safe_neutral(self) -> None:
        """Sets throttle=0, brake=100 directly without enable check."""
        self._state.throttle_pct = 0.0
        self._state.brake_pct = 100.0
        self._state.is_braking = True
        self._pico.send(PicoMessage(PicoMessageType.CMD_THROTTLE, {"value": 0.0}))
        self._pico.send(PicoMessage(PicoMessageType.CMD_BRAKE, {"value": 100.0}))

    def __repr__(self) -> str:
        s = self._state
        return (
            f"ActuatorManager(enabled={self._enabled}, "
            f"throttle={s.throttle_pct:.1f}%, "
            f"brake={s.brake_pct:.1f}%, "
            f"steering={s.steering_pct:.1f}%)"
        )
