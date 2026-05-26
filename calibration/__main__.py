"""
River Vector - Calibration CLI
Run with:  python3 -m calibration <command> [options]

Commands
--------
intrinsic       Calibrate one camera's lens distortion (indoors, any surface).
extrinsic       Calibrate ground-plane homographies    (outdoors, on the mower).
auto-extrinsic  ArUco yard-marker auto calibration     (outdoors, needs GPS).
preview         Show live bird's-eye stitched view     (requires both calibrations).
status          Show which cameras have saved calibration data.

Examples
--------
    python3 -m calibration intrinsic      --unit VOY-RV-001 --camera front
    python3 -m calibration intrinsic      --unit VOY-RV-001 --camera rear_left
    python3 -m calibration extrinsic      --unit VOY-RV-001
    python3 -m calibration auto-extrinsic --unit VOY-RV-001 --survey fleets/yard_markers.json --lat 40.712950 --lng -74.005900
    python3 -m calibration preview        --unit VOY-RV-001
    python3 -m calibration status         --unit VOY-RV-001
"""

import argparse
import logging
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("calibration")

from core.constants import CAMERA_NAMES
from calibration.store import CalibrationStore
from calibration.intrinsic import IntrinsicCalibrator, MIN_SAMPLES, RMS_GOOD_THRESHOLD
from calibration.extrinsic import ExtrinsicCalibrator
from calibration.stitcher import BirdEyeStitcher
from calibration.auto_extrinsic import (
    AutoExtrinsicCalibrator,
    YardMarkerSurvey,
    MIN_MARKERS_PER_CAMERA,
)


# ------------------------------------------------------------------
# Intrinsic calibration
# ------------------------------------------------------------------

def cmd_intrinsic(args) -> int:
    """Walks the operator through per-camera checkerboard calibration."""
    try:
        import cv2
    except ImportError:
        print("ERROR: OpenCV (cv2) is required. Install with: pip install opencv-contrib-python")
        return 1

    unit_id    = args.unit
    cam_name   = args.camera
    device_idx = args.device
    store      = CalibrationStore(unit_id)

    print(f"\n=== Intrinsic Calibration — {unit_id} / {cam_name} ===")
    print(f"Camera device index : /dev/video{device_idx}")
    print(f"Target              : {MIN_SAMPLES} checkerboard samples minimum")
    print(f"Checkerboard        : 9×6 inner corners, 25mm squares")
    print()
    print("Instructions:")
    print("  1. Print the 9×6 checkerboard on A3 paper, mount flat on a rigid board.")
    print("  2. Hold it in front of the camera at different angles and distances.")
    print("  3. Press SPACE to capture a sample when the board is clearly detected")
    print("     (green corners appear). Press ESC to finish and compute calibration.")
    print()
    input("Press ENTER when ready...")

    cap = cv2.VideoCapture(device_idx)
    if not cap.isOpened():
        print(f"ERROR: Cannot open camera device {device_idx}.")
        return 1

    calibrator = IntrinsicCalibrator(cam_name)
    last_capture_time = 0.0
    capture_cooldown = 1.5  # seconds between auto-captures

    print("Live feed open — move the checkerboard around. Close window or press ESC to finish.\n")

    while True:
        ret, frame = cap.read()
        if not ret:
            print("ERROR: Frame read failed.")
            break

        # Try to detect and auto-capture (throttled)
        now = time.time()
        found = False
        if now - last_capture_time > capture_cooldown and not calibrator.is_ready:
            found, annotated = calibrator.collect_sample(frame)
            if found:
                last_capture_time = now
                display = annotated
            else:
                display = frame.copy()
        else:
            found, annotated = calibrator.collect_sample(frame)
            display = annotated if found else frame.copy()

        # Overlay progress
        n = calibrator.sample_count
        status = f"Samples: {n}/{MIN_SAMPLES}  {'READY — press ESC to calibrate' if calibrator.is_ready else 'Keep moving the board'}"
        colour = (0, 255, 0) if calibrator.is_ready else (0, 200, 255)
        cv2.putText(display, status, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, colour, 2)

        cv2.imshow(f"Intrinsic calibration — {cam_name}", display)
        key = cv2.waitKey(1) & 0xFF

        if key == 27:  # ESC
            break
        elif key == ord(' ') and calibrator.is_ready:
            break

    cap.release()
    cv2.destroyAllWindows()

    if not calibrator.is_ready:
        print(f"\nNot enough samples ({calibrator.sample_count}/{MIN_SAMPLES}). Run again.")
        return 1

    print(f"\nComputing calibration from {calibrator.sample_count} samples...")
    try:
        result = calibrator.calibrate()
    except Exception as exc:
        print(f"ERROR: Calibration failed: {exc}")
        return 1

    print(f"\n{'='*50}")
    print(f"  Camera       : {result.camera_name}")
    print(f"  RMS error    : {result.rms_error:.4f} px  ({result.quality_label})")
    print(f"  Samples used : {result.sample_count}")
    print(f"  Resolution   : {result.resolution[0]}×{result.resolution[1]}")
    print(f"  Focal length : fx={result.camera_matrix[0,0]:.1f}  fy={result.camera_matrix[1,1]:.1f}")

    if result.rms_error > RMS_GOOD_THRESHOLD:
        print(f"\nWARNING: RMS {result.rms_error:.4f}px is above {RMS_GOOD_THRESHOLD}px.")
        print("  Try again with better lighting, a flatter board, and more varied angles.")

    store.save_intrinsic(
        result.camera_name,
        result.camera_matrix,
        result.dist_coeffs,
        result.rms_error,
        result.resolution,
    )
    print(f"\nCalibration saved to calibration_data/{unit_id}/{cam_name}_intrinsic.npz")
    return 0


# ------------------------------------------------------------------
# Extrinsic calibration
# ------------------------------------------------------------------

def cmd_extrinsic(args) -> int:
    """Walks the operator through ground-plane homography calibration."""
    try:
        import cv2
    except ImportError:
        print("ERROR: OpenCV (cv2) is required.")
        return 1

    unit_id    = args.unit
    store      = CalibrationStore(unit_id)
    calibrator = ExtrinsicCalibrator()
    device_base = args.device_base

    # Build list of (camera_name, device_index) from profile or defaults
    cameras = list(enumerate(CAMERA_NAMES))  # (device_offset, name)

    print(f"\n=== Extrinsic / Ground-Plane Calibration — {unit_id} ===")
    print()
    print("Physical setup:")
    print("  1. Park the mower on flat ground.")
    print("  2. Place a 1.0m × 1.0m square mat flat on the ground,")
    print("     centred 1.5m in front of the mower (rear edge at 1.0m,")
    print("     front edge at 2.0m from mower centre).")
    print("  3. Mark the four corners clearly (bright tape or paint).")
    print()
    print("For each camera, you will click the four corners in order:")
    print("  [1] rear-left  [2] rear-right  [3] front-right  [4] front-left")
    print()
    input("Press ENTER when the mat is in position...")

    results = {}
    for dev_offset, cam_name in cameras:
        dev_idx = device_base + dev_offset

        # Check intrinsic calibration exists — warn but don't block
        cal = store.load_intrinsic(cam_name)
        if cal is None:
            print(f"\nWARNING: No intrinsic calibration for '{cam_name}'. "
                  "Run 'intrinsic' first for best results. Continuing anyway.")

        cap = cv2.VideoCapture(dev_idx)
        if not cap.isOpened():
            print(f"  Skipping '{cam_name}' — device {dev_idx} not available.")
            continue

        print(f"\n--- Camera: {cam_name} (device {dev_idx}) ---")
        ret, frame = cap.read()
        cap.release()

        if not ret:
            print(f"  ERROR: Could not read frame from '{cam_name}'.")
            continue

        # Undistort if calibration is available
        if cal is not None:
            import cv2 as cv
            frame = cv.undistort(frame, cal["camera_matrix"], cal["dist_coeffs"])

        try:
            result = calibrator.compute_homography_from_clicks(cam_name, frame)
            results[cam_name] = result
            print(f"  Homography computed — RMS={result.rms_error:.2f}px")
        except RuntimeError as exc:
            print(f"  Cancelled: {exc}")

    if not results:
        print("\nNo homographies computed — exiting.")
        return 1

    cv2.destroyAllWindows()

    store.save_homographies(calibrator.homographies, calibrator.canvas_params)
    print(f"\nHomographies saved for: {list(results.keys())}")
    print(f"Saved to calibration_data/{unit_id}/homographies.npz")
    return 0


# ------------------------------------------------------------------
# Auto ArUco extrinsic calibration
# ------------------------------------------------------------------

def cmd_auto_extrinsic(args) -> int:
    """Runs ArUco yard-marker auto extrinsic calibration."""
    try:
        import cv2
    except ImportError:
        print("ERROR: OpenCV (cv2) is required. Install with: pip install opencv-contrib-python")
        return 1

    unit_id     = args.unit
    survey_path = args.survey
    mower_lat   = args.lat
    mower_lng   = args.lng
    store       = CalibrationStore(unit_id)

    print(f"\n=== Auto Extrinsic Calibration — {unit_id} ===")
    print(f"Survey file    : {survey_path}")
    print(f"Mower position : lat={mower_lat:.6f}  lng={mower_lng:.6f}")
    print()
    print("Requirements:")
    print(f"  • ≥{MIN_MARKERS_PER_CAMERA} yard markers visible per camera")
    print("  • Markers flat on the ground (height_m = 0.0)")
    print("  • RTK GPS position provided via --lat / --lng")
    print()

    # Load survey
    try:
        survey = YardMarkerSurvey(survey_path)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        return 1

    print(f"Survey loaded  : {len(survey)} markers (IDs: {survey.marker_ids})")
    print()

    # Build camera manager — sim mode if no devices
    from hardware.cameras import CameraManager
    cam_mgr = CameraManager(sim_mode=args.sim)
    cam_mgr.load_calibration(store)

    calibrator = AutoExtrinsicCalibrator(survey=survey, store=store)

    input("Press ENTER to start scanning cameras...")

    result = calibrator.calibrate_all(cam_mgr, mower_lat, mower_lng)

    print(f"\n{'='*50}")
    if result.success:
        print(f"  Calibrated cameras : {result.calibrated_cameras}")
        print(f"  Skipped cameras    : {result.skipped_cameras}")
        print(f"  Markers detected   : {result.markers_detected}")
        print(f"\nHomographies saved to calibration_data/{unit_id}/")
        return 0
    else:
        print("  No cameras had enough visible markers.")
        print(f"  Need ≥{MIN_MARKERS_PER_CAMERA} yard markers visible per camera.")
        print("  Check that markers are deployed and RTK position is accurate.")
        return 1


# ------------------------------------------------------------------
# Preview — live bird's-eye view
# ------------------------------------------------------------------

def cmd_preview(args) -> int:
    """Shows a live bird's-eye stitched view using saved calibration."""
    try:
        import cv2
    except ImportError:
        print("ERROR: OpenCV (cv2) is required.")
        return 1

    unit_id = args.unit
    store   = CalibrationStore(unit_id)

    hom_data = store.load_homographies()
    if hom_data is None:
        print("ERROR: No homography calibration found. Run 'extrinsic' first.")
        return 1

    homographies, canvas_params = hom_data
    stitcher = BirdEyeStitcher(homographies, canvas_params)

    # Open cameras
    caps = {}
    for i, cam_name in enumerate(CAMERA_NAMES):
        dev_idx = args.device_base + i
        cap = cv2.VideoCapture(dev_idx)
        if cap.isOpened():
            caps[cam_name] = cap
        else:
            logger.warning("Camera '%s' (device %d) not available.", cam_name, dev_idx)

    if not caps:
        print("ERROR: No cameras available for preview.")
        return 1

    print(f"\nShowing bird's-eye preview ({len(caps)} cameras). Press ESC to quit.\n")

    while True:
        frames = {}
        for cam_name, cap in caps.items():
            ret, frame = cap.read()
            if ret:
                cal = store.load_intrinsic(cam_name)
                if cal is not None:
                    frame = cv2.undistort(frame, cal["camera_matrix"], cal["dist_coeffs"])
                frames[cam_name] = frame

        view = stitcher.stitch(frames, draw_mower=True)
        cv2.imshow(f"Bird's-Eye View — {unit_id}", view)

        if cv2.waitKey(1) & 0xFF == 27:
            break

    for cap in caps.values():
        cap.release()
    cv2.destroyAllWindows()
    return 0


# ------------------------------------------------------------------
# Status
# ------------------------------------------------------------------

def cmd_status(args) -> int:
    """Shows calibration status for each camera."""
    unit_id = args.unit
    store   = CalibrationStore(unit_id)

    print(f"\n=== Calibration Status — {unit_id} ===\n")
    print(f"  {'Camera':<14} {'Intrinsic':<20} {'RMS'}")
    print(f"  {'-'*14} {'-'*20} {'-'*10}")

    for cam_name in CAMERA_NAMES:
        if store.has_intrinsic(cam_name):
            cal = store.load_intrinsic(cam_name)
            rms = f"{cal['rms_error']:.4f} px"
            q = "EXCELLENT" if cal["rms_error"] < 0.5 else "ACCEPTABLE" if cal["rms_error"] < 1.0 else "POOR"
            print(f"  {cam_name:<14} {'CALIBRATED':<20} {rms}  ({q})")
        else:
            print(f"  {cam_name:<14} {'MISSING':<20} —")

    print()
    hom = "CALIBRATED" if store.has_homographies() else "MISSING"
    print(f"  Ground-plane homographies : {hom}")
    print()
    return 0


# ------------------------------------------------------------------
# Argument parsing and dispatch
# ------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m calibration",
        description="River Vector camera calibration toolkit",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # intrinsic
    p_int = sub.add_parser("intrinsic", help="Per-camera lens calibration")
    p_int.add_argument("--unit",   required=True, help="Unit ID (e.g. VOY-RV-001)")
    p_int.add_argument("--camera", required=True, choices=CAMERA_NAMES, help="Camera name")
    p_int.add_argument("--device", type=int, default=0, help="OS camera device index")

    # extrinsic
    p_ext = sub.add_parser("extrinsic", help="Ground-plane homography calibration")
    p_ext.add_argument("--unit",        required=True, help="Unit ID")
    p_ext.add_argument("--device-base", type=int, default=0, dest="device_base",
                       help="Base OS device index (camera 0 = base + 0, etc.)")

    # auto-extrinsic
    p_aex = sub.add_parser("auto-extrinsic", help="ArUco yard-marker auto extrinsic calibration")
    p_aex.add_argument("--unit",   required=True, help="Unit ID (e.g. VOY-RV-001)")
    p_aex.add_argument("--survey", required=True, help="Path to yard_markers.json survey file")
    p_aex.add_argument("--lat",    required=True, type=float, help="Mower latitude (decimal degrees)")
    p_aex.add_argument("--lng",    required=True, type=float, help="Mower longitude (decimal degrees)")
    p_aex.add_argument("--sim",    action="store_true", default=False,
                       help="Force simulation mode (no real cameras)")

    # preview
    p_pre = sub.add_parser("preview", help="Live bird's-eye view preview")
    p_pre.add_argument("--unit",        required=True, help="Unit ID")
    p_pre.add_argument("--device-base", type=int, default=0, dest="device_base")

    # status
    p_sta = sub.add_parser("status", help="Show calibration data status")
    p_sta.add_argument("--unit", required=True, help="Unit ID")

    args = parser.parse_args()

    dispatch = {
        "intrinsic":      cmd_intrinsic,
        "extrinsic":      cmd_extrinsic,
        "auto-extrinsic": cmd_auto_extrinsic,
        "preview":        cmd_preview,
        "status":         cmd_status,
    }
    return dispatch[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
