"""
River Vector - Relay Control
Controls ignition, starter, and PTO relays via Pico CMD_RELAY_* messages.
All relay state changes are logged and tracked locally for status reporting.
"""

import logging
import time
from dataclasses import dataclass

from pico.protocol import PicoMessage, PicoMessageType

logger = logging.getLogger(__name__)

STARTER_PULSE_SEC: float = 2.0    # How long to hold starter relay on
STARTER_SETTLE_SEC: float = 1.0   # Settle time after cranking before PTO allowed
PTO_ENGAGE_DELAY_SEC: float = 0.5  # Time for PTO clutch to engage


@dataclass
class RelayState:
    """Live snapshot of all relay positions."""
    ignition: bool = False
    starter: bool = False
    pto: bool = False


class RelayError(Exception):
    """Raised when a relay command is rejected due to state or sequencing."""


class RelayManager:
    """
    Controls the three mower relays: ignition, starter, and PTO deck.

    Enforces safe sequencing — e.g., starter cannot engage without ignition,
    PTO cannot engage without the engine running. All relay changes are
    forwarded to the Pico via CMD_RELAY_* messages.

    Args:
        pico_bridge: PicoBridge instance for hardware communication.
    """

    def __init__(self, pico_bridge) -> None:
        if pico_bridge is None:
            raise ValueError("pico_bridge must not be None.")
        self._pico = pico_bridge
        self._state = RelayState()
        self._engine_running = False

    # ------------------------------------------------------------------
    # Ignition
    # ------------------------------------------------------------------

    def ignition_on(self) -> None:
        """Powers the ignition circuit. Required before cranking."""
        self._set_ignition(True)
        logger.info("Ignition ON.")

    def ignition_off(self) -> None:
        """Cuts ignition. Also cuts PTO and marks engine stopped."""
        if self._state.pto:
            self.pto_off()
        self._set_ignition(False)
        self._engine_running = False
        logger.info("Ignition OFF — engine stopped.")

    # ------------------------------------------------------------------
    # Starter
    # ------------------------------------------------------------------

    def crank_engine(self) -> None:
        """
        Pulses the starter relay to crank the engine.

        Requires ignition to be ON. Automatically releases starter after
        STARTER_PULSE_SEC seconds and waits for engine to stabilize.

        Raises:
            RelayError: If ignition is not on.
        """
        if not self._state.ignition:
            raise RelayError("Cannot crank — ignition is OFF. Call ignition_on() first.")

        logger.info("Cranking engine (%.1fs pulse)...", STARTER_PULSE_SEC)
        self._set_starter(True)
        time.sleep(STARTER_PULSE_SEC)
        self._set_starter(False)
        time.sleep(STARTER_SETTLE_SEC)
        self._engine_running = True
        logger.info("Engine crank complete — engine assumed running.")

    # ------------------------------------------------------------------
    # PTO (Power Take-Off) deck
    # ------------------------------------------------------------------

    def pto_on(self) -> None:
        """
        Engages the PTO cutting deck.

        Requires the engine to be running.

        Raises:
            RelayError: If the engine is not running.
        """
        if not self._engine_running:
            raise RelayError("Cannot engage PTO — engine is not running.")
        self._set_pto(True)
        time.sleep(PTO_ENGAGE_DELAY_SEC)
        logger.info("PTO engaged — cutting deck active.")

    def pto_off(self) -> None:
        """Disengages the PTO cutting deck."""
        self._set_pto(False)
        logger.info("PTO disengaged — cutting deck stopped.")

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def state(self) -> RelayState:
        """Current relay state snapshot."""
        return self._state

    @property
    def engine_running(self) -> bool:
        """True if the engine has been successfully cranked."""
        return self._engine_running

    def emergency_off(self) -> None:
        """Cuts all relays immediately — no sequencing checks."""
        logger.critical("RelayManager: emergency off — cutting all relays.")
        self._set_pto(False)
        self._set_starter(False)
        self._set_ignition(False)
        self._engine_running = False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _set_ignition(self, active: bool) -> None:
        self._state.ignition = active
        self._pico.send(
            PicoMessage(PicoMessageType.CMD_RELAY_IGNITION, {"active": active})
        )

    def _set_starter(self, active: bool) -> None:
        self._state.starter = active
        self._pico.send(
            PicoMessage(PicoMessageType.CMD_RELAY_STARTER, {"active": active})
        )

    def _set_pto(self, active: bool) -> None:
        self._state.pto = active
        self._pico.send(
            PicoMessage(PicoMessageType.CMD_RELAY_PTO, {"active": active})
        )

    def __repr__(self) -> str:
        s = self._state
        return (
            f"RelayManager(ignition={s.ignition}, starter={s.starter}, "
            f"pto={s.pto}, engine={self._engine_running})"
        )
