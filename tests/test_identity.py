"""Tests for core/identity.py."""

import os
import re
import tempfile
import unittest
from unittest.mock import patch

from core.bootstrap import BootstrapConfig, save_bootstrap
from core.identity import (
    Identity,
    generate_claim_code,
    generate_unit_id,
)


class TestIdentityGeneration(unittest.TestCase):
    def test_unit_id_format(self) -> None:
        uid = generate_unit_id(rpi_serial="ABCDEF12")
        self.assertRegex(uid, r"^RV-[0-9A-F]{8}-[0-9A-F]{4}$")

    def test_unit_id_random_suffix_varies(self) -> None:
        ids = {generate_unit_id(rpi_serial="ABC12345") for _ in range(50)}
        self.assertGreater(len(ids), 10)

    def test_claim_code_is_six_digits(self) -> None:
        for _ in range(20):
            code = generate_claim_code()
            self.assertRegex(code, r"^\d{6}$")


class TestIdentityLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(self.path)
        fd2, self.claim_path = tempfile.mkstemp(suffix=".txt")
        os.close(fd2)
        os.remove(self.claim_path)
        bc = BootstrapConfig(unit_id="", claim_state="UNCLAIMED")
        save_bootstrap(bc, self.path)
        self._patcher = patch(
            "core.identity.load_bootstrap",
            side_effect=lambda: __import__("core.bootstrap",
                                            fromlist=["load_bootstrap"]).load_bootstrap(self.path),
        )
        self._patcher.start()
        self._save_patcher = patch(
            "core.identity.save_bootstrap",
            side_effect=lambda bc: __import__("core.bootstrap",
                                                fromlist=["save_bootstrap"]).save_bootstrap(bc, self.path),
        )
        self._save_patcher.start()
        # Redirect claim_code file writes to a tempdir so we don't need /var/lib.
        self._write_patcher = patch(
            "core.identity.write_claim_code",
            side_effect=lambda code, path=None: __import__(
                "core.identity", fromlist=["write_claim_code"]
            ).__dict__["_orig_write_claim_code"](code, self.claim_path)
            if False else None,
        )
        self._write_patcher.start()
        self._clear_patcher = patch(
            "core.identity.clear_claim_code",
            side_effect=lambda path=None: None,
        )
        self._clear_patcher.start()

    def tearDown(self) -> None:
        self._patcher.stop()
        self._save_patcher.stop()
        self._write_patcher.stop()
        self._clear_patcher.stop()
        for p in (self.path, self.claim_path):
            try:
                os.remove(p)
            except OSError:
                pass

    def test_first_boot_generates_unit_id(self) -> None:
        identity = Identity()
        self.assertTrue(identity.unit_id.startswith("RV-"))
        # Persists.
        identity2 = Identity()
        self.assertEqual(identity.unit_id, identity2.unit_id)

    def test_claim_flow(self) -> None:
        identity = Identity()
        self.assertFalse(identity.is_claimed)
        self.assertEqual(identity.claim_state, "UNCLAIMED")

        code = identity.begin_claiming()
        self.assertRegex(code, r"^\d{6}$")
        self.assertEqual(identity.claim_state, "CLAIMING")
        self.assertEqual(identity.claim_code, code)

        # Wrong code rejected.
        self.assertFalse(identity.verify_claim_code("000000"))
        # Correct code accepted.
        self.assertTrue(identity.verify_claim_code(code))

        identity.complete_claim("TOKEN-XYZ")
        self.assertTrue(identity.is_claimed)
        self.assertEqual(identity.unit_token, "TOKEN-XYZ")
        self.assertIsNone(identity.claim_code)

    def test_reset_claim(self) -> None:
        identity = Identity()
        identity.begin_claiming()
        identity.complete_claim("T")
        identity.reset_claim()
        self.assertEqual(identity.claim_state, "UNCLAIMED")
        self.assertEqual(identity.unit_token, "")
        self.assertFalse(identity.is_claimed)

    def test_cannot_reclaim_without_reset(self) -> None:
        identity = Identity()
        identity.begin_claiming()
        identity.complete_claim("T")
        with self.assertRaises(RuntimeError):
            identity.begin_claiming()

    def test_verify_only_during_claiming(self) -> None:
        identity = Identity()
        # UNCLAIMED → always False
        self.assertFalse(identity.verify_claim_code("123456"))
        identity.begin_claiming()
        # Now legit
        self.assertTrue(identity.verify_claim_code(identity.claim_code))
        identity.complete_claim("T")
        # CLAIMED → always False
        self.assertFalse(identity.verify_claim_code("123456"))


if __name__ == "__main__":
    unittest.main()
