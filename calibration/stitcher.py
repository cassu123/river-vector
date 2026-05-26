"""
River Vector - Bird's-Eye View Stitcher
Composites undistorted camera frames into a single top-down image using
the ground-plane homography for each camera. Output is centred on the mower.

Output canvas
-------------
    Width  : canvas_w pixels  (default 800 = 8m at 100px/m)
    Height : canvas_h pixels  (default 800 = 8m at 100px/m)
    Centre : mower position   (origin_x, origin_y in pixels)
    +X     : mower right
    +Y     : mower forward (top of image)
    A white rectangle shows the mower footprint at centre.

Blending
--------
Overlapping camera regions are blended using per-pixel weight maps.
Each camera's weight is inversely proportional to distance from the
image centre, so central pixels dominate over edge pixels.
"""

import logging
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# Mower footprint dimensions on canvas (drawn as reference rectangle)
_MOWER_W_M: float = 1.1    # ~42in deck width
_MOWER_H_M: float = 1.8    # approximate mower body length

# Canvas background colour (dark grey — distinguishable from grass and sky)
_BG_COLOR = (30, 30, 30)


class BirdEyeStitcher:
    """
    Composites multiple camera frames into a top-down bird's-eye view.

    Requires ground-plane homography matrices from ExtrinsicCalibrator.
    Each camera's warped view is blended into a shared canvas weighted by
    proximity to the camera's image centre (closer to centre = higher weight).

    Args:
        homographies:   Dict of camera_name → 3×3 numpy homography matrix.
        canvas_params:  Dict with 'scale_px_per_m', 'canvas_w', 'canvas_h',
                        'origin_x', 'origin_y'.
    """

    def __init__(
        self,
        homographies: Dict[str, np.ndarray],
        canvas_params: Dict[str, float],
    ) -> None:
        self._H = homographies
        self._scale = canvas_params.get("scale_px_per_m", 100.0)
        self._canvas_w = int(canvas_params.get("canvas_w", 800))
        self._canvas_h = int(canvas_params.get("canvas_h", 800))
        self._origin_x = canvas_params.get("origin_x", 400.0)
        self._origin_y = canvas_params.get("origin_y", 400.0)
        self._weight_maps: Dict[str, np.ndarray] = {}

    # ------------------------------------------------------------------
    # Stitching
    # ------------------------------------------------------------------

    def stitch(
        self,
        frames: Dict[str, np.ndarray],
        draw_mower: bool = True,
    ) -> np.ndarray:
        """
        Produces a bird's-eye composite from the provided camera frames.

        Args:
            frames:     Dict mapping camera_name → undistorted BGR frame.
                        Cameras not in homographies dict are ignored.
            draw_mower: If True, draws a mower footprint rectangle at centre.

        Returns:
            Bird's-eye composite as a BGR numpy array (canvas_h × canvas_w × 3).
        """
        try:
            import cv2
        except ImportError:
            logger.error("cv2 not available — cannot stitch frames.")
            return self._blank_canvas()

        canvas = self._blank_canvas().astype(np.float32)
        weight_total = np.zeros((self._canvas_h, self._canvas_w), dtype=np.float32)

        for cam_name, H in self._H.items():
            frame = frames.get(cam_name)
            if frame is None:
                continue

            # Warp this camera's frame to the canvas
            warped = cv2.warpPerspective(
                frame.astype(np.float32),
                H,
                (self._canvas_w, self._canvas_h),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=0,
            )

            # Generate or retrieve weight map for this camera
            weight = self._get_weight_map(cam_name, frame.shape[:2], H)

            # Accumulate weighted contributions
            w3 = weight[:, :, np.newaxis]
            canvas += warped * w3
            weight_total += weight

        # Normalise — avoid divide-by-zero in regions with no camera coverage
        mask = weight_total > 0
        weight_total_3 = np.where(mask, weight_total, 1.0)[:, :, np.newaxis]
        canvas = canvas / weight_total_3

        # Fill uncovered regions with background colour
        bg = np.array(_BG_COLOR, dtype=np.float32)
        for c in range(3):
            canvas[:, :, c] = np.where(mask, canvas[:, :, c], bg[c])

        result = canvas.astype(np.uint8)

        if draw_mower:
            self._draw_mower_footprint(result)

        return result

    # ------------------------------------------------------------------
    # Canvas info
    # ------------------------------------------------------------------

    @property
    def canvas_size(self) -> Tuple[int, int]:
        """Canvas dimensions as (width, height) pixels."""
        return (self._canvas_w, self._canvas_h)

    @property
    def scale_px_per_m(self) -> float:
        """Pixels per metre on the output canvas."""
        return self._scale

    def world_to_canvas(self, x_m: float, y_m: float) -> Tuple[int, int]:
        """
        Converts world coordinates (metres from mower centre) to canvas pixels.

        Args:
            x_m: Metres to the right of mower centre.
            y_m: Metres forward of mower centre.

        Returns:
            (col, row) pixel coordinates on the output canvas.
        """
        col = int(self._origin_x + x_m * self._scale)
        row = int(self._origin_y - y_m * self._scale)
        return col, row

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _blank_canvas(self) -> np.ndarray:
        """Returns a blank canvas filled with the background colour."""
        canvas = np.zeros((self._canvas_h, self._canvas_w, 3), dtype=np.uint8)
        canvas[:] = _BG_COLOR
        return canvas

    def _get_weight_map(
        self,
        cam_name: str,
        frame_shape: Tuple[int, int],
        H: np.ndarray,
    ) -> np.ndarray:
        """
        Returns (or computes and caches) a blending weight map for this camera.

        Weight is 1.0 at the image centre, falling off toward edges using a
        2D Gaussian. Warped to canvas space via the homography.

        Args:
            cam_name:    Camera name (used for caching).
            frame_shape: (height, width) of the source frame.
            H:           3×3 homography for this camera.

        Returns:
            Weight map of shape (canvas_h, canvas_w).
        """
        try:
            import cv2
        except ImportError:
            return np.ones((self._canvas_h, self._canvas_w), dtype=np.float32)

        if cam_name in self._weight_maps:
            return self._weight_maps[cam_name]

        fh, fw = frame_shape

        # Build Gaussian weight in image space
        cx, cy = fw / 2.0, fh / 2.0
        sigma_x, sigma_y = fw / 2.5, fh / 2.5
        xs = np.arange(fw, dtype=np.float32)
        ys = np.arange(fh, dtype=np.float32)
        xx, yy = np.meshgrid(xs, ys)
        weight_img = np.exp(
            -((xx - cx) ** 2 / (2 * sigma_x ** 2) + (yy - cy) ** 2 / (2 * sigma_y ** 2))
        ).astype(np.float32)

        # Warp weight map to canvas using same homography
        warped_weight = cv2.warpPerspective(
            weight_img, H,
            (self._canvas_w, self._canvas_h),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0.0,
        )

        self._weight_maps[cam_name] = warped_weight
        return warped_weight

    def _draw_mower_footprint(self, canvas: np.ndarray) -> None:
        """Draws a white outline rectangle representing the mower at centre."""
        try:
            import cv2
        except ImportError:
            return

        half_w = int((_MOWER_W_M / 2) * self._scale)
        half_h = int((_MOWER_H_M / 2) * self._scale)
        ox, oy = int(self._origin_x), int(self._origin_y)

        top_left = (ox - half_w, oy - half_h)
        bot_right = (ox + half_w, oy + half_h)

        cv2.rectangle(canvas, top_left, bot_right, (255, 255, 255), 2)
        cv2.circle(canvas, (ox, oy - half_h + 6), 5, (255, 255, 0), -1)  # front indicator

    def __repr__(self) -> str:
        return (
            f"BirdEyeStitcher(cameras={list(self._H.keys())}, "
            f"canvas={self._canvas_w}×{self._canvas_h}px, "
            f"scale={self._scale}px/m)"
        )
