"""
River Vector - Automatic ArUco Extrinsic Calibration
Computes ground-plane homographies without operator clicks by detecting
ArUco markers at RTK-surveyed yard positions.

Physical setup (one-time)
--------------------------
1. Print 5+ ArUco markers (DICT_4X4_50, IDs 10–19) at 20×20cm.
2. Laminate each marker and stake flat into the ground at known positions.
3. Walk each stake with the RTK GPS unit and record coordinates.
4. Save to fleets/yard_markers.json.

Runtime
-------
On startup (or on command), the mower scans all cameras for visible
markers. For each camera with ≥4 marker detections, a homography is
computed and saved. Results supplement or replace the click-based
extrinsic calibration.

Coordinate system
-----------------
All world positions are expressed in metres relative to the mower's
current GPS position:
    +X  = mower right
    +Y  = mower forward
    Z   = 0 (ground plane — markers must be flat on the ground)
"""

import json
import logging
import math
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from calibration.extrinsic import ExtrinsicCalibrator, HomographyResult
from calibration.store import CalibrationStore
from core.constants import CAMERA_NAMES

logger = logging.getLogger(__name__)

# Earth radius for local coordinate conversion
_EARTH_R_M: float = 6_371_000.0

# Minimum markers visible in one camera to compute a valid homography
MIN_MARKERS_PER_CAMERA: int = 4

# ArUco dictionary to use (must match printed markers)
_ARUCO_DICT_NAME: str = "DICT_4X4_50"

# Marker ID range reserved for yard survey markers
YARD_MARKER_ID_MIN: int = 10
YARD_MARKER_ID_MAX: int = 99


# ------------------------------------------------------------------
# Yard marker data model
# ------------------------------------------------------------------

@dataclass
class YardMarker:
    """One surveyed ground marker."""
    marker_id: int
    lat: float
    lng: float
    height_m: float = 0.0
    label: str = ""


class YardMarkerSurvey:
    """
    Loads and provides access to RTK-surveyed yard marker positions.

    Args:
        survey_path: Path to the yard_markers.json file.
    """

    def __init__(self, survey_path: str) -> None:
        self._path = survey_path
        self._markers: Dict[int, YardMarker] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._path):
            raise FileNotFoundError(f"Yard marker survey not found: {self._path}")

        with open(self._path, "r") as f:
            data = json.load(f)

        for m in data.get("markers", []):
            marker = YardMarker(
                marker_id=m["id"],
                lat=m["lat"],
                lng=m["lng"],
                height_m=m.get("height_m", 0.0),
                label=m.get("label", ""),
            )
            self._markers[marker.marker_id] = marker

        logger.info(
            "Yard marker survey loaded: %d markers from %s",
            len(self._markers), self._path,
        )

    def get_marker(self, marker_id: int) -> Optional[YardMarker]:
        """Returns the YardMarker for the given ID, or None if not surveyed."""
        return self._markers.get(marker_id)

    def world_position(
        self,
        marker_id: int,
        mower_lat: float,
        mower_lng: float,
    ) -> Optional[Tuple[float, float]]:
        """
        Returns the marker's position in metres relative to the mower centre.

        Args:
            marker_id:  ArUco marker ID.
            mower_lat:  Current mower latitude (decimal degrees).
            mower_lng:  Current mower longitude (decimal degrees).

        Returns:
            (x_m, y_m) where +X is mower-right, +Y is mower-forward.
            None if the marker ID is not in this survey.
        """
        marker = self._markers.get(marker_id)
        if marker is None:
            return None
        return _geo_to_local_mower(
            marker.lat, marker.lng, mower_lat, mower_lng
        )

    @property
    def marker_ids(self) -> List[int]:
        return list(self._markers.keys())

    def __len__(self) -> int:
        return len(self._markers)

    def __repr__(self) -> str:
        return f"YardMarkerSurvey({len(self._markers)} markers, path={self._path!r})"


# ------------------------------------------------------------------
# Auto calibrator
# ------------------------------------------------------------------

@dataclass
class AutoCalibrationResult:
    """Summary of one automatic extrinsic calibration run."""
    calibrated_cameras: List[str] = field(default_factory=list)
    skipped_cameras: List[str] = field(default_factory=list)
    markers_detected: int = 0
    mower_lat: Optional[float] = None
    mower_lng: Optional[float] = None

    @property
    def success(self) -> bool:
        return len(self.calibrated_cameras) > 0


class AutoExtrinsicCalibrator:
    """
    Automatically computes ground-plane homographies from ArUco yard markers.

    Requires:
    - RTK GPS fix on the mower (for mower-relative world coordinates)
    - ≥4 yard markers visible in each camera to be calibrated
    - Markers flat on the ground (height_m = 0.0)

    Args:
        survey:        YardMarkerSurvey with known marker GPS positions.
        store:         CalibrationStore for saving results.
        canvas_params: Optional canvas config — defaults match ExtrinsicCalibrator defaults.
    """

    def __init__(
        self,
        survey: YardMarkerSurvey,
        store: CalibrationStore,
        canvas_params: Optional[Dict[str, float]] = None,
    ) -> None:
        self._survey = survey
        self._store = store
        self._ext = ExtrinsicCalibrator(**(canvas_params or {}))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def calibrate_all(
        self,
        camera_manager,
        mower_lat: float,
        mower_lng: float,
    ) -> AutoCalibrationResult:
        """
        Scans all cameras and computes homographies for any that see ≥4 markers.

        Args:
            camera_manager: CameraManager instance.
            mower_lat:      Current mower latitude (RTK fix required).
            mower_lng:      Current mower longitude.

        Returns:
            AutoCalibrationResult with lists of calibrated and skipped cameras.
        """
        result = AutoCalibrationResult(mower_lat=mower_lat, mower_lng=mower_lng)

        for slot, cam_name in enumerate(CAMERA_NAMES):
            logger.info("Auto-extrinsic: scanning camera %d (%s)...", slot, cam_name)
            hom = self.calibrate_camera(slot, cam_name, camera_manager, mower_lat, mower_lng)

            if hom is not None:
                result.calibrated_cameras.append(cam_name)
                result.markers_detected += len(hom.image_corners)
            else:
                result.skipped_cameras.append(cam_name)

        if result.success:
            # Save the homographies that were computed
            computed = {n: r.H for n, r in self._ext.results.items()}
            if computed:
                self._store.save_homographies(computed, self._ext.canvas_params)
                logger.info(
                    "Auto-extrinsic complete — calibrated: %s, skipped: %s",
                    result.calibrated_cameras, result.skipped_cameras,
                )
        else:
            logger.warning(
                "Auto-extrinsic: no cameras had enough visible markers. "
                "Check that markers are deployed and visible. "
                "Need ≥%d markers per camera.", MIN_MARKERS_PER_CAMERA,
            )

        return result

    def calibrate_camera(
        self,
        camera_id: int,
        camera_name: str,
        camera_manager,
        mower_lat: float,
        mower_lng: float,
    ) -> Optional[HomographyResult]:
        """
        Attempts to compute a ground-plane homography for one camera.

        Captures a frame, detects all visible yard markers, and computes
        the homography from the image→world correspondences.

        Args:
            camera_id:      Camera slot index.
            camera_name:    Camera slot name.
            camera_manager: CameraManager for frame capture and ArUco detection.
            mower_lat:      Current mower latitude.
            mower_lng:      Current mower longitude.

        Returns:
            HomographyResult if successful, None if too few markers were visible.
        """
        # Collect image ↔ world point correspondences
        img_pts, world_pts = self._collect_correspondences(
            camera_id, camera_name, camera_manager, mower_lat, mower_lng
        )

        if len(img_pts) < MIN_MARKERS_PER_CAMERA:
            logger.info(
                "Camera '%s': %d/%d markers — skipping.",
                camera_name, len(img_pts), MIN_MARKERS_PER_CAMERA,
            )
            return None

        logger.info(
            "Camera '%s': %d markers detected — computing homography.",
            camera_name, len(img_pts),
        )

        try:
            return self._compute_from_correspondences(
                camera_name,
                np.array(img_pts, dtype=np.float32),
                np.array(world_pts, dtype=np.float32),
            )
        except Exception as exc:
            logger.error(
                "Camera '%s': homography computation failed: %s", camera_name, exc
            )
            return None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _collect_correspondences(
        self,
        camera_id: int,
        camera_name: str,
        camera_manager,
        mower_lat: float,
        mower_lng: float,
    ) -> Tuple[List[np.ndarray], List[Tuple[float, float]]]:
        """
        Captures a frame and returns parallel lists of image and world points.

        Image point  = marker centre in pixels (average of 4 corners)
        World point  = (x_m, y_m) relative to mower centre

        Returns:
            (image_points, world_points)
        """
        try:
            import cv2
        except ImportError:
            logger.warning("cv2 not available — cannot detect ArUco markers.")
            return [], []

        frame = camera_manager.capture_undistorted(camera_id)
        if not frame.is_valid or frame.data is None:
            logger.warning("Camera '%s': no valid frame.", camera_name)
            return [], []

        gray = cv2.cvtColor(frame.data, cv2.COLOR_BGR2GRAY)

        aruco_dict = cv2.aruco.getPredefinedDictionary(
            getattr(cv2.aruco, _ARUCO_DICT_NAME)
        )
        params = cv2.aruco.DetectorParameters()
        detector = cv2.aruco.ArucoDetector(aruco_dict, params)
        corners_list, ids, _ = detector.detectMarkers(gray)

        if ids is None:
            return [], []

        img_pts: List[np.ndarray] = []
        world_pts: List[Tuple[float, float]] = []

        for i, marker_id in enumerate(ids.flatten()):
            mid = int(marker_id)
            if mid < YARD_MARKER_ID_MIN or mid > YARD_MARKER_ID_MAX:
                continue  # Skip dock markers (IDs 0–9)

            world = self._survey.world_position(mid, mower_lat, mower_lng)
            if world is None:
                logger.debug("Marker %d detected but not in survey — skipping.", mid)
                continue

            # Image point = centroid of the 4 marker corners
            centre = corners_list[i][0].mean(axis=0)
            img_pts.append(centre)
            world_pts.append(world)

            logger.debug(
                "Camera '%s': marker %d at image (%.1f, %.1f) → world (%.2fm, %.2fm)",
                camera_name, mid, centre[0], centre[1], world[0], world[1],
            )

        return img_pts, world_pts

    def _compute_from_correspondences(
        self,
        camera_name: str,
        img_pts: np.ndarray,
        world_pts: np.ndarray,
    ) -> HomographyResult:
        """
        Computes a homography from Nx2 image and world point arrays using RANSAC.

        World metres → canvas pixels via ExtrinsicCalibrator's canvas transform.
        """
        import cv2

        canvas_pts = self._ext._world_to_canvas(world_pts)

        H, mask = cv2.findHomography(
            img_pts, canvas_pts, method=cv2.RANSAC, ransacReprojThreshold=4.0
        )

        if H is None:
            raise RuntimeError("findHomography returned None — check point quality.")

        inliers = int(mask.sum()) if mask is not None else len(img_pts)
        logger.info(
            "Camera '%s': RANSAC homography — %d/%d inliers.",
            camera_name, inliers, len(img_pts),
        )

        rms = ExtrinsicCalibrator._reprojection_error(H, img_pts, canvas_pts)
        result = HomographyResult(
            camera_name=camera_name,
            H=H,
            image_corners=img_pts,
            world_corners=world_pts,
            rms_error=rms,
        )
        # Register into the ExtrinsicCalibrator so canvas_params / save work
        self._ext._results[camera_name] = result
        return result

    def __repr__(self) -> str:
        return (
            f"AutoExtrinsicCalibrator(survey={len(self._survey)} markers, "
            f"unit={self._store._unit_id!r})"
        )


# ------------------------------------------------------------------
# Coordinate utilities
# ------------------------------------------------------------------

def _geo_to_local_mower(
    lat: float, lng: float,
    mower_lat: float, mower_lng: float,
) -> Tuple[float, float]:
    """
    Converts GPS to local metres relative to mower centre.

    Returns (x_m, y_m) where:
        +X = mower right   (East  relative to mower heading = 0°N, simplified)
        +Y = mower forward (North relative to mower heading = 0°N, simplified)

    Note: this uses a simple flat-Earth approximation valid for distances < 500m.
    At this scale the error is < 1mm — negligible for calibration.
    """
    x = math.radians(lng - mower_lng) * _EARTH_R_M * math.cos(math.radians(mower_lat))
    y = math.radians(lat - mower_lat) * _EARTH_R_M
    return x, y
