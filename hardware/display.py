"""
River Vector - Display Hardware Interface
Nextion 3.5" weatherproof touchscreen operator panel.
Communicates over UART using the Nextion instruction set.
Falls back to console logging when no display is connected.

Touch event format (Nextion → MCU):
    0x65  page_id  comp_id  event  0xFF 0xFF 0xFF
    event: 0x01 = finger down, 0x00 = finger up
"""

import logging
import threading
import time
from typing import Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Nextion command terminator — three 0xFF bytes
_NEXTION_END = b"\xff\xff\xff"

# Page IDs (must match the .HMI project)
_PAGE_MAIN     = 0
_PAGE_CAL_MENU = 1
_PAGE_CAL_PROG = 2

# Touch event byte (first byte of a touch packet)
_EVT_TOUCH = 0x65
_EVT_FINGER_DOWN = 0x01

# Text component names on the main page (page 0)
_COMP_MODE    = "t_mode"
_COMP_STATUS  = "t_status"
_COMP_FUEL    = "t_fuel"
_COMP_VOLTAGE = "t_voltage"
_COMP_SPEED   = "t_speed"
_COMP_GPS     = "t_gps"
_COMP_FAULT   = "t_fault"

# Cal-menu page (page 1) button component IDs
CAL_BTN_INTRINSIC  = 1   # "INTRINSIC" button
CAL_BTN_AUTO       = 2   # "AUTO EXTRINSIC" button
CAL_BTN_STATUS     = 3   # "STATUS" button
CAL_BTN_EXIT       = 4   # "EXIT" button

# Cal-progress page (page 2) component IDs
CAL_PROG_BTN_CANCEL = 5  # "CANCEL" button
# Text components on page 2
_COMP_CAL_CAM     = "t_cam"
_COMP_CAL_SAMPLES = "t_samples"
_COMP_CAL_RMS     = "t_rms"
_COMP_CAL_MSG     = "t_msg"


class DisplayManager:
    """
    Manages the Nextion 3.5" operator touchscreen.

    Sends text and value updates to the display over UART. A background
    reader thread parses incoming touch events and dispatches them to
    registered handlers. Falls back to console log output when no serial
    port is available (dev/sim mode).

    Args:
        port:     Serial device path (e.g. '/dev/ttyUSB0').
        baud_rate: Nextion default baud rate (9600).
        sim_mode: Force sim mode — log instead of transmitting.
    """

    def __init__(
        self,
        port: str = "/dev/ttyUSB0",
        baud_rate: int = 9600,
        sim_mode: bool = False,
    ) -> None:
        self._port = port
        self._baud = baud_rate
        self._sim = sim_mode
        self._serial = None
        self._lock = threading.Lock()
        self._connected = False
        # Touch handlers: (page_id, comp_id) → callable
        self._touch_handlers: Dict[Tuple[int, int], Callable] = {}
        self._reader_thread: Optional[threading.Thread] = None
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """
        Opens the serial port and starts the background touch reader.

        Returns:
            True always — falls back to sim mode on error.
        """
        if self._sim:
            logger.info("DisplayManager: sim mode — no serial I/O.")
            self._connected = True
            return True

        try:
            import serial as pyserial
            self._serial = pyserial.Serial(self._port, self._baud, timeout=0.1)
            self._connected = True
            logger.info("DisplayManager: connected to Nextion on %s.", self._port)
            self._init_display()
            self._start_reader()
        except Exception as exc:
            logger.warning(
                "DisplayManager: cannot open %s (%s) — sim mode.", self._port, exc
            )
            self._sim = True
            self._connected = True

        return True

    def disconnect(self) -> None:
        """Closes the serial connection and stops the reader thread."""
        self._running = False
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2.0)
        if self._serial:
            try:
                self._serial.close()
            except Exception:
                pass
        self._connected = False

    # ------------------------------------------------------------------
    # Touch event handling
    # ------------------------------------------------------------------

    def register_touch_handler(
        self,
        page_id: int,
        comp_id: int,
        callback: Callable,
    ) -> None:
        """
        Registers a callback for a Nextion touch event.

        Callback is invoked (with no arguments) when a finger-down event
        arrives for the specified page and component.

        Args:
            page_id:  Nextion page index.
            comp_id:  Component ID within that page.
            callback: Zero-argument callable to invoke on touch.
        """
        self._touch_handlers[(page_id, comp_id)] = callback

    def unregister_touch_handler(self, page_id: int, comp_id: int) -> None:
        """Removes a previously registered touch handler."""
        self._touch_handlers.pop((page_id, comp_id), None)

    def clear_touch_handlers(self) -> None:
        """Removes all registered touch handlers."""
        self._touch_handlers.clear()

    # ------------------------------------------------------------------
    # Status updates
    # ------------------------------------------------------------------

    def update_mode(self, mode: str) -> None:
        """
        Updates the operating mode label.

        Args:
            mode: Mode string (e.g. 'AUTO', 'MANUAL', 'ESTOP').
        """
        self._set_text(_COMP_MODE, mode)

    def update_status(self, status: str) -> None:
        """
        Updates the status line.

        Args:
            status: Short status description.
        """
        self._set_text(_COMP_STATUS, status[:30])

    def update_fuel(self, fuel_pct: Optional[float]) -> None:
        """
        Updates the fuel level display.

        Args:
            fuel_pct: Fuel percentage (0–100), or None if unknown.
        """
        text = f"{fuel_pct:.0f}%" if fuel_pct is not None else "---"
        self._set_text(_COMP_FUEL, text)

    def update_voltage(self, voltage_v: Optional[float]) -> None:
        """
        Updates the battery/charging voltage display.

        Args:
            voltage_v: Voltage in volts, or None if unknown.
        """
        text = f"{voltage_v:.1f}V" if voltage_v is not None else "---"
        self._set_text(_COMP_VOLTAGE, text)

    def update_speed(self, speed_kmh: Optional[float]) -> None:
        """
        Updates the speed display.

        Args:
            speed_kmh: Speed in km/h, or None if unknown.
        """
        text = f"{speed_kmh:.1f}" if speed_kmh is not None else "---"
        self._set_text(_COMP_SPEED, text)

    def update_gps(self, fix_quality: str, accuracy_m: Optional[float]) -> None:
        """
        Updates the GPS status display.

        Args:
            fix_quality: Fix quality string (e.g. 'RTK_FIXED', 'NO_FIX').
            accuracy_m:  Positional accuracy in meters, or None.
        """
        if accuracy_m is not None:
            text = f"{fix_quality} ±{accuracy_m*100:.0f}cm"
        else:
            text = fix_quality
        self._set_text(_COMP_GPS, text[:20])

    def show_fault(self, fault_code: str) -> None:
        """
        Displays a fault code on the fault line.

        Args:
            fault_code: Fault code string (e.g. 'LOW_VOLTAGE').
        """
        self._set_text(_COMP_FAULT, fault_code[:20])
        logger.warning("Display: fault shown — %s", fault_code)

    def clear_fault(self) -> None:
        """Clears the fault display."""
        self._set_text(_COMP_FAULT, "")

    # ------------------------------------------------------------------
    # Calibration display helpers
    # ------------------------------------------------------------------

    def navigate_to_page(self, page_id: int) -> None:
        """Navigates the Nextion to the specified page."""
        self._send_cmd(f"page {page_id}")

    def show_cal_menu(self) -> None:
        """Switches to the calibration menu page (page 1)."""
        self._send_cmd(f"page {_PAGE_CAL_MENU}")
        logger.info("Display: calibration menu shown.")

    def show_main_page(self) -> None:
        """Returns to the main status page (page 0)."""
        self._send_cmd(f"page {_PAGE_MAIN}")

    def show_cal_progress(
        self,
        camera_name: str,
        sample_count: int,
        total_needed: int,
        message: str = "",
        rms: Optional[float] = None,
    ) -> None:
        """
        Switches to the calibration progress page and updates its fields.

        Args:
            camera_name:   Camera being calibrated.
            sample_count:  Number of checkerboard samples collected so far.
            total_needed:  Minimum samples required.
            message:       Optional status message (max 30 chars).
            rms:           RMS reprojection error once available; None hides it.
        """
        self._send_cmd(f"page {_PAGE_CAL_PROG}")
        self._set_text(_COMP_CAL_CAM, camera_name[:14])
        self._set_text(_COMP_CAL_SAMPLES, f"{sample_count}/{total_needed}")
        self._set_text(_COMP_CAL_MSG, message[:30])
        rms_txt = f"{rms:.3f}px" if rms is not None else "---"
        self._set_text(_COMP_CAL_RMS, rms_txt)

    def show_cal_result(
        self,
        camera_name: str,
        rms: float,
        quality: str,
        success: bool = True,
    ) -> None:
        """
        Updates the progress page to display the final calibration result.

        Args:
            camera_name: Camera that was calibrated.
            rms:         Final RMS reprojection error in pixels.
            quality:     Quality label (e.g. 'EXCELLENT', 'ACCEPTABLE', 'POOR').
            success:     False if calibration failed or was cancelled.
        """
        if success:
            self._set_text(_COMP_CAL_MSG, f"Done  {quality}"[:30])
        else:
            self._set_text(_COMP_CAL_MSG, "FAILED/CANCELLED")
        self._set_text(_COMP_CAL_CAM, camera_name[:14])
        rms_txt = f"{rms:.3f}px" if success else "---"
        self._set_text(_COMP_CAL_RMS, rms_txt)
        self._set_text(_COMP_CAL_SAMPLES, "DONE" if success else "---")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _start_reader(self) -> None:
        """Starts the background thread that reads and dispatches touch events."""
        self._running = True
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="nextion-reader", daemon=True
        )
        self._reader_thread.start()

    def _reader_loop(self) -> None:
        """
        Background loop: reads bytes from serial and parses Nextion touch packets.

        Touch packet layout (7 bytes):
            0x65  page_id  comp_id  event  0xFF  0xFF  0xFF
        """
        buf = bytearray()
        while self._running:
            try:
                if self._serial and self._serial.in_waiting:
                    buf.extend(self._serial.read(self._serial.in_waiting))
                else:
                    time.sleep(0.02)
                    continue

                # Scan for complete touch packets
                while len(buf) >= 7:
                    if buf[0] != _EVT_TOUCH:
                        buf.pop(0)
                        continue
                    if buf[4] == 0xFF and buf[5] == 0xFF and buf[6] == 0xFF:
                        page_id  = buf[1]
                        comp_id  = buf[2]
                        event    = buf[3]
                        del buf[:7]
                        if event == _EVT_FINGER_DOWN:
                            self._dispatch_touch(page_id, comp_id)
                    else:
                        buf.pop(0)
            except Exception as exc:
                logger.error("Display reader error: %s", exc)
                time.sleep(0.1)

    def _dispatch_touch(self, page_id: int, comp_id: int) -> None:
        """Calls the registered handler for a touch event, if any."""
        key = (page_id, comp_id)
        handler = self._touch_handlers.get(key)
        if handler:
            try:
                handler()
            except Exception as exc:
                logger.error(
                    "Touch handler error (page=%d comp=%d): %s", page_id, comp_id, exc
                )
        else:
            logger.debug("Unhandled touch: page=%d comp=%d", page_id, comp_id)

    def _init_display(self) -> None:
        """Sends initialization commands to wake up and configure the display."""
        self._send_cmd("page 0")                    # Navigate to main page
        self._send_cmd("dim=100")                   # Full brightness
        self._send_cmd("sleep=0")                   # Keep awake
        logger.info("DisplayManager: Nextion initialized.")

    def _set_text(self, component: str, text: str) -> None:
        """
        Sets a Nextion text component value.

        Args:
            component: Component name from the .HMI project.
            text:      Text to display.
        """
        cmd = f'{component}.txt="{text}"'
        self._send_cmd(cmd)

    def _send_cmd(self, cmd: str) -> None:
        """
        Sends a Nextion command string terminated with 0xFF 0xFF 0xFF.

        In sim mode, logs the command instead.

        Args:
            cmd: Nextion instruction string.
        """
        if self._sim:
            logger.debug("Display [SIM]: %s", cmd)
            return
        try:
            data = cmd.encode("ascii") + _NEXTION_END
            with self._lock:
                if self._serial and self._serial.is_open:
                    self._serial.write(data)
        except Exception as exc:
            logger.error("Display send error: %s", exc)

    def __repr__(self) -> str:
        return (
            f"DisplayManager(port={self._port!r}, sim={self._sim}, "
            f"connected={self._connected})"
        )
