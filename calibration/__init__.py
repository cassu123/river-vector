"""
River Vector - Camera Calibration Package

Intrinsic calibration:  per-camera lens distortion correction
Extrinsic calibration:  camera-to-ground-plane homography (click-based)
Auto extrinsic:         ArUco yard-marker homography (no operator clicks)
Stitcher:               composites all cameras into a bird's-eye top-down view
Display menu:           on-mower Nextion calibration UI state machine

Typical workflow
----------------
Step 1 — calibrate each camera lens (run once per camera, indoors):
    python3 -m calibration intrinsic --unit VOY-RV-001 --camera front

Step 2a — click-based ground-plane calibration (mat + 4 clicks, outdoors):
    python3 -m calibration extrinsic --unit VOY-RV-001

Step 2b — ArUco auto extrinsic (yard markers surveyed, GPS required):
    python3 -m calibration auto-extrinsic --unit VOY-RV-001 --survey fleets/yard_markers.json

Step 3 — verify stitching:
    python3 -m calibration preview --unit VOY-RV-001

Results are saved to  calibration_data/<unit_id>/
and loaded automatically by CameraManager at startup.
"""

from calibration.store import CalibrationStore
from calibration.intrinsic import IntrinsicCalibrator, IntrinsicResult
from calibration.extrinsic import ExtrinsicCalibrator, HomographyResult
from calibration.stitcher import BirdEyeStitcher
from calibration.auto_extrinsic import (
    AutoExtrinsicCalibrator,
    AutoCalibrationResult,
    YardMarkerSurvey,
    YardMarker,
)
from calibration.display_menu import CalibrationDisplayMenu, CalMenuState

__all__ = [
    "CalibrationStore",
    "IntrinsicCalibrator",
    "IntrinsicResult",
    "ExtrinsicCalibrator",
    "HomographyResult",
    "BirdEyeStitcher",
    "AutoExtrinsicCalibrator",
    "AutoCalibrationResult",
    "YardMarkerSurvey",
    "YardMarker",
    "CalibrationDisplayMenu",
    "CalMenuState",
]
