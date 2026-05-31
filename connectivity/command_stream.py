"""
River Vector - Command Stream (Long-Poll)

Opens a persistent long-poll connection to
GET /api/vector/command/stream/{unit_id} and processes commands as they
arrive. Sub-100ms command latency.

Flow:
  1. GET held by server for up to 30 seconds.
  2. If a pending command exists, server returns 200 with the command body
     and X-Config-Version header.
  3. If no command, server returns 204 with X-Config-Version.
  4. Device acknowledges the command immediately, then pushes it onto
     the internal queue for the main loop to drain.
  5. Device reopens the connection.

On auth failure: transition the device back to UNCLAIMED (handled by
the caller — this module just signals it).
On config_version bump: caller pulls fresh config (handled by main
loop — this module just exposes the latest seen version).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable, Dict, Optional

import requests

from core.constants import (
    BACKOFF_INITIAL_SEC,
    BACKOFF_MAX_SEC,
    LONG_POLL_CLIENT_TIMEOUT_SEC,
    RIVER_SONG_API_PREFIX,
)

logger = logging.getLogger(__name__)


# Callbacks raised by the stream into the main loop.
OnCommandCallback = Callable[[Dict[str, Any]], None]
OnAuthFailureCallback = Callable[[], None]
OnConfigVersionCallback = Callable[[int], None]


class CommandStream:
    """
    Long-poll command receiver. Runs on a dedicated daemon thread.

    Args:
        identity:  Identity instance.
        probe:     ConnectivityProbe instance.
        api_client: RiverSongClient (used only for command ack).
        on_command: Called for every received command.
                    Must be fast — do not block.
        on_auth_failure: Called on persistent 401/403. Caller should
                         drop to UNCLAIMED and stop the stream.
        on_config_version: Called whenever X-Config-Version changes.
                           Caller decides whether to re-pull config.
    """

    def __init__(
        self,
        identity,
        probe,
        api_client,
        on_command: OnCommandCallback,
        on_auth_failure: OnAuthFailureCallback,
        on_config_version: OnConfigVersionCallback,
    ) -> None:
        self._identity = identity
        self._probe = probe
        self._api = api_client
        self._on_command = on_command
        self._on_auth_failure = on_auth_failure
        self._on_config_version = on_config_version
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._session = requests.Session()
        self._consecutive_errors = 0
        self._last_config_version: int = 0
        # Queue exposed for callers that prefer pull-style consumption.
        self.queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()

    # ──────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            name="CommandStream",
            daemon=True,
        )
        self._thread.start()
        logger.info("CommandStream started.")

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        try:
            self._session.close()
        except Exception:
            pass
        logger.info("CommandStream stopped.")

    @property
    def last_config_version(self) -> int:
        """Last X-Config-Version observed from the server."""
        return self._last_config_version

    # ──────────────────────────────────────────────────────────────────
    # Loop
    # ──────────────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        while self._running:
            base = self._probe.active_url
            if not base or not self._identity.is_claimed:
                time.sleep(2.0)
                continue

            url = (
                f"{base.rstrip('/')}{RIVER_SONG_API_PREFIX}"
                f"/command/stream/{self._identity.unit_id}"
            )
            try:
                resp = self._session.get(
                    url,
                    timeout=LONG_POLL_CLIENT_TIMEOUT_SEC,
                    headers={
                        "X-Unit-ID": self._identity.unit_id,
                        "X-Unit-Token": self._identity.unit_token,
                    },
                )
            except requests.Timeout:
                # Server didn't send anything in window — normal, reopen.
                self._consecutive_errors = 0
                continue
            except requests.RequestException as exc:
                self._handle_connection_error(exc)
                continue

            # Always check config_version first.
            self._observe_config_version(resp.headers.get("X-Config-Version"))

            if resp.status_code == 200:
                self._consecutive_errors = 0
                self._handle_command(resp)
            elif resp.status_code == 204:
                self._consecutive_errors = 0
                # No command. Loop reopens immediately.
            elif resp.status_code in (401, 403):
                logger.critical(
                    "CommandStream: auth failed (status=%d). Signaling reset.",
                    resp.status_code,
                )
                try:
                    self._on_auth_failure()
                finally:
                    self._running = False
                    break
            else:
                logger.warning(
                    "CommandStream: unexpected status=%d", resp.status_code
                )
                self._consecutive_errors += 1
                self._backoff()

    # ──────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────

    def _handle_command(self, resp: requests.Response) -> None:
        try:
            cmd = resp.json()
        except ValueError:
            logger.error("CommandStream: 200 with unparseable body.")
            return

        command_id = cmd.get("command_id")
        if not command_id:
            logger.error("CommandStream: received command without command_id: %r", cmd)
            return

        logger.info(
            "Command received: id=%s action=%s",
            command_id, cmd.get("action"),
        )

        # Ack first, then dispatch. If ack fails, we still dispatch so
        # the device acts; server will time out the command and may retry.
        self._api.ack_command(command_id)
        try:
            self._on_command(cmd)
        except Exception as exc:
            logger.error("CommandStream: on_command callback raised: %s",
                         exc, exc_info=True)
        # Mirror into the queue for any pull-style consumer.
        try:
            self.queue.put_nowait(cmd)
        except queue.Full:
            logger.warning("CommandStream queue full; dropping mirror.")

    def _observe_config_version(self, header_value: Optional[str]) -> None:
        if not header_value:
            return
        try:
            v = int(header_value)
        except ValueError:
            return
        if v != self._last_config_version:
            logger.info(
                "Config version: %d → %d", self._last_config_version, v
            )
            self._last_config_version = v
            try:
                self._on_config_version(v)
            except Exception as exc:
                logger.error(
                    "CommandStream: on_config_version raised: %s",
                    exc, exc_info=True,
                )

    def _handle_connection_error(self, exc: Exception) -> None:
        self._consecutive_errors += 1
        logger.debug("CommandStream connection error: %s", exc)
        self._backoff()

    def _backoff(self) -> None:
        """Exponential backoff, capped."""
        delay = min(
            BACKOFF_INITIAL_SEC * (2 ** min(self._consecutive_errors, 6)),
            BACKOFF_MAX_SEC,
        )
        time.sleep(delay)
