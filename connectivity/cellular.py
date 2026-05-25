"""
================================================================================
Project:  River Vector — Autonomous Mower Control System
File:     connectivity/cellular.py
Purpose:  4G LTE cellular connection manager. Uses ModemManager (mmcli) to
          monitor modem state, signal strength, and data connectivity.
          Attempts reconnection on link loss.
Author:   [Author Placeholder]
Version:  0.1.0
Date:     2026-05-25
================================================================================
"""

import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ModemStatus:
    """Current cellular modem status."""
    connected: bool = False
    signal_quality: int = 0     # 0–100%
    operator: str = ""
    technology: str = ""        # LTE, 5G, etc.
    ip_address: str = ""


class CellularManager:
    """
    Manages the 4G LTE cellular connection via ModemManager.

    Monitors connection state and signal quality. Attempts automatic
    reconnection when the link drops. All external connectivity
    (River Song API, VPN) depends on this being active.

    Args:
        check_interval_sec: Seconds between connectivity checks.
        reconnect_attempts: Number of reconnect attempts before giving up.
    """

    CHECK_INTERVAL_SEC: float = 30.0
    RECONNECT_ATTEMPTS: int = 3
    RECONNECT_DELAY_SEC: float = 10.0

    def __init__(
        self,
        check_interval_sec: float = CHECK_INTERVAL_SEC,
    ) -> None:
        self._check_interval = check_interval_sec
        self._status = ModemStatus()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Starts the cellular monitoring thread."""
        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop,
            name="CellularManager",
            daemon=True,
        )
        self._thread.start()
        logger.info("Cellular manager started.")

    def stop(self) -> None:
        """Stops the cellular monitoring thread."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        logger.info("Cellular manager stopped.")

    def ensure_connected(self) -> bool:
        """
        Checks connectivity and attempts reconnection if needed.

        Returns:
            True if connected after the check.
        """
        self._update_status()
        if not self._status.connected:
            logger.warning("Cellular not connected — attempting reconnect.")
            return self._reconnect()
        return True

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def status(self) -> ModemStatus:
        """Current modem status snapshot."""
        return self._status

    @property
    def is_connected(self) -> bool:
        """True if cellular data connection is active."""
        return self._status.connected

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _monitor_loop(self) -> None:
        """Periodically checks cellular status and reconnects if needed."""
        while self._running:
            try:
                self._update_status()
                if not self._status.connected:
                    logger.warning("Cellular link lost — attempting reconnect.")
                    self._reconnect()
            except Exception as exc:
                logger.error("Cellular monitor error: %s", exc, exc_info=True)
            time.sleep(self._check_interval)

    def _update_status(self) -> None:
        """Queries ModemManager for current modem state."""
        try:
            result = subprocess.run(
                ["mmcli", "-m", "0", "--output-keyvalue"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                self._status.connected = False
                return

            lines = {
                line.split(":")[0].strip(): line.split(":", 1)[1].strip()
                for line in result.stdout.splitlines()
                if ":" in line
            }

            state = lines.get("modem.status.state", "").lower()
            self._status.connected = state == "connected"
            self._status.operator = lines.get("modem.3gpp.operator-name", "")
            self._status.technology = lines.get("modem.status.access-technologies", "")

            signal_str = lines.get("modem.generic.signal-quality.value", "0")
            self._status.signal_quality = int(signal_str) if signal_str.isdigit() else 0

        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.debug("mmcli query failed (modem may not be present): %s", exc)
            self._status.connected = False
        except Exception as exc:
            logger.error("Modem status query error: %s", exc)
            self._status.connected = False

    def _reconnect(self) -> bool:
        """
        Attempts to reconnect the cellular data connection.

        Returns:
            True if reconnection succeeded.
        """
        for attempt in range(1, self.RECONNECT_ATTEMPTS + 1):
            logger.info(
                "Cellular reconnect attempt %d/%d...", attempt, self.RECONNECT_ATTEMPTS
            )
            try:
                subprocess.run(
                    ["mmcli", "-m", "0", "--simple-connect=apn=internet"],
                    capture_output=True,
                    timeout=30,
                )
                time.sleep(self.RECONNECT_DELAY_SEC)
                self._update_status()
                if self._status.connected:
                    logger.info("Cellular reconnected.")
                    return True
            except Exception as exc:
                logger.warning("Reconnect attempt %d failed: %s", attempt, exc)
            time.sleep(self.RECONNECT_DELAY_SEC)

        logger.error("Cellular reconnection failed after %d attempts.", self.RECONNECT_ATTEMPTS)
        return False

    def __repr__(self) -> str:
        return (
            f"CellularManager(connected={self._status.connected}, "
            f"signal={self._status.signal_quality}%, "
            f"operator={self._status.operator!r})"
        )
