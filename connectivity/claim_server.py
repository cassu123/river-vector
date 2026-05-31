"""
River Vector - Claim Server

A tiny HTTP server that runs ONLY during the CLAIMING phase. River Song
(after discovering us via mDNS) POSTs the claim code from the operator
to /verify-claim. If the code matches, we transition to CLAIMED with
the unit_token returned by River Song.

This server binds to 0.0.0.0:8765 so River Song can reach it from the
LAN. After successful claim, the server stops.

Implementation uses Python's stdlib http.server — no extra dependencies.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

from core.constants import CLAIM_SERVER_HOST, CLAIM_SERVER_PORT

logger = logging.getLogger(__name__)


# Shared state holder. The HTTP handler reads from this; the surrounding
# code mutates the Identity object directly on successful claim.
class _ClaimState:
    def __init__(self, identity) -> None:
        self.identity = identity
        self.claimed_event = threading.Event()
        self.last_error: Optional[str] = None


class _Handler(BaseHTTPRequestHandler):
    state: _ClaimState  # injected by ClaimServer

    def log_message(self, fmt: str, *args) -> None:
        logger.debug("claim_server: " + fmt, *args)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._reply(200, {
                "status": "ok",
                "claim_state": self.state.identity.claim_state,
                "unit_id": self.state.identity.unit_id,
            })
            return
        self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/verify-claim":
            self._reply(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            self._reply(400, {"error": "invalid json"})
            return

        code = str(payload.get("claim_code", "")).strip()
        unit_token = str(payload.get("unit_token", "")).strip()

        if not code or not unit_token:
            self._reply(400, {"error": "claim_code and unit_token required"})
            return

        if not self.state.identity.verify_claim_code(code):
            logger.warning("Claim verify failed — code mismatch.")
            self._reply(401, {"error": "claim code mismatch"})
            return

        try:
            self.state.identity.complete_claim(unit_token)
        except ValueError as exc:
            self._reply(400, {"error": str(exc)})
            return

        self._reply(200, {
            "status": "claimed",
            "unit_id": self.state.identity.unit_id,
        })
        # Signal the surrounding code to shut us down.
        self.state.claimed_event.set()

    def _reply(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ClaimServer:
    """
    Runs a small HTTP server for the claim handshake.

    Args:
        identity:  Identity instance (must be in CLAIMING state when
                   serving requests).
        host:      Bind host (default 0.0.0.0).
        port:      Bind port (default 8765).
    """

    def __init__(
        self,
        identity,
        host: str = CLAIM_SERVER_HOST,
        port: int = CLAIM_SERVER_PORT,
    ) -> None:
        self._state = _ClaimState(identity)
        self._host = host
        self._port = port
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ──────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._server is not None:
            return

        # Subclass _Handler to inject state into class attribute.
        class _BoundHandler(_Handler):
            state = self._state

        self._server = HTTPServer((self._host, self._port), _BoundHandler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="ClaimServer",
            daemon=True,
        )
        self._thread.start()
        logger.info("ClaimServer listening on %s:%d", self._host, self._port)

    def stop(self) -> None:
        if self._server is None:
            return
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception as exc:
            logger.warning("ClaimServer shutdown error: %s", exc)
        finally:
            self._server = None
            self._thread = None
            logger.info("ClaimServer stopped.")

    def wait_for_claim(self, timeout: Optional[float] = None) -> bool:
        """
        Blocks until the device is claimed (or timeout).

        Returns True if claimed, False on timeout.
        """
        return self._state.claimed_event.wait(timeout=timeout)
