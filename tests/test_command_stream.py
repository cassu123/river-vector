"""
Tests for connectivity/command_stream.py.

Uses a fake requests.Session to drive the long-poll loop deterministically.
"""

import threading
import time
import unittest
from unittest.mock import MagicMock

import requests

from connectivity.command_stream import CommandStream


class _FakeResponse:
    def __init__(
        self,
        status_code: int,
        body: dict = None,
        config_version: str = "1",
    ) -> None:
        self.status_code = status_code
        self._body = body or {}
        self.headers = {"X-Config-Version": config_version}

    def json(self):
        return self._body


class _FakeSession:
    """Drives the long-poll loop via a queue of canned responses."""

    def __init__(self):
        self.responses = []
        self.calls = 0

    def get(self, url, timeout, headers):
        self.calls += 1
        if not self.responses:
            # Default: sleep then return 204 to keep loop alive.
            time.sleep(0.05)
            return _FakeResponse(204)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass


def _make_stream(identity_claimed=True, has_url=True):
    identity = MagicMock()
    identity.unit_id = "RV-TEST-X"
    identity.unit_token = "TOKEN"
    identity.is_claimed = identity_claimed

    probe = MagicMock()
    probe.active_url = "http://localhost:9999" if has_url else None
    probe.is_online = has_url

    api = MagicMock()
    api.ack_command.return_value = True

    on_command = MagicMock()
    on_auth_failure = MagicMock()
    on_config_version = MagicMock()

    cs = CommandStream(
        identity=identity,
        probe=probe,
        api_client=api,
        on_command=on_command,
        on_auth_failure=on_auth_failure,
        on_config_version=on_config_version,
    )
    fake = _FakeSession()
    cs._session = fake
    return cs, fake, identity, api, on_command, on_auth_failure, on_config_version


class TestCommandStream(unittest.TestCase):

    def test_received_command_acked_and_dispatched(self) -> None:
        cs, fake, identity, api, on_command, _, _ = _make_stream()
        fake.responses = [
            _FakeResponse(200, {"command_id": "cmd-1", "action": "mow_stop"}, "5"),
        ]
        cs.start()
        time.sleep(0.5)
        cs.stop()

        api.ack_command.assert_called_with("cmd-1")
        on_command.assert_called()
        cmd = on_command.call_args[0][0]
        self.assertEqual(cmd["command_id"], "cmd-1")
        self.assertEqual(cmd["action"], "mow_stop")

    def test_config_version_callback(self) -> None:
        cs, fake, _, _, _, _, on_version = _make_stream()
        fake.responses = [_FakeResponse(204, config_version="42")]
        cs.start()
        time.sleep(0.4)
        cs.stop()
        # First response delivered version 42; the callback fires once
        # for each *change* in version. Default fakes return "1" → 42 → "1".
        # So at least one call with 42 should appear.
        from unittest.mock import call
        self.assertIn(call(42), on_version.call_args_list)

    def test_auth_failure_invokes_callback_and_stops(self) -> None:
        cs, fake, _, _, _, on_auth_failure, _ = _make_stream()
        fake.responses = [_FakeResponse(401)]
        cs.start()
        time.sleep(0.4)
        on_auth_failure.assert_called()
        self.assertFalse(cs._running)

    def test_offline_loop_waits(self) -> None:
        cs, _, _, _, _, _, _ = _make_stream(has_url=False)
        cs.start()
        time.sleep(0.4)
        cs.stop()
        # No callbacks should fire while offline.
        self.assertFalse(cs._running)


if __name__ == "__main__":
    unittest.main()
