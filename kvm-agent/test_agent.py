#!/usr/bin/env python3
"""Unit tests for the agent's console authentication (session cookies + admin-credential relay)."""

import base64
import json
import os
import time
import unittest
from unittest import mock

os.environ.setdefault("KVM_AI_USAGE_AUTH_SECRET", "/tmp/kvm-ai-usage-test-secret")

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


if __name__ == "__main__":
    unittest.main()
