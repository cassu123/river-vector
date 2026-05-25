"""
================================================================================
Project:  River Vector — Autonomous Mower Control System
File:     connectivity/api_client.py
Purpose:  River Song API client. Handles device registration, status
          reporting, command polling, telemetry push, and alert dispatch.
          All communication goes through WireGuard VPN — no direct internet
          access. Implements retry logic with exponential backoff.
Author:   [Author Placeholder]
Version:  0.1.0
Date:     2026-05-25
================================================================================
"""

import logging
import time
from typing import Any, Dict, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.constants import (
    API_RETRY_ATTEMPTS,
    API_RETRY_BACKOFF_SEC,
    API_TIMEOUT_SEC,
    FaultCode,
    RIVER_SONG_API_PREFIX,
    RIVER_SONG_BASE_URL,
)

logger = logging.getLogger(__name__)


class APIError(Exception):
    """Raised when a River Song API call fails after all retries."""


class RiverSongClient:
    """
    HTTP client for the River Song API.

    Handles all communication between River Vector and riversongai.com.
    Uses a persistent requests.Session with retry logic. All requests
    include the unit_id and API key in headers.

    Args:
        config: MowerConfig with unit_id and river_song_api_key.
        base_url: River Song API base URL (default from constants).
    """

    def __init__(self, config, base_url: str = RIVER_SONG_BASE_URL) -> None:
        if config is None:
            raise ValueError("config must not be None.")
        self._config = config
        self._base_url = base_url.rstrip("/")
        self._prefix = RIVER_SONG_API_PREFIX
        self._session = self._build_session()
        self._registered = False

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self) -> bool:
        """
        Registers this unit with River Song on boot.

        Sends unit profile data to POST /api/vector/register.
        River Song uses this to create or update the device node.

        Returns:
            True if registration succeeded.
        """
        payload = {
            "unit_id": self._config.unit_id,
            "name": self._config.name,
            "platform": self._config.platform,
            "transmission": self._config.transmission,
            "deck_width_inches": self._config.deck_width_inches,
            "hardware": self._config.hardware,
            "features": self._config.features,
        }
        try:
            resp = self._post("/register", payload)
            self._registered = True
            logger.info(
                "Registered with River Song: unit_id=%s", self._config.unit_id
            )
            return True
        except APIError as exc:
            logger.error("Registration failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def push_status(self, status: Dict[str, Any]) -> bool:
        """
        Pushes current mower status to River Song.

        Called by the mode manager on state transitions.

        Args:
            status: Dict with operating_mode, session_state, fault_codes, etc.

        Returns:
            True if the push succeeded.
        """
        try:
            self._post("/status", {"unit_id": self._config.unit_id, **status})
            return True
        except APIError as exc:
            logger.warning("Status push failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------

    def push_telemetry(self, telemetry: Dict[str, Any]) -> bool:
        """
        Pushes a telemetry snapshot to River Song.

        River Song stores this for dashboard display and AI analysis.

        Args:
            telemetry: TelemetrySnapshot.to_dict() output.

        Returns:
            True if the push succeeded.
        """
        try:
            self._post("/telemetry", telemetry)
            return True
        except APIError as exc:
            logger.warning("Telemetry push failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def poll_commands(self) -> Optional[Dict[str, Any]]:
        """
        Polls River Song for pending commands.

        River Song queues commands (mow_start, mow_stop, return_home, etc.)
        that were issued by the user or AI. This method retrieves and
        clears the queue.

        Returns:
            Command dict if a command is pending, None otherwise.
        """
        try:
            resp = self._get(f"/command/{self._config.unit_id}")
            return resp if resp else None
        except APIError as exc:
            logger.debug("Command poll failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------

    def post_alert(self, alert) -> bool:
        """
        Pushes an alert to River Song for operator notification.

        Args:
            alert: Alert instance from telemetry/alerts.py.

        Returns:
            True if the push succeeded.
        """
        try:
            self._post("/alert", {
                "unit_id": self._config.unit_id,
                "level": alert.level.name,
                "title": alert.title,
                "message": alert.message,
                "fault_code": alert.fault_code,
                "timestamp": alert.timestamp,
            })
            return True
        except APIError as exc:
            logger.warning("Alert push failed: %s", exc)
            return False

    def post_event(self, event: str, data: Dict[str, Any]) -> bool:
        """
        Posts a session lifecycle event to River Song.

        Args:
            event: Event name string (e.g. 'session_started', 'session_complete').
            data: Event payload dict.

        Returns:
            True if the push succeeded.
        """
        try:
            self._post("/event", {
                "unit_id": self._config.unit_id,
                "event": event,
                **data,
            })
            return True
        except APIError as exc:
            logger.warning("Event post failed for '%s': %s", event, exc)
            return False

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _post(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Makes a POST request to the River Song API.

        Args:
            endpoint: API endpoint path (relative to prefix).
            payload: JSON request body.

        Returns:
            Parsed JSON response dict.

        Raises:
            APIError: If the request fails after retries.
        """
        url = f"{self._base_url}{self._prefix}{endpoint}"
        try:
            resp = self._session.post(
                url,
                json=payload,
                timeout=API_TIMEOUT_SEC,
            )
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        except requests.RequestException as exc:
            raise APIError(f"POST {url} failed: {exc}") from exc

    def _get(self, endpoint: str) -> Optional[Dict[str, Any]]:
        """
        Makes a GET request to the River Song API.

        Args:
            endpoint: API endpoint path (relative to prefix).

        Returns:
            Parsed JSON response dict, or None if empty.

        Raises:
            APIError: If the request fails after retries.
        """
        url = f"{self._base_url}{self._prefix}{endpoint}"
        try:
            resp = self._session.get(url, timeout=API_TIMEOUT_SEC)
            resp.raise_for_status()
            return resp.json() if resp.content else None
        except requests.RequestException as exc:
            raise APIError(f"GET {url} failed: {exc}") from exc

    def _build_session(self) -> requests.Session:
        """
        Builds a requests.Session with retry logic and auth headers.

        Returns:
            Configured requests.Session.
        """
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

        api_key = self._config.river_song_api_key
        if api_key:
            session.headers.update({
                "Authorization": f"Bearer {api_key}",
                "X-Unit-ID": self._config.unit_id,
                "Content-Type": "application/json",
            })
        else:
            logger.warning(
                "RIVER_SONG_API_KEY not set — API calls will be unauthenticated."
            )

        return session

    def __repr__(self) -> str:
        return (
            f"RiverSongClient(unit={self._config.unit_id}, "
            f"registered={self._registered}, "
            f"base_url={self._base_url!r})"
        )
