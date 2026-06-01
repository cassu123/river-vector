"""
River Vector - Vision Node

The server side of a SPLIT compute topology. Runs on the node that owns the
`vision` role (the Pi 4 on Voyager) and exposes the cameras + CV to the
control node (the Pi 5) over the internal LAN.

It is a thin HTTP wrapper around hardware.cameras.CameraManager:

    GET /health                          → {status, role, cameras, calibrated}
    GET /cameras                         → {count, calibrated, bird_eye}
    GET /camera/{id}/snapshot?undistort= → image/jpeg
    GET /camera/{id}/aruco?target_id=    → {detected, marker_id, corners}
    GET /bird_eye                        → image/jpeg (if composite available)

The control node reaches these via hardware.remote_camera.RemoteCameraManager.

Run as:  python3 -m vision.node            (reads /etc/river-vector/bootstrap.json)
         python3 -m vision.node --port 8090 --sim

Provisioned by scripts/install.sh as the `river-vector-vision.service` unit on
nodes whose compute role is `vision`. Uses only stdlib http.server — no extra
web framework — matching connectivity/claim_server.py.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from core.compute_topology import DEFAULT_VISION_PORT, ROLE_VISION
from hardware.cameras import CameraManager

logger = logging.getLogger("river_vector.vision")

_SNAPSHOT_RE = re.compile(r"^/camera/(\d+)/snapshot$")
_ARUCO_RE = re.compile(r"^/camera/(\d+)/aruco$")


def _encode_jpeg(frame_data, quality: int = 80):
    """Encodes a BGR numpy frame to JPEG bytes, or None on failure."""
    if frame_data is None:
        return None
    try:
        import cv2
        ok, buf = cv2.imencode(".jpg", frame_data, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return buf.tobytes() if ok else None
    except Exception as exc:  # cv2 missing or encode failure
        logger.debug("JPEG encode failed: %s", exc)
        return None


class _VisionHandler(BaseHTTPRequestHandler):
    # Injected by the bound subclass in VisionNode.start().
    cameras: CameraManager = None  # type: ignore[assignment]

    server_version = "RiverVectorVision/0.1"

    def log_message(self, fmt, *args):  # quieter than the default stderr spam
        logger.debug("vision-http: " + fmt, *args)

    # ── helpers ─────────────────────────────────────────────────────────

    def _send_json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_jpeg(self, payload) -> None:
        if not payload:
            self._send_json(503, {"error": "no_frame"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # ── routing ─────────────────────────────────────────────────────────

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/health":
            self._send_json(200, {
                "status": "ok",
                "role": ROLE_VISION,
                "cameras": True,
                "calibrated": self.cameras.is_calibrated,
            })
            return

        if path == "/cameras":
            self._send_json(200, {
                "calibrated": self.cameras.is_calibrated,
                "bird_eye": self.cameras.has_bird_eye,
            })
            return

        m = _SNAPSHOT_RE.match(path)
        if m:
            cam_id = int(m.group(1))
            undistort = query.get("undistort", ["0"])[0] == "1"
            frame = (
                self.cameras.capture_undistorted(cam_id)
                if undistort else self.cameras.capture(cam_id)
            )
            if not frame.is_valid:
                self._send_json(503, {"error": "capture_failed", "camera": cam_id})
                return
            self._send_jpeg(_encode_jpeg(frame.data))
            return

        m = _ARUCO_RE.match(path)
        if m:
            cam_id = int(m.group(1))
            try:
                target_id = int(query.get("target_id", ["0"])[0])
            except ValueError:
                target_id = 0
            detection = self.cameras.detect_aruco(cam_id, target_id)
            if detection is None or not detection.detected:
                self._send_json(200, {"detected": False, "marker_id": target_id})
                return
            corners = detection.corners.tolist() if detection.corners is not None else None
            self._send_json(200, {
                "detected": True,
                "marker_id": detection.marker_id,
                "corners": corners,
            })
            return

        if path == "/bird_eye":
            img = self.cameras.bird_eye_view() if self.cameras.has_bird_eye else None
            self._send_jpeg(_encode_jpeg(img))
            return

        self._send_json(404, {"error": "not_found", "path": path})


class VisionNode:
    """Owns the CameraManager and serves it over HTTP."""

    def __init__(self, host: str = "0.0.0.0", port: int = DEFAULT_VISION_PORT,
                 sim: bool = False) -> None:
        self._host = host
        self._port = port
        self._sim = sim
        self._cameras = CameraManager(sim_mode=sim)
        self._httpd = None

    def start(self) -> None:
        cameras = self._cameras

        class _Bound(_VisionHandler):
            pass
        _Bound.cameras = cameras

        self._httpd = ThreadingHTTPServer((self._host, self._port), _Bound)
        logger.info("Vision node serving on %s:%d (sim=%s).",
                    self._host, self._port, self._sim)
        try:
            self._httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        # Idempotent: stop() is invoked both from start()'s finally and by the
        # caller. Grab-and-clear so a concurrent second call is a no-op.
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            httpd.server_close()
            try:
                self._cameras.release()
            except Exception:
                pass


def _detect_sim() -> bool:
    """Default to sim mode off Raspberry Pi / when no camera devices exist."""
    import os
    return not any(os.path.exists(f"/dev/video{i}") for i in range(6))


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="River Vector vision node")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_VISION_PORT)
    parser.add_argument("--sim", action="store_true", help="Force simulation mode")
    args = parser.parse_args(argv)

    sim = args.sim or _detect_sim()
    node = VisionNode(host=args.host, port=args.port, sim=sim)
    node.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
