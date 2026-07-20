#!/usr/bin/env python3
"""Offline unit tests for the macOS push helper. No network, no `claude`/`security` binaries,
no real Keychain access — all subprocess/network adapters are patched. Run with:
    python3 helper/test_helper.py
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
    """Points the home directory at a scratch directory so tests never touch real files.
    Sets both HOME (POSIX) and USERPROFILE (Windows) so Path.home() is isolated everywhere."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        patcher = mock.patch.dict(os.environ, {"HOME": self.tempdir.name, "USERPROFILE": self.tempdir.name})
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

    @mock.patch("kvm_ai_push.load_targets", side_effect=RuntimeError("no config"))
    def test_send_activity_active_skips_silently_when_throttled(self, mock_targets):
        helper.mark_activity_sent()
        args = mock.Mock(event="active")
        helper.cmd_send_activity(args)  # must return without raising or reading config
        mock_targets.assert_not_called()


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

    @mock.patch("kvm_ai_push.account_limits", return_value=[])
    @mock.patch("kvm_ai_push.claude_auth_status", return_value=(True, "Max"))
    def test_payload_has_only_whitelisted_fields_and_no_planted_secrets(self, _auth, _limits):
        payload = helper.claude_usage_payload()
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

    @mock.patch("kvm_ai_push.account_limits", return_value=[])
    @mock.patch("kvm_ai_push.claude_auth_status", return_value=(None, None))
    def test_optional_fields_are_omitted_when_unavailable(self, _auth, _limits):
        payload = helper.claude_usage_payload()
        self.assertNotIn("loggedIn", payload)
        self.assertNotIn("plan", payload)
        self.assertNotIn("limits", payload)


# Shape observed live from api.anthropic.com/api/oauth/usage on 2026-07-18: the structured
# `limits` array is authoritative; the top-level five_hour/seven_day buckets are legacy.
STRUCTURED_RESPONSE = {
    "five_hour": {"utilization": 15.0, "resets_at": "2026-07-19T02:30:00+00:00"},
    "seven_day": {"utilization": 13.0, "resets_at": "2026-07-21T12:00:00+00:00"},
    "seven_day_opus": None,
    "extra_usage": {"is_enabled": True, "utilization": 4.54},
    "limits": [
        {"kind": "session", "group": "session", "percent": 15, "severity": "normal",
         "resets_at": "2026-07-19T02:30:00+00:00", "scope": None, "is_active": False},
        {"kind": "weekly_all", "group": "weekly", "percent": 13, "severity": "normal",
         "resets_at": "2026-07-21T12:00:00+00:00", "scope": None, "is_active": False},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 16, "severity": "normal",
         "resets_at": "2026-07-21T12:00:00+00:00",
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
         "is_active": True},
    ],
    "spend": {"percent": 5, "enabled": True},
}


class OAuthUsageMappingTests(unittest.TestCase):
    def test_structured_limits_array_is_preferred_and_fully_mapped(self):
        limits = helper.oauth_usage_limits(STRUCTURED_RESPONSE)
        by_label = {entry["label"]: entry for entry in limits}
        self.assertEqual(set(by_label), {"Current session", "Weekly limit", "Weekly (Fable)"})
        self.assertEqual(by_label["Current session"]["usedPercent"], 15)
        self.assertEqual(by_label["Current session"]["windowMinutes"], 300)
        self.assertEqual(by_label["Weekly limit"]["usedPercent"], 13)
        self.assertEqual(by_label["Weekly limit"]["windowMinutes"], 10080)
        self.assertEqual(by_label["Weekly limit"]["resetsAt"], "2026-07-21T12:00:00+00:00")
        self.assertEqual(by_label["Weekly (Fable)"]["usedPercent"], 16)
        self.assertEqual(by_label["Weekly (Fable)"]["windowMinutes"], 10080)

    def test_structured_limits_skips_malformed_entries_and_clamps(self):
        limits = helper.structured_limits({"limits": [
            None, "text", {"kind": "session", "percent": None},
            {"kind": "weekly_all", "group": "weekly", "percent": 250},
            {"kind": "unknown_kind", "group": "monthly", "percent": 10},
        ]})
        self.assertEqual(limits, [{"label": "Weekly limit", "usedPercent": 100,
                                   "windowMinutes": 10080}])

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


class MultiTargetTests(HomeIsolatedTestCase):
    def _write_config(self, payload):
        helper.config_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
        helper.config_path().write_text(json.dumps(payload))

    def test_legacy_single_target_config_still_loads(self):
        self._write_config({"kvmHost": "kvm-a", "deviceId": "d-1"})
        self.assertEqual(helper.load_targets(), [{"kvmHost": "kvm-a", "deviceId": "d-1"}])

    def test_targets_list_loads_and_skips_malformed_entries(self):
        self._write_config({"targets": [
            {"kvmHost": "kvm-a", "deviceId": "d-1"},
            {"kvmHost": "", "deviceId": "d-x"},
            "junk",
            {"kvmHost": "kvm-b", "deviceId": "d-2"},
        ]})
        self.assertEqual(helper.load_targets(), [
            {"kvmHost": "kvm-a", "deviceId": "d-1"},
            {"kvmHost": "kvm-b", "deviceId": "d-2"},
        ])

    def test_empty_targets_raise(self):
        self._write_config({"targets": []})
        with self.assertRaises(RuntimeError):
            helper.load_targets()

    @mock.patch("kvm_ai_push.post")
    @mock.patch("kvm_ai_push.load_secret", side_effect=lambda host: "secret-" + host)
    def test_pushes_to_every_target(self, _secret, post):
        self._write_config({"targets": [
            {"kvmHost": "kvm-a", "deviceId": "d-1"},
            {"kvmHost": "kvm-b", "deviceId": "d-2"},
        ]})
        total, errors = helper.push_to_targets("/push/v1/usage", {"x": 1}, timeout=5)
        self.assertEqual((total, errors), (2, []))
        called_hosts = [call.args[0] for call in post.call_args_list]
        self.assertEqual(called_hosts, ["kvm-a", "kvm-b"])
        self.assertEqual(post.call_args_list[0].args[4], "d-1")
        self.assertEqual(post.call_args_list[1].args[4], "d-2")

    @mock.patch("kvm_ai_push.post")
    @mock.patch("kvm_ai_push.load_secret", side_effect=lambda host: "secret-" + host)
    def test_partial_failure_reports_but_does_not_exit(self, _secret, post):
        post.side_effect = [RuntimeError("down"), None]
        self._write_config({"targets": [
            {"kvmHost": "kvm-a", "deviceId": "d-1"},
            {"kvmHost": "kvm-b", "deviceId": "d-2"},
        ]})
        total, errors = helper.push_to_targets("/push/v1/usage", {"x": 1}, timeout=5)
        self.assertEqual(total, 2)
        self.assertEqual(len(errors), 1)
        self.assertIn("kvm-a", errors[0])

    @mock.patch("kvm_ai_push.post", side_effect=RuntimeError("down"))
    @mock.patch("kvm_ai_push.load_secret", return_value="s")
    def test_send_usage_exits_only_when_all_targets_fail(self, _secret, _post):
        self._write_config({"targets": [{"kvmHost": "kvm-a", "deviceId": "d-1"}]})
        # One provider with data, and every target fails to receive it -> exit non-zero.
        with mock.patch.dict(
            "kvm_ai_push.USAGE_COLLECTORS",
            {"claude": lambda: {"schemaVersion": 1, "provider": "claude"}}, clear=True,
        ):
            with self.assertRaises(SystemExit):
                helper.cmd_send_usage(mock.Mock())

    @mock.patch("kvm_ai_push.post")
    @mock.patch("kvm_ai_push.load_secret", return_value="s")
    def test_send_usage_pushes_every_provider_with_data(self, _secret, post):
        self._write_config({"targets": [{"kvmHost": "kvm-a", "deviceId": "d-1"}]})
        with mock.patch.dict(
            "kvm_ai_push.USAGE_COLLECTORS",
            {"claude": lambda: {"schemaVersion": 1, "provider": "claude"},
             "codex": lambda: {"schemaVersion": 1, "provider": "codex"},
             "absent": lambda: None},
            clear=True,
        ):
            helper.cmd_send_usage(mock.Mock())
        pushed = [call.args[2]["provider"] for call in post.call_args_list]
        self.assertEqual(sorted(pushed), ["claude", "codex"])


class SecretBackendTests(HomeIsolatedTestCase):
    def test_forced_backend_env_wins(self):
        with mock.patch.dict(os.environ, {"KVM_AI_SECRET_BACKEND": "file"}):
            self.assertEqual(helper.secret_backend(), "file")

    def test_platform_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KVM_AI_SECRET_BACKEND", None)
            with mock.patch.object(helper.sys, "platform", "darwin"):
                self.assertEqual(helper.secret_backend(), "keychain")
            with mock.patch.object(helper.sys, "platform", "win32"):
                self.assertEqual(helper.secret_backend(), "dpapi")
            with mock.patch.object(helper.sys, "platform", "linux"):
                with mock.patch.object(helper.shutil, "which", return_value="/usr/bin/secret-tool"):
                    self.assertEqual(helper.secret_backend(), "secret-tool")
                with mock.patch.object(helper.shutil, "which", return_value=None):
                    self.assertEqual(helper.secret_backend(), "file")

    def test_file_backend_round_trip_and_permissions(self):
        with mock.patch.dict(os.environ, {"KVM_AI_SECRET_BACKEND": "file"}):
            helper.store_secret("kvm-a", "0123456789abcdef")
            self.assertEqual(helper.load_secret("kvm-a"), "0123456789abcdef")
            if os.name == "posix":
                mode = helper.secret_file("kvm-a").stat().st_mode & 0o777
                self.assertEqual(mode, 0o600)

    def test_missing_secret_raises_with_backend_name(self):
        with mock.patch.dict(os.environ, {"KVM_AI_SECRET_BACKEND": "file"}):
            with self.assertRaisesRegex(RuntimeError, "file backend"):
                helper.load_secret("kvm-unknown")

    @unittest.skipUnless(sys.platform == "win32", "DPAPI exists only on Windows")
    def test_dpapi_backend_round_trip(self):
        with mock.patch.dict(os.environ, {"KVM_AI_SECRET_BACKEND": "dpapi"}):
            helper.store_secret("kvm-w", "fedcba9876543210")
            self.assertEqual(helper.load_secret("kvm-w"), "fedcba9876543210")


class CredentialSourceTests(HomeIsolatedTestCase):
    def test_non_macos_reads_claude_credentials_file(self):
        claude_dir = pathlib.Path(self.tempdir.name) / ".claude"
        claude_dir.mkdir()
        (claude_dir / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "tok-file"}}))
        with mock.patch.object(helper.sys, "platform", "linux"):
            raw = helper.read_claude_credentials()
        self.assertEqual(helper.extract_access_token(json.loads(raw)), "tok-file")

    def test_non_macos_missing_credentials_returns_none(self):
        with mock.patch.object(helper.sys, "platform", "linux"):
            self.assertIsNone(helper.read_claude_credentials())


class AccountLimitsCacheTests(HomeIsolatedTestCase):
    LIMITS = [{"label": "Weekly limit", "usedPercent": 13, "windowMinutes": 10080}]

    @mock.patch("kvm_ai_push.keychain_oauth_limits")
    def test_successful_fetch_is_cached_and_not_refetched_within_window(self, fetch):
        fetch.return_value = self.LIMITS
        self.assertEqual(helper.account_limits(), self.LIMITS)
        self.assertEqual(helper.account_limits(), self.LIMITS)
        self.assertEqual(fetch.call_count, 1)

    @mock.patch("kvm_ai_push.keychain_oauth_limits")
    def test_failed_fetch_reuses_last_good_limits(self, fetch):
        fetch.return_value = self.LIMITS
        helper.account_limits()
        cache = json.loads(helper.limits_cache_path().read_text())
        cache["attemptedAt"] = time.time() - helper.LIMITS_MIN_FETCH_SECONDS - 1
        helper.limits_cache_path().write_text(json.dumps(cache))
        fetch.return_value = []  # e.g. HTTP 429 from the usage endpoint
        self.assertEqual(helper.account_limits(), self.LIMITS)
        self.assertEqual(fetch.call_count, 2)

    @mock.patch("kvm_ai_push.keychain_oauth_limits")
    def test_expired_cache_is_not_served(self, fetch):
        fetch.return_value = self.LIMITS
        helper.account_limits()
        stale = time.time() - helper.LIMITS_MAX_AGE_SECONDS - 1
        helper.limits_cache_path().write_text(
            json.dumps({"fetchedAt": stale, "attemptedAt": stale, "limits": self.LIMITS}))
        fetch.return_value = []
        self.assertEqual(helper.account_limits(), [])

    @mock.patch("kvm_ai_push.keychain_oauth_limits")
    def test_failed_attempts_are_throttled_too(self, fetch):
        fetch.return_value = []
        self.assertEqual(helper.account_limits(), [])
        self.assertEqual(helper.account_limits(), [])
        self.assertEqual(fetch.call_count, 1)

    def test_cache_file_contains_only_whitelisted_fields(self):
        with mock.patch("kvm_ai_push.keychain_oauth_limits", return_value=self.LIMITS):
            helper.account_limits()
        cache = json.loads(helper.limits_cache_path().read_text())
        self.assertEqual(set(cache), {"fetchedAt", "attemptedAt", "limits"})
        for entry in cache["limits"]:
            self.assertTrue(set(entry) <= {"label", "usedPercent", "windowMinutes", "resetsAt"})


if __name__ == "__main__":
    unittest.main()
