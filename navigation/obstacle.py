"""
================================================================================
Project:  River Vector — Autonomous Mower Control System
File:     navigation/obstacle.py
Purpose:  Obstacle detection and avoidance logic. Fuses ultrasonic sensor
          data with camera-based detection to classify obstacles and
          determine appropriate response (slow, stop, avoid).
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

from core.constants import (
    FaultCode,
    OBSTACLE_CLEAR_DISTANCE_CM,
    OBSTACLE_SLOW_DISTANCE_CM,
    OBSTACLE_STOP_DISTANCE_CM,
)
from safety.fault_manager import FaultManager, FaultSeverity

logger = logging.getLogger(__name__)


class ObstacleResponse(Enum):
    """Required response to current obstacle state."""
    CLEAR = auto()      # No obstacle — proceed normally
    SLOW = auto()       # Obstacle in slow zone — reduce speed
    STOP = auto()       # Obstacle in stop zone — halt immediately
    AVOID = auto()      # Obstacle requires path deviation (future)


@dataclass
class ObstacleState:
    """Current obstacle detection state."""
    response: ObstacleResponse = ObstacleResponse.CLEAR
    front_distance_cm: Optional[float] = None
    rear_distance_cm: Optional[float] = None
    detected_at: Optional[float] = None
    cleared_at: Optional[float] = None

    @property
    def is_blocked(self) -> bool:
        """True if a stop-level obstacle is present."""
        return self.response == ObstacleResponse.STOP


class ObstacleDetector:
    """
    Fuses ultrasonic and camera data to detect and classify obstacles.

    Reads sensor snapshots from the SensorManager and determines the
    appropriate response. Reports OBSTACLE_DETECTED faults to the
    FaultManager when a stop-level obstacle is present.

    Args:
        sensor_manager: SensorManager for ultrasonic readings.
        fault_manager: FaultManager for fault reporting.
        camera_manager: CameraManager for visual obstacle detection (optional).
    """

    def __init__(
        self,
        sensor_manager,
        fault_manager: FaultManager,
        camera_manager=None,
    ) -> None:
        if sensor_manager is None:
            raise ValueError("sensor_manager must not be None.")
        if fault_manager is None:
            raise ValueError("fault_manager must not be None.")
        self._sensors = sensor_manager
        self._fault_manager = fault_manager
        self._cameras = camera_manager
        self._state = ObstacleState()

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def update(self) -> ObstacleState:
        """
        Reads current sensor data and updates the obstacle state.

        Should be called at the navigation loop rate (10+ Hz).

        Returns:
            Updated ObstacleState.
        """
        snap = self._sensors.snapshot
        front = snap.ultrasonic_front_cm
        rear = snap.ultrasonic_rear_cm

        self._state.front_distance_cm = front
        self._state.rear_distance_cm = rear

        response = self._classify(front, rear)
        old_response = self._state.response
        self._state.response = response

        if response == ObstacleResponse.STOP and old_response != ObstacleResponse.STOP:
            self._state.detected_at = time.time()
            self._state.cleared_at = None
            self._fault_manager.report_fault(
                code=FaultCode.OBSTACLE_DETECTED,
                detail=f"Obstacle: front={front}cm, rear={rear}cm",
                severity=FaultSeverity.CRITICAL,
            )
            logger.warning(
                "OBSTACLE STOP: front=%.1fcm, rear=%.1fcm",
                front or -1,
                rear or -1,
            )

        elif response == ObstacleResponse.CLEAR and old_response == ObstacleResponse.STOP:
            self._state.cleared_at = time.time()
            self._fault_manager.clear_fault(FaultCode.OBSTACLE_DETECTED)
            logger.info("Obstacle cleared.")

        return self._state

    @property
    def state(self) -> ObstacleState:
        """Current obstacle detection state."""
        return self._state

    @property
    def is_clear(self) -> bool:
        """True if no obstacle is detected."""
        return self._state.response == ObstacleResponse.CLEAR

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _classify(
        front_cm: Optional[float], rear_cm: Optional[float]
    ) -> ObstacleResponse:
        """
        Classifies obstacle proximity from sensor distances.

        Args:
            front_cm: Front ultrasonic distance in cm (None if no reading).
            rear_cm: Rear ultrasonic distance in cm (None if no reading).

        Returns:
            ObstacleResponse classification.
        """
        distances = [d for d in (front_cm, rear_cm) if d is not None]
        if not distances:
            return ObstacleResponse.CLEAR

        min_dist = min(distances)

        if min_dist <= OBSTACLE_STOP_DISTANCE_CM:
            return ObstacleResponse.STOP
        elif min_dist <= OBSTACLE_SLOW_DISTANCE_CM:
            return ObstacleResponse.SLOW
        else:
            return ObstacleResponse.CLEAR

    def __repr__(self) -> str:
        return (
            f"ObstacleDetector(response={self._state.response.name}, "
            f"front={self._state.front_distance_cm}cm, "
            f"rear={self._state.rear_distance_cm}cm)"
        )
