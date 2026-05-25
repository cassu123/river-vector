"""
================================================================================
Project:  River Vector — Autonomous Mower Control System
File:     autonomy/mow_session.py
Purpose:  Full mow session lifecycle controller. Manages the sequence from
          session start through mowing completion: engine start, PTO engage,
          path following, obstacle handling, and session end. Reports
          progress to River Song API throughout.
Author:   [Author Placeholder]
Version:  0.1.0
Date:     2026-05-25
================================================================================
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from core.constants import FaultCode, MIN_FUEL_PERCENT
from safety.fault_manager import FaultManager

logger = logging.getLogger(__name__)


class SessionState(Enum):
    """States within a mow session lifecycle."""
    IDLE = auto()
    STARTING_ENGINE = auto()
    ENGAGING_PTO = auto()
    MOWING = auto()
    PAUSED_OBSTACLE = auto()
    PAUSED_FAULT = auto()
    COMPLETING = auto()
    RETURNING_HOME = auto()
    DONE = auto()
    ABORTED = auto()


@dataclass
class SessionStats:
    """Accumulated statistics for a mow session."""
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None
    waypoints_completed: int = 0
    total_waypoints: int = 0
    obstacles_encountered: int = 0
    distance_m: float = 0.0
    area_covered_m2: float = 0.0
    abort_reason: str = ""

    @property
    def duration_sec(self) -> float:
        """Session duration in seconds."""
        end = self.end_time or time.time()
        return end - self.start_time

    @property
    def completion_pct(self) -> float:
        """Percentage of waypoints completed."""
        if self.total_waypoints == 0:
            return 0.0
        return (self.waypoints_completed / self.total_waypoints) * 100.0


class MowSession:
    """
    Controls the full lifecycle of an autonomous mowing session.

    Coordinates the path planner, actuators, relays, shift controller,
    and safety systems to execute a complete mow. Reports state changes
    to the River Song API client.

    Args:
        config: MowerConfig instance.
        fault_manager: FaultManager for fault monitoring.
        actuator_manager: ActuatorManager for motion control.
        relay_manager: RelayManager for PTO and ignition.
        shift_controller: ShiftController for gear management.
        path_planner: PathPlanner for waypoint generation.
        sensor_manager: SensorManager for obstacle detection.
        api_client: RiverSongClient for progress reporting (optional).
        light_manager: LightManager for status indication (optional).
    """

    def __init__(
        self,
        config,
        fault_manager: FaultManager,
        actuator_manager=None,
        relay_manager=None,
        shift_controller=None,
        path_planner=None,
        sensor_manager=None,
        api_client=None,
        light_manager=None,
    ) -> None:
        self._config = config
        self._fault_manager = fault_manager
        self._actuators = actuator_manager
        self._relays = relay_manager
        self._shift = shift_controller
        self._path_planner = path_planner
        self._sensors = sensor_manager
        self._api = api_client
        self._lights = light_manager
        self._state = SessionState.IDLE
        self._stats = SessionStats()
        self._abort_requested = False

    # ------------------------------------------------------------------
    # Session control
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """
        Starts a mowing session.

        Validates preconditions, starts the engine, engages PTO, and
        begins path following.

        Returns:
            True if the session started successfully.
        """
        if self._state != SessionState.IDLE:
            logger.warning("Cannot start session — current state: %s", self._state.name)
            return False

        logger.info("Starting mow session...")
        self._stats = SessionStats()
        self._abort_requested = False

        # Validate fuel
        if self._sensors:
            snap = self._sensors.snapshot
            if snap.fuel_percent is not None and snap.fuel_percent < MIN_FUEL_PERCENT:
                logger.error(
                    "Insufficient fuel (%.1f%%) — session aborted.", snap.fuel_percent
                )
                self._abort("LOW_FUEL_AT_START")
                return False

        # Start engine
        if not self._start_engine():
            return False

        # Engage PTO
        if not self._engage_pto():
            return False

        # Begin mowing
        self._transition(SessionState.MOWING)
        if self._lights:
            self._lights.indicate_mowing()
        self._report_to_api("session_started")
        logger.info("Mow session active.")
        return True

    def pause(self, reason: str = "") -> None:
        """
        Pauses the mowing session (e.g., obstacle detected).

        Args:
            reason: Description of why the session was paused.
        """
        if self._state == SessionState.MOWING:
            logger.info("Session paused: %s", reason)
            self._transition(SessionState.PAUSED_OBSTACLE)
            if self._actuators:
                self._actuators.set_throttle(0.0)
            if self._lights:
                self._lights.indicate_obstacle()

    def resume(self) -> bool:
        """
        Resumes a paused session.

        Returns:
            True if the session resumed successfully.
        """
        if self._state not in (SessionState.PAUSED_OBSTACLE, SessionState.PAUSED_FAULT):
            return False
        if not self._fault_manager.is_safe_to_operate():
            logger.warning("Cannot resume — active faults present.")
            return False
        self._transition(SessionState.MOWING)
        if self._lights:
            self._lights.indicate_mowing()
        logger.info("Session resumed.")
        return True

    def complete(self) -> None:
        """
        Marks the session as complete and initiates return-to-home.
        """
        logger.info("Mow session complete — initiating return home.")
        self._stats.end_time = time.time()
        self._transition(SessionState.COMPLETING)
        if self._relays:
            self._relays.pto_off()
        self._transition(SessionState.RETURNING_HOME)
        self._report_to_api("session_complete")

    def abort(self, reason: str = "OPERATOR_ABORT") -> None:
        """
        Aborts the session immediately.

        Args:
            reason: Description of why the session was aborted.
        """
        self._abort_requested = True
        self._abort(reason)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def state(self) -> SessionState:
        """Current session state."""
        return self._state

    @property
    def stats(self) -> SessionStats:
        """Current session statistics."""
        return self._stats

    @property
    def is_active(self) -> bool:
        """True if the session is in an active mowing state."""
        return self._state in (
            SessionState.MOWING,
            SessionState.PAUSED_OBSTACLE,
            SessionState.PAUSED_FAULT,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _start_engine(self) -> bool:
        """Starts the engine via relay manager."""
        self._transition(SessionState.STARTING_ENGINE)
        if self._relays is None:
            logger.warning("No relay manager — skipping engine start (stub).")
            return True
        try:
            self._relays.ignition_on()
            time.sleep(1.0)
            self._relays.crank_engine()
            time.sleep(2.0)  # Allow engine to stabilize
            logger.info("Engine started.")
            return True
        except Exception as exc:
            logger.error("Engine start failed: %s", exc)
            self._abort(f"ENGINE_START_FAILED: {exc}")
            return False

    def _engage_pto(self) -> bool:
        """Engages the PTO deck."""
        self._transition(SessionState.ENGAGING_PTO)
        if self._relays is None:
            logger.warning("No relay manager — skipping PTO engage (stub).")
            return True
        try:
            self._relays.pto_on()
            time.sleep(0.5)
            logger.info("PTO engaged.")
            return True
        except Exception as exc:
            logger.error("PTO engage failed: %s", exc)
            self._abort(f"PTO_ENGAGE_FAILED: {exc}")
            return False

    def _abort(self, reason: str) -> None:
        """Internal abort handler."""
        logger.error("Session ABORTED: %s", reason)
        self._stats.abort_reason = reason
        self._stats.end_time = time.time()
        if self._relays:
            try:
                self._relays.pto_off()
            except Exception:
                pass
        if self._actuators:
            self._actuators.emergency_stop()
        self._transition(SessionState.ABORTED)
        self._report_to_api("session_aborted")

    def _transition(self, new_state: SessionState) -> None:
        """Transitions to a new session state."""
        logger.debug("Session: %s → %s", self._state.name, new_state.name)
        self._state = new_state

    def _report_to_api(self, event: str) -> None:
        """Reports a session event to the River Song API."""
        if self._api is None:
            return
        try:
            self._api.post_event(event, {
                "state": self._state.name,
                "completion_pct": self._stats.completion_pct,
                "duration_sec": self._stats.duration_sec,
            })
        except Exception as exc:
            logger.warning("API report failed for event '%s': %s", event, exc)

    def __repr__(self) -> str:
        return (
            f"MowSession(state={self._state.name}, "
            f"completion={self._stats.completion_pct:.1f}%)"
        )
