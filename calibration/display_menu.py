"""
River Vector - Nextion Calibration Menu
On-mower calibration UI driven by the Nextion 3.5" touchscreen.

Page layout (must match the .HMI project):
    Page 0 — MAIN      (normal operator status view)
    Page 1 — CAL_MENU  (buttons: INTRINSIC / AUTO EXTRINSIC / STATUS / EXIT)
    Page 2 — CAL_PROG  (camera name, sample counter, RMS, CANCEL button)

Workflow:
    1. Operator navigates to cal menu (e.g. long-press on page 0 or via API).
    2. CalibrationDisplayMenu.enter() switches to page 1 and wires touch handlers.
    3. Touching INTRINSIC → camera select → collects checkerboard samples.
    4. Touching AUTO EXTRINSIC → runs AutoExtrinsicCalibrator (needs GPS).
    5. Touching STATUS → shows saved calibration status for each camera.
    6. Touching EXIT (or CANCEL mid-flow) → returns to page 0.
"""

import logging
import threading
import time
from enum import Enum, auto
from typing import Optional

from calibration.auto_extrinsic import AutoExtrinsicCalibrator, YardMarkerSurvey
from calibration.intrinsic import IntrinsicCalibrator, MIN_SAMPLES, RMS_GOOD_THRESHOLD
from calibration.store import CalibrationStore
from core.constants import CAMERA_NAMES
from hardware.display import (
    CAL_BTN_AUTO,
    CAL_BTN_EXIT,
    CAL_BTN_INTRINSIC,
    CAL_BTN_STATUS,
    CAL_PROG_BTN_CANCEL,
    DisplayManager,
    _PAGE_CAL_MENU,
    _PAGE_CAL_PROG,
)

logger = logging.getLogger(__name__)

# How long to leave a result on screen before returning to the cal menu
_RESULT_LINGER_SEC: float = 4.0


class CalMenuState(Enum):
    IDLE            = auto()
    MAIN_MENU       = auto()
    SELECT_CAMERA   = auto()
    COLLECTING      = auto()
    RESULT          = auto()
    AUTO_EXTRINSIC  = auto()
    STATUS          = auto()
    EXITING         = auto()


class CalibrationDisplayMenu:
    """
    State machine that drives the on-mower Nextion calibration UI.

    Ties together:
    - DisplayManager (Nextion hardware interface)
    - IntrinsicCalibrator  (checkerboard lens calibration)
    - AutoExtrinsicCalibrator (ArUco ground-marker extrinsic calibration)
    - CalibrationStore (persistence)

    All blocking work (frame capture, calibration computation) runs on a
    background thread so the touch handler thread is never blocked.

    Args:
        display:          Connected DisplayManager.
        store:            CalibrationStore for the active unit.
        camera_manager:   CameraManager for frame capture (may be None in sim mode).
        auto_calibrator:  AutoExtrinsicCalibrator (may be None if no survey loaded).
        gps_provider:     Callable[[], Tuple[float, float]] returning (lat, lng),
                          or None if GPS is unavailable.
    """

    def __init__(
        self,
        display: DisplayManager,
        store: CalibrationStore,
        camera_manager=None,
        auto_calibrator: Optional[AutoExtrinsicCalibrator] = None,
        gps_provider=None,
    ) -> None:
        self._display = display
        self._store = store
        self._cams = camera_manager
        self._auto_cal = auto_calibrator
        self._gps = gps_provider
        self._state = CalMenuState.IDLE
        self._worker: Optional[threading.Thread] = None
        self._cancel_flag = threading.Event()
        # Current camera being calibrated (index into CAMERA_NAMES)
        self._active_cam_idx: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enter(self) -> None:
        """
        Enters the calibration menu. Switches the display to the cal-menu
        page and wires all touch handlers.
        """
        if self._state not in (CalMenuState.IDLE, CalMenuState.RESULT, CalMenuState.STATUS):
            logger.warning("CalibrationDisplayMenu.enter() called while state=%s", self._state)
            return

        self._cancel_flag.clear()
        self._wire_menu_handlers()
        self._display.show_cal_menu()
        self._state = CalMenuState.MAIN_MENU
        logger.info("Calibration menu: entered.")

    def exit(self) -> None:
        """
        Exits the calibration menu and returns to the main operator page.
        Cancels any in-progress background work.
        """
        self._cancel_flag.set()
        self._state = CalMenuState.EXITING
        self._display.clear_touch_handlers()
        self._display.show_main_page()
        self._state = CalMenuState.IDLE
        logger.info("Calibration menu: exited.")

    @property
    def state(self) -> CalMenuState:
        return self._state

    # ------------------------------------------------------------------
    # Touch handler wiring
    # ------------------------------------------------------------------

    def _wire_menu_handlers(self) -> None:
        """Registers touch callbacks for the cal-menu page (page 1)."""
        self._display.clear_touch_handlers()
        self._display.register_touch_handler(_PAGE_CAL_MENU, CAL_BTN_INTRINSIC, self._on_intrinsic)
        self._display.register_touch_handler(_PAGE_CAL_MENU, CAL_BTN_AUTO,      self._on_auto_extrinsic)
        self._display.register_touch_handler(_PAGE_CAL_MENU, CAL_BTN_STATUS,    self._on_status)
        self._display.register_touch_handler(_PAGE_CAL_MENU, CAL_BTN_EXIT,      self.exit)

    def _wire_progress_handlers(self) -> None:
        """Registers touch callbacks for the cal-progress page (page 2)."""
        self._display.clear_touch_handlers()
        self._display.register_touch_handler(_PAGE_CAL_PROG, CAL_PROG_BTN_CANCEL, self._on_cancel)

    # ------------------------------------------------------------------
    # Button handlers (called from reader thread — must not block)
    # ------------------------------------------------------------------

    def _on_intrinsic(self) -> None:
        if self._state != CalMenuState.MAIN_MENU:
            return
        self._state = CalMenuState.SELECT_CAMERA
        # Start with camera 0; the background worker cycles through cameras
        self._active_cam_idx = 0
        self._cancel_flag.clear()
        self._wire_progress_handlers()
        self._worker = threading.Thread(
            target=self._run_intrinsic, name="cal-intrinsic", daemon=True
        )
        self._worker.start()
        logger.info("Calibration menu: intrinsic flow started.")

    def _on_auto_extrinsic(self) -> None:
        if self._state != CalMenuState.MAIN_MENU:
            return
        if self._auto_cal is None:
            logger.warning("Auto-extrinsic requested but no calibrator configured.")
            self._display.show_cal_progress("N/A", 0, 1, message="No survey loaded")
            time.sleep(_RESULT_LINGER_SEC)
            self._return_to_menu()
            return

        self._state = CalMenuState.AUTO_EXTRINSIC
        self._cancel_flag.clear()
        self._wire_progress_handlers()
        self._worker = threading.Thread(
            target=self._run_auto_extrinsic, name="cal-auto-ext", daemon=True
        )
        self._worker.start()
        logger.info("Calibration menu: auto-extrinsic flow started.")

    def _on_status(self) -> None:
        if self._state != CalMenuState.MAIN_MENU:
            return
        self._state = CalMenuState.STATUS
        self._show_status()

    def _on_cancel(self) -> None:
        logger.info("Calibration menu: cancel requested.")
        self._cancel_flag.set()

    # ------------------------------------------------------------------
    # Background workers
    # ------------------------------------------------------------------

    def _run_intrinsic(self) -> None:
        """
        Background: steps through each camera and collects checkerboard samples.
        The operator holds the board in front of each camera in turn.
        """
        for cam_idx, cam_name in enumerate(CAMERA_NAMES):
            if self._cancel_flag.is_set():
                break

            self._active_cam_idx = cam_idx
            calibrator = IntrinsicCalibrator(cam_name)
            self._state = CalMenuState.COLLECTING

            self._display.show_cal_progress(
                cam_name, 0, MIN_SAMPLES, message="Hold board in view"
            )

            logger.info("Intrinsic calibration: starting camera '%s'.", cam_name)

            while not calibrator.is_ready and not self._cancel_flag.is_set():
                if self._cams is None:
                    # Sim mode — inject fake samples quickly
                    time.sleep(0.3)
                    calibrator._samples.append(([], []))  # dummy
                    n = calibrator.sample_count
                else:
                    frame = self._cams.capture_undistorted(cam_idx)
                    if not frame.is_valid or frame.data is None:
                        time.sleep(0.2)
                        continue
                    found, _ = calibrator.collect_sample(frame.data)
                    if not found:
                        time.sleep(0.1)
                        continue
                    n = calibrator.sample_count

                self._display.show_cal_progress(cam_name, n, MIN_SAMPLES)

            if self._cancel_flag.is_set():
                self._display.show_cal_result(cam_name, 0.0, "", success=False)
                time.sleep(_RESULT_LINGER_SEC)
                break

            if not calibrator.is_ready:
                continue

            # Compute
            self._display.show_cal_progress(
                cam_name, calibrator.sample_count, MIN_SAMPLES, message="Computing..."
            )
            try:
                result = calibrator.calibrate()
                self._store.save_intrinsic(
                    result.camera_name,
                    result.camera_matrix,
                    result.dist_coeffs,
                    result.rms_error,
                    result.resolution,
                )
                quality = (
                    "EXCELLENT" if result.rms_error < 0.5 else
                    "ACCEPTABLE" if result.rms_error < RMS_GOOD_THRESHOLD else
                    "POOR"
                )
                logger.info(
                    "Intrinsic calibration saved for '%s' — RMS=%.4fpx (%s).",
                    cam_name, result.rms_error, quality,
                )
                self._display.show_cal_result(cam_name, result.rms_error, quality)
            except Exception as exc:
                logger.error("Intrinsic computation failed for '%s': %s", cam_name, exc)
                self._display.show_cal_result(cam_name, 0.0, "", success=False)

            time.sleep(_RESULT_LINGER_SEC)

        self._return_to_menu()

    def _run_auto_extrinsic(self) -> None:
        """Background: runs AutoExtrinsicCalibrator for all cameras."""
        cam_count = len(CAMERA_NAMES)
        self._display.show_cal_progress(
            "ALL CAMERAS", 0, cam_count, message="Getting GPS..."
        )

        if self._gps is None:
            logger.warning("Auto-extrinsic: no GPS provider — cannot run.")
            self._display.show_cal_result("ALL CAMERAS", 0.0, "NO GPS", success=False)
            time.sleep(_RESULT_LINGER_SEC)
            self._return_to_menu()
            return

        try:
            mower_lat, mower_lng = self._gps()
        except Exception as exc:
            logger.error("Auto-extrinsic: GPS read failed: %s", exc)
            self._display.show_cal_result("ALL CAMERAS", 0.0, "GPS ERR", success=False)
            time.sleep(_RESULT_LINGER_SEC)
            self._return_to_menu()
            return

        self._display.show_cal_progress(
            "ALL CAMERAS", 0, cam_count,
            message=f"GPS OK  lat={mower_lat:.5f}"[:30],
        )

        if self._cancel_flag.is_set():
            self._return_to_menu()
            return

        try:
            result = self._auto_cal.calibrate_all(self._cams, mower_lat, mower_lng)
        except Exception as exc:
            logger.error("Auto-extrinsic failed: %s", exc)
            self._display.show_cal_result("ALL CAMERAS", 0.0, "ERROR", success=False)
            time.sleep(_RESULT_LINGER_SEC)
            self._return_to_menu()
            return

        if result.success:
            n_cal = len(result.calibrated_cameras)
            quality = "OK" if n_cal == cam_count else f"{n_cal}/{cam_count}"
            self._display.show_cal_result("AUTO EXTRINSIC", 0.0, quality, success=True)
            self._display.show_cal_progress(
                "DONE",
                n_cal,
                cam_count,
                message=f"Calibrated {n_cal} cameras",
            )
        else:
            self._display.show_cal_result("ALL CAMERAS", 0.0, "NO MARKERS", success=False)

        time.sleep(_RESULT_LINGER_SEC)
        self._return_to_menu()

    # ------------------------------------------------------------------
    # Status display (blocking — called from touch handler thread briefly)
    # ------------------------------------------------------------------

    def _show_status(self) -> None:
        """
        Shows intrinsic calibration status for each camera, cycling through
        them at 2-second intervals on the progress page.
        """
        def _worker():
            for cam_name in CAMERA_NAMES:
                if self._cancel_flag.is_set():
                    break
                cal = self._store.load_intrinsic(cam_name)
                if cal is not None:
                    rms = cal["rms_error"]
                    quality = (
                        "EXCELLENT" if rms < 0.5 else
                        "ACCEPTABLE" if rms < RMS_GOOD_THRESHOLD else
                        "POOR"
                    )
                    self._display.show_cal_progress(
                        cam_name, 1, 1, message=quality, rms=rms
                    )
                else:
                    self._display.show_cal_progress(cam_name, 0, 0, message="NOT CALIBRATED")
                time.sleep(2.0)

            hom = "READY" if self._store.has_homographies() else "MISSING"
            self._display.show_cal_progress("EXTRINSIC", 0, 0, message=hom)
            time.sleep(2.0)
            self._return_to_menu()

        self._wire_progress_handlers()
        t = threading.Thread(target=_worker, name="cal-status", daemon=True)
        t.start()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _return_to_menu(self) -> None:
        """Returns to the cal-menu page and resets state."""
        self._state = CalMenuState.MAIN_MENU
        self._wire_menu_handlers()
        self._display.show_cal_menu()

    def __repr__(self) -> str:
        return f"CalibrationDisplayMenu(state={self._state.name})"
