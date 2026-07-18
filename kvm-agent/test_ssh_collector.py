#!/usr/bin/env python3

import json
import unittest
from datetime import datetime

from agent import Agent, usage_panel_visible
from ssh_collector import (
    REMOTE_ACTIVITY_PROBE,
    REMOTE_COLLECTOR,
    build_usage_snapshot,
    parse_activity_entry,
    parse_activity_probe,
    parse_working_processes,
)


class SnapshotTests(unittest.TestCase):
    def test_remote_collector_is_valid_python(self):
        compile(REMOTE_COLLECTOR, "<remote_collector>", "exec")
        compile(REMOTE_ACTIVITY_PROBE, "<remote_activity_probe>", "exec")

    def test_builds_aggregate_snapshot_without_identity_or_content(self):
        today = datetime.now().astimezone().date().isoformat()
        raw = {
            "generatedAt": "2026-07-17T12:00:00Z",
            "identity": {"email": "private@example.com"},
            "providers": [{
                "id": "claude",
                "account": {
                    "source": "Codex app-server",
                    "plan": "Pro",
                    "limits": [{"label": "Weekly limit", "usedPercent": 42}],
                },
                "installation": {"cliInstalled": True, "desktopInstalled": False},
                "authenticated": True,
                "working": False,
                "daily": [{
                    "date": today,
                    "totalTokens": 1200,
                    "inputTokens": 700,
                    "outputTokens": 500,
                    "totalCost": 1.25,
                    "project": "/Users/private/secret-project",
                    "prompt": "confidential prompt",
                    "credential": "secret-token",
                }],
            }],
        }

        snapshot = build_usage_snapshot(raw)
        claude = snapshot["providers"][0]
        encoded = json.dumps(snapshot)

        self.assertEqual(claude["activity"]["today"]["tokens"], 1200)
        self.assertEqual(claude["activity"]["last30DaysCostUSD"], 1.25)
        self.assertEqual(claude["connectionState"], "ready")
        self.assertNotIn("private@example.com", encoded)
        self.assertNotIn("secret-project", encoded)
        self.assertNotIn("confidential prompt", encoded)
        self.assertNotIn("secret-token", encoded)

    def test_working_probe_uses_cpu_and_provider_process_name(self):
        states = parse_working_processes(
            "/opt/homebrew/bin/codex 18.4\n"
            "/opt/homebrew/bin/claude 0.0\n"
            "/Applications/Gemini.app/Contents/MacOS/Gemini 1.2\n"
        )
        self.assertTrue(states["codex"])
        self.assertTrue(states["gemini"])
        self.assertFalse(states["claude"])

    def test_native_codex_daily_buckets_are_account_totals_without_fake_cost(self):
        today = datetime.now().astimezone().date().isoformat()
        snapshot = build_usage_snapshot({
            "providers": [{
                "id": "codex",
                "account": {
                    "source": "Codex app-server",
                    "plan": "Plus",
                    "limits": [{"label": "Weekly limit", "usedPercent": 20}],
                },
                "installation": {"cliInstalled": True},
                "authenticated": True,
                "working": False,
                "tokenScope": "account",
                "daily": [{"date": today, "totalTokens": 4321}],
            }],
        })
        codex = next(item for item in snapshot["providers"] if item["id"] == "codex")
        self.assertEqual(codex["activity"]["today"]["tokens"], 4321)
        self.assertIsNone(codex["activity"]["last30DaysCostUSD"])
        self.assertTrue(codex["accountTokenTotalsAvailable"])
        self.assertEqual(codex["tokenTotalsScope"], "account")

    def test_authorized_device_activity_marks_cross_device_working(self):
        snapshot = {"providers": [{"id": "codex", "working": False}]}
        Agent.apply_activity_states(snapshot, {
            "192.168.0.218": {"codex": False},
            "192.168.0.219": {"codex": True},
        }, "192.168.0.218")
        provider = snapshot["providers"][0]
        self.assertTrue(provider["working"])
        self.assertFalse(provider["deviceWorking"])
        self.assertTrue(provider["authorizedDeviceWorking"])
        self.assertEqual(provider["workingSource"], "authorized_device")

    def test_activity_probe_accepts_only_known_boolean_fields(self):
        states = parse_activity_probe(
            '{"codex":true,"claude":false,"email":"private@example.com"}'
        )
        self.assertTrue(states["codex"])
        self.assertFalse(states["claude"])
        self.assertNotIn("email", states)

    def test_unknown_provider_fields_are_dropped(self):
        snapshot = build_usage_snapshot({
            "providers": [{"id": "unknown", "password": "do-not-copy"}],
        })
        self.assertEqual([item["id"] for item in snapshot["providers"]], [
            "claude", "codex", "copilot", "gemini", "grok",
        ])
        self.assertNotIn("do-not-copy", json.dumps(snapshot))

    def test_protected_claude_credential_requires_local_verification(self):
        snapshot = build_usage_snapshot({
            "providers": [{
                "id": "claude",
                "installation": {
                    "cliInstalled": True,
                    "protectedCredentialDetected": True,
                },
                "authenticated": False,
                "working": False,
                "daily": [],
            }],
        })
        claude = snapshot["providers"][0]
        self.assertEqual(claude["connectionState"], "verification_required")
        self.assertTrue(claude["authentication"]["protectedCredentialDetected"])
        self.assertFalse(claude["authentication"]["authenticated"])

    def test_verification_required_with_daily_totals_renders_usage_panel(self):
        snapshot = build_usage_snapshot({
            "providers": [{
                "id": "claude",
                "installation": {"cliInstalled": True, "protectedCredentialDetected": True},
                "authenticated": False,
                "working": False,
                "daily": [{"date": datetime.now().astimezone().date().isoformat(), "totalTokens": 500}],
            }],
        })
        claude = snapshot["providers"][0]
        self.assertEqual(claude["connectionState"], "verification_required")
        self.assertTrue(claude["trackedTokenTotalsAvailable"])
        self.assertTrue(usage_panel_visible(claude, claude["limits"]))

    def test_verification_required_without_usage_shows_setup(self):
        snapshot = build_usage_snapshot({
            "providers": [{
                "id": "claude",
                "installation": {"cliInstalled": True, "protectedCredentialDetected": True},
                "authenticated": False,
                "working": False,
                "daily": [],
            }],
        })
        claude = snapshot["providers"][0]
        self.assertFalse(usage_panel_visible(claude, claude["limits"]))

    def test_parse_activity_entry_defaults_and_overrides(self):
        self.assertEqual(parse_activity_entry("192.168.1.5", "alice", 22), ("192.168.1.5", "alice", 22))
        self.assertEqual(parse_activity_entry("bob@192.168.1.6", "alice", 22), ("192.168.1.6", "bob", 22))
        self.assertEqual(parse_activity_entry("192.168.1.7:2222", "alice", 22), ("192.168.1.7", "alice", 2222))
        self.assertEqual(
            parse_activity_entry("bob@mac-mini.local:2200", "alice", 22), ("mac-mini.local", "bob", 2200))
        self.assertEqual(parse_activity_entry("fe80::1", "alice", 22), ("fe80::1", "alice", 22))


if __name__ == "__main__":
    unittest.main()
