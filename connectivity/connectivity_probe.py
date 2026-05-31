"""
River Vector - Connectivity Probe

Determines which River Song URL the device can currently reach, and
reports a connectivity tier (internet | lan | offline | meshtastic_only).

The probe sends a HEAD request to {server_url}/api/health. A 200
response means we can talk to River Song. A 3xx redirect to a
different host indicates a captive portal.

This module is the single source of truth for connectivity state.
api_client.py, command_stream.py, and telemetry_thread.py all consult
the probe before initiating outbound requests.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional
from urllib.parse import urlparse

import requests

from core.bootstrap import BootstrapConfig
from core.constants import ConnectivityTier

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_SEC = 5.0
PROBE_PATH = "/api/health"


class ConnectivityProbe:
    """
    Probes server URLs to determine the active connectivity tier.

    Args:
        bootstrap: The loaded BootstrapConfig (provides server URLs).
        probe_interval_sec: How often the background thread re-probes.
                            Default 30s.
    """

    def __init__(
        self,
        bootstrap: BootstrapConfig,
        probe_interval_sec: float = 30.0,
    ) -> None:
        self._bootstrap = bootstrap
        self._interval = probe_interval_sec
        self._lock = threading.Lock()
        self._tier: ConnectivityTier = ConnectivityTier.OFFLINE
        self._active_url: Optional[str] = None
        self._last_probe: float = 0.0
        self._thread: Optional[threading.Thread] = None
        self._running = False

    # ──────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Starts the background probe thread."""
        self.probe_once()
        self._running = True
        self._thread = threading.Thread(
            target=self._probe_loop,
            name="ConnectivityProbe",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stops the background probe thread."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    # ──────────────────────────────────────────────────────────────────
    # State
    # ──────────────────────────────────────────────────────────────────

    @property
    def tier(self) -> ConnectivityTier:
        """Current connectivity tier."""
        with self._lock:
            return self._tier

    @property
    def active_url(self) -> Optional[str]:
        """
        The server URL we can currently reach, or None if offline.

        api_client.py uses this to construct outbound URLs.
        """
        with self._lock:
            return self._active_url

    @property
    def is_online(self) -> bool:
        """True if we can reach River Song over internet or LAN."""
        return self.tier in (ConnectivityTier.INTERNET, ConnectivityTier.LAN)

    # ──────────────────────────────────────────────────────────────────
    # Probing
    # ──────────────────────────────────────────────────────────────────

    def probe_once(self) -> ConnectivityTier:
        """
        Probes both URLs, picks the highest-tier reachable one, updates
        state, returns the new tier.

        Try order:
          1. url_primary (internet) → tier=INTERNET
          2. url_fallback (LAN)     → tier=LAN
          3. Neither reachable      → tier=OFFLINE
        """
        primary = self._bootstrap.server.url_primary
        fallback = self._bootstrap.server.url_fallback

        if primary and self._probe(primary):
            self._set(ConnectivityTier.INTERNET, primary)
            return ConnectivityTier.INTERNET

        if fallback and self._probe(fallback):
            self._set(ConnectivityTier.LAN, fallback)
            return ConnectivityTier.LAN

        self._set(ConnectivityTier.OFFLINE, None)
        return ConnectivityTier.OFFLINE

    def _probe(self, base_url: str) -> bool:
        """
        Returns True if base_url is reachable AND not behind a captive portal.
        """
        url = base_url.rstrip("/") + PROBE_PATH
        try:
            resp = requests.head(
                url,
                timeout=PROBE_TIMEOUT_SEC,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            logger.debug("Probe %s failed: %s", url, exc)
            return False

        # Direct success.
        if resp.status_code == 200:
            return True

        # Captive portal detection: 3xx to a different host.
        if 300 <= resp.status_code < 400:
            target = resp.headers.get("Location", "")
            if target and urlparse(target).hostname != urlparse(base_url).hostname:
                logger.warning("Captive portal detected at %s → %s", url, target)
                return False

        logger.debug("Probe %s returned status=%d", url, resp.status_code)
        return False

    def _probe_loop(self) -> None:
        while self._running:
            try:
                self.probe_once()
            except Exception as exc:
                logger.error("Probe loop error: %s", exc, exc_info=True)
            time.sleep(self._interval)

    def _set(self, tier: ConnectivityTier, url: Optional[str]) -> None:
        with self._lock:
            if self._tier != tier:
                logger.info(
                    "Connectivity tier: %s → %s (url=%s)",
                    self._tier.value,
                    tier.value,
                    url,
                )
            self._tier = tier
            self._active_url = url
            self._last_probe = time.time()

    def __repr__(self) -> str:
        return (
            f"ConnectivityProbe(tier={self._tier.value}, "
            f"url={self._active_url!r})"
        )
