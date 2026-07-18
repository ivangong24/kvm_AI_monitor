#!/usr/bin/env python3
"""Offline unit tests for the macOS push helper. No network, no `claude`/`security` binaries,
no real Keychain access — all subprocess/network adapters are patched. Run with:
    python3 mac-helper/test_helper.py
"""

import datetime
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kvm_ai_push as helper  # noqa: E402

TODAY = datetime.date.today()
DAY0 = TODAY.isoformat()
DAY1 = (TODAY - datetime.timedelta(days=1)).isoformat()

PLANTED_TOKEN = "sk-ant-oat01-FAKE-TOKEN-DO-NOT-SHIP"
PLANTED_PROMPT = "please summarize this confidential customer email thread"
PLANTED_PROJECT = "/Users/hidden/secret-internal-project"
PLANTED_SESSION = "session-id-should-never-leave-this-machine"


def fixture_event(date, message_id, input_tokens, output_tokens, cache_read=0, cache_creation=0):
    return {
        "type": "assistant",
        "timestamp": date + "T12:00:00.000Z",
        "cwd": PLANTED_PROJECT,
        "sessionId": PLANTED_SESSION,
        "authToken": PLANTED_TOKEN,
        "message": {
            "id": message_id,
            "content": [{"type": "text", "text": PLANTED_PROMPT}],
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
            },
        },
    }


class HomeIsolatedTestCase(unittest.TestCase):
    """Points $HOME at a scratch directory so tests never touch the real machine's files."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        patcher = mock.patch.dict(os.environ, {"HOME": self.tempdir.name})
        patcher.start()
        self.addCleanup(patcher.stop)


class SigningTests(unittest.TestCase):
    def test_matches_push_protocol_test_vector(self):
        body = b'{"event":"active","provider":"claude","schemaVersion":1}'
        signature = helper.sign(
            "0123456789abcdef0123456789abcdef0123456789abcdef",
            "1752800000",
            "abcdef0123456789",
            "POST",
            "/push/v1/activity",
            body,
        )
        self.assertEqual(
            signature,
            "4d23e79d847fee9e540fa1b24e8328681fab6e77043fff7d0503cef0f2832ead",
        )

    def test_signature_changes_with_method_case_normalized(self):
        body = b"{}"
        lower = helper.sign("secret", "1", "n", "post", "/x", body)
        upper = helper.sign("secret", "1", "n", "POST", "/x", body)
        self.assertEqual(lower, upper)


class ThrottleTests(HomeIsolatedTestCase):
    def test_not_throttled_when_marker_absent(self):
        self.assertFalse(helper.throttled())

    def test_throttled_immediately_after_marking(self):
        helper.mark_activity_sent()
        self.assertTrue(helper.throttled())

    def test_not_throttled_once_window_elapses(self):
        helper.mark_activity_sent()
        marker = helper.activity_marker_path()
        stale = time.time() - helper.ACTIVE_THROTTLE_SECONDS - 1
        marker.write_text(str(int(stale)))
        self.assertFalse(helper.throttled())

    def test_marker_file_contains_only_a_timestamp(self):
        helper.mark_activity_sent()
        content = helper.activity_marker_path().read_text().strip()
        self.assertRegex(content, r"^\d+$")

    @mock.patch("kvm_ai_push.load_config", side_effect=RuntimeError("no config"))
    def test_send_activity_active_skips_silently_when_throttled(self, mock_config):
        helper.mark_activity_sent()
        args = mock.Mock(event="active")
        helper.cmd_send_activity(args)  # must return without raising or calling load_config
        mock_config.assert_not_called()


class ClaudeDailyTests(HomeIsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.project_dir = pathlib.Path(self.tempdir.name) / ".claude" / "projects" / "proj"
        self.project_dir.mkdir(parents=True)

    def _write(self, name, events):
        with (self.project_dir / name).open("w") as stream:
            for event in events:
                stream.write(json.dumps(event) + "\n")

    def test_sums_tokens_per_date_across_files(self):
        self._write("a.jsonl", [
            fixture_event(DAY0, "msg-1", 100, 50),
            fixture_event(DAY0, "msg-2", 10, 5, cache_read=2, cache_creation=1),
        ])
        self._write("b.jsonl", [
            fixture_event(DAY1, "msg-3", 7, 3),
        ])
        by_date = {row["date"]: row for row in helper.claude_daily()}
        self.assertEqual(set(by_date), {DAY0, DAY1})
        self.assertEqual(by_date[DAY0]["inputTokens"], 110)
        self.assertEqual(by_date[DAY0]["outputTokens"], 55)
        self.assertEqual(by_date[DAY0]["cacheReadTokens"], 2)
        self.assertEqual(by_date[DAY0]["cacheCreationTokens"], 1)
        self.assertEqual(by_date[DAY0]["totalTokens"], 110 + 55 + 2 + 1)
        self.assertEqual(by_date[DAY1]["totalTokens"], 10)

    def test_dedups_by_message_id_without_double_counting(self):
        self._write("a.jsonl", [
            fixture_event(DAY0, "dup-1", 100, 50),
            fixture_event(DAY0, "dup-1", 100, 50),  # exact repeat of the same message
        ])
        by_date = {row["date"]: row for row in helper.claude_daily()}
        self.assertEqual(by_date[DAY0]["inputTokens"], 100)
        self.assertEqual(by_date[DAY0]["outputTokens"], 50)

    def test_dedup_keeps_max_for_partial_duplicate_records(self):
        # A resumed transcript can re-emit the same message id with a smaller partial usage;
        # ssh_collector.claude_daily() takes the max per field rather than summing duplicates.
        self._write("a.jsonl", [
            fixture_event(DAY0, "dup-2", 100, 50),
            fixture_event(DAY0, "dup-2", 40, 20),
        ])
        row = next(r for r in helper.claude_daily() if r["date"] == DAY0)
        self.assertEqual(row["inputTokens"], 100)
        self.assertEqual(row["outputTokens"], 50)

    def test_ignores_non_assistant_events_and_missing_usage(self):
        self._write("a.jsonl", [
            {"type": "user", "timestamp": DAY0 + "T00:00:00Z", "message": {"id": "u1"}},
            {"type": "assistant", "timestamp": DAY0 + "T00:00:00Z", "message": {"id": "m-no-usage"}},
        ])
        self.assertEqual(helper.claude_daily(), [])

    def test_drops_records_older_than_30_days(self):
        old_date = (TODAY - datetime.timedelta(days=40)).isoformat()
        self._write("a.jsonl", [fixture_event(old_date, "old-1", 100, 50)])
        self.assertEqual(helper.claude_daily(), [])

    def test_no_projects_directory_returns_empty(self):
        for entry in self.project_dir.iterdir():
            entry.unlink()
        self.project_dir.rmdir()
        self.assertEqual(helper.claude_daily(), [])


class PayloadNonDisclosureTests(HomeIsolatedTestCase):
    def setUp(self):
        super().setUp()
        project_dir = pathlib.Path(self.tempdir.name) / ".claude" / "projects" / "proj"
        project_dir.mkdir(parents=True)
        with (project_dir / "a.jsonl").open("w") as stream:
            stream.write(json.dumps(fixture_event(DAY0, "msg-1", 100, 50)) + "\n")

    @mock.patch("kvm_ai_push.claude_usage_cli_limits", return_value=[])
    @mock.patch("kvm_ai_push.keychain_oauth_limits", return_value=[])
    @mock.patch("kvm_ai_push.claude_auth_status", return_value=(True, "Max"))
    def test_payload_has_only_whitelisted_fields_and_no_planted_secrets(self, _auth, _keychain, _cli):
        payload = helper.build_usage_payload()
        encoded = json.dumps(payload)

        self.assertNotIn(PLANTED_TOKEN, encoded)
        self.assertNotIn(PLANTED_PROMPT, encoded)
        self.assertNotIn(PLANTED_PROJECT, encoded)
        self.assertNotIn(PLANTED_SESSION, encoded)
        self.assertNotIn("secret-internal-project", encoded)

        allowed = {"schemaVersion", "provider", "collectedAt", "plan", "loggedIn", "limits", "daily"}
        self.assertTrue(set(payload.keys()) <= allowed)
        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual(payload["provider"], "claude")
        self.assertEqual(payload["plan"], "Max")
        self.assertTrue(payload["loggedIn"])

        for row in payload.get("daily", []):
            self.assertEqual(
                set(row.keys()),
                {"date", "totalTokens", "inputTokens", "outputTokens", "cacheReadTokens", "cacheCreationTokens"},
            )

    @mock.patch("kvm_ai_push.claude_usage_cli_limits", return_value=[])
    @mock.patch("kvm_ai_push.keychain_oauth_limits", return_value=[])
    @mock.patch("kvm_ai_push.claude_auth_status", return_value=(None, None))
    def test_optional_fields_are_omitted_when_unavailable(self, _auth, _keychain, _cli):
        payload = helper.build_usage_payload()
        self.assertNotIn("loggedIn", payload)
        self.assertNotIn("plan", payload)
        self.assertNotIn("limits", payload)


class OAuthUsageMappingTests(unittest.TestCase):
    def test_maps_session_and_weekly_buckets_defensively(self):
        limits = helper.oauth_usage_limits({
            "five_hour": {"utilization": 34.2, "resets_at": "2026-07-18T04:00:00Z"},
            "seven_day": {"utilization": 12, "resets_at": "2026-07-20T00:00:00Z"},
            "seven_day_opus": {"utilization": 5},
        })
        by_label = {entry["label"]: entry for entry in limits}
        self.assertEqual(by_label["Current session"]["usedPercent"], 34.2)
        self.assertEqual(by_label["Current session"]["windowMinutes"], 300)
        self.assertEqual(by_label["Weekly limit"]["usedPercent"], 12)
        self.assertEqual(by_label["Weekly (Opus)"]["usedPercent"], 5)
        self.assertNotIn("resetsAt", by_label["Weekly (Opus)"])

    def test_unrecognized_shape_yields_no_limits(self):
        self.assertEqual(helper.oauth_usage_limits({"unexpected": {"foo": "bar"}}), [])
        self.assertEqual(helper.oauth_usage_limits(None), [])
        self.assertEqual(helper.oauth_usage_limits("not a dict"), [])

    def test_extract_access_token_handles_known_shapes(self):
        self.assertEqual(helper.extract_access_token({"accessToken": "tok-a"}), "tok-a")
        self.assertEqual(helper.extract_access_token({"claudeAiOauth": {"accessToken": "tok-b"}}), "tok-b")
        self.assertIsNone(helper.extract_access_token({"nothing": "here"}))
        self.assertIsNone(helper.extract_access_token("not-a-dict"))


if __name__ == "__main__":
    unittest.main()
