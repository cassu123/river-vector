"""Tests for core/bootstrap.py."""

import json
import os
import tempfile
import unittest

from core.bootstrap import (
    BootstrapConfig,
    BootstrapInvalidError,
    BootstrapNotFoundError,
    ServerUrls,
    WifiNetwork,
    decrypt_psk,
    encrypt_psk,
    load_bootstrap,
    save_bootstrap,
    update_bootstrap,
)


class TestBootstrapRoundTrip(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(self.path)
        fd2, self.keystore = tempfile.mkstemp(suffix=".key")
        os.close(fd2)
        os.remove(self.keystore)

    def tearDown(self) -> None:
        for p in (self.path, self.keystore):
            try:
                os.remove(p)
            except OSError:
                pass

    def test_save_and_load_roundtrip(self) -> None:
        bc = BootstrapConfig(
            unit_id="RV-TEST-0001",
            claim_state="CLAIMED",
            unit_token="abc123",
            wifi_networks=[
                WifiNetwork(ssid="HomeWifi", psk_encrypted="xx", priority=1),
                WifiNetwork(ssid="PhoneAP", psk_encrypted="yy", priority=2),
            ],
            server=ServerUrls(url_primary="https://example.com",
                              url_fallback="http://lan.local"),
        )
        save_bootstrap(bc, self.path)
        loaded = load_bootstrap(self.path)
        self.assertEqual(loaded.unit_id, "RV-TEST-0001")
        self.assertEqual(loaded.claim_state, "CLAIMED")
        self.assertEqual(loaded.unit_token, "abc123")
        self.assertEqual(len(loaded.wifi_networks), 2)
        self.assertEqual(loaded.wifi_networks[0].ssid, "HomeWifi")
        self.assertTrue(loaded.is_claimed())

    def test_load_missing_raises(self) -> None:
        with self.assertRaises(BootstrapNotFoundError):
            load_bootstrap(self.path)

    def test_load_invalid_json_raises(self) -> None:
        with open(self.path, "w") as f:
            f.write("{not json")
        with self.assertRaises(BootstrapInvalidError):
            load_bootstrap(self.path)

    def test_update_partial(self) -> None:
        bc = BootstrapConfig(unit_id="RV-X", claim_state="UNCLAIMED")
        save_bootstrap(bc, self.path)
        update_bootstrap(
            claim_state="CLAIMED",
            unit_token="newtoken",
            path=self.path,
        )
        loaded = load_bootstrap(self.path)
        self.assertEqual(loaded.claim_state, "CLAIMED")
        self.assertEqual(loaded.unit_token, "newtoken")
        self.assertEqual(loaded.unit_id, "RV-X")  # unchanged

    def test_psk_roundtrip(self) -> None:
        original = "my super secret WiFi password 123!"
        ct = encrypt_psk(original, key=b"\x01" * 32)
        pt = decrypt_psk(ct, key=b"\x01" * 32)
        self.assertEqual(pt, original)

    def test_psk_empty(self) -> None:
        self.assertEqual(encrypt_psk(""), "")
        self.assertEqual(decrypt_psk(""), "")

    def test_is_claimed_requires_token(self) -> None:
        bc = BootstrapConfig(unit_id="RV-Y", claim_state="CLAIMED", unit_token="")
        self.assertFalse(bc.is_claimed())
        bc.unit_token = "x"
        self.assertTrue(bc.is_claimed())


if __name__ == "__main__":
    unittest.main()
