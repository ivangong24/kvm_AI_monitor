#!/usr/bin/env python3

import hashlib
import hmac
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from push_receiver import DeviceStore, PushReceiver


def sign(secret: str, ts: str, nonce: str, method: str, path: str, body: bytes) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    string_to_sign = f"v1\n{ts}\n{nonce}\n{method}\n{path}\n{body_hash}"
    return hmac.new(secret.encode("ascii"), string_to_sign.encode("ascii"), hashlib.sha256).hexdigest()


class PushReceiverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = DeviceStore(self.tmp / "devices.json")
        self.time = 1752800000.0
        self.receiver = PushReceiver(self.store, self.tmp / "push-state.json", clock=lambda: self.time)

    def push(self, device, event_or_body, path="/push/v1/usage", nonce="0" * 16, ts=None):
        ts = str(int(self.time if ts is None else ts))
        body = json.dumps(event_or_body, separators=(",", ":")).encode()
        signature = sign(device["secret"], ts, nonce, "POST", path, body)
        return self.receiver.verify(device["id"], ts, nonce, signature, "POST", path, body)

    def test_vector_signature_matches_spec(self):
        secret = "0123456789abcdef0123456789abcdef0123456789abcdef"
        self.store.devices["d-test0001"] = {
            "id": "d-test0001", "name": "vector", "secret": secret,
            "createdAt": "2026-07-18T00:00:00Z", "revoked": False,
            "lastSeenAt": None, "lastUsageAt": None, "lastActivityAt": None,
        }
        body = b'{"event":"active","provider":"claude","schemaVersion":1}'
        signature = "4d23e79d847fee9e540fa1b24e8328681fab6e77043fff7d0503cef0f2832ead"
        result = self.receiver.verify("d-test0001", "1752800000", "abcdef0123456789", signature,
                                      "POST", "/push/v1/activity", body)
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], "d-test0001")

    def test_wrong_signature_rejected(self):
        device = self.store.create("laptop")
        ts = str(int(self.time))
        nonce = "1" * 16
        body = b"{}"
        bad_signature = "0" * 64
        result = self.receiver.verify(device["id"], ts, nonce, bad_signature, "POST", "/push/v1/usage", body)
        self.assertIsNone(result)

    def test_stale_timestamp_rejected_both_directions(self):
        device = self.store.create("laptop")
        body = b"{}"
        old_ts = str(int(self.time) - 121)
        old_nonce = "2" * 16
        old_signature = sign(device["secret"], old_ts, old_nonce, "POST", "/push/v1/usage", body)
        self.assertIsNone(self.receiver.verify(
            device["id"], old_ts, old_nonce, old_signature, "POST", "/push/v1/usage", body))
        future_ts = str(int(self.time) + 121)
        future_nonce = "3" * 16
        future_signature = sign(device["secret"], future_ts, future_nonce, "POST", "/push/v1/usage", body)
        self.assertIsNone(self.receiver.verify(
            device["id"], future_ts, future_nonce, future_signature, "POST", "/push/v1/usage", body))

    def test_nonce_replay_rejected(self):
        device = self.store.create("laptop")
        body = {"schemaVersion": 1, "provider": "claude", "event": "start"}
        first = self.push(device, body, path="/push/v1/activity", nonce="4" * 16)
        self.assertIsNotNone(first)
        second = self.push(device, body, path="/push/v1/activity", nonce="4" * 16)
        self.assertIsNone(second)

    def test_revoked_device_rejected(self):
        device = self.store.create("laptop")
        self.store.revoke(device["id"])
        result = self.push(device, {}, nonce="5" * 16)
        self.assertIsNone(result)

    def test_rate_limit(self):
        device = self.store.create("laptop")
        for _ in range(120):
            self.assertFalse(self.receiver.rate_limited(device["id"]))
        self.assertTrue(self.receiver.rate_limited(device["id"]))

    def test_usage_whitelist_drops_forbidden_fields(self):
        device = self.store.create("laptop")
        today = datetime.now().astimezone().date().isoformat()
        body = {
            "schemaVersion": 1, "provider": "claude", "plan": "Max", "loggedIn": True,
            "email": "person@example.com",
            "accessToken": "sk-super-secret-token",
            "prompt": "please summarize this confidential document",
            "projectPath": "/Users/person/secret-project",
            "sessionId": "session-abc-123",
            "limits": [{"label": "Current session", "usedPercent": 50, "windowMinutes": 300}],
            "daily": [{"date": today, "totalTokens": 100}],
        }
        self.assertTrue(self.receiver.handle_usage(device, body))
        encoded = (self.tmp / "push-state.json").read_text()
        self.assertNotIn("person@example.com", encoded)
        self.assertNotIn("sk-super-secret-token", encoded)
        self.assertNotIn("confidential", encoded)
        self.assertNotIn("secret-project", encoded)
        self.assertNotIn("session-abc-123", encoded)
        self.assertIn("Max", encoded)

    def test_limits_retained_when_next_push_lacks_them(self):
        device = self.store.create("laptop")
        limits = [{"label": "Weekly limit", "usedPercent": 13, "windowMinutes": 10080}]
        self.assertTrue(self.receiver.handle_usage(
            device, {"schemaVersion": 1, "provider": "claude", "loggedIn": True, "limits": limits}))
        self.assertTrue(self.receiver.handle_usage(
            device, {"schemaVersion": 1, "provider": "claude", "loggedIn": True}))
        overlay = self.receiver.usage_overlay()["claude"]
        self.assertEqual(overlay["limits"][0]["usedPercent"], 13)

    def test_daily_date_window_filtering(self):
        device = self.store.create("laptop")
        today = datetime.now().astimezone().date()
        body = {
            "schemaVersion": 1, "provider": "claude",
            "daily": [
                {"date": today.isoformat(), "totalTokens": 10},
                {"date": (today - timedelta(days=15)).isoformat(), "totalTokens": 20},
                {"date": (today - timedelta(days=40)).isoformat(), "totalTokens": 999},
                {"date": (today + timedelta(days=1)).isoformat(), "totalTokens": 999},
                {"date": "not-a-date", "totalTokens": 999},
            ],
        }
        self.assertTrue(self.receiver.handle_usage(device, body))
        stored = self.receiver.usage[(device["id"], "claude")]
        dates = [entry["date"] for entry in stored["daily"]]
        self.assertEqual(len(dates), 2)
        self.assertIn(today.isoformat(), dates)
        self.assertIn((today - timedelta(days=15)).isoformat(), dates)

    def test_activity_lifecycle_and_expiry(self):
        device = self.store.create("laptop")
        self.assertTrue(self.receiver.handle_activity(
            device, {"schemaVersion": 1, "provider": "claude", "event": "start"}))
        self.assertTrue(self.receiver.active_providers()["claude"])
        self.time += 119
        self.assertTrue(self.receiver.active_providers()["claude"])
        self.time += 2
        self.assertFalse(self.receiver.active_providers()["claude"])
        self.assertTrue(self.receiver.handle_activity(
            device, {"schemaVersion": 1, "provider": "claude", "event": "active"}))
        self.assertTrue(self.receiver.active_providers()["claude"])
        self.assertTrue(self.receiver.handle_activity(
            device, {"schemaVersion": 1, "provider": "claude", "event": "stop"}))
        self.assertFalse(self.receiver.active_providers()["claude"])

    def test_usage_overlay_merge_across_devices(self):
        device_a = self.store.create("desktop")
        device_b = self.store.create("laptop")
        today = datetime.now().astimezone().date().isoformat()
        self.receiver.handle_usage(device_a, {
            "schemaVersion": 1, "provider": "claude", "plan": "Pro", "loggedIn": True,
            "limits": [{"label": "Current session", "usedPercent": 10}],
            "daily": [{"date": today, "totalTokens": 100}],
        })
        self.time += 1
        self.receiver.handle_usage(device_b, {
            "schemaVersion": 1, "provider": "claude", "plan": "Max", "loggedIn": True,
            "limits": [{"label": "Current session", "usedPercent": 90}],
            "daily": [{"date": today, "totalTokens": 50}],
        })
        overlay = self.receiver.usage_overlay()["claude"]
        self.assertEqual(overlay["plan"], "Max")
        self.assertEqual(overlay["limits"][0]["usedPercent"], 90)
        self.assertEqual(overlay["daily"][0]["totalTokens"], 150)
        self.assertTrue(overlay["loggedIn"])
        self.assertIsNone(self.receiver.usage_overlay()["codex"])

    def test_persistence_round_trip(self):
        device = self.store.create("laptop")
        self.receiver.handle_usage(device, {"schemaVersion": 1, "provider": "codex", "plan": "Plus", "loggedIn": True})
        self.receiver.handle_activity(device, {"schemaVersion": 1, "provider": "codex", "event": "start"})
        self.assertTrue(self.receiver.active_providers()["codex"])
        reloaded = PushReceiver(self.store, self.tmp / "push-state.json", clock=lambda: self.time)
        self.assertEqual(reloaded.usage[(device["id"], "codex")]["plan"], "Plus")
        self.assertFalse(reloaded.active_providers()["codex"])

    def test_device_store_lifecycle_hides_secret_from_list(self):
        created = self.store.create("Mac mini")
        self.assertTrue(created["id"].startswith("d-"))
        self.assertEqual(len(created["secret"]), 48)
        listed = self.store.list()
        self.assertEqual(len(listed), 1)
        self.assertNotIn("secret", listed[0])
        rotated = self.store.rotate(created["id"])
        self.assertNotEqual(rotated["secret"], created["secret"])
        self.assertTrue(self.store.revoke(created["id"]))
        self.assertTrue(self.store.get(created["id"])["revoked"])
        self.assertTrue(self.store.delete(created["id"]))
        self.assertIsNone(self.store.get(created["id"]))

    def test_device_name_sanitized_and_length_bounded(self):
        created = self.store.create("  Mac\x07 mini  " + "x" * 60)
        self.assertNotIn("\x07", created["name"])
        self.assertLessEqual(len(created["name"]), 48)
        with self.assertRaises(ValueError):
            self.store.create("\x01\x02")


if __name__ == "__main__":
    unittest.main()
