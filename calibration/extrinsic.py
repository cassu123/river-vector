"""
River Vector - Extrinsic / Ground-Plane Homography Calibration
Computes the 3×3 homography that maps each camera's image pixels to a
common top-down (bird's-eye) canvas measured in metres from mower centre.

Physical setup required
-----------------------
Place a flat calibration mat on level ground beneath/around the mower.
The mat has a known rectangle marked on it — default 1.0m × 1.0m.
The operator clicks the four corners of that rectangle in each camera's
live view. The homography is solved from those 4-point correspondences.

Canvas coordinate system
------------------------
    Origin  : mower centre
    +X      : mower right
    +Y      : mower forward
    Units   : metres
    Pixels  : scale_px_per_m pixels per metre (default 100 → 1px = 1cm)
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# Default calibration mat in world coordinates (metres, relative to mower centre)
# A 1m×1m square centred 1.5m in front of the mower
DEFAULT_MAT_WORLD_CORNERS = np.array([
    [-0.5,  1.0],   # rear-left  corner of mat
    [ 0.5,  1.0],   # rear-right corner of mat
    [ 0.5,  2.0],   # front-right corner of mat
    [-0.5,  2.0],   # front-left  corner of mat
], dtype=np.float32)

# Canvas parameters
DEFAULT_SCALE_PX_PER_M: float = 100.0   # 100 px per metre → 1px = 1cm
DEFAULT_CANVAS_W: int = 800             # 8m wide  at 100px/m
DEFAULT_CANVAS_H: int = 800             # 8m tall  at 100px/m
DEFAULT_ORIGIN_X: float = 400.0         # Mower centre on canvas (pixels)
DEFAULT_ORIGIN_Y: float = 400.0


@dataclass
class HomographyResult:
    """
    Ground-plane homography for one camera.

    H maps image pixel (u, v, 1) → canvas pixel (cx, cy, w) via:
        [cx, cy, w] = H @ [u, v, 1]
        canvas_x = cx / w,  canvas_y = cy / w
    """
    camera_name: str
    H: np.ndarray                          # 3×3 homography matrix
    image_corners: np.ndarray              # 4 image-space corner points used
    world_corners: np.ndarray              # 4 world-space corner points
    rms_error: float = 0.0                 # Corner reprojection error in pixels


class ExtrinsicCalibrator:
    """
    Computes ground-plane homographies for each camera via 4-point correspondence.

    The operator marks the four corners of a known physical mat in each
    camera's live image. cv2.getPerspectiveTransform() solves the homography
    from those 4 correspondences and the known world positions.

    Args:
        canvas_w:        Output bird's-eye canvas width in pixels.
        canvas_h:        Output bird's-eye canvas height in pixels.
        scale_px_per_m:  Pixels per metre on the output canvas.
        origin_x:        Mower centre X position on canvas (pixels).
        origin_y:        Mower centre Y position on canvas (pixels).
        mat_world:       4×2 array of mat corner world coords (metres, XY).
    """

    def __init__(
        self,
        canvas_w: int = DEFAULT_CANVAS_W,
        canvas_h: int = DEFAULT_CANVAS_H,
        scale_px_per_m: float = DEFAULT_SCALE_PX_PER_M,
        origin_x: float = DEFAULT_ORIGIN_X,
        origin_y: float = DEFAULT_ORIGIN_Y,
        mat_world: Optional[np.ndarray] = None,
    ) -> None:
        self._canvas_w = canvas_w
        self._canvas_h = canvas_h
        self._scale = scale_px_per_m
        self._origin_x = origin_x
        self._origin_y = origin_y
        self._mat_world = mat_world if mat_world is not None else DEFAULT_MAT_WORLD_CORNERS
        self._results: Dict[str, HomographyResult] = {}

    # ------------------------------------------------------------------
    # Homography computation
    # ------------------------------------------------------------------

    def compute_homography(
        self,
        camera_name: str,
        image_corners: np.ndarray,
    ) -> HomographyResult:
        """
        Computes the ground-plane homography for one camera.

        Args:
            camera_name:   Camera slot name (e.g. 'front').
            image_corners: 4×2 float32 array of pixel coordinates corresponding
                           to the four mat corners in the same order as mat_world.
                           Order must match: [rear-left, rear-right, front-right, front-left]

        Returns:
            HomographyResult with 3×3 homography matrix H.

        Raises:
            ValueError: If image_corners does not have shape (4, 2).
            ImportError: If cv2 is unavailable.
        """
        import cv2

        if image_corners.shape != (4, 2):
            raise ValueError(
                f"image_corners must have shape (4, 2), got {image_corners.shape}."
            )

        # Convert world metres to canvas pixels
        canvas_corners = self._world_to_canvas(self._mat_world)

        H, mask = cv2.findHomography(
            image_corners.astype(np.float32),
            canvas_corners.astype(np.float32),
            method=0,  # Least-squares (exact 4-point solution)
        )

        if H is None:
            raise RuntimeError(
                f"cv2.findHomography failed for camera '{camera_name}'. "
                "Check that corner order matches mat orientation."
            )

        # Measure reprojection error
        rms = self._reprojection_error(H, image_corners, canvas_corners)

        result = HomographyResult(
            camera_name=camera_name,
            H=H,
            image_corners=image_corners.copy(),
            world_corners=self._mat_world.copy(),
            rms_error=rms,
        )
        self._results[camera_name] = result
        logger.info(
            "Homography computed for '%s' — RMS=%.2fpx.", camera_name, rms
        )
        return result

    def compute_homography_from_clicks(
        self,
        camera_name: str,
        frame: np.ndarray,
        window_title: Optional[str] = None,
    ) -> HomographyResult:
        """
        Interactive: opens a window for the operator to click the four mat corners.

        Click order: rear-left → rear-right → front-right → front-left
        (matching DEFAULT_MAT_WORLD_CORNERS row order)

        Args:
            camera_name:  Camera slot name.
            frame:        BGR image from the camera (already undistorted).
            window_title: OpenCV window title.

        Returns:
            HomographyResult.
        """
        import cv2

        title = window_title or f"Click 4 corners — {camera_name}"
        instructions = (
            "Click: [1] rear-left  [2] rear-right  [3] front-right  [4] front-left"
        )
        clicks: List[Tuple[int, int]] = []

        display = frame.copy()
        cv2.putText(
            display, instructions, (10, 30),
            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2,
        )

        corner_labels = ["rear-left", "rear-right", "front-right", "front-left"]

        def _on_click(event, x, y, flags, param):
            if event == cv2.EVENT_LBUTTONDOWN and len(clicks) < 4:
                clicks.append((x, y))
                label = corner_labels[len(clicks) - 1]
                cv2.circle(display, (x, y), 6, (0, 255, 0), -1)
                cv2.putText(
                    display, label, (x + 8, y - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1,
                )
                logger.info("Corner %d/%d: %s at (%d, %d)", len(clicks), 4, label, x, y)
                cv2.imshow(title, display)

        cv2.namedWindow(title)
        cv2.setMouseCallback(title, _on_click)
        cv2.imshow(title, display)

        logger.info("Waiting for 4 corner clicks on camera '%s'...", camera_name)
        while len(clicks) < 4:
            cv2.imshow(title, display)
            if cv2.waitKey(50) == 27:  # ESC to cancel
                cv2.destroyWindow(title)
                raise RuntimeError("Extrinsic calibration cancelled by operator.")

        cv2.destroyWindow(title)
        image_corners = np.array(clicks, dtype=np.float32)
        return self.compute_homography(camera_name, image_corners)

    # ------------------------------------------------------------------
    # Canvas parameters for store
    # ------------------------------------------------------------------

    @property
    def canvas_params(self) -> Dict[str, float]:
        """Returns canvas parameters for serialisation into CalibrationStore."""
        return {
            "scale_px_per_m": self._scale,
            "canvas_w": float(self._canvas_w),
            "canvas_h": float(self._canvas_h),
            "origin_x": self._origin_x,
            "origin_y": self._origin_y,
        }

    @property
    def homographies(self) -> Dict[str, np.ndarray]:
        """Dict of camera_name → H matrix for all calibrated cameras."""
        return {name: r.H for name, r in self._results.items()}

    @property
    def results(self) -> Dict[str, HomographyResult]:
        return self._results

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _world_to_canvas(self, world_pts: np.ndarray) -> np.ndarray:
        """
        Converts world coordinates (metres, XY) to canvas pixel coordinates.

        World X+ = mower right  → canvas X+ = right
        World Y+ = mower forward → canvas Y+ = up → canvas row decreases
        """
        canvas_pts = np.zeros_like(world_pts)
        canvas_pts[:, 0] = self._origin_x + world_pts[:, 0] * self._scale   # X right
        canvas_pts[:, 1] = self._origin_y - world_pts[:, 1] * self._scale   # Y up (flip)
        return canvas_pts

    @staticmethod
    def _reprojection_error(
        H: np.ndarray,
        src: np.ndarray,
        dst: np.ndarray,
    ) -> float:
        """RMS reprojection error of H mapping src → dst."""
        src_h = np.column_stack([src, np.ones(len(src))])  # Nx3
        proj = (H @ src_h.T).T
        proj[:, 0] /= proj[:, 2]
        proj[:, 1] /= proj[:, 2]
        diff = proj[:, :2] - dst
        return float(np.sqrt(np.mean(diff ** 2)))

    def __repr__(self) -> str:
        return (
            f"ExtrinsicCalibrator(cameras={list(self._results.keys())}, "
            f"canvas={self._canvas_w}×{self._canvas_h}px, "
            f"scale={self._scale}px/m)"
        )
