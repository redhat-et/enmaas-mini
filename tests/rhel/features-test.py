#!/usr/bin/env python3
"""Quota qualification must restore managed files and isolate persistent counters."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from features import body, quota_config, temporary_limits, restore_limits, require_mock


class FeaturesTest(unittest.TestCase):
    def test_settlement_requests_usage_only_for_streamed_chat(self):
        gateway = Mock(model='fixture')
        for path in ('/v1/chat/completions', '/v1/responses', '/v1/messages'):
            for stream in (False, True):
                with self.subTest(path=path, stream=stream):
                    request = body(gateway, path, stream)
                    if path == '/v1/chat/completions' and stream:
                        self.assertEqual(request['stream_options'], {'include_usage': True})
                    else:
                        self.assertNotIn('stream_options', request)

    def test_lab_limits_use_separate_namespaces_without_changing_routes_or_auth(self):
        original = {"listeners": ["private"], "filter_chains": [{"filters": [
            {"filter": "policy", "config_path": "identity"},
            {"filter": "token_rate_limit", "backend": {"kind": "valkey", "url": "${SECRET}", "namespace": "production"},
             "rules": [{"name": "vllm-openai", "capacity": 1000000, "reserved_tokens": 8192, "window": "24h"}]},
            {"filter": "load_balancer", "clusters": [{"endpoints": ["praxis-rhel-mock:18080"]}]}]}]}
        before = copy.deepcopy(original)
        changed = quota_config(original, "abc123")
        filters = changed["filter_chains"][0]["filters"]
        self.assertEqual(original, before)
        self.assertEqual(filters[0], before["filter_chains"][0]["filters"][0])
        self.assertEqual(filters[-1], before["filter_chains"][0]["filters"][-1])
        self.assertEqual(filters[1]["backend"]["url"], "${SECRET}")
        self.assertTrue(filters[1]["backend"]["namespace"].startswith("secure-single-server:limits:qualification:abc123:"))
        self.assertEqual(filters[1]["rules"][0]["capacity"], 20)
        self.assertEqual(filters[1]["rules"][0]["reserved_tokens"], 10)

    def test_real_or_nonfixture_installations_are_refused(self):
        config = {"filter_chains": [{"filters": [{"filter": "load_balancer", "clusters": [
            {"endpoints": ["praxis-rhel-mock:18080"]}]}]}]}
        require_mock({"mock": True, "scenario": "all-in-one", "profile": "memory"}, config, "all-in-one", "memory")
        for state in ({"mock": False}, {"mock": True, "scenario": "remote-gateway", "profile": "memory"}):
            with self.assertRaises(ValueError):
                require_mock(state, config, "all-in-one", "memory")
        config["filter_chains"][0]["filters"][0]["clusters"][0]["endpoints"] = ["api.openai.com:443"]
        with self.assertRaises(ValueError):
            require_mock({"mock": True, "scenario": "all-in-one", "profile": "memory"}, config, "all-in-one", "memory")

    def test_failure_restores_bytes_manifest_and_leaves_other_files_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, manifest, recovery = root / "gateway", root / "manifest", root / "recovery.json"
            original = b'{"filter_chains": []}\n'
            config.write_bytes(original)
            manifest.write_text(hashlib.sha256(original).hexdigest() + "  " + str(config) + "\n")
            before = manifest.read_bytes()
            activate = Mock()
            with self.assertRaisesRegex(RuntimeError, "task failed"):
                with temporary_limits(config, manifest, recovery, os.getgid(), activate=activate) as reset:
                    reset()
                    self.assertTrue(recovery.exists())
                    self.assertEqual(recovery.stat().st_mode & 0o777, 0o600)
                    raise RuntimeError("task failed")
            self.assertEqual(config.read_bytes(), original)
            self.assertEqual(manifest.read_bytes(), before)
            self.assertFalse(recovery.exists())
            self.assertEqual(activate.call_count, 2)

    def test_recovery_refuses_to_overwrite_intervening_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, manifest, recovery = root / "gateway", root / "manifest", root / "recovery.json"
            config.write_text('{"filter_chains": []}')
            manifest.write_text(hashlib.sha256(config.read_bytes()).hexdigest() + "  " + str(config) + "\n")
            with self.assertRaisesRegex(ValueError, "changed"):
                with temporary_limits(config, manifest, recovery, os.getgid(), activate=Mock()) as reset:
                    reset()
                    config.write_text("intervening administrator change")
            self.assertTrue(recovery.exists())
            self.assertEqual(config.read_text(), "intervening administrator change")
            with self.assertRaises(ValueError):
                restore_limits(config, manifest, recovery, os.getgid(), activate=Mock())


if __name__ == "__main__":
    unittest.main()
