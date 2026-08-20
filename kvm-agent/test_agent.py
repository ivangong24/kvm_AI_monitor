#!/usr/bin/env python3
"""Unit tests for the agent's console authentication (session cookies + admin-credential relay)."""

import base64
import io
import json
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("KVM_AI_USAGE_AUTH_SECRET", "/tmp/kvm-ai-usage-test-secret")
os.environ.setdefault("KVM_AI_USAGE_PROVISION_TOKEN", "/tmp/kvm-ai-usage-test-provision")

import agent  # noqa: E402


class SessionCookieTests(unittest.TestCase):
    def test_valid_cookie_round_trips(self):
        self.assertTrue(agent.session_cookie_valid(agent.issue_session_cookie()))

    def test_rejects_tampered_signature(self):
        cookie = agent.issue_session_cookie()
        flipped = cookie[:-1] + ("0" if cookie[-1] != "0" else "1")
        self.assertFalse(agent.session_cookie_valid(flipped))

    def test_rejects_garbage_and_empty(self):
        for value in ("", "no-dot", "a.b.c", None, 123):
            self.assertFalse(agent.session_cookie_valid(value))

    def test_rejects_expired(self):
        payload = base64.urlsafe_b64encode(
            json.dumps({"exp": int(time.time()) - 5}).encode()
        ).decode().rstrip("=")
        import hashlib
        import hmac
        signature = hmac.new(agent.auth_secret(), payload.encode(), hashlib.sha256).hexdigest()
        self.assertFalse(agent.session_cookie_valid(f"{payload}.{signature}"))

    def test_request_session_token_parses_cookie_header(self):
        handler = mock.Mock()
        handler.headers = {"Cookie": f"other=1; {agent.SESSION_COOKIE}=abc.def; x=y"}
        self.assertEqual(agent.request_session_token(handler), "abc.def")
        handler.headers = {}
        self.assertEqual(agent.request_session_token(handler), "")


class ProvisionTokenTests(unittest.TestCase):
    def test_token_persists_and_authorizes(self):
        token = agent.provision_token()
        self.assertGreaterEqual(len(token), 16)
        self.assertEqual(token, agent.provision_token())  # cached and stable across calls
        handler = SimpleNamespace(headers={"X-KVM-Provision": token})
        self.assertTrue(agent.Handler.provision_authorized(handler))

    def test_rejects_wrong_or_missing_token(self):
        for headers in ({"X-KVM-Provision": "not-the-token"}, {"X-KVM-Provision": ""}, {}):
            handler = SimpleNamespace(headers=headers)
            self.assertFalse(agent.Handler.provision_authorized(handler))


class AdminRelayTests(unittest.TestCase):
    def _response(self, payload):
        response = mock.MagicMock()
        response.read.return_value = json.dumps(payload).encode()
        response.__enter__.return_value = response
        return response

    def test_valid_credentials_when_comet_returns_token(self):
        with mock.patch("agent.urllib.request.urlopen", return_value=self._response({"result": {"token": "t"}})):
            self.assertTrue(agent.verify_admin_credentials("pw", "123456"))

    def test_invalid_when_comet_rejects(self):
        with mock.patch("agent.urllib.request.urlopen", return_value=self._response({"ok": False})):
            self.assertFalse(agent.verify_admin_credentials("bad", ""))

    def test_invalid_on_network_failure(self):
        with mock.patch("agent.urllib.request.urlopen", side_effect=OSError("refused")):
            self.assertFalse(agent.verify_admin_credentials("pw", ""))

    def test_empty_password_never_calls_comet(self):
        with mock.patch("agent.urllib.request.urlopen") as urlopen:
            self.assertFalse(agent.verify_admin_credentials("", ""))
        urlopen.assert_not_called()

    def test_password_and_totp_are_concatenated(self):
        captured = {}

        def fake_urlopen(request, *args, **kwargs):
            captured["body"] = request.data.decode()
            return self._response({"result": {"token": "t"}})

        with mock.patch("agent.urllib.request.urlopen", side_effect=fake_urlopen):
            agent.verify_admin_credentials("secret", "999888")
        self.assertIn('name="passwd"', captured["body"])
        self.assertIn("secret999888", captured["body"])


class PrimaryStorageTests(unittest.TestCase):
    # Mirrors the Comet Pro layout: `/` is a tiny overlay, the firmware is read-only squashfs, and
    # the real storage is the 28.8 GB /userdata/media partition.
    MOUNTS = (
        "/dev/root /rom squashfs ro,relatime 0 0\n"
        "tmpfs /tmp tmpfs rw,relatime 0 0\n"
        "/dev/mmcblk0p8 /userdata ext4 rw,relatime 0 0\n"
        "/dev/mmcblk0p10 /userdata/media exfat rw,relatime 0 0\n"
        "overlay:/overlay / overlay rw,noatime 0 0\n"
    )
    SIZES = {"/rom": (275e6, 275e6), "/userdata": (974e6, 16e6), "/userdata/media": (28.8e9, 0.1e9)}

    def _fake_statvfs(self, path):
        if path not in self.SIZES:
            raise OSError("no such mount")
        total, used = self.SIZES[path]
        frsize = 4096
        return SimpleNamespace(f_frsize=frsize, f_blocks=int(total / frsize),
                               f_bavail=int((total - used) / frsize))

    def test_picks_largest_real_writable_partition(self):
        with mock.patch("builtins.open", side_effect=lambda *a, **k: io.StringIO(self.MOUNTS)), \
             mock.patch("agent.os.statvfs", side_effect=self._fake_statvfs):
            stats = agent.primary_storage_stats()
        # Not the ~1 GB overlay or the read-only firmware — the 28.8 GB media partition.
        self.assertEqual(stats["diskMount"], "/userdata/media")
        self.assertEqual(stats["diskTotalGb"], 28.8)
        self.assertEqual(stats["diskPercent"], 0)

    def test_falls_back_to_root_when_no_real_mounts(self):
        with mock.patch("builtins.open", side_effect=lambda *a, **k: io.StringIO("tmpfs /tmp tmpfs rw 0 0\n")), \
             mock.patch("agent.shutil.disk_usage", return_value=SimpleNamespace(total=int(1e9), free=int(5e8))):
            stats = agent.primary_storage_stats()
        self.assertEqual(stats["diskMount"], "/")


class StaleUsageTests(unittest.TestCase):
    """What the screen says once the enrolled device stops reporting: the frozen snapshot must
    read as history, and any quota whose window has since rolled over must stop showing a number."""

    def _provider(self, **overrides):
        provider = {"id": "claude", "name": "Claude Code", "connectionState": "ready",
                    "limits": [], "activity": {}}
        provider.update(overrides)
        return provider

    def test_chip_reports_offline_when_no_device_has_pushed_recently(self):
        self.assertEqual(agent.status_chip_text(self._provider()), "READY")
        self.assertEqual(agent.status_chip_text(self._provider(usageStale=True)), "OFFLINE")
        # A device that is working is by definition reporting; setup states still win.
        self.assertEqual(agent.status_chip_text(self._provider(usageStale=True, working=True)), "WORK")
        self.assertEqual(
            agent.status_chip_text(self._provider(usageStale=True, connectionState="not_installed")),
            "SETUP")

    def test_reset_label_stops_counting_down_to_a_moment_already_past(self):
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(agent.reset_label((now + timedelta(hours=3)).isoformat(), now), "RESETS IN 3H")
        self.assertEqual(agent.reset_label((now - timedelta(hours=3)).isoformat(), now), "WAITING FOR DATA")
        self.assertEqual(agent.short_reset((now - timedelta(hours=3)).isoformat()), "--")

    def test_age_label_wording(self):
        self.assertEqual(agent.age_label(30), "JUST NOW")
        self.assertEqual(agent.age_label(18 * 60), "18M AGO")
        self.assertEqual(agent.age_label(5 * 3600), "5H AGO")
        self.assertEqual(agent.age_label(3 * 86400), "3D AGO")
        self.assertIsNone(agent.age_label(None))

    def test_overlay_carries_freshness_onto_the_provider(self):
        snapshot = {"providers": [self._provider()]}
        agent.Agent.apply_usage_overlay(snapshot, {"claude": {
            "plan": "Max", "limits": [{"label": "Weekly limit", "usedPercent": 20}], "daily": [],
            "loggedIn": True, "stale": True, "ageSeconds": 7200,
            "lastPushAt": "2026-08-20T10:00:00Z", "limitsStale": True, "limitsAgeSeconds": 7200,
        }})
        provider = snapshot["providers"][0]
        self.assertTrue(provider["usageStale"])
        self.assertEqual(provider["usageAgeSeconds"], 7200)
        self.assertEqual(agent.status_chip_text(provider), "OFFLINE")
        self.assertTrue(agent.summarize_provider(provider)["usageStale"])

    def _stale_republish(self, minutes_since_success, limit):
        """Drive Agent.republish_as_stale() against a stub instance (a real Agent needs the KVM)."""
        import threading
        provider = self._provider(limits=[limit])
        stub = SimpleNamespace(
            lock=threading.Lock(),
            snapshot={"providers": [provider]},
            state={"lastSuccessAt": (datetime.now(timezone.utc)
                                     - timedelta(minutes=minutes_since_success)).isoformat()},
            republished=False,
        )
        stub.republish = lambda: setattr(stub, "republished", True)
        agent.Agent.republish_as_stale(stub)
        return stub, provider

    def test_unreachable_device_redraws_its_last_reading_as_history(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        stub, provider = self._stale_republish(180, {
            "label": "Current session", "usedPercent": 78, "windowMinutes": 300, "resetsAt": past})
        self.assertTrue(stub.republished)
        self.assertTrue(provider["usageStale"])
        self.assertEqual(agent.status_chip_text(provider), "OFFLINE")
        self.assertNotIn("usedPercent", provider["limits"][0])

    def test_a_brief_outage_is_not_yet_stale(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        stub, provider = self._stale_republish(5, {
            "label": "Current session", "usedPercent": 78, "windowMinutes": 300, "resetsAt": future})
        self.assertFalse(stub.republished)
        self.assertNotIn("usageStale", provider)
        self.assertEqual(provider["limits"][0]["usedPercent"], 78)

    def test_bars_age_from_the_limits_not_from_the_push(self):
        # A device whose Claude login died keeps pushing every minute with no limits to report, so
        # the panel's numbers are ancient while the device is plainly online. The bars must age
        # from the reading they show, not from the last push.
        provider = self._provider(usageStale=False, usageAgeSeconds=30,
                                  limitsStale=True, limitsAgeSeconds=6 * 86400)
        self.assertEqual(agent.age_label(agent.limits_age_seconds(provider)), "6D AGO")
        self.assertEqual(agent.status_chip_text(provider), "READY")
        # With nothing pushing at all, the push age is the best available answer.
        offline = self._provider(usageStale=True, usageAgeSeconds=3600)
        self.assertEqual(agent.age_label(agent.limits_age_seconds(offline)), "60M AGO")
        self.assertIsNone(agent.limits_age_seconds(self._provider()))

    def test_expired_limit_renders_as_unknown_not_as_a_number(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        provider = self._provider(limits=[
            {"label": "Current session", "windowMinutes": 300, "resetsAt": past, "expired": True}])
        limit = agent.summarize_usage(provider)["limits"][0]
        self.assertIsNone(limit["usedPercent"])
        self.assertIsNone(limit["resetLabel"])
        self.assertTrue(limit["expired"])


if __name__ == "__main__":
    unittest.main()
