"""
River Vector - Device Identity

Manages the device's identity across the claim lifecycle.

  UNCLAIMED → CLAIMING → CLAIMED

The unit_id is generated deterministically from the Raspberry Pi CPU
serial plus a 4-hex random suffix. This gives:
  - Stability across reboots (same Pi → same id prefix)
  - Uniqueness even after a re-flash (random suffix)
  - Human-readable identification (last 8 of serial is short)

The claim_code is a 6-digit number shown on the device's OLED and
written to /var/lib/river-vector/claim_code.txt during the CLAIMING
phase. The operator reads it and enters it on riversongai.com to
complete pairing.
"""

from __future__ import annotations

import logging
import os
import random
import re
import secrets
import stat
from typing import Optional

from core.bootstrap import (
    BootstrapConfig,
    load_bootstrap,
    save_bootstrap,
    update_bootstrap,
)
from core.constants import CLAIM_CODE_LENGTH, CLAIM_CODE_PATH

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# Unit ID generation
# ──────────────────────────────────────────────────────────────────────────


def _read_rpi_serial() -> str:
    """
    Reads the Raspberry Pi CPU serial from /proc/cpuinfo.

    Returns the last 8 hex digits in uppercase. Returns 'NOSERIAL'
    if the file cannot be read or no serial line is present (e.g.,
    development on a non-Pi machine).
    """
    try:
        with open("/proc/cpuinfo", "r") as f:
            for line in f:
                m = re.match(r"^Serial\s*:\s*([0-9a-fA-F]+)", line)
                if m:
                    return m.group(1)[-8:].upper()
    except OSError:
        pass
    logger.warning("Could not read RPi serial — using 'NOSERIAL'. Dev environment?")
    return "NOSERIAL"


def generate_unit_id(rpi_serial: Optional[str] = None) -> str:
    """
    Generates a new unit_id.

    Format: RV-{rpi_serial_last8}-{4_random_hex}

    Args:
        rpi_serial: Override the auto-detected serial (for tests).

    Returns:
        A unit_id like 'RV-A1B2C3D4-9F2E'.
    """
    serial = rpi_serial if rpi_serial is not None else _read_rpi_serial()
    suffix = secrets.token_hex(2).upper()
    return f"RV-{serial}-{suffix}"


def generate_claim_code() -> str:
    """Generates a 6-digit claim code as a zero-padded string."""
    return f"{random.SystemRandom().randint(0, 10 ** CLAIM_CODE_LENGTH - 1):0{CLAIM_CODE_LENGTH}d}"


# ──────────────────────────────────────────────────────────────────────────
# Claim code persistence
# ──────────────────────────────────────────────────────────────────────────


def write_claim_code(code: str, path: str = CLAIM_CODE_PATH) -> None:
    """
    Writes the claim code to disk, 0600. Operators read this when
    the device has no OLED to display the code visually.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(code + "\n")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def clear_claim_code(path: str = CLAIM_CODE_PATH) -> None:
    """Removes the claim code file. Called after successful claim."""
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError as exc:
            logger.warning("Could not remove claim_code file: %s", exc)


# ──────────────────────────────────────────────────────────────────────────
# Identity class
# ──────────────────────────────────────────────────────────────────────────


class Identity:
    """
    Holds the device's current identity and claim state.

    On instantiation, loads bootstrap. If bootstrap has no unit_id
    (truly first boot), generates one and persists it.

    The claim_code is only set in memory while in CLAIMING state.
    After CLAIMED, it is cleared from disk and memory.
    """

    def __init__(self, bootstrap: Optional[BootstrapConfig] = None) -> None:
        self._bootstrap = bootstrap if bootstrap is not None else load_bootstrap()
        self._claim_code: Optional[str] = None

        if not self._bootstrap.unit_id:
            new_id = generate_unit_id()
            logger.info("First boot — generated unit_id=%s", new_id)
            self._bootstrap.unit_id = new_id
            save_bootstrap(self._bootstrap)

    # ──────────────────────────────────────────────────────────────────
    # Properties
    # ──────────────────────────────────────────────────────────────────

    @property
    def unit_id(self) -> str:
        return self._bootstrap.unit_id

    @property
    def claim_state(self) -> str:
        return self._bootstrap.claim_state

    @property
    def unit_token(self) -> str:
        return self._bootstrap.unit_token

    @property
    def claim_code(self) -> Optional[str]:
        """The current 6-digit code (only valid in CLAIMING state)."""
        return self._claim_code

    @property
    def is_claimed(self) -> bool:
        return self._bootstrap.is_claimed()

    # ──────────────────────────────────────────────────────────────────
    # State transitions
    # ──────────────────────────────────────────────────────────────────

    def begin_claiming(self) -> str:
        """
        Transitions UNCLAIMED → CLAIMING.

        Generates a fresh claim_code, writes it to disk and to the
        bootstrap's transient state, returns it so the caller (display
        driver) can show it.
        """
        if self._bootstrap.claim_state == "CLAIMED":
            raise RuntimeError(
                "Cannot begin_claiming(): device is already CLAIMED. "
                "Call reset_claim() first to re-pair."
            )
        self._claim_code = generate_claim_code()
        self._bootstrap.claim_state = "CLAIMING"
        save_bootstrap(self._bootstrap)
        write_claim_code(self._claim_code)
        logger.info(
            "Identity: now CLAIMING. unit_id=%s claim_code=%s",
            self.unit_id,
            self._claim_code,
        )
        return self._claim_code

    def complete_claim(self, unit_token: str) -> None:
        """
        Transitions CLAIMING → CLAIMED with the issued token.

        Called by claim_server after River Song verifies the claim code
        and returns the unit_token.
        """
        if not unit_token:
            raise ValueError("unit_token must be non-empty.")
        self._bootstrap.unit_token = unit_token
        self._bootstrap.claim_state = "CLAIMED"
        save_bootstrap(self._bootstrap)
        self._claim_code = None
        clear_claim_code()
        logger.info("Identity: CLAIMED. unit_id=%s", self.unit_id)

    def reset_claim(self) -> None:
        """
        Transitions back to UNCLAIMED. Wipes unit_token.

        Used by operator-initiated reset (physical button or
        explicit reset command). After this, begin_claiming() must
        be called to re-pair.
        """
        self._bootstrap.claim_state = "UNCLAIMED"
        self._bootstrap.unit_token = ""
        save_bootstrap(self._bootstrap)
        self._claim_code = None
        clear_claim_code()
        logger.warning("Identity: RESET — device is now UNCLAIMED.")

    def verify_claim_code(self, presented_code: str) -> bool:
        """
        Constant-time comparison of presented code vs. current claim_code.

        Returns True if codes match, False otherwise. Always returns
        False if the device is not currently in CLAIMING state.
        """
        if self._bootstrap.claim_state != "CLAIMING":
            return False
        if not self._claim_code or not presented_code:
            return False
        return secrets.compare_digest(self._claim_code, presented_code)

    # ──────────────────────────────────────────────────────────────────
    # Convenience
    # ──────────────────────────────────────────────────────────────────

    @property
    def bootstrap(self) -> BootstrapConfig:
        """Returns the underlying BootstrapConfig (read access)."""
        return self._bootstrap

    def refresh(self) -> None:
        """Re-reads bootstrap from disk. Call after external updates."""
        self._bootstrap = load_bootstrap()

    def __repr__(self) -> str:
        return (
            f"Identity(unit_id={self.unit_id!r}, "
            f"state={self.claim_state}, "
            f"claimed={self.is_claimed})"
        )
