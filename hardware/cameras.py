"""
River Vector - Camera Interface (5-Camera Setup)
Manages the 5-camera vision system: front center, front left/right 45°,
rear left/right bag. Provides frame capture, undistortion, ArUco marker
detection, and (when calibrated) a bird's-eye composite view.
Falls back to sim mode when no cameras are physically available.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from core.constants import CAMERA_COUNT, CAMERA_NAMES, CAMERA_RESOLUTION

logger = logging.getLogger(__name__)


@dataclass
class ArucoDetection:
    """Result of an ArUco marker detection attempt."""
    marker_id: int
    corners: Optional[np.ndarray] = None   # Shape (1, 4, 2) — pixel coordinates
    detected: bool = False
    camera_id: int = 0


@dataclass
class CameraFrame:
    """A single captured frame from one camera."""
    camera_id: int
    camera_name: str
    data: Optional[np.ndarray] = None     # HxWxC BGR numpy array
    is_valid: bool = False


class CameraManager:
    """
    Manages the 5-camera vision system for River Vector.

    Camera layout (from voyager.json):
      0 — front       (120° FOV, center)
      1 — front_left  (90°  FOV, 45° left)
      2 — front_right (90°  FOV, 45° right)
      3 — rear_left   (90°  FOV, bag left)
      4 — rear_right  (90°  FOV, bag right)

    ArUco detection is used for precision docking (camera 3, rear_left).
    Falls back to sim mode when cv2 or cameras are unavailable.

    Args:
        camera_indices: List of OS camera device indices, one per camera slot.
        resolution:     Capture resolution (width, height).
        sim_mode:       Force simulation mode — no real camera I/O.
    """

    ARUCO_DICT_ID = None  # Resolved at runtime to avoid import-time cv2 errors

    def __init__(
        self,
        camera_indices: Optional[List[int]] = None,
        resolution: tuple = CAMERA_RESOLUTION,
        sim_mode: bool = False,
    ) -> None:
        self._resolution = resolution
        self._sim = sim_mode
        self._captures: Dict[int, object] = {}

        # Default indices: /dev/video0 … /dev/video4
        indices = camera_indices or list(range(CAMERA_COUNT))

        if not self._sim:
            self._open_cameras(indices)

    # ------------------------------------------------------------------
    # Frame capture
    # ------------------------------------------------------------------

    def capture(self, camera_id: int) -> CameraFrame:
        """
        Captures a frame from the specified camera.

        Args:
            camera_id: Camera slot index (0–4).

        Returns:
            CameraFrame. is_valid=False if capture failed or in sim mode.
        """
        name = CAMERA_NAMES[camera_id] if camera_id < len(CAMERA_NAMES) else f"cam{camera_id}"

        if self._sim:
            w, h = self._resolution
            dummy = np.zeros((h, w, 3), dtype=np.uint8)
            return CameraFrame(camera_id=camera_id, camera_name=name, data=dummy, is_valid=True)

        cap = self._captures.get(camera_id)
        if cap is None:
            logger.warning("Camera %d (%s) not open.", camera_id, name)
            return CameraFrame(camera_id=camera_id, camera_name=name, is_valid=False)

        try:
            import cv2
            ret, frame = cap.read()
            if not ret or frame is None:
                logger.warning("Camera %d: failed to read frame.", camera_id)
                return CameraFrame(camera_id=camera_id, camera_name=name, is_valid=False)
            return CameraFrame(camera_id=camera_id, camera_name=name, data=frame, is_valid=True)
        except Exception as exc:
            logger.error("Camera %d capture error: %s", camera_id, exc)
            return CameraFrame(camera_id=camera_id, camera_name=name, is_valid=False)

    # ------------------------------------------------------------------
    # ArUco detection
    # ------------------------------------------------------------------

    def detect_aruco(
        self, camera_id: int, target_id: int
    ) -> Optional[ArucoDetection]:
        """
        Detects a specific ArUco marker in the given camera's frame.

        Used for precision docking — searches for the home dock marker
        (default ID 0) on camera 3 (rear_left).

        Args:
            camera_id: Camera slot to search (typically 3 for docking).
            target_id: ArUco marker ID to find.

        Returns:
            ArucoDetection with corners if found, None if not detected.
        """
        frame = self.capture(camera_id)

        if self._sim:
            logger.debug(
                "Camera [SIM] detect_aruco: camera=%d, target_id=%d — not detected.",
                camera_id, target_id,
            )
            return None

        if not frame.is_valid or frame.data is None:
            return None

        try:
            import cv2
            aruco = cv2.aruco
            if CameraManager.ARUCO_DICT_ID is None:
                CameraManager.ARUCO_DICT_ID = aruco.getPredefinedDictionary(
                    aruco.DICT_4X4_50
                )

            gray = cv2.cvtColor(frame.data, cv2.COLOR_BGR2GRAY)
            params = aruco.DetectorParameters()
            detector = aruco.ArucoDetector(CameraManager.ARUCO_DICT_ID, params)
            corners, ids, _ = detector.detectMarkers(gray)

            if ids is None:
                return None

            for i, marker_id in enumerate(ids.flatten()):
                if int(marker_id) == target_id:
                    logger.info(
                        "ArUco marker %d detected on camera %d.", target_id, camera_id
                    )
                    return ArucoDetection(
                        marker_id=target_id,
                        corners=corners[i],
                        detected=True,
                        camera_id=camera_id,
                    )

        except ImportError:
            logger.warning("cv2 not available — ArUco detection skipped.")
        except Exception as exc:
            logger.error("ArUco detection error (camera %d): %s", camera_id, exc)

        return None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def release(self) -> None:
        """Releases all open camera captures."""
        for cam_id, cap in self._captures.items():
            try:
                cap.release()
            except Exception:
                pass
        self._captures.clear()
        logger.info("CameraManager: all cameras released.")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _open_cameras(self, indices: List[int]) -> None:
        """Opens VideoCapture for each camera index."""
        try:
            import cv2
        except ImportError:
            logger.warning("cv2 not available — CameraManager running in sim mode.")
            self._sim = True
            return

        for slot, dev_idx in enumerate(indices):
            try:
                cap = cv2.VideoCapture(dev_idx)
                if not cap.isOpened():
                    logger.warning(
                        "Camera slot %d (device %d) could not be opened — sim for this slot.",
                        slot, dev_idx,
                    )
                    continue
                w, h = self._resolution
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
                self._captures[slot] = cap
                logger.info(
                    "Camera slot %d (%s) opened on device %d.",
                    slot, CAMERA_NAMES[slot] if slot < len(CAMERA_NAMES) else f"cam{slot}", dev_idx,
                )
            except Exception as exc:
                logger.warning("Camera slot %d open error: %s", slot, exc)

        if not self._captures:
            logger.warning("No cameras opened — CameraManager in sim mode.")
            self._sim = True

    # ------------------------------------------------------------------
    # Calibration integration
    # ------------------------------------------------------------------

    def load_calibration(self, store) -> None:
        """
        Loads intrinsic calibration and ground-plane homographies from a
        CalibrationStore. Call this after construction when calibration data
        is available. Silently skips cameras without saved calibration.

        Args:
            store: CalibrationStore instance for the active unit.
        """
        self._intrinsics: Dict[int, Dict] = {}
        self._stitcher = None

        # Load per-camera intrinsics
        for slot, cam_name in enumerate(CAMERA_NAMES):
            cal = store.load_intrinsic(cam_name)
            if cal is not None:
                self._intrinsics[slot] = cal
                logger.info(
                    "Intrinsic calibration loaded for camera %d (%s), RMS=%.4fpx.",
                    slot, cam_name, cal["rms_error"],
                )

        # Load stitcher if homographies are saved
        hom_data = store.load_homographies()
        if hom_data is not None:
            from calibration.stitcher import BirdEyeStitcher
            homographies, canvas_params = hom_data
            self._stitcher = BirdEyeStitcher(homographies, canvas_params)
            logger.info("Bird's-eye stitcher loaded — cameras: %s", list(homographies.keys()))

    def undistort(self, camera_id: int, frame: np.ndarray) -> np.ndarray:
        """
        Removes lens distortion from a frame using saved intrinsic calibration.

        Falls back to returning the original frame if no calibration is loaded
        for this camera — so all code can call this unconditionally.

        Args:
            camera_id: Camera slot index (0–N).
            frame:     Raw BGR frame from capture().

        Returns:
            Undistorted BGR frame, or original frame if uncalibrated.
        """
        cal = getattr(self, "_intrinsics", {}).get(camera_id)
        if cal is None:
            return frame

        try:
            import cv2
            return cv2.undistort(frame, cal["camera_matrix"], cal["dist_coeffs"])
        except Exception as exc:
            logger.warning("Undistort failed for camera %d: %s", camera_id, exc)
            return frame

    def capture_undistorted(self, camera_id: int) -> CameraFrame:
        """
        Captures and undistorts a frame in one call.

        Args:
            camera_id: Camera slot index.

        Returns:
            Undistorted CameraFrame.
        """
        frame = self.capture(camera_id)
        if frame.is_valid and frame.data is not None:
            frame.data = self.undistort(camera_id, frame.data)
        return frame

    def bird_eye_view(self) -> Optional[np.ndarray]:
        """
        Produces a bird's-eye top-down composite from all cameras.

        Requires extrinsic calibration to have been loaded via load_calibration().
        Returns None if not calibrated.

        Returns:
            BGR numpy array (canvas_h × canvas_w × 3), or None.
        """
        stitcher = getattr(self, "_stitcher", None)
        if stitcher is None:
            logger.debug("Bird's-eye view requested but no stitcher loaded.")
            return None

        frames: Dict[str, np.ndarray] = {}
        for slot, cam_name in enumerate(CAMERA_NAMES):
            cf = self.capture_undistorted(slot)
            if cf.is_valid and cf.data is not None:
                frames[cam_name] = cf.data

        return stitcher.stitch(frames, draw_mower=True)

    @property
    def is_calibrated(self) -> bool:
        """True if intrinsic calibration has been loaded for at least one camera."""
        return bool(getattr(self, "_intrinsics", {}))

    @property
    def has_bird_eye(self) -> bool:
        """True if extrinsic calibration (stitcher) is loaded."""
        return getattr(self, "_stitcher", None) is not None

    def __repr__(self) -> str:
        return (
            f"CameraManager(cameras={len(self._captures)}, sim={self._sim}, "
            f"calibrated={self.is_calibrated}, bird_eye={self.has_bird_eye}, "
            f"resolution={self._resolution})"
        )
