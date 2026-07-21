#!/usr/bin/env python3

import json
import socket
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from agent import Agent, read_kvm_identity, usage_panel_visible
from ssh_collector import (
    REMOTE_ACTIVITY_PROBE,
    REMOTE_COLLECTOR,
    build_usage_snapshot,
    parse_activity_entry,
    parse_activity_probe,
    parse_working_processes,
)


class SnapshotTests(unittest.TestCase):
    def test_kvm_identity_reads_rm10_firmware_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "etc").mkdir()
            (root / "etc/version").write_text("RK_MODEL=RM10\nRK_VERSION=V1.9.1 release1\n")
            (root / "etc/os-release").write_text('PRETTY_NAME="Buildroot 2024.02"\n')
            with mock.patch.object(socket, "gethostname", return_value="glkvm"):
                identity = read_kvm_identity(root)
        self.assertEqual(identity, {
            "model": "GL.iNet Comet Pro",
            "modelCode": "RM10",
            "firmwareVersion": "V1.9.1 release1",
            "platformVersion": "Buildroot 2024.02",
            "hostname": "glkvm",
        })

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


class ThemeLoaderTests(unittest.TestCase):
    def _load_with(self, content):
        import tempfile
        from unittest import mock
        import agent as agent_module
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as stream:
            stream.write(content)
            path = stream.name
        try:
            with mock.patch.object(agent_module, "THEME_PATH", type(agent_module.THEME_PATH)(path)):
                return agent_module.load_providers()
        finally:
            import os
            os.unlink(path)

    def test_valid_override_is_applied(self):
        themes, display, layout = self._load_with(json.dumps({
            "schemaVersion": 1,
            "providers": {"claude": {"bar": "#123456", "glyph": "gemini"}},
            "display": {"limitEmphasis": "time"},
            "layout": {"preset": "detailed"},
        }))
        self.assertEqual(themes["claude"]["bar"], "#123456")
        self.assertEqual(themes["claude"]["glyph"], "gemini")
        self.assertEqual(themes["codex"]["bar"], "#10a37f")  # untouched provider keeps builtin
        self.assertEqual(display["limitEmphasis"], "time")
        self.assertEqual(layout, {"preset": "detailed"})

    def test_invalid_colors_unknown_keys_and_providers_are_ignored(self):
        themes, display, layout = self._load_with(json.dumps({
            "schemaVersion": 1,
            "layout": {"preset": "nonsense"},
            "providers": {
                "claude": {"bar": "red", "accent": "#12345", "logo": "evil.png", "name": "X",
                           "glyph": "nonsense"},
                "notaprovider": {"bar": "#123456"},
            },
            "display": {"limitEmphasis": "bogus"},
        }))
        self.assertEqual(themes["claude"]["bar"], "#d97757")
        self.assertEqual(themes["claude"]["accent"], "#d97757")
        self.assertEqual(themes["claude"]["logo"], "claude.png")
        self.assertEqual(themes["claude"]["name"], "Claude Code")
        self.assertNotIn("glyph", themes["claude"])
        self.assertNotIn("notaprovider", themes)
        self.assertEqual(display["limitEmphasis"], "percent")
        self.assertEqual(layout, {"preset": "classic"})

    def test_bad_json_and_wrong_schema_fall_back_to_builtin(self):
        import agent as agent_module
        themes, display, layout = self._load_with("{nope")
        self.assertEqual(themes, {
            provider_id: dict(theme) for provider_id, theme in agent_module.BUILTIN_PROVIDERS.items()
        })
        self.assertEqual(display, dict(agent_module.DEFAULT_DISPLAY))
        themes, _, _ = self._load_with(json.dumps({"schemaVersion": 2, "providers": {"claude": {"bar": "#123456"}}}))
        self.assertEqual(themes["claude"]["bar"], "#d97757")

    def test_sanitize_theme_rejects_non_documents(self):
        import agent as agent_module
        self.assertIsNone(agent_module.sanitize_theme(None))
        self.assertIsNone(agent_module.sanitize_theme("text"))
        self.assertIsNone(agent_module.sanitize_theme({"schemaVersion": 3}))
        clean = agent_module.sanitize_theme({
            "schemaVersion": 1,
            "providers": {"grok": {"bar": "#abcdef", "glyph": "claude"}},
            "display": {"limitEmphasis": "time"},
        })
        self.assertEqual(clean, {
            "schemaVersion": 1,
            "providers": {"grok": {"bar": "#abcdef", "glyph": "claude"}},
            "display": {"limitEmphasis": "time"},
        })


class LayoutTests(unittest.TestCase):
    def test_presets_resolve(self):
        import agent as agent_module
        for name in agent_module.LAYOUT_PRESETS:
            layout = agent_module.resolve_layout({"preset": name})
            self.assertTrue(layout["widgets"])
            for widget in layout["widgets"]:
                self.assertIn(widget["widget"], agent_module.WIDGET_TYPES)

    def test_custom_layout_sanitized(self):
        import agent as agent_module
        clean = agent_module._sanitize_layout({"widgets": [
            {"widget": "clock", "x": 218, "y": 100, "w": 246, "h": 40},
            {"widget": "evil", "x": 0, "y": 0, "w": 100, "h": 100},
            {"widget": "limitBar", "x": 400, "y": 0, "w": 400, "h": 60},
            "junk",
        ], "divider": False})
        self.assertEqual(clean, {"widgets": [
            {"widget": "clock", "x": 218, "y": 100, "w": 246, "h": 40},
        ], "divider": False})
        self.assertIsNone(agent_module._sanitize_layout({"widgets": []}))
        self.assertIsNone(agent_module._sanitize_layout({"preset": "bogus"}))

    def test_all_presets_render(self):
        import agent as agent_module
        snap = agent_module.build_preview_snapshot("claude")
        for name in agent_module.LAYOUT_PRESETS:
            image, meta = agent_module.compose_wallpaper(
                snap, "claude", layout_override={"preset": name})
            self.assertEqual(image.size, (480, 160), name)


if __name__ == "__main__":
    unittest.main()
