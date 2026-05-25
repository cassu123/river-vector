"""
================================================================================
Project:  River Vector — Autonomous Mower Control System
File:     connectivity/vpn.py
Purpose:  WireGuard VPN status monitor. Verifies the VPN tunnel is active
          before allowing any River Song API communication. The VPN is the
          only authorized path for external connectivity.
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

# WireGuard interface name — must match /etc/wireguard/<interface>.conf
WG_INTERFACE: str = "wg0"


@dataclass
class VPNStatus:
    """Current WireGuard VPN tunnel status."""
    active: bool = False
    interface: str = WG_INTERFACE
    peer_endpoint: str = ""
    last_handshake_sec: Optional[int] = None    # Seconds since last handshake
    transfer_rx_bytes: int = 0
    transfer_tx_bytes: int = 0


class VPNMonitor:
    """
    Monitors the WireGuard VPN tunnel status.

    Checks that the wg0 interface is up and has an active peer handshake.
    A stale handshake (> 3 minutes) indicates the tunnel is broken.
    Attempts to restart the tunnel if it goes down.

    Args:
        interface: WireGuard interface name (default: wg0).
        check_interval_sec: Seconds between status checks.
        handshake_timeout_sec: Max seconds since last handshake before restart.
    """

    HANDSHAKE_TIMEOUT_SEC: int = 180    # 3 minutes
    CHECK_INTERVAL_SEC: float = 60.0

    def __init__(
        self,
        interface: str = WG_INTERFACE,
        check_interval_sec: float = CHECK_INTERVAL_SEC,
        handshake_timeout_sec: int = HANDSHAKE_TIMEOUT_SEC,
    ) -> None:
        self._interface = interface
        self._check_interval = check_interval_sec
        self._handshake_timeout = handshake_timeout_sec
        self._status = VPNStatus(interface=interface)
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Starts the VPN monitoring thread."""
        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop,
            name="VPNMonitor",
            daemon=True,
        )
        self._thread.start()
        logger.info("VPN monitor started on interface %s.", self._interface)

    def stop(self) -> None:
        """Stops the VPN monitoring thread."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        logger.info("VPN monitor stopped.")

    def verify(self) -> bool:
        """
        Checks VPN status and attempts restart if tunnel is down.

        Returns:
            True if VPN is active after the check.
        """
        self._update_status()
        if not self._status.active:
            logger.warning("VPN tunnel is down — attempting restart.")
            return self._restart_tunnel()
        return True

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def status(self) -> VPNStatus:
        """Current VPN status snapshot."""
        return self._status

    @property
    def is_active(self) -> bool:
        """True if the VPN tunnel is active with a recent handshake."""
        return self._status.active

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _monitor_loop(self) -> None:
        """Periodically checks VPN status and restarts if needed."""
        while self._running:
            try:
                self._update_status()
                if not self._status.active:
                    logger.warning("VPN tunnel down — attempting restart.")
                    self._restart_tunnel()
            except Exception as exc:
                logger.error("VPN monitor error: %s", exc, exc_info=True)
            time.sleep(self._check_interval)

    def _update_status(self) -> None:
        """Queries wg show for current tunnel status."""
        try:
            result = subprocess.run(
                ["wg", "show", self._interface],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                self._status.active = False
                return

            output = result.stdout
            self._status.active = self._interface in output

            # Parse last handshake
            for line in output.splitlines():
                line = line.strip()
                if line.startswith("latest handshake:"):
                    # Format: "latest handshake: X seconds ago"
                    parts = line.split()
                    if len(parts) >= 3 and parts[2].isdigit():
                        self._status.last_handshake_sec = int(parts[2])
                        # Mark as inactive if handshake is stale
                        if self._status.last_handshake_sec > self._handshake_timeout:
                            self._status.active = False
                elif line.startswith("endpoint:"):
                    self._status.peer_endpoint = line.split(":", 1)[1].strip()
                elif line.startswith("transfer:"):
                    # Format: "transfer: X MiB received, Y MiB sent"
                    pass  # Parse if needed

        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.debug("wg query failed: %s", exc)
            self._status.active = False
        except Exception as exc:
            logger.error("VPN status query error: %s", exc)
            self._status.active = False

    def _restart_tunnel(self) -> bool:
        """
        Restarts the WireGuard tunnel using wg-quick.

        Returns:
            True if the tunnel came back up.
        """
        try:
            logger.info("Restarting WireGuard tunnel %s...", self._interface)
            subprocess.run(
                ["wg-quick", "down", self._interface],
                capture_output=True,
                timeout=15,
            )
            time.sleep(2)
            subprocess.run(
                ["wg-quick", "up", self._interface],
                capture_output=True,
                timeout=15,
            )
            time.sleep(5)
            self._update_status()
            if self._status.active:
                logger.info("WireGuard tunnel %s restarted successfully.", self._interface)
                return True
            else:
                logger.error("WireGuard tunnel %s failed to restart.", self._interface)
                return False
        except Exception as exc:
            logger.error("VPN restart error: %s", exc)
            return False

    def __repr__(self) -> str:
        return (
            f"VPNMonitor(interface={self._interface!r}, "
            f"active={self._status.active}, "
            f"handshake={self._status.last_handshake_sec}s ago)"
        )
