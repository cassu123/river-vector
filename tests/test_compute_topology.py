"""Tests for core/compute_topology.py and its bootstrap integration."""

import json
import os
import tempfile
import unittest

from core.bootstrap import BootstrapConfig, load_bootstrap, save_bootstrap
from core.compute_topology import (
    ALL_ROLES,
    ROLE_CONTROL,
    ROLE_VISION,
    TOPOLOGY_SOLO,
    TOPOLOGY_SPLIT,
    ComputeProfile,
    ComputeTopologyError,
)


class TestDefaults(unittest.TestCase):
    def test_default_solo_owns_everything(self):
        p = ComputeProfile.default_solo("box-1")
        self.assertEqual(p.topology, TOPOLOGY_SOLO)
        self.assertEqual(set(p.roles), set(ALL_ROLES))
        for role in ALL_ROLES:
            self.assertTrue(p.owns_role(role))
            self.assertIsNone(p.peer_url(role))
        self.assertTrue(p.is_valid(), p.problems())

    def test_from_empty_dict_is_solo(self):
        # Old bootstraps without a compute block must keep working.
        self.assertTrue(ComputeProfile.from_dict(None).is_valid())
        self.assertTrue(ComputeProfile.from_dict({}).is_valid())
        self.assertEqual(ComputeProfile.from_dict({}).topology, TOPOLOGY_SOLO)


class TestSplit(unittest.TestCase):
    def test_control_node_with_vision_peer_is_valid(self):
        p = ComputeProfile(
            node_id="voyager-ctrl",
            topology=TOPOLOGY_SPLIT,
            roles=[ROLE_CONTROL],
            peers={ROLE_VISION: "http://10.55.0.2:8090"},
        )
        self.assertTrue(p.is_valid(), p.problems())
        self.assertTrue(p.owns_role(ROLE_CONTROL))
        self.assertFalse(p.owns_role(ROLE_VISION))
        self.assertEqual(p.peer_url(ROLE_VISION), "http://10.55.0.2:8090")
        self.assertEqual(p.location_of(ROLE_VISION), "http://10.55.0.2:8090")

    def test_vision_node_is_valid(self):
        p = ComputeProfile(
            node_id="voyager-vision",
            topology=TOPOLOGY_SPLIT,
            roles=[ROLE_VISION],
            peers={},
        )
        self.assertTrue(p.is_valid(), p.problems())
        self.assertFalse(p.owns_role(ROLE_CONTROL))


class TestValidationRejections(unittest.TestCase):
    def test_safety_critical_role_may_not_be_delegated(self):
        p = ComputeProfile(
            topology=TOPOLOGY_SPLIT,
            roles=[ROLE_VISION],
            peers={ROLE_CONTROL: "http://10.55.0.3:9000"},
        )
        self.assertFalse(p.is_valid())
        self.assertTrue(any("safety-critical" in m for m in p.problems()))
        with self.assertRaises(ComputeTopologyError):
            p.validated()

    def test_unknown_topology_rejected(self):
        p = ComputeProfile(topology="quantum", roles=[ROLE_CONTROL, ROLE_VISION])
        self.assertFalse(p.is_valid())

    def test_unknown_role_rejected(self):
        p = ComputeProfile(topology=TOPOLOGY_SOLO, roles=["control", "telepathy"])
        self.assertFalse(p.is_valid())

    def test_role_both_local_and_peer_rejected(self):
        p = ComputeProfile(
            topology=TOPOLOGY_SPLIT,
            roles=[ROLE_CONTROL, ROLE_VISION],
            peers={ROLE_VISION: "http://x:8090"},
        )
        self.assertFalse(p.is_valid())

    def test_solo_must_own_every_role(self):
        p = ComputeProfile(topology=TOPOLOGY_SOLO, roles=[ROLE_CONTROL])
        self.assertFalse(p.is_valid())

    def test_solo_must_not_have_peers(self):
        p = ComputeProfile(
            topology=TOPOLOGY_SOLO,
            roles=list(ALL_ROLES),
            peers={ROLE_VISION: "http://x:8090"},
        )
        self.assertFalse(p.is_valid())


class TestSerialization(unittest.TestCase):
    def test_roundtrip(self):
        p = ComputeProfile(
            node_id="ctrl",
            topology=TOPOLOGY_SPLIT,
            roles=[ROLE_CONTROL],
            peers={ROLE_VISION: "http://10.55.0.2:8090"},
        )
        again = ComputeProfile.from_dict(p.to_dict())
        self.assertEqual(again.node_id, p.node_id)
        self.assertEqual(again.topology, p.topology)
        self.assertEqual(again.roles, p.roles)
        self.assertEqual(again.peers, p.peers)


class TestBootstrapIntegration(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_bootstrap_default_compute_is_solo(self):
        bc = BootstrapConfig()
        self.assertEqual(bc.compute.topology, TOPOLOGY_SOLO)
        self.assertTrue(bc.compute.is_valid())

    def test_bootstrap_roundtrip_preserves_split_compute(self):
        bc = BootstrapConfig(unit_id="RV-TEST")
        bc.compute = ComputeProfile(
            node_id="ctrl",
            topology=TOPOLOGY_SPLIT,
            roles=[ROLE_CONTROL],
            peers={ROLE_VISION: "http://10.55.0.2:8090"},
        )
        save_bootstrap(bc, self.path)
        loaded = load_bootstrap(self.path)
        self.assertEqual(loaded.compute.topology, TOPOLOGY_SPLIT)
        self.assertEqual(loaded.compute.roles, [ROLE_CONTROL])
        self.assertEqual(loaded.compute.peer_url(ROLE_VISION), "http://10.55.0.2:8090")

    def test_legacy_bootstrap_without_compute_loads_as_solo(self):
        # Simulate a bootstrap written before compute topology existed.
        legacy = {
            "protocol_version": 1,
            "unit_id": "RV-OLD",
            "claim_state": "CLAIMED",
            "unit_token": "tok",
            "firmware_version": "0.2.0",
            "server": {"url_primary": "https://riversongai.com",
                       "url_fallback": "http://192.168.1.221:8000"},
            "wifi_networks": [],
        }
        with open(self.path, "w") as f:
            json.dump(legacy, f)
        loaded = load_bootstrap(self.path)
        self.assertEqual(loaded.compute.topology, TOPOLOGY_SOLO)
        self.assertTrue(loaded.compute.owns_role(ROLE_VISION))


if __name__ == "__main__":
    unittest.main()
