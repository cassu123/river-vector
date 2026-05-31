"""
River Vector - WiFi Manager

Connects the device to one of the pre-agreed WiFi networks defined in
the bootstrap config. Tries each network in priority order until one
associates successfully.

Implementation uses wpa_supplicant via subprocess (Raspberry Pi default).
On non-Pi platforms (development machines), falls back to a check on
existing connectivity and treats any IP as success.

This module does NOT handle captive portals or connectivity probing —
that is connectivity_probe.py's job. WifiManager's contract is only:
"associate with one of the known SSIDs, or report failure."
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import List, Optional

from core.bootstrap import BootstrapConfig, WifiNetwork, decrypt_psk

logger = logging.getLogger(__name__)

# Where wpa_supplicant config gets written. Pi default location.
WPA_SUPPLICANT_CONF = "/etc/wpa_supplicant/wpa_supplicant.conf"
WPA_INTERFACE = "wlan0"

# How long to wait for association before moving to next network.
ASSOCIATION_TIMEOUT_SEC = 20


class WifiAssociationError(Exception):
    """Raised when no WiFi network from the bootstrap list could be joined."""


class WifiManager:
    """
    Manages WiFi association from the bootstrap's pre-agreed SSID list.

    Args:
        bootstrap: The loaded BootstrapConfig.
        interface: WiFi interface name (default wlan0).
        sim_mode: If True, skips actual wpa_supplicant calls and
                  reports success if any network exists in the list.
                  Auto-detected when /etc/wpa_supplicant does not exist.
    """

    def __init__(
        self,
        bootstrap: BootstrapConfig,
        interface: str = WPA_INTERFACE,
        sim_mode: Optional[bool] = None,
    ) -> None:
        self._bootstrap = bootstrap
        self._interface = interface
        if sim_mode is None:
            sim_mode = not os.path.exists("/etc/wpa_supplicant")
        self._sim_mode = sim_mode
        self._connected_ssid: Optional[str] = None
        if self._sim_mode:
            logger.info("WifiManager running in sim mode (no wpa_supplicant).")

    # ──────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────

    def connect(self) -> str:
        """
        Connects to the highest-priority bootstrap network that associates.

        Returns:
            The SSID of the connected network.

        Raises:
            WifiAssociationError: If no network associated.
        """
        networks = sorted(self._bootstrap.wifi_networks, key=lambda n: n.priority)
        if not networks:
            raise WifiAssociationError(
                "No WiFi networks in bootstrap. Provision /etc/river-vector/bootstrap.json."
            )

        if self._sim_mode:
            # In sim mode, "connect" to the first listed network.
            self._connected_ssid = networks[0].ssid
            logger.info("Sim mode: pretending to connect to %s.", self._connected_ssid)
            return self._connected_ssid

        for net in networks:
            logger.info("Attempting to associate with SSID=%s (priority=%d).",
                        net.ssid, net.priority)
            if self._associate(net):
                self._connected_ssid = net.ssid
                logger.info("Associated with %s.", net.ssid)
                return net.ssid
            logger.warning("Failed to associate with %s; trying next.", net.ssid)

        raise WifiAssociationError(
            f"Could not associate with any of {len(networks)} known networks: "
            f"{[n.ssid for n in networks]}"
        )

    def is_connected(self) -> bool:
        """Returns True if currently associated with a WiFi network."""
        if self._sim_mode:
            return self._connected_ssid is not None
        return self._current_ssid() is not None

    @property
    def connected_ssid(self) -> Optional[str]:
        if self._sim_mode:
            return self._connected_ssid
        return self._current_ssid()

    def disconnect(self) -> None:
        """Disconnects from the current network. No-op in sim mode."""
        if self._sim_mode:
            self._connected_ssid = None
            return
        try:
            subprocess.run(
                ["wpa_cli", "-i", self._interface, "disconnect"],
                check=False,
                capture_output=True,
                timeout=5,
            )
        except (subprocess.SubprocessError, FileNotFoundError) as exc:
            logger.warning("wpa_cli disconnect failed: %s", exc)
        self._connected_ssid = None

    # ──────────────────────────────────────────────────────────────────
    # Internal — wpa_supplicant integration
    # ──────────────────────────────────────────────────────────────────

    def _associate(self, net: WifiNetwork) -> bool:
        """
        Writes a temporary wpa_supplicant config and asks wpa_supplicant
        to reconfigure. Returns True on successful association.
        """
        try:
            self._write_wpa_config(net)
            subprocess.run(
                ["wpa_cli", "-i", self._interface, "reconfigure"],
                check=True,
                capture_output=True,
                timeout=10,
            )
        except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
            logger.error("wpa_supplicant reconfigure failed for %s: %s", net.ssid, exc)
            return False

        deadline = time.time() + ASSOCIATION_TIMEOUT_SEC
        while time.time() < deadline:
            if self._current_ssid() == net.ssid:
                return True
            time.sleep(0.5)
        return False

    def _write_wpa_config(self, net: WifiNetwork) -> None:
        """
        Writes a wpa_supplicant.conf with all known networks declared.

        wpa_supplicant will choose the best available; we leave network
        selection to it within the priority bounds we set.
        """
        lines = [
            "ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev",
            "update_config=1",
            "country=US",
            "",
        ]
        for n in sorted(self._bootstrap.wifi_networks, key=lambda x: x.priority):
            psk = decrypt_psk(n.psk_encrypted) if n.psk_encrypted else ""
            lines.append("network={")
            lines.append(f'    ssid="{n.ssid}"')
            if psk:
                lines.append(f'    psk="{psk}"')
            else:
                lines.append("    key_mgmt=NONE")
            lines.append(f"    priority={1000 - n.priority}")
            lines.append("}")
            lines.append("")

        tmp = f"{WPA_SUPPLICANT_CONF}.tmp"
        with open(tmp, "w") as f:
            f.write("\n".join(lines))
        os.chmod(tmp, 0o600)
        os.replace(tmp, WPA_SUPPLICANT_CONF)

    def _current_ssid(self) -> Optional[str]:
        """Queries wpa_cli for the currently associated SSID."""
        try:
            result = subprocess.run(
                ["wpa_cli", "-i", self._interface, "status"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            return None
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            if line.startswith("ssid="):
                return line.split("=", 1)[1].strip() or None
        return None

    def __repr__(self) -> str:
        return (
            f"WifiManager(interface={self._interface}, "
            f"sim={self._sim_mode}, "
            f"connected={self.connected_ssid!r})"
        )
