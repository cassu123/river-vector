"""
River Vector - River Song API Client

HTTP client for the River Song API. All endpoints under /api/vector/*.

Responsibilities:
  - Authenticate every request with X-Unit-Token (after claim).
  - Resolve outbound URL via ConnectivityProbe (internet primary, LAN
    fallback).
  - Support batched telemetry posts for offline replay.
  - Acknowledge and complete commands.
  - Report session lifecycle (start/end).
  - Push status, alerts, and events.

This module does NOT implement the long-poll command stream — that lives
in connectivity/command_stream.py.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.constants import (
    API_RETRY_ATTEMPTS,
    API_RETRY_BACKOFF_SEC,
    API_TIMEOUT_SEC,
    RIVER_SONG_API_PREFIX,
)

logger = logging.getLogger(__name__)


class APIError(Exception):
    """Raised when a River Song API call fails after all retries."""


class AuthError(APIError):
    """Raised on 401/403 responses. Caller should drop to UNCLAIMED."""


class OfflineError(APIError):
    """Raised when no server URL is currently reachable."""


class RiverSongClient:
    """
    HTTP client for the River Song API.

    Args:
        identity:  Identity instance — provides unit_id and unit_token.
        probe:     ConnectivityProbe instance — provides active server URL.
    """

    def __init__(self, identity, probe) -> None:
        if identity is None:
            raise ValueError("identity must not be None.")
        if probe is None:
            raise ValueError("probe must not be None.")
        self._identity = identity
        self._probe = probe
        self._prefix = RIVER_SONG_API_PREFIX
        self._session = self._build_session()

    # ──────────────────────────────────────────────────────────────────
    # Registration
    # ──────────────────────────────────────────────────────────────────

    def register(
        self,
        firmware_version: str,
        auto_detected_hardware: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Announces the device to River Song on boot.

        The new payload is minimal — hardware specs are no longer sent
        TO the server; they are pulled FROM the server via config_sync.
        We just tell the server we are alive and how we found it.
        """
        payload = {
            "unit_id": self._identity.unit_id,
            "firmware_version": firmware_version,
            "boot_time": _utc_iso(),
            "connectivity_tier": self._probe.tier.value,
            "auto_detected_hardware": auto_detected_hardware or {},
        }
        try:
            self._post("/register", payload)
            logger.info("Registered with River Song: unit_id=%s", self._identity.unit_id)
            return True
        except OfflineError:
            logger.warning("Cannot register — server unreachable.")
            return False
        except AuthError:
            logger.error("Registration auth failed — device may need re-claim.")
            return False
        except APIError as exc:
            logger.error("Registration failed: %s", exc)
            return False

    # ──────────────────────────────────────────────────────────────────
    # Config pull
    # ──────────────────────────────────────────────────────────────────

    def pull_config(self) -> Optional[Dict[str, Any]]:
        """
        Pulls the full operational config bundle. Returns None on failure.

        config_sync.py is the caller; this method is the wire layer.
        """
        try:
            return self._get(f"/config/{self._identity.unit_id}")
        except OfflineError:
            logger.warning("Cannot pull config — server unreachable.")
            return None
        except APIError as exc:
            logger.error("Config pull failed: %s", exc)
            return None

    # ──────────────────────────────────────────────────────────────────
    # Status / Telemetry / Alerts / Events
    # ──────────────────────────────────────────────────────────────────

    def push_status(self, status: Dict[str, Any]) -> bool:
        """Pushes operating-mode / session-state transitions."""
        return self._safe_post("/status", {
            "unit_id": self._identity.unit_id,
            **status,
        })

    def push_telemetry_batch(self, snapshots: List[Dict[str, Any]]) -> bool:
        """
        Pushes a batch of telemetry snapshots.

        Supports up to TELEMETRY_BATCH_MAX per call. The telemetry thread
        uses this for offline replay; single-snapshot pushes wrap in a
        one-element list.
        """
        if not snapshots:
            return True
        payload = {
            "unit_id": self._identity.unit_id,
            "snapshots": snapshots,
        }
        return self._safe_post("/telemetry", payload)

    def post_alert(self, alert: Dict[str, Any]) -> bool:
        """Pushes an alert to River Song."""
        return self._safe_post("/alert", {
            "unit_id": self._identity.unit_id,
            **alert,
        })

    def post_event(self, event: str, data: Dict[str, Any]) -> bool:
        """Posts a session lifecycle event."""
        return self._safe_post("/event", {
            "unit_id": self._identity.unit_id,
            "event": event,
            **data,
        })

    # ──────────────────────────────────────────────────────────────────
    # Sessions
    # ──────────────────────────────────────────────────────────────────

    def session_start(
        self,
        program_id: Optional[str],
        config_version: int,
    ) -> Optional[str]:
        """
        Announces the start of a mowing session. Returns server-assigned
        session_id, or None on failure.
        """
        payload = {
            "unit_id": self._identity.unit_id,
            "program_id": program_id,
            "config_version": config_version,
            "started_at": _utc_iso(),
        }
        try:
            resp = self._post("/session/start", payload)
            return resp.get("session_id") if resp else None
        except APIError as exc:
            logger.error("session_start failed: %s", exc)
            return None

    def session_end(
        self,
        session_id: str,
        status: str,
        area_mowed_sqm: Optional[float] = None,
        battery_used_pct: Optional[float] = None,
        fuel_used_pct: Optional[float] = None,
        abort_reason: Optional[str] = None,
    ) -> bool:
        """Reports the end of a mowing session with totals."""
        payload = {
            "unit_id": self._identity.unit_id,
            "session_id": session_id,
            "ended_at": _utc_iso(),
            "status": status,
            "area_mowed_sqm": area_mowed_sqm,
            "battery_used_pct": battery_used_pct,
            "fuel_used_pct": fuel_used_pct,
            "abort_reason": abort_reason,
        }
        return self._safe_post("/session/end", payload)

    # ──────────────────────────────────────────────────────────────────
    # Commands
    # ──────────────────────────────────────────────────────────────────

    def ack_command(self, command_id: str) -> bool:
        """Acknowledges receipt of a command (pending → acknowledged)."""
        return self._safe_post(f"/command/{command_id}/ack", {
            "unit_id": self._identity.unit_id,
            "acknowledged_at": _utc_iso(),
        })

    def complete_command(
        self,
        command_id: str,
        success: bool,
        result: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Reports command completion or failure."""
        return self._safe_post(f"/command/{command_id}/complete", {
            "unit_id": self._identity.unit_id,
            "completed_at": _utc_iso(),
            "status": "completed" if success else "failed",
            "result": result or {},
        })

    # ──────────────────────────────────────────────────────────────────
    # Boundary teach
    # ──────────────────────────────────────────────────────────────────

    def push_teach_waypoints(
        self,
        zone_name: str,
        waypoints: List[List[Optional[float]]],
        finalize: bool = False,
    ) -> bool:
        """Pushes accumulated boundary waypoints ([lat, lng, alt_m] triplets) during teach mode."""
        return self._safe_post("/zones/teach", {
            "unit_id": self._identity.unit_id,
            "zone_name": zone_name,
            "waypoints": waypoints,
            "finalize": finalize,
        })

    # ──────────────────────────────────────────────────────────────────
    # Internal HTTP
    # ──────────────────────────────────────────────────────────────────

    def _post(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST with full retry semantics. Raises APIError on failure."""
        base_url = self._probe.active_url
        if not base_url:
            raise OfflineError("No server URL is reachable.")
        url = f"{base_url.rstrip('/')}{self._prefix}{endpoint}"
        try:
            resp = self._session.post(
                url,
                json=payload,
                timeout=API_TIMEOUT_SEC,
                headers=self._auth_headers(),
            )
        except requests.RequestException as exc:
            raise APIError(f"POST {url} failed: {exc}") from exc

        if resp.status_code in (401, 403):
            raise AuthError(f"POST {url} returned {resp.status_code}")
        if resp.status_code >= 400:
            raise APIError(f"POST {url} returned {resp.status_code}: {resp.text[:200]}")

        if resp.content:
            try:
                return resp.json()
            except ValueError:
                return {}
        return {}

    def _get(self, endpoint: str) -> Optional[Dict[str, Any]]:
        base_url = self._probe.active_url
        if not base_url:
            raise OfflineError("No server URL is reachable.")
        url = f"{base_url.rstrip('/')}{self._prefix}{endpoint}"
        try:
            resp = self._session.get(
                url,
                timeout=API_TIMEOUT_SEC,
                headers=self._auth_headers(),
            )
        except requests.RequestException as exc:
            raise APIError(f"GET {url} failed: {exc}") from exc

        if resp.status_code in (401, 403):
            raise AuthError(f"GET {url} returned {resp.status_code}")
        if resp.status_code >= 400:
            raise APIError(f"GET {url} returned {resp.status_code}")

        if resp.content:
            try:
                return resp.json()
            except ValueError:
                return None
        return None

    def _safe_post(self, endpoint: str, payload: Dict[str, Any]) -> bool:
        """POST that swallows errors and returns success bool."""
        try:
            self._post(endpoint, payload)
            return True
        except OfflineError:
            return False
        except AuthError:
            logger.error("Auth failed on %s — device may need re-claim.", endpoint)
            return False
        except APIError as exc:
            logger.warning("POST %s failed: %s", endpoint, exc)
            return False

    def _auth_headers(self) -> Dict[str, str]:
        """Returns auth headers including unit_token if claimed."""
        h = {
            "Content-Type": "application/json",
            "X-Unit-ID": self._identity.unit_id,
        }
        token = self._identity.unit_token
        if token:
            h["X-Unit-Token"] = token
        return h

    def _build_session(self) -> requests.Session:
        """Builds a requests.Session with retry adapter."""
        session = requests.Session()
        retry = Retry(
            total=API_RETRY_ATTEMPTS,
            backoff_factor=API_RETRY_BACKOFF_SEC,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def __repr__(self) -> str:
        return (
            f"RiverSongClient(unit_id={self._identity.unit_id}, "
            f"tier={self._probe.tier.value}, "
            f"url={self._probe.active_url!r})"
        )


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _utc_iso() -> str:
    """Returns the current UTC time in ISO 8601 with trailing Z."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
