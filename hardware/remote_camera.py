"""
River Vector - Remote Camera Manager

A drop-in replacement for hardware.cameras.CameraManager used when this
(control) node does NOT own the `vision` role — i.e. the cameras are wired
to a separate vision node (the Pi 4) reached over the internal LAN.

It mirrors the public surface of CameraManager that the rest of the stack
calls (capture, capture_undistorted, detect_aruco, release, calibration
queries) but satisfies each call with an HTTP request to the vision node's
snapshot/ArUco API (see vision/node.py).

Failure philosophy matches the rest of the hardware layer: if the vision
peer is unreachable or returns an error, we degrade exactly like a camera
that failed to read — an invalid CameraFrame / None detection — rather than
raising. The control node keeps running; vision-dependent features (obstacle
assist, ArUco docking) simply report "no data", which the autonomy/safety
layers already handle.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import requests

from core.constants import CAMERA_NAMES
from hardware.cameras import ArucoDetection, CameraFrame

logger = logging.getLogger(__name__)

# Short timeouts: cameras live on the same LAN segment inside the enclosure.
# A slow/missing vision node must not stall the real-time control loop.
_CONNECT_TIMEOUT_S = 1.0
_READ_TIMEOUT_S = 2.0


def _camera_name(camera_id: int) -> str:
    if 0 <= camera_id < len(CAMERA_NAMES):
        return CAMERA_NAMES[camera_id]
    return f"cam{camera_id}"


class RemoteCameraManager:
    """
    Camera access over HTTP against a peer vision node.

    Args:
        base_url: Base URL of the vision node, e.g. "http://10.55.0.2:8090".
    """

    def __init__(self, base_url: str) -> None:
        self._base = base_url.rstrip("/")
        self._session = requests.Session()
        self._calibrated = False
        self._has_bird_eye = False
        self._refresh_capabilities()
        logger.info("RemoteCameraManager: vision peer at %s.", self._base)

    # ── Capabilities ────────────────────────────────────────────────────

    def _refresh_capabilities(self) -> None:
        """Best-effort one-shot query of the vision node's calibration state."""
        try:
            resp = self._session.get(
                f"{self._base}/cameras",
                timeout=(_CONNECT_TIMEOUT_S, _READ_TIMEOUT_S),
            )
            if resp.ok:
                body = resp.json()
                self._calibrated = bool(body.get("calibrated", False))
                self._has_bird_eye = bool(body.get("bird_eye", False))
        except (requests.RequestException, ValueError) as exc:
            logger.warning("RemoteCameraManager: vision peer unreachable (%s).", exc)

    # ── Frame capture ───────────────────────────────────────────────────

    def capture(self, camera_id: int) -> CameraFrame:
        """Fetches a raw snapshot from the vision node. Mirrors CameraManager.capture."""
        return self._fetch_frame(camera_id, undistort=False)

    def capture_undistorted(self, camera_id: int) -> CameraFrame:
        """Fetches an undistorted snapshot. Mirrors CameraManager.capture_undistorted."""
        return self._fetch_frame(camera_id, undistort=True)

    def _fetch_frame(self, camera_id: int, undistort: bool) -> CameraFrame:
        name = _camera_name(camera_id)
        try:
            resp = self._session.get(
                f"{self._base}/camera/{camera_id}/snapshot",
                params={"undistort": "1" if undistort else "0"},
                timeout=(_CONNECT_TIMEOUT_S, _READ_TIMEOUT_S),
            )
            if not resp.ok:
                logger.debug("Vision peer camera %d → HTTP %s.", camera_id, resp.status_code)
                return CameraFrame(camera_id=camera_id, camera_name=name, is_valid=False)
            data = self._decode_jpeg(resp.content)
            if data is None:
                return CameraFrame(camera_id=camera_id, camera_name=name, is_valid=False)
            return CameraFrame(camera_id=camera_id, camera_name=name, data=data, is_valid=True)
        except requests.RequestException as exc:
            logger.debug("Vision peer camera %d fetch failed: %s", camera_id, exc)
            return CameraFrame(camera_id=camera_id, camera_name=name, is_valid=False)

    @staticmethod
    def _decode_jpeg(payload: bytes) -> Optional[np.ndarray]:
        try:
            import cv2
            arr = np.frombuffer(payload, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            return img if img is not None and img.size else None
        except Exception as exc:  # cv2 missing or decode failure
            logger.debug("JPEG decode failed: %s", exc)
            return None

    # ── ArUco (docking) ─────────────────────────────────────────────────

    def detect_aruco(self, camera_id: int, target_id: int) -> Optional[ArucoDetection]:
        """
        Asks the vision node to detect a marker. Detection runs server-side
        (where the camera and calibration live); we receive the result.

        Returns an ArucoDetection if found, else None — same contract as
        CameraManager.detect_aruco, so autonomy/return_home is unaffected.
        """
        try:
            resp = self._session.get(
                f"{self._base}/camera/{camera_id}/aruco",
                params={"target_id": target_id},
                timeout=(_CONNECT_TIMEOUT_S, _READ_TIMEOUT_S),
            )
            if not resp.ok:
                return None
            body = resp.json()
            if not body.get("detected"):
                return None
            corners_list = body.get("corners")
            corners = (
                np.asarray(corners_list, dtype=np.float32)
                if corners_list is not None
                else None
            )
            return ArucoDetection(
                marker_id=int(body.get("marker_id", target_id)),
                corners=corners,
                detected=True,
                camera_id=camera_id,
            )
        except (requests.RequestException, ValueError) as exc:
            logger.debug("Vision peer ArUco detect failed: %s", exc)
            return None

    # ── Calibration / composite (queried, not computed locally) ─────────

    def load_calibration(self, store) -> None:
        """No-op: calibration lives on the vision node. Present for API parity."""
        logger.debug("RemoteCameraManager.load_calibration is a no-op (peer owns calibration).")

    @property
    def is_calibrated(self) -> bool:
        return self._calibrated

    @property
    def has_bird_eye(self) -> bool:
        return self._has_bird_eye

    def bird_eye_view(self) -> Optional[np.ndarray]:
        """Fetches the composite bird's-eye image if the peer produces one."""
        try:
            resp = self._session.get(
                f"{self._base}/bird_eye",
                timeout=(_CONNECT_TIMEOUT_S, _READ_TIMEOUT_S),
            )
            if not resp.ok:
                return None
            return self._decode_jpeg(resp.content)
        except requests.RequestException:
            return None

    # ── Lifecycle ───────────────────────────────────────────────────────

    def release(self) -> None:
        """Closes the HTTP session. Cameras themselves are released by the peer."""
        try:
            self._session.close()
        except Exception:
            pass
