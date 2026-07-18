#!/usr/bin/env python3
"""Outbound-only macOS helper. Collects Claude usage/activity locally and pushes signed,
whitelisted aggregates to the KVM. Provider credentials never leave this process: the push
secret comes from the login Keychain, the OAuth token (if read) is used in memory only, and
nothing but the schema fields below is ever sent, printed, or written to disk.

Single stdlib file so it can be copied standalone into Application Support. No inbound socket
is ever opened."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import hmac
import json
import os
import pathlib
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

SCHEMA_VERSION = 1
PROVIDER = "claude"
ACTIVE_THROTTLE_SECONDS = 20

os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")


# --- signing (docs/PUSH_PROTOCOL.md "Request signing") ---------------------------------

def sign(secret, timestamp, nonce, method, path, body_bytes):
    body_hash = hashlib.sha256(body_bytes).hexdigest()
    string_to_sign = "v1\n" + str(timestamp) + "\n" + nonce + "\n" + method.upper() + "\n" + path + "\n" + body_hash
    return hmac.new(secret.encode("ascii"), string_to_sign.encode("ascii"), hashlib.sha256).hexdigest()


def unverified_ssl_context():
    # The Comet uses a self-signed certificate; the HMAC signature (not TLS) is what proves
    # authenticity here, so certificate verification is intentionally skipped for this host only.
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


# --- config / secret (never logged) -----------------------------------------------------

def config_dir():
    return pathlib.Path.home() / ".kvm-ai-monitor"


def config_path():
    return config_dir() / "helper.json"


def activity_marker_path():
    return config_dir() / "last-activity"


def load_config():
    with config_path().open() as stream:
        config = json.load(stream)
    if not config.get("kvmHost") or not config.get("deviceId"):
        raise RuntimeError("helper.json missing kvmHost/deviceId")
    return config


def load_secret(kvm_host):
    result = subprocess.run(
        ("/usr/bin/security", "find-generic-password", "-s", "kvm-ai-monitor-push:" + kvm_host, "-a", "device", "-w"),
        capture_output=True, text=True, timeout=5, check=False,
    )
    secret = result.stdout.strip()
    if result.returncode != 0 or not secret:
        raise RuntimeError("push secret not found in Keychain")
    return secret


# --- activity throttle marker (timestamp only) ------------------------------------------

def throttled():
    try:
        last = float(activity_marker_path().read_text().strip())
    except Exception:
        return False
    return (time.time() - last) < ACTIVE_THROTTLE_SECONDS


def mark_activity_sent():
    try:
        config_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
        marker = activity_marker_path()
        marker.write_text(str(int(time.time())))
        os.chmod(marker, 0o600)
    except Exception:
        pass


# --- small shared helpers (ported from kvm-agent/ssh_collector.py's REMOTE_COLLECTOR) ---

def run(args, timeout, input_text=None):
    try:
        return subprocess.run(args, input=input_text, capture_output=True, text=True, timeout=timeout, check=False).stdout
    except Exception:
        return ""


def number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def plan_label(value):
    if not isinstance(value, str) or not value:
        return None
    return re.sub(r"\b\w", lambda match: match.group(0).upper(), re.sub(r"[_-]+", " ", value))


# --- adapter a: claude auth status -------------------------------------------------------

def claude_auth_status():
    if not shutil.which("claude"):
        return None, None
    try:
        status = json.loads(run(("claude", "auth", "status", "--json"), 10))
        logged_in = status.get("loggedIn") is True
        plan = plan_label(status.get("subscriptionType"))
        return logged_in, plan
    except Exception:
        return None, None


# --- adapter b: optional non-interactive usage command (best-effort, may not exist) -----

def _bucket_percent(bucket):
    if not isinstance(bucket, dict):
        return None
    for key in ("usedPercent", "used_percent", "percentUsed", "percent_used", "utilization"):
        value = number(bucket.get(key))
        if value is not None:
            return max(0, min(100, value))
    return None


def _bucket_reset(bucket):
    if not isinstance(bucket, dict):
        return None
    for key in ("resetsAt", "resets_at", "resetAt", "reset_at"):
        value = bucket.get(key)
        if isinstance(value, str) and value:
            return value
    return None


WEEKLY_KEY_HINTS = ("week", "seven_day", "sevenday", "7d", "7_day")
SESSION_KEY_HINTS = ("session", "five_hour", "fivehour", "5h", "5_hour")


def oauth_usage_limits(payload):
    """Map an unknown-shaped usage payload onto the spec's limit labels, defensively."""
    if not isinstance(payload, dict):
        return []
    session_bucket = weekly_bucket = weekly_opus_bucket = None
    for key, value in payload.items():
        if not isinstance(value, dict) or not isinstance(key, str):
            continue
        lowered = key.lower()
        is_weekly = any(hint in lowered for hint in WEEKLY_KEY_HINTS)
        is_session = any(hint in lowered for hint in SESSION_KEY_HINTS)
        if "opus" in lowered and is_weekly:
            weekly_opus_bucket = weekly_opus_bucket or value
        elif is_weekly:
            weekly_bucket = weekly_bucket or value
        elif is_session:
            session_bucket = session_bucket or value
    limits = []
    for label, bucket, minutes in (
        ("Current session", session_bucket, 300),
        ("Weekly limit", weekly_bucket, None),
        ("Weekly (Opus)", weekly_opus_bucket, None),
    ):
        percent = _bucket_percent(bucket)
        if percent is None:
            continue
        entry = {"label": label, "usedPercent": percent}
        if minutes is not None:
            entry["windowMinutes"] = minutes
        resets_at = _bucket_reset(bucket)
        if resets_at:
            entry["resetsAt"] = resets_at
        limits.append(entry)
    return limits


def claude_usage_cli_limits():
    if not shutil.which("claude"):
        return []
    try:
        raw = run(("claude", "usage", "--json"), 10)
        if not raw.strip():
            return []
        data = json.loads(raw)
    except Exception:
        return []
    return oauth_usage_limits(data)


# --- adapter c: Keychain OAuth token -> api.anthropic.com/api/oauth/usage ----------------

def extract_access_token(data):
    if not isinstance(data, dict):
        return None
    direct = data.get("accessToken") or data.get("access_token")
    if isinstance(direct, str) and direct:
        return direct
    for key in ("claudeAiOauth", "oauth", "credentials"):
        nested = data.get(key)
        if isinstance(nested, dict):
            token = nested.get("accessToken") or nested.get("access_token")
            if isinstance(token, str) and token:
                return token
    return None


def keychain_oauth_limits():
    token = None
    try:
        result = subprocess.run(
            ("/usr/bin/security", "find-generic-password", "-s", "Claude Code-credentials", "-w"),
            capture_output=True, text=True, timeout=5, check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        token = extract_access_token(json.loads(result.stdout))
        if not token:
            return []
        request = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={"Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read()
        return oauth_usage_limits(json.loads(body))
    except Exception:
        return []
    finally:
        # Best-effort scrub: the token and raw response live only in these locals and are
        # never printed, logged, or written anywhere.
        token = None


# --- adapter d: local daily token totals (faithful port of ssh_collector.claude_daily) ---

def claude_daily():
    root = pathlib.Path.home() / ".claude/projects"
    cutoff = datetime.date.today() - datetime.timedelta(days=29)
    messages = {}
    if not root.is_dir():
        return []
    for path in root.rglob("*.jsonl"):
        try:
            with path.open(errors="replace") as stream:
                for index, line in enumerate(stream):
                    try:
                        event = json.loads(line)
                        message = event.get("message") if isinstance(event.get("message"), dict) else {}
                        usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
                        timestamp = str(event.get("timestamp") or "")
                        day = datetime.date.fromisoformat(timestamp[:10])
                        if event.get("type") != "assistant" or day < cutoff or not usage:
                            continue
                        message_id = message.get("id") or event.get("uuid") or (path.name + ":" + str(index))
                        values = {
                            "date": day.isoformat(),
                            "inputTokens": number(usage.get("input_tokens")) or 0,
                            "outputTokens": number(usage.get("output_tokens")) or 0,
                            "cacheReadTokens": number(usage.get("cache_read_input_tokens")) or 0,
                            "cacheCreationTokens": number(usage.get("cache_creation_input_tokens")) or 0,
                        }
                        previous = messages.get(message_id)
                        if previous:
                            for key in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheCreationTokens"):
                                values[key] = max(values[key], previous[key])
                        messages[message_id] = values
                    except Exception:
                        continue
        except OSError:
            continue
    by_day = {}
    for value in messages.values():
        day = by_day.setdefault(value["date"], {"date": value["date"], "inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0, "cacheCreationTokens": 0, "totalTokens": 0})
        for key in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheCreationTokens"):
            day[key] += value[key]
            day["totalTokens"] += value[key]
    return [by_day[key] for key in sorted(by_day)]


# --- payload assembly (whitelisted fields only, docs/PUSH_PROTOCOL.md "POST /push/v1/usage") ---

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def build_usage_payload():
    logged_in, plan = claude_auth_status()
    limits = claude_usage_cli_limits() or keychain_oauth_limits()
    payload = {"schemaVersion": SCHEMA_VERSION, "provider": PROVIDER, "collectedAt": now_iso()}
    if logged_in is not None:
        payload["loggedIn"] = logged_in
    if plan:
        payload["plan"] = plan
    if limits:
        payload["limits"] = limits[:8]
    daily = claude_daily()
    if daily:
        payload["daily"] = daily[-31:]
    return payload


def encode_body(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


# --- transport ---------------------------------------------------------------------------

def post(kvm_host, backend_path, payload, secret, device_id, timeout):
    body_bytes = encode_body(payload)
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    signature = sign(secret, timestamp, nonce, "POST", backend_path, body_bytes)
    url = "https://" + kvm_host + "/extras/ai-usage" + backend_path
    request = urllib.request.Request(
        url, data=body_bytes, method="POST",
        headers={
            "Content-Type": "application/json",
            "X-KVM-Device": device_id,
            "X-KVM-Timestamp": timestamp,
            "X-KVM-Nonce": nonce,
            "X-KVM-Signature": signature,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout, context=unverified_ssl_context()) as response:
        response.read()


# --- CLI -----------------------------------------------------------------------------------

def cmd_sign(args):
    body = sys.stdin.buffer.read()
    print(sign(args.secret, args.timestamp, args.nonce, args.method, args.path, body))


def cmd_print_payload(_args):
    print(json.dumps(build_usage_payload(), indent=2, sort_keys=True))


def cmd_send_usage(_args):
    try:
        config = load_config()
        secret = load_secret(config["kvmHost"])
        post(config["kvmHost"], "/push/v1/usage", build_usage_payload(), secret, config["deviceId"], timeout=10)
    except Exception:
        print("kvm-ai-monitor: usage push failed", file=sys.stderr)
        sys.exit(1)


def cmd_send_activity(args):
    if args.event == "active" and throttled():
        return
    try:
        config = load_config()
        secret = load_secret(config["kvmHost"])
        payload = {"schemaVersion": SCHEMA_VERSION, "provider": PROVIDER, "event": args.event}
        post(config["kvmHost"], "/push/v1/activity", payload, secret, config["deviceId"], timeout=3)
        if args.event == "active":
            mark_activity_sent()
    except Exception:
        print("kvm-ai-monitor: activity push failed", file=sys.stderr)
        sys.exit(1)


def build_parser():
    parser = argparse.ArgumentParser(prog="kvm_ai_push.py")
    sub = parser.add_subparsers(dest="command", required=True)

    sign_parser = sub.add_parser("sign", help="print the HMAC signature for a body on stdin")
    sign_parser.add_argument("--secret", required=True)
    sign_parser.add_argument("--timestamp", required=True)
    sign_parser.add_argument("--nonce", required=True)
    sign_parser.add_argument("--method", required=True)
    sign_parser.add_argument("--path", required=True)
    sign_parser.set_defaults(func=cmd_sign)

    sub.add_parser("send-usage", help="collect and push the usage snapshot").set_defaults(func=cmd_send_usage)

    activity_parser = sub.add_parser("send-activity", help="push a lifecycle activity event")
    activity_parser.add_argument("event", choices=("start", "active", "stop"))
    activity_parser.set_defaults(func=cmd_send_activity)

    sub.add_parser("print-payload", help="print the usage payload without sending it").set_defaults(func=cmd_print_payload)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
