"""
River Vector - Bootstrap Configuration

Loads and saves the minimal device-local bootstrap file. The bootstrap
file contains ONLY:
  - Device identity (unit_id, claim state, unit_token)
  - Known WiFi networks (SSID + encrypted PSK)
  - River Song server URLs (primary internet, fallback LAN)
  - Firmware version

Everything else (hardware specs, safety floors, zones, programs) lives
in River Song and is pulled into config_cache.json by config_sync.

The bootstrap file lives at /etc/river-vector/bootstrap.json and is
root-owned, mode 0600. WiFi PSKs are encrypted at rest using the
device-bound keystore.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import stat
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from core.constants import BOOTSTRAP_PATH, KEYSTORE_PATH, PROTOCOL_VERSION

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# Dataclasses
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class WifiNetwork:
    """One known WiFi network the device may join."""

    ssid: str
    psk_encrypted: str = ""        # empty → open network
    priority: int = 100            # lower = preferred


@dataclass
class ServerUrls:
    """River Song server endpoints to try in priority order."""

    url_primary: str = "https://riversongai.com"
    url_fallback: str = "http://192.168.1.221:8000"


@dataclass
class BootstrapConfig:
    """
    Complete bootstrap config persisted to /etc/river-vector/bootstrap.json.

    The device cannot operate without this file. First-boot provisioning
    must write a valid bootstrap before River Vector starts.
    """

    protocol_version: int = PROTOCOL_VERSION
    unit_id: str = ""
    claim_state: str = "UNCLAIMED"     # UNCLAIMED | CLAIMING | CLAIMED
    unit_token: str = ""               # populated on successful claim
    firmware_version: str = "0.2.0"
    server: ServerUrls = field(default_factory=ServerUrls)
    wifi_networks: List[WifiNetwork] = field(default_factory=list)

    def is_claimed(self) -> bool:
        """True if the device has completed the claim handshake."""
        return self.claim_state == "CLAIMED" and bool(self.unit_token)

    def to_dict(self) -> dict:
        """Serializes the bootstrap to a JSON-compatible dict."""
        d = asdict(self)
        d["server"] = asdict(self.server)
        d["wifi_networks"] = [asdict(n) for n in self.wifi_networks]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "BootstrapConfig":
        """Builds a BootstrapConfig from a parsed JSON dict."""
        server_data = d.get("server", {})
        wifi_data = d.get("wifi_networks", [])
        return cls(
            protocol_version=d.get("protocol_version", PROTOCOL_VERSION),
            unit_id=d.get("unit_id", ""),
            claim_state=d.get("claim_state", "UNCLAIMED"),
            unit_token=d.get("unit_token", ""),
            firmware_version=d.get("firmware_version", "0.2.0"),
            server=ServerUrls(
                url_primary=server_data.get("url_primary", "https://riversongai.com"),
                url_fallback=server_data.get("url_fallback", "http://192.168.1.221:8000"),
            ),
            wifi_networks=[
                WifiNetwork(
                    ssid=n.get("ssid", ""),
                    psk_encrypted=n.get("psk_encrypted", ""),
                    priority=n.get("priority", 100),
                )
                for n in wifi_data
            ],
        )


# ──────────────────────────────────────────────────────────────────────────
# Keystore (device-bound encryption key for WiFi PSKs and unit_token)
# ──────────────────────────────────────────────────────────────────────────


def _get_or_create_keystore_key(path: str = KEYSTORE_PATH) -> bytes:
    """
    Returns the device-bound encryption key, creating it on first use.

    The key is 32 random bytes stored in a 0600 file. It is NOT
    backed up — losing it means re-claiming the device. For higher
    security a TPM-bound key should be used in future hardware revisions.
    """
    if os.path.exists(path):
        with open(path, "rb") as f:
            key = f.read()
        if len(key) == 32:
            return key
        logger.warning("Keystore key at %s has wrong length; regenerating.", path)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    key = secrets.token_bytes(32)
    with open(path, "wb") as f:
        f.write(key)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    logger.info("Generated new device keystore key at %s.", path)
    return key


def encrypt_psk(plaintext: str, key: Optional[bytes] = None) -> str:
    """
    XOR-encrypts a WiFi PSK with the device-bound key, returns base64.

    XOR with a random 32-byte key is sufficient against casual filesystem
    inspection; the threat model is "anyone with read access to bootstrap
    cannot recover plaintext WiFi passwords without also having the
    keystore." Both files are 0600.
    """
    if not plaintext:
        return ""
    if key is None:
        key = _get_or_create_keystore_key()
    pt = plaintext.encode("utf-8")
    ct = bytes(b ^ key[i % len(key)] for i, b in enumerate(pt))
    return base64.b64encode(ct).decode("ascii")


def decrypt_psk(ciphertext_b64: str, key: Optional[bytes] = None) -> str:
    """Reverses encrypt_psk()."""
    if not ciphertext_b64:
        return ""
    if key is None:
        key = _get_or_create_keystore_key()
    ct = base64.b64decode(ciphertext_b64.encode("ascii"))
    pt = bytes(b ^ key[i % len(key)] for i, b in enumerate(ct))
    return pt.decode("utf-8")


# ──────────────────────────────────────────────────────────────────────────
# Load / Save
# ──────────────────────────────────────────────────────────────────────────


class BootstrapNotFoundError(Exception):
    """Raised when the bootstrap file does not exist on this device."""


class BootstrapInvalidError(Exception):
    """Raised when the bootstrap file is unparseable or schema-mismatched."""


def load_bootstrap(path: str = BOOTSTRAP_PATH) -> BootstrapConfig:
    """
    Loads the bootstrap config from disk.

    Raises:
        BootstrapNotFoundError: if the file does not exist (first boot,
            unprovisioned device).
        BootstrapInvalidError: if the file is corrupt or schema-mismatched.
    """
    if not os.path.exists(path):
        raise BootstrapNotFoundError(
            f"Bootstrap file not found at {path}. Device is not provisioned. "
            f"See scripts/install.sh for provisioning."
        )
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise BootstrapInvalidError(f"Cannot read bootstrap at {path}: {exc}") from exc

    try:
        bc = BootstrapConfig.from_dict(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise BootstrapInvalidError(f"Bootstrap schema invalid: {exc}") from exc

    if bc.protocol_version != PROTOCOL_VERSION:
        logger.warning(
            "Bootstrap protocol_version=%s but device expects %s. "
            "Continuing — fields may be missing.",
            bc.protocol_version,
            PROTOCOL_VERSION,
        )

    logger.info(
        "Loaded bootstrap: unit_id=%s, claim_state=%s, %d wifi network(s).",
        bc.unit_id or "(unassigned)",
        bc.claim_state,
        len(bc.wifi_networks),
    )
    return bc


def save_bootstrap(bc: BootstrapConfig, path: str = BOOTSTRAP_PATH) -> None:
    """
    Persists bootstrap to disk atomically with 0600 mode.

    Atomic write: writes to {path}.tmp then renames. This guarantees that
    a crash mid-write does not leave a half-written file.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(bc.to_dict(), f, indent=2)
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, path)
    logger.debug("Saved bootstrap to %s.", path)


def update_bootstrap(
    *,
    claim_state: Optional[str] = None,
    unit_token: Optional[str] = None,
    firmware_version: Optional[str] = None,
    path: str = BOOTSTRAP_PATH,
) -> BootstrapConfig:
    """
    Updates specific bootstrap fields in place. Loads, mutates, saves.

    Returns the updated BootstrapConfig.
    """
    bc = load_bootstrap(path)
    if claim_state is not None:
        bc.claim_state = claim_state
    if unit_token is not None:
        bc.unit_token = unit_token
    if firmware_version is not None:
        bc.firmware_version = firmware_version
    save_bootstrap(bc, path)
    return bc
