"""
================================================================================
Project:  River Vector — Autonomous Mower Control System
File:     navigation/gps_manager.py
Purpose:  High-level GPS position manager. Wraps the low-level GPSInterface,
          provides distance/bearing calculations, and exposes the position
          data needed by the path planner and boundary checker.
Author:   [Author Placeholder]
Version:  0.1.0
Date:     2026-05-25
================================================================================
"""

import logging
import math
from typing import Optional, Tuple

from core.constants import FaultCode, RTK_ACCURACY_THRESHOLD_M
from hardware.gps import GPSFix, GPSInterface
from safety.fault_manager import FaultManager

logger = logging.getLogger(__name__)

# Earth radius in meters (WGS84 mean)
EARTH_RADIUS_M: float = 6_371_000.0


class GPSManager:
    """
    High-level GPS position manager for navigation subsystems.

    Wraps GPSInterface and provides utility methods for distance and
    bearing calculations used by the path planner and boundary checker.

    Args:
        gps_interface: Low-level GPSInterface instance.
        fault_manager: FaultManager for fault reporting.
    """

    def __init__(
        self,
        gps_interface: GPSInterface,
        fault_manager: FaultManager,
    ) -> None:
        if gps_interface is None:
            raise ValueError("gps_interface must not be None.")
        if fault_manager is None:
            raise ValueError("fault_manager must not be None.")
        self._gps = gps_interface
        self._fault_manager = fault_manager

    # ------------------------------------------------------------------
    # Position access
    # ------------------------------------------------------------------

    @property
    def fix(self) -> GPSFix:
        """Current GPS fix from the hardware interface."""
        return self._gps.fix

    @property
    def position(self) -> Optional[Tuple[float, float]]:
        """
        Current position as (latitude, longitude) decimal degrees.

        Returns:
            (lat, lng) tuple, or None if no fix is available.
        """
        fix = self.fix
        if fix.has_fix and fix.latitude is not None and fix.longitude is not None:
            return (fix.latitude, fix.longitude)
        return None

    @property
    def heading(self) -> Optional[float]:
        """Current heading in degrees (0–360, True North), or None."""
        return self.fix.heading_deg

    @property
    def is_ready_for_autonomous(self) -> bool:
        """
        True if GPS accuracy meets the RTK threshold required for autonomous operation.
        """
        fix = self.fix
        if not fix.has_fix:
            self._fault_manager.report_fault(
                FaultCode.GPS_SIGNAL_LOST, "No GPS fix available."
            )
            return False
        if not fix.meets_accuracy_requirement:
            self._fault_manager.report_fault(
                FaultCode.GPS_ACCURACY_LOW,
                f"GPS accuracy {fix.accuracy_m:.3f}m > threshold {RTK_ACCURACY_THRESHOLD_M}m",
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Geometry utilities
    # ------------------------------------------------------------------

    @staticmethod
    def distance_m(
        lat1: float, lng1: float, lat2: float, lng2: float
    ) -> float:
        """
        Calculates the great-circle distance between two GPS coordinates
        using the Haversine formula.

        Args:
            lat1, lng1: Origin coordinates in decimal degrees.
            lat2, lng2: Destination coordinates in decimal degrees.

        Returns:
            Distance in meters.
        """
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        d_phi = math.radians(lat2 - lat1)
        d_lambda = math.radians(lng2 - lng1)

        a = (
            math.sin(d_phi / 2) ** 2
            + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
        )
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return EARTH_RADIUS_M * c

    @staticmethod
    def bearing_deg(
        lat1: float, lng1: float, lat2: float, lng2: float
    ) -> float:
        """
        Calculates the initial bearing from point 1 to point 2.

        Args:
            lat1, lng1: Origin coordinates in decimal degrees.
            lat2, lng2: Destination coordinates in decimal degrees.

        Returns:
            Bearing in degrees (0–360, True North).
        """
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        d_lambda = math.radians(lng2 - lng1)

        x = math.sin(d_lambda) * math.cos(phi2)
        y = (
            math.cos(phi1) * math.sin(phi2)
            - math.sin(phi1) * math.cos(phi2) * math.cos(d_lambda)
        )
        bearing = math.degrees(math.atan2(x, y))
        return (bearing + 360) % 360

    def distance_to(self, lat: float, lng: float) -> Optional[float]:
        """
        Distance from current position to a target coordinate.

        Args:
            lat, lng: Target coordinates in decimal degrees.

        Returns:
            Distance in meters, or None if no fix is available.
        """
        pos = self.position
        if pos is None:
            return None
        return self.distance_m(pos[0], pos[1], lat, lng)

    def bearing_to(self, lat: float, lng: float) -> Optional[float]:
        """
        Bearing from current position to a target coordinate.

        Args:
            lat, lng: Target coordinates in decimal degrees.

        Returns:
            Bearing in degrees (0–360), or None if no fix is available.
        """
        pos = self.position
        if pos is None:
            return None
        return self.bearing_deg(pos[0], pos[1], lat, lng)

    def __repr__(self) -> str:
        pos = self.position
        fix = self.fix
        return (
            f"GPSManager(pos={pos}, "
            f"fix_quality={fix.fix_quality}, "
            f"accuracy={fix.accuracy_m}m)"
        )
