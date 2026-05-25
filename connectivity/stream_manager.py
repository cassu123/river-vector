"""
================================================================================
Project:  River Vector — Autonomous Mower Control System
File:     connectivity/stream_manager.py
Purpose:  On-demand camera stream manager. Starts and stops MJPEG or WebRTC
          streams for remote viewing via River Song dashboard. Streams are
          only active when explicitly requested to conserve bandwidth.
Author:   [Author Placeholder]
Version:  0.1.0
Date:     2026-05-25
================================================================================
"""

import logging
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Default stream port base — camera 0 = 8080, camera 1 = 8081, etc.
STREAM_PORT_BASE: int = 8080
STREAM_QUALITY: int = 70        # JPEG quality 0–100
STREAM_FPS: int = 15            # Stream frame rate (lower than capture FPS)


@dataclass
class StreamInfo:
    """Information about an active camera stream."""
    camera_id: int
    port: int
    url: str
    active: bool = False
    started_at: Optional[float] = None
    process: Optional[subprocess.Popen] = field(default=None, repr=False)


class StreamManager:
    """
    Manages on-demand MJPEG camera streams.

    Uses mjpg-streamer or a similar tool to serve camera frames over HTTP.
    Streams are started on request from River Song and stopped when no
    longer needed to conserve cellular bandwidth.

    Args:
        camera_manager: CameraManager with open camera devices.
        host: Bind address for stream servers.
        port_base: Base port number (camera N uses port_base + N).
    """

    STREAM_TIMEOUT_SEC: float = 300.0   # Auto-stop streams after 5 minutes of inactivity

    def __init__(
        self,
        camera_manager=None,
        host: str = "0.0.0.0",
        port_base: int = STREAM_PORT_BASE,
    ) -> None:
        self._cameras = camera_manager
        self._host = host
        self._port_base = port_base
        self._streams: Dict[int, StreamInfo] = {}
        self._lock = threading.Lock()
        self._watchdog_thread: Optional[threading.Thread] = None
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Starts the stream watchdog thread."""
        self._running = True
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name="StreamManager",
            daemon=True,
        )
        self._watchdog_thread.start()
        logger.info("Stream manager started.")

    def stop(self) -> None:
        """Stops all active streams and the watchdog thread."""
        self._running = False
        self.stop_all_streams()
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            self._watchdog_thread.join(timeout=5.0)
        logger.info("Stream manager stopped.")

    # ------------------------------------------------------------------
    # Stream control
    # ------------------------------------------------------------------

    def start_stream(self, camera_id: int) -> Optional[StreamInfo]:
        """
        Starts an MJPEG stream for the specified camera.

        Args:
            camera_id: Camera index to stream.

        Returns:
            StreamInfo with the stream URL, or None if start failed.
        """
        with self._lock:
            if camera_id in self._streams and self._streams[camera_id].active:
                logger.debug("Stream for camera %d already active.", camera_id)
                return self._streams[camera_id]

        port = self._port_base + camera_id
        url = f"http://{self._host}:{port}/?action=stream"

        info = StreamInfo(
            camera_id=camera_id,
            port=port,
            url=url,
        )

        try:
            # Launch mjpg-streamer as a subprocess
            # Requires: sudo apt install mjpg-streamer
            proc = subprocess.Popen(
                [
                    "mjpg_streamer",
                    "-i", f"input_uvc.so -d /dev/video{camera_id} "
                          f"-r 1280x720 -f {STREAM_FPS}",
                    "-o", f"output_http.so -p {port} -w /usr/share/mjpg-streamer/www",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            info.process = proc
            info.active = True
            info.started_at = time.time()

            with self._lock:
                self._streams[camera_id] = info

            logger.info(
                "Stream started: camera %d → %s", camera_id, url
            )
            return info

        except FileNotFoundError:
            logger.error(
                "mjpg_streamer not found. Install with: sudo apt install mjpg-streamer"
            )
            return None
        except Exception as exc:
            logger.error("Failed to start stream for camera %d: %s", camera_id, exc)
            return None

    def stop_stream(self, camera_id: int) -> None:
        """
        Stops the stream for the specified camera.

        Args:
            camera_id: Camera index to stop streaming.
        """
        with self._lock:
            info = self._streams.get(camera_id)
            if info and info.active:
                if info.process:
                    info.process.terminate()
                    info.process = None
                info.active = False
                logger.info("Stream stopped: camera %d", camera_id)

    def stop_all_streams(self) -> None:
        """Stops all active camera streams."""
        with self._lock:
            camera_ids = list(self._streams.keys())
        for cam_id in camera_ids:
            self.stop_stream(cam_id)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def active_streams(self) -> List[StreamInfo]:
        """List of currently active stream infos."""
        with self._lock:
            return [s for s in self._streams.values() if s.active]

    def get_stream_url(self, camera_id: int) -> Optional[str]:
        """
        Returns the stream URL for a camera if it's active.

        Args:
            camera_id: Camera index.

        Returns:
            Stream URL string, or None if not streaming.
        """
        with self._lock:
            info = self._streams.get(camera_id)
            return info.url if info and info.active else None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _watchdog_loop(self) -> None:
        """Auto-stops streams that have been running longer than STREAM_TIMEOUT_SEC."""
        while self._running:
            try:
                now = time.time()
                with self._lock:
                    timed_out = [
                        cam_id for cam_id, info in self._streams.items()
                        if info.active
                        and info.started_at is not None
                        and (now - info.started_at) > self.STREAM_TIMEOUT_SEC
                    ]
                for cam_id in timed_out:
                    logger.info(
                        "Stream timeout: auto-stopping camera %d stream.", cam_id
                    )
                    self.stop_stream(cam_id)
            except Exception as exc:
                logger.error("Stream watchdog error: %s", exc)
            time.sleep(30.0)

    def __repr__(self) -> str:
        return (
            f"StreamManager(active={len(self.active_streams)}, "
            f"host={self._host!r})"
        )
