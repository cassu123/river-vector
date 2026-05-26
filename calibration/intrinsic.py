"""
River Vector - Intrinsic Camera Calibration
Per-camera lens distortion calibration using a printed checkerboard target.
Produces camera_matrix and dist_coeffs used by undistortion and ArUco pose estimation.

Checkerboard spec (print and laminate):
    Pattern : 9×6 inner corners  (10×7 squares)
    Square  : 25mm per square
    Paper   : A3 or larger, glued flat to a rigid board
"""

import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# Checkerboard geometry — inner corners (columns, rows)
BOARD_COLS: int = 9
BOARD_ROWS: int = 6
SQUARE_SIZE_M: float = 0.025      # 25mm squares
MIN_SAMPLES: int = 15             # Minimum frames for reliable calibration
MAX_SAMPLES: int = 40             # Cap to avoid redundant frames
RMS_GOOD_THRESHOLD: float = 0.5  # Reprojection error below this is excellent
RMS_WARN_THRESHOLD: float = 1.0  # Above this, recalibration recommended


@dataclass
class IntrinsicResult:
    """
    Result of a single-camera intrinsic calibration run.

    Fields mirror the outputs of cv2.calibrateCamera() plus metadata.
    """
    camera_name: str
    camera_matrix: np.ndarray          # 3×3 intrinsic matrix
    dist_coeffs: np.ndarray            # (k1, k2, p1, p2, k3)
    rms_error: float                   # Reprojection RMS in pixels
    sample_count: int
    resolution: Tuple[int, int]        # (width, height) calibration was done at
    per_view_errors: List[float] = field(default_factory=list)

    @property
    def is_good(self) -> bool:
        """True if RMS error is within the acceptable threshold."""
        return self.rms_error < RMS_GOOD_THRESHOLD

    @property
    def quality_label(self) -> str:
        if self.rms_error < RMS_GOOD_THRESHOLD:
            return "EXCELLENT"
        if self.rms_error < RMS_WARN_THRESHOLD:
            return "ACCEPTABLE"
        return "POOR — recalibrate"


class IntrinsicCalibrator:
    """
    Collects checkerboard frames from a camera and computes intrinsic calibration.

    Workflow:
        cal = IntrinsicCalibrator("front", resolution=(640, 480))
        while not cal.is_ready:
            frame = camera.capture(0)
            found, preview = cal.collect_sample(frame)
        result = cal.calibrate()

    Args:
        camera_name: Slot name for logging and storage (e.g. 'front').
        resolution:  Expected frame resolution (width, height).
        board_size:  Checkerboard inner corner count (cols, rows).
        square_size: Physical square size in metres.
    """

    def __init__(
        self,
        camera_name: str,
        resolution: Tuple[int, int] = (640, 480),
        board_size: Tuple[int, int] = (BOARD_COLS, BOARD_ROWS),
        square_size: float = SQUARE_SIZE_M,
    ) -> None:
        self._name = camera_name
        self._resolution = resolution
        self._board_size = board_size
        self._square_size = square_size

        # 3D world points for one checkerboard view
        self._obj_template = self._make_obj_points()

        self._obj_points: List[np.ndarray] = []   # 3D world points per frame
        self._img_points: List[np.ndarray] = []   # 2D image points per frame

    # ------------------------------------------------------------------
    # Sample collection
    # ------------------------------------------------------------------

    def collect_sample(
        self, frame: np.ndarray
    ) -> Tuple[bool, Optional[np.ndarray]]:
        """
        Attempts to detect a checkerboard in the provided frame.

        Call this in a loop (e.g. once per second) while holding the
        checkerboard in different positions and angles.

        Args:
            frame: BGR image as numpy array.

        Returns:
            (found, annotated_frame) — found=True if corners detected.
            annotated_frame has corners drawn on it for visual feedback.
        """
        try:
            import cv2
        except ImportError:
            logger.error("cv2 not available — cannot collect calibration samples.")
            return False, frame

        if len(self._img_points) >= MAX_SAMPLES:
            logger.debug("Max samples (%d) already collected.", MAX_SAMPLES)
            return False, frame

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        flags = cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE
        found, corners = cv2.findChessboardCorners(gray, self._board_size, flags)

        annotated = frame.copy()

        if found:
            # Refine corner locations to subpixel accuracy
            criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
            corners = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), criteria)

            self._obj_points.append(self._obj_template.copy())
            self._img_points.append(corners)

            cv2.drawChessboardCorners(annotated, self._board_size, corners, found)
            logger.info(
                "Sample %d/%d collected for camera '%s'.",
                len(self._img_points), MIN_SAMPLES, self._name,
            )

        return found, annotated

    def reset(self) -> None:
        """Clears all collected samples to start over."""
        self._obj_points.clear()
        self._img_points.clear()
        logger.info("IntrinsicCalibrator '%s' reset.", self._name)

    # ------------------------------------------------------------------
    # Calibration
    # ------------------------------------------------------------------

    def calibrate(self) -> IntrinsicResult:
        """
        Computes intrinsic calibration from collected samples.

        Returns:
            IntrinsicResult with camera_matrix, dist_coeffs, and RMS error.

        Raises:
            RuntimeError: If fewer than MIN_SAMPLES have been collected.
            ImportError:  If cv2 is not available.
        """
        import cv2

        if len(self._img_points) < MIN_SAMPLES:
            raise RuntimeError(
                f"Need at least {MIN_SAMPLES} samples, have {len(self._img_points)}. "
                f"Keep collecting — move the checkerboard to different positions and angles."
            )

        w, h = self._resolution
        logger.info(
            "Calibrating '%s' from %d samples at %dx%d...",
            self._name, len(self._img_points), w, h,
        )

        rms, camera_matrix, dist_coeffs, rvecs, tvecs = cv2.calibrateCamera(
            self._obj_points,
            self._img_points,
            (w, h),
            None,
            None,
        )

        # Per-view reprojection errors
        per_view = []
        for i, (obj_pts, img_pts, rvec, tvec) in enumerate(
            zip(self._obj_points, self._img_points, rvecs, tvecs)
        ):
            projected, _ = cv2.projectPoints(obj_pts, rvec, tvec, camera_matrix, dist_coeffs)
            err = float(np.sqrt(np.mean((img_pts - projected) ** 2)))
            per_view.append(err)

        result = IntrinsicResult(
            camera_name=self._name,
            camera_matrix=camera_matrix,
            dist_coeffs=dist_coeffs,
            rms_error=rms,
            sample_count=len(self._img_points),
            resolution=self._resolution,
            per_view_errors=per_view,
        )

        logger.info(
            "Calibration complete — camera='%s', RMS=%.4fpx (%s), samples=%d",
            self._name, rms, result.quality_label, result.sample_count,
        )

        return result

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def sample_count(self) -> int:
        """Number of valid checkerboard frames collected so far."""
        return len(self._img_points)

    @property
    def is_ready(self) -> bool:
        """True when enough samples have been collected to calibrate."""
        return len(self._img_points) >= MIN_SAMPLES

    @property
    def camera_name(self) -> str:
        return self._name

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _make_obj_points(self) -> np.ndarray:
        """
        Generates 3D world coordinates for all inner corners of the checkerboard.
        Z=0 (flat on ground), X/Y in metres based on square size.
        """
        cols, rows = self._board_size
        pts = np.zeros((cols * rows, 3), dtype=np.float32)
        pts[:, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2) * self._square_size
        return pts

    def __repr__(self) -> str:
        return (
            f"IntrinsicCalibrator(camera={self._name!r}, "
            f"samples={self.sample_count}/{MIN_SAMPLES}, "
            f"ready={self.is_ready})"
        )
