"""Tests for hardware/remote_camera.py — the split-topology camera client."""

import unittest
from unittest import mock

import numpy as np
import requests

from hardware.remote_camera import RemoteCameraManager


def _resp(ok=True, status=200, content=b"", json_body=None):
    r = mock.Mock()
    r.ok = ok
    r.status_code = status
    r.content = content
    r.json = mock.Mock(return_value=json_body if json_body is not None else {})
    return r


class RemoteCameraTestBase(unittest.TestCase):
    def setUp(self):
        # Patch the Session so construction (which probes /cameras) and all
        # calls hit a mock, never the network.
        patcher = mock.patch("hardware.remote_camera.requests.Session")
        self.addCleanup(patcher.stop)
        self.SessionCls = patcher.start()
        self.session = self.SessionCls.return_value
        self.session.get.return_value = _resp(json_body={"calibrated": False, "bird_eye": False})
        self.mgr = RemoteCameraManager("http://10.55.0.2:8090")


class TestCapture(RemoteCameraTestBase):
    @mock.patch.object(RemoteCameraManager, "_decode_jpeg", return_value=np.zeros((4, 4, 3), np.uint8))
    def test_capture_ok(self, _decode):
        self.session.get.return_value = _resp(content=b"\xff\xd8jpeg")
        frame = self.mgr.capture(0)
        self.assertTrue(frame.is_valid)
        self.assertEqual(frame.camera_id, 0)

    def test_capture_http_error_degrades(self):
        self.session.get.return_value = _resp(ok=False, status=503)
        frame = self.mgr.capture(2)
        self.assertFalse(frame.is_valid)

    def test_capture_network_error_degrades(self):
        self.session.get.side_effect = requests.RequestException("boom")
        frame = self.mgr.capture(1)
        self.assertFalse(frame.is_valid)


class TestAruco(RemoteCameraTestBase):
    def test_aruco_not_detected_returns_none(self):
        self.session.get.return_value = _resp(json_body={"detected": False, "marker_id": 0})
        self.assertIsNone(self.mgr.detect_aruco(3, 0))

    def test_aruco_detected_returns_detection_with_corners(self):
        corners = [[[1.0, 2.0], [3.0, 4.0], [5.0, 6.0], [7.0, 8.0]]]
        self.session.get.return_value = _resp(
            json_body={"detected": True, "marker_id": 7, "corners": corners}
        )
        det = self.mgr.detect_aruco(3, 7)
        self.assertIsNotNone(det)
        self.assertTrue(det.detected)
        self.assertEqual(det.marker_id, 7)
        self.assertEqual(det.camera_id, 3)
        self.assertEqual(det.corners.shape, (1, 4, 2))

    def test_aruco_network_error_returns_none(self):
        self.session.get.side_effect = requests.RequestException("down")
        self.assertIsNone(self.mgr.detect_aruco(3, 0))


class TestLifecycle(RemoteCameraTestBase):
    def test_release_closes_session(self):
        self.mgr.release()
        self.session.close.assert_called_once()

    def test_load_calibration_is_noop(self):
        self.mgr.load_calibration(store=None)  # must not raise


if __name__ == "__main__":
    unittest.main()
