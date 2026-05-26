"""
River Vector - Calibration Data Store
Saves and loads per-camera calibration results to/from calibration_data/<unit_id>/.
Uses numpy .npz for compact, portable binary storage.
"""

import logging
import os
from typing import Dict, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# Root directory for all calibration data, relative to project root
_CALIBRATION_ROOT = os.path.join(
    os.path.dirname(__file__), "..", "calibration_data"
)


class CalibrationStore:
    """
    File-based store for camera calibration data.

    Directory layout:
        calibration_data/
            <unit_id>/
                <camera_name>_intrinsic.npz   ← camera matrix + dist coeffs + RMS
                homographies.npz              ← all camera ground-plane homographies

    Args:
        unit_id:  Unit identifier (e.g. 'VOY-RV-001'). Determines subdirectory.
        root_dir: Override base directory (used in tests).
    """

    def __init__(self, unit_id: str, root_dir: Optional[str] = None) -> None:
        self._unit_id = unit_id
        self._root = root_dir or _CALIBRATION_ROOT
        self._unit_dir = os.path.join(self._root, unit_id)
        os.makedirs(self._unit_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # Intrinsic calibration
    # ------------------------------------------------------------------

    def save_intrinsic(
        self,
        camera_name: str,
        camera_matrix: np.ndarray,
        dist_coeffs: np.ndarray,
        rms_error: float,
        resolution: Tuple[int, int],
    ) -> None:
        """
        Saves intrinsic calibration for one camera.

        Args:
            camera_name:   Camera slot name (e.g. 'front', 'rear_left').
            camera_matrix: 3×3 intrinsic matrix from cv2.calibrateCamera.
            dist_coeffs:   Distortion coefficients (k1,k2,p1,p2,k3).
            rms_error:     RMS reprojection error in pixels.
            resolution:    (width, height) the calibration was performed at.
        """
        path = self._intrinsic_path(camera_name)
        np.savez_compressed(
            path,
            camera_matrix=camera_matrix,
            dist_coeffs=dist_coeffs,
            rms_error=np.array([rms_error]),
            resolution=np.array(resolution),
        )
        logger.info(
            "Intrinsic calibration saved: %s — RMS=%.4fpx (%s)",
            path, rms_error, camera_name,
        )

    def load_intrinsic(
        self, camera_name: str
    ) -> Optional[Dict[str, np.ndarray]]:
        """
        Loads intrinsic calibration for one camera.

        Args:
            camera_name: Camera slot name.

        Returns:
            Dict with keys 'camera_matrix', 'dist_coeffs', 'rms_error',
            'resolution', or None if no calibration file exists.
        """
        path = self._intrinsic_path(camera_name)
        if not os.path.exists(path + ".npz"):
            logger.debug("No intrinsic calibration for camera '%s'.", camera_name)
            return None

        data = np.load(path + ".npz")
        logger.info(
            "Intrinsic calibration loaded: %s — RMS=%.4fpx",
            camera_name, float(data["rms_error"][0]),
        )
        return {
            "camera_matrix": data["camera_matrix"],
            "dist_coeffs":   data["dist_coeffs"],
            "rms_error":     float(data["rms_error"][0]),
            "resolution":    tuple(data["resolution"].tolist()),
        }

    def has_intrinsic(self, camera_name: str) -> bool:
        """True if a saved intrinsic calibration exists for this camera."""
        return os.path.exists(self._intrinsic_path(camera_name) + ".npz")

    # ------------------------------------------------------------------
    # Extrinsic / ground-plane homographies
    # ------------------------------------------------------------------

    def save_homographies(
        self,
        homographies: Dict[str, np.ndarray],
        canvas_params: Dict[str, float],
    ) -> None:
        """
        Saves ground-plane homography matrices for all cameras.

        Args:
            homographies:  Dict mapping camera_name → 3×3 homography matrix.
                           Each matrix maps image pixels → canvas pixels in
                           the bird's-eye output coordinate frame.
            canvas_params: Dict with keys 'scale_px_per_m', 'canvas_w', 'canvas_h',
                           'origin_x', 'origin_y' (mower position on canvas).
        """
        path = self._homographies_path()
        save_dict = {}
        for name, H in homographies.items():
            save_dict[f"H_{name}"] = H
        for key, val in canvas_params.items():
            save_dict[f"param_{key}"] = np.array([val])

        np.savez_compressed(path, **save_dict)
        logger.info(
            "Homographies saved for cameras: %s", list(homographies.keys())
        )

    def load_homographies(
        self,
    ) -> Optional[Tuple[Dict[str, np.ndarray], Dict[str, float]]]:
        """
        Loads saved ground-plane homography matrices.

        Returns:
            Tuple of (homographies dict, canvas_params dict), or None if
            no homography file exists.
        """
        path = self._homographies_path() + ".npz"
        if not os.path.exists(path):
            logger.debug("No homography calibration found for unit %s.", self._unit_id)
            return None

        data = np.load(path)
        homographies: Dict[str, np.ndarray] = {}
        canvas_params: Dict[str, float] = {}

        for key in data.files:
            if key.startswith("H_"):
                cam_name = key[2:]
                homographies[cam_name] = data[key]
            elif key.startswith("param_"):
                param_name = key[6:]
                canvas_params[param_name] = float(data[key][0])

        logger.info(
            "Homographies loaded for cameras: %s", list(homographies.keys())
        )
        return homographies, canvas_params

    def has_homographies(self) -> bool:
        """True if saved homography data exists for this unit."""
        return os.path.exists(self._homographies_path() + ".npz")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _intrinsic_path(self, camera_name: str) -> str:
        return os.path.join(self._unit_dir, f"{camera_name}_intrinsic")

    def _homographies_path(self) -> str:
        return os.path.join(self._unit_dir, "homographies")

    def __repr__(self) -> str:
        return f"CalibrationStore(unit={self._unit_id!r}, dir={self._unit_dir!r})"
