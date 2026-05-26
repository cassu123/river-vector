"""
================================================================================
Project:  River Vector — Autonomous Mower Control System
File:     navigation/parking.py
Purpose:  ArUco marker-based precision parking/docking. Computes the
          translation and rotation needed to align the mower with the
          home dock marker and generates steering corrections.
Author:   [Author Placeholder]
Version:  0.1.0
Date:     2026-05-25
================================================================================
"""

import logging
import math
from dataclasses import dataclass
from typing import Optional, Tuple

import cv2
import numpy as np

from core.constants import ARUCO_HOME_MARKER_ID, CAMERA_RESOLUTION
from hardware.cameras import ArucoDetection

logger = logging.getLogger(__name__)

# Physical size of the ArUco marker in meters (must match printed marker)
MARKER_SIZE_M: float = 0.20  # 20cm marker


@dataclass
class DockAlignment:
    """Computed alignment correction for docking."""
    lateral_error_m: float = 0.0    # Positive = marker is to the right
    distance_m: float = 0.0         # Distance to marker
    heading_error_deg: float = 0.0  # Positive = need to turn right
    is_aligned: bool = False        # True if within docking tolerance

    LATERAL_TOLERANCE_M: float = 0.05   # 5cm lateral tolerance
    HEADING_TOLERANCE_DEG: float = 3.0  # 3° heading tolerance
    DOCK_DISTANCE_M: float = 0.3        # Stop when this close to marker


class ParkingController:
    """
    Computes docking alignment corrections from ArUco marker detections.

    Uses OpenCV's solvePnP to estimate the 3D pose of the home marker
    relative to the camera, then computes lateral and heading corrections
    for the actuator manager.

    Args:
        camera_matrix: Camera intrinsic matrix (3x3 numpy array).
        dist_coeffs: Camera distortion coefficients.
        marker_size_m: Physical marker size in meters.
    """

    def __init__(
        self,
        camera_matrix: Optional[np.ndarray] = None,
        dist_coeffs: Optional[np.ndarray] = None,
        marker_size_m: float = MARKER_SIZE_M,
    ) -> None:
        self._marker_size = marker_size_m
        # Default camera matrix — replace with calibrated values
        self._camera_matrix = camera_matrix if camera_matrix is not None else self._default_camera_matrix()
        self._dist_coeffs = dist_coeffs if dist_coeffs is not None else np.zeros((4, 1))

    # ------------------------------------------------------------------
    # Alignment computation
    # ------------------------------------------------------------------

    def compute_alignment(self, detection: ArucoDetection) -> DockAlignment:
        """
        Computes docking alignment from an ArUco marker detection.

        Args:
            detection: ArucoDetection from CameraManager.detect_aruco().

        Returns:
            DockAlignment with lateral error, distance, and heading error.
        """
        alignment = DockAlignment()

        if detection is None or detection.corners is None:
            return alignment

        try:
            # Define 3D marker corner points in marker coordinate frame
            half = self._marker_size / 2.0
            obj_points = np.array([
                [-half,  half, 0],
                [ half,  half, 0],
                [ half, -half, 0],
                [-half, -half, 0],
            ], dtype=np.float32)

            img_points = detection.corners.reshape(4, 2).astype(np.float32)

            success, rvec, tvec = cv2.solvePnP(
                obj_points,
                img_points,
                self._camera_matrix,
                self._dist_coeffs,
            )

            if not success:
                logger.warning("solvePnP failed for marker alignment.")
                return alignment

            # tvec = [x, y, z] in camera frame
            # x = lateral offset (positive = right)
            # z = distance forward
            lateral_m = float(tvec[0][0])
            distance_m = float(tvec[2][0])

            # Heading error from rotation vector
            rmat, _ = cv2.Rodrigues(rvec)
            heading_error_deg = math.degrees(math.atan2(rmat[0][2], rmat[2][2]))

            alignment.lateral_error_m = lateral_m
            alignment.distance_m = distance_m
            alignment.heading_error_deg = heading_error_deg
            alignment.is_aligned = (
                abs(lateral_m) < DockAlignment.LATERAL_TOLERANCE_M
                and abs(heading_error_deg) < DockAlignment.HEADING_TOLERANCE_DEG
                and distance_m > DockAlignment.DOCK_DISTANCE_M
            )

            logger.debug(
                "Dock alignment: lateral=%.3fm, dist=%.3fm, heading=%.1f°, aligned=%s",
                lateral_m, distance_m, heading_error_deg, alignment.is_aligned,
            )

        except Exception as exc:
            logger.error("Alignment computation error: %s", exc, exc_info=True)

        return alignment

    def compute_steering_correction(self, alignment: DockAlignment) -> float:
        """
        Converts a DockAlignment into a steering percentage correction.

        Args:
            alignment: DockAlignment from compute_alignment().

        Returns:
            Steering correction percentage (-100 to +100).
            Positive = steer right, negative = steer left.
        """
        if alignment.is_aligned:
            return 0.0

        # Simple proportional correction — tune gains during commissioning
        lateral_gain = 50.0     # % steering per meter of lateral error
        heading_gain = 2.0      # % steering per degree of heading error

        correction = (
            alignment.lateral_error_m * lateral_gain
            + alignment.heading_error_deg * heading_gain
        )
        return max(-100.0, min(100.0, correction))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @classmethod
    def from_calibration(cls, store, camera_name: str = "front") -> "ParkingController":
        """
        Constructs a ParkingController using saved intrinsic calibration data.

        Loads the camera matrix and distortion coefficients from the
        CalibrationStore for the specified camera. Falls back to default
        values if no calibration is saved for that camera.

        Args:
            store:       CalibrationStore for the active unit.
            camera_name: Which camera's calibration to use (default 'front'
                         — change to 'rear_left' for rear-mounted dock marker).

        Returns:
            ParkingController with real or default camera parameters.
        """
        cal = store.load_intrinsic(camera_name)
        if cal is None:
            logger.warning(
                "No intrinsic calibration for '%s' — ParkingController using defaults. "
                "Run: python3 -m calibration intrinsic --unit <id> --camera %s",
                camera_name, camera_name,
            )
            return cls()
        logger.info(
            "ParkingController loaded calibration for '%s' (RMS=%.4fpx).",
            camera_name, cal["rms_error"],
        )
        return cls(
            camera_matrix=cal["camera_matrix"].astype(np.float64),
            dist_coeffs=cal["dist_coeffs"].astype(np.float64),
        )

    def _default_camera_matrix(self) -> np.ndarray:
        """
        Rough default camera matrix based on CAMERA_RESOLUTION.
        Accuracy is poor — run intrinsic calibration to replace this.
        """
        w, h = CAMERA_RESOLUTION
        fx = fy = w  # Approximate focal length
        cx, cy = w / 2.0, h / 2.0
        return np.array([
            [fx,  0, cx],
            [ 0, fy, cy],
            [ 0,  0,  1],
        ], dtype=np.float64)

    def __repr__(self) -> str:
        return f"ParkingController(marker_size={self._marker_size}m)"
