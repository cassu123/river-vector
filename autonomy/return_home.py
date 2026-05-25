"""
================================================================================
Project:  River Vector — Autonomous Mower Control System
File:     autonomy/return_home.py
Purpose:  End-of-mow return-to-home and precision docking sequence.
          Navigates to the home position using GPS, then switches to
          ArUco marker-based precision alignment for final docking.
Author:   [Author Placeholder]
Version:  0.1.0
Date:     2026-05-25
================================================================================
"""

import logging
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

from core.constants import ARUCO_HOME_MARKER_ID, FaultCode
from safety.fault_manager import FaultManager

logger = logging.getLogger(__name__)


class ReturnState(Enum):
    """States within the return-to-home sequence."""
    IDLE = auto()
    NAVIGATING_TO_HOME = auto()
    SEARCHING_MARKER = auto()
    ALIGNING = auto()
    DOCKING = auto()
    DOCKED = auto()
    FAILED = auto()


@dataclass
class HomePosition:
    """Home dock position data."""
    lat: Optional[float]
    lng: Optional[float]
    aruco_marker_id: int = ARUCO_HOME_MARKER_ID


class ReturnHome:
    """
    Manages the return-to-home and precision docking sequence.

    Phase 1 — GPS navigation: Drives to within ~2m of the home position
    using RTK GPS waypoint following.

    Phase 2 — ArUco alignment: Switches to camera-based ArUco marker
    detection for sub-centimeter precision docking.

    Args:
        config: MowerConfig with home_position data.
        fault_manager: FaultManager for fault reporting.
        gps_manager: GPSManager for position data.
        camera_manager: CameraManager for ArUco detection.
        actuator_manager: ActuatorManager for motion control.
        light_manager: LightManager for status indication.
    """

    GPS_ARRIVAL_THRESHOLD_M: float = 2.0    # Switch to ArUco within 2m of home
    MARKER_SEARCH_TIMEOUT_SEC: float = 30.0
    DOCK_TIMEOUT_SEC: float = 60.0

    def __init__(
        self,
        config,
        fault_manager: FaultManager,
        gps_manager=None,
        camera_manager=None,
        actuator_manager=None,
        light_manager=None,
    ) -> None:
        self._config = config
        self._fault_manager = fault_manager
        self._gps = gps_manager
        self._cameras = camera_manager
        self._actuators = actuator_manager
        self._lights = light_manager
        self._state = ReturnState.IDLE
        self._home = self._load_home_position()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(self) -> bool:
        """
        Executes the full return-to-home sequence.

        Returns:
            True if docking was successful.
        """
        if self._home.lat is None or self._home.lng is None:
            logger.error("Home position not set — cannot return home.")
            self._fault_manager.report_fault(
                FaultCode.GPS_SIGNAL_LOST,
                "Home position lat/lng not configured in voyager.json",
            )
            self._state = ReturnState.FAILED
            return False

        logger.info(
            "Return-to-home initiated. Target: (%.6f, %.6f)",
            self._home.lat,
            self._home.lng,
        )

        if self._lights:
            self._lights.indicate_returning_home()

        # Phase 1 — GPS navigation to home vicinity
        if not self._navigate_to_home():
            return False

        # Phase 2 — ArUco precision docking
        if not self._precision_dock():
            return False

        self._state = ReturnState.DOCKED
        logger.info("Return-to-home complete — DOCKED.")
        return True

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def state(self) -> ReturnState:
        """Current return-to-home state."""
        return self._state

    @property
    def is_docked(self) -> bool:
        """True if the mower is successfully docked."""
        return self._state == ReturnState.DOCKED

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_home_position(self) -> HomePosition:
        """Loads home position from the unit config."""
        hp = self._config.home_position
        return HomePosition(
            lat=hp.get("lat"),
            lng=hp.get("lng"),
            aruco_marker_id=hp.get("aruco_marker_id", ARUCO_HOME_MARKER_ID),
        )

    def _navigate_to_home(self) -> bool:
        """
        Phase 1: GPS-guided navigation to within GPS_ARRIVAL_THRESHOLD_M of home.

        Returns:
            True when the mower is within threshold distance of home.
        """
        self._state = ReturnState.NAVIGATING_TO_HOME
        logger.info("Phase 1: GPS navigation to home position.")

        # TODO (Phase 8): Integrate with path_planner and gps_manager
        # for actual waypoint following. Stub returns True for now.
        logger.info("GPS navigation stub — assuming arrival at home vicinity.")
        time.sleep(0.1)
        return True

    def _precision_dock(self) -> bool:
        """
        Phase 2: ArUco marker-based precision docking.

        Searches for the home marker, aligns, and drives to dock.

        Returns:
            True if docking succeeded.
        """
        self._state = ReturnState.SEARCHING_MARKER
        logger.info(
            "Phase 2: Searching for ArUco marker ID %d.", self._home.aruco_marker_id
        )

        if self._lights:
            self._lights.indicate_docking()

        if self._cameras is None:
            logger.warning("No camera manager — skipping ArUco docking (stub).")
            self._state = ReturnState.DOCKED
            return True

        # Search for marker with timeout
        deadline = time.time() + self.MARKER_SEARCH_TIMEOUT_SEC
        detection = None
        while time.time() < deadline:
            # Check rear camera (camera 3) for home marker
            detection = self._cameras.detect_aruco(
                camera_id=3,
                target_id=self._home.aruco_marker_id,
            )
            if detection:
                logger.info("Home marker detected on camera 3.")
                break
            time.sleep(0.1)

        if detection is None:
            logger.error(
                "ArUco marker %d not found within %.1fs.",
                self._home.aruco_marker_id,
                self.MARKER_SEARCH_TIMEOUT_SEC,
            )
            self._state = ReturnState.FAILED
            return False

        # Align and dock
        self._state = ReturnState.ALIGNING
        logger.info("Aligning to marker...")
        # TODO (Phase 9): Implement PID-based alignment using marker corners
        time.sleep(0.5)

        self._state = ReturnState.DOCKING
        logger.info("Executing final dock approach...")
        # TODO (Phase 9): Drive forward slowly until contact sensor triggers
        time.sleep(0.5)

        return True

    def __repr__(self) -> str:
        return f"ReturnHome(state={self._state.name}, docked={self.is_docked})"
