#!/usr/bin/env python3
"""Outbound-only device helper (macOS, Linux, Windows). Collects Claude usage/activity locally
and pushes signed, whitelisted aggregates to the KVM. Provider credentials never leave this
process: the push secret comes from the platform vault (macOS Keychain, libsecret, or Windows
DPAPI, with a 0600-file fallback), the OAuth token (if read) is used in memory only, and nothing
but the schema fields below is ever sent, printed, or written to disk unencrypted except the
documented file-backend fallback.

Single stdlib file so it can be copied standalone into the per-user app directory. No inbound
socket is ever opened."""

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
import select
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

SCHEMA_VERSION = 1
# Default provider for the lifecycle-hook activity path (the Claude Code hook calls
# `send-activity` with no --provider). Usage collection and the activity poller are NOT keyed to
# this — they use the registries below so any selected provider is covered.
DEFAULT_ACTIVITY_PROVIDER = "claude"
ACTIVE_THROTTLE_SECONDS = 20

if sys.platform == "darwin":
    os.environ["PATH"] = os.pathsep.join(("/opt/homebrew/bin", "/usr/local/bin", os.environ.get("PATH", "")))


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


def load_targets():
    """KVM push targets. Modern helper.json holds {"targets": [{kvmHost, deviceId}, ...]};
    the legacy single kvmHost/deviceId layout is still accepted."""
    with config_path().open() as stream:
        config = json.load(stream)
    raw = config.get("targets")
    if not isinstance(raw, list):
        raw = [{"kvmHost": config.get("kvmHost"), "deviceId": config.get("deviceId")}]
    targets = [
        {"kvmHost": target["kvmHost"], "deviceId": target["deviceId"]}
        for target in raw
        if isinstance(target, dict) and target.get("kvmHost") and target.get("deviceId")
    ]
    if not targets:
        raise RuntimeError("helper.json has no usable KVM targets")
    return targets


# --- push-secret storage: platform vault with a 0600-file fallback ----------------------

def secret_backend():
    forced = os.environ.get("KVM_AI_SECRET_BACKEND")
    if forced in ("keychain", "secret-tool", "dpapi", "file"):
        return forced
    if sys.platform == "darwin":
        return "keychain"
    if sys.platform == "win32":
        return "dpapi"
    return "secret-tool" if shutil.which("secret-tool") else "file"


def secret_service(kvm_host):
    return "kvm-ai-monitor-push:" + kvm_host


def secret_file(kvm_host):
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", kvm_host)
    return config_dir() / "secrets" / ("push-" + safe)


def _dpapi(data, protect):
    """Windows DPAPI (user-scoped) via ctypes; real encryption without third-party modules."""
    import ctypes
    import ctypes.wintypes as wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = (("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char)))

    buffer = ctypes.create_string_buffer(data, len(data))
    incoming = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    outgoing = DataBlob()
    call = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not call(ctypes.byref(incoming), None, None, None, None, 0, ctypes.byref(outgoing)):
        raise RuntimeError("Windows DPAPI call failed")
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(outgoing.pbData)


def _write_secret_file(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(data)
    os.chmod(path, 0o600)


def store_secret(kvm_host, secret):
    backend = secret_backend()
    if backend == "keychain":
        subprocess.run(
            ("/usr/bin/security", "add-generic-password", "-s", secret_service(kvm_host),
             "-a", "device", "-w", secret, "-U"),
            capture_output=True, timeout=10, check=True,
        )
    elif backend == "secret-tool":
        subprocess.run(
            ("secret-tool", "store", "--label", "KVM AI Monitor push secret",
             "service", secret_service(kvm_host), "account", "device"),
            input=secret, text=True, capture_output=True, timeout=10, check=True,
        )
    elif backend == "dpapi":
        _write_secret_file(secret_file(kvm_host), _dpapi(secret.encode("utf-8"), protect=True))
    else:
        _write_secret_file(secret_file(kvm_host), secret.encode("utf-8"))


def load_secret(kvm_host):
    backend = secret_backend()
    secret = ""
    if backend == "keychain":
        result = subprocess.run(
            ("/usr/bin/security", "find-generic-password", "-s", secret_service(kvm_host), "-a", "device", "-w"),
            capture_output=True, text=True, timeout=5, check=False,
        )
        secret = result.stdout.strip() if result.returncode == 0 else ""
    elif backend == "secret-tool":
        result = subprocess.run(
            ("secret-tool", "lookup", "service", secret_service(kvm_host), "account", "device"),
            capture_output=True, text=True, timeout=10, check=False,
        )
        secret = result.stdout.strip() if result.returncode == 0 else ""
    elif backend == "dpapi":
        try:
            secret = _dpapi(secret_file(kvm_host).read_bytes(), protect=False).decode("utf-8").strip()
        except (OSError, RuntimeError):
            secret = ""
    else:
        try:
            secret = secret_file(kvm_host).read_text().strip()
        except OSError:
            secret = ""
    if not secret:
        raise RuntimeError(f"push secret for {kvm_host} not found ({backend} backend)")
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


# --- adapter b: account limits from api.anthropic.com/api/oauth/usage -------------------

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
WEEK_MINUTES = 7 * 24 * 60


def structured_limits(payload):
    """Parse the endpoint's `limits` array: the authoritative per-account session/weekly
    entries (including model-scoped weekly limits) that the top-level buckets don't cover."""
    entries = payload.get("limits") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []
    limits = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        percent = number(item.get("percent"))
        if percent is None:
            continue
        kind = item.get("kind")
        group = item.get("group")
        if kind == "session" or group == "session":
            label, minutes = "Current session", 300
        elif kind == "weekly_all":
            label, minutes = "Weekly limit", WEEK_MINUTES
        elif group == "weekly":
            scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
            model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
            name = model.get("display_name")
            label = f"Weekly ({name})" if isinstance(name, str) and name else "Weekly (model)"
            minutes = WEEK_MINUTES
        else:
            continue
        entry = {"label": label, "usedPercent": max(0, min(100, percent)), "windowMinutes": minutes}
        resets_at = item.get("resets_at")
        if isinstance(resets_at, str) and resets_at:
            entry["resetsAt"] = resets_at
        limits.append(entry)
    return limits


def oauth_usage_limits(payload):
    """Map a usage payload onto the spec's limit labels: the structured `limits` array when
    present, otherwise the legacy top-level bucket keys, matched defensively by name."""
    if not isinstance(payload, dict):
        return []
    limits = structured_limits(payload)
    if limits:
        return limits
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


def read_claude_credentials():
    """Claude Code's credential JSON: the login Keychain on macOS, ~/.claude/.credentials.json
    on Linux and Windows. The value is used in memory only and never persisted or printed."""
    if sys.platform == "darwin":
        # The first read shows a Keychain consent dialog; give the user time to answer it.
        result = subprocess.run(
            ("/usr/bin/security", "find-generic-password", "-s", "Claude Code-credentials", "-w"),
            capture_output=True, text=True, timeout=90, check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return result.stdout
    try:
        return (pathlib.Path.home() / ".claude" / ".credentials.json").read_text()
    except OSError:
        return None


def keychain_oauth_limits():
    token = None
    try:
        raw = read_claude_credentials()
        if not raw:
            return []
        token = extract_access_token(json.loads(raw))
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


# --- limits cache: the usage endpoint rate-limits aggressively (HTTP 429), so fetch at
# --- most once per LIMITS_MIN_FETCH_SECONDS and reuse the last good result on failure.
# --- The cache file holds only already-whitelisted limit entries plus timestamps.

LIMITS_MIN_FETCH_SECONDS = 240
LIMITS_MAX_AGE_SECONDS = 24 * 3600


def limits_cache_path():
    return config_dir() / "limits-cache.json"


def read_limits_cache():
    try:
        data = json.loads(limits_cache_path().read_text())
        if not isinstance(data, dict) or not isinstance(data.get("limits"), list):
            return None
        return data
    except Exception:
        return None


def write_limits_cache(cache):
    try:
        config_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
        path = limits_cache_path()
        path.write_text(json.dumps(cache))
        os.chmod(path, 0o600)
    except Exception:
        pass


def account_limits():
    now = time.time()
    cache = read_limits_cache()
    if cache is not None:
        fetched_age = now - (number(cache.get("fetchedAt")) or 0)
        attempted_age = now - (number(cache.get("attemptedAt")) or 0)
        if 0 <= attempted_age < LIMITS_MIN_FETCH_SECONDS:
            return cache["limits"] if 0 <= fetched_age < LIMITS_MAX_AGE_SECONDS else []
    limits = keychain_oauth_limits()
    if limits:
        write_limits_cache({"fetchedAt": now, "attemptedAt": now, "limits": limits})
        return limits
    if cache is not None and 0 <= now - (number(cache.get("fetchedAt")) or 0) < LIMITS_MAX_AGE_SECONDS:
        write_limits_cache({**cache, "attemptedAt": now})
        return cache["limits"]
    write_limits_cache({"fetchedAt": 0, "attemptedAt": now, "limits": []})
    return []


# --- adapter d: local daily token totals (faithful port of ssh_collector.claude_daily) ---

def claude_scan():
    """One pass over Claude transcripts → (per-day totals, per-model totals, per-platform totals)
    for the last 30 days. Platform is the transcript `entrypoint` (cli / claude-desktop / sdk-cli);
    web and API usage are server-side and never appear locally. Shared by claude_daily() and the
    usage payload so the files are read only once."""
    root = pathlib.Path.home() / ".claude/projects"
    cutoff = datetime.date.today() - datetime.timedelta(days=29)
    messages = {}
    if not root.is_dir():
        return [], [], []
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
                            "model": str(message.get("model") or ""),
                            "platform": str(event.get("entrypoint") or ""),
                            "inputTokens": number(usage.get("input_tokens")) or 0,
                            "outputTokens": number(usage.get("output_tokens")) or 0,
                            "cacheReadTokens": number(usage.get("cache_read_input_tokens")) or 0,
                            "cacheCreationTokens": number(usage.get("cache_creation_input_tokens")) or 0,
                        }
                        previous = messages.get(message_id)
                        if previous:
                            for key in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheCreationTokens"):
                                values[key] = max(values[key], previous[key])
                            values["model"] = values["model"] or previous.get("model", "")
                            values["platform"] = values["platform"] or previous.get("platform", "")
                        messages[message_id] = values
                    except Exception:
                        continue
        except OSError:
            continue
    by_day = {}
    by_model = {}
    by_platform = {}
    for value in messages.values():
        day = by_day.setdefault(value["date"], {"date": value["date"], "inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0, "cacheCreationTokens": 0, "totalTokens": 0})
        subtotal = 0
        for key in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheCreationTokens"):
            day[key] += value[key]
            day["totalTokens"] += value[key]
            subtotal += value[key]
        model = value.get("model") or ""
        if model and not model.startswith("<"):
            by_model[model] = by_model.get(model, 0) + subtotal
        platform = value.get("platform") or ""
        if platform:
            by_platform[platform] = by_platform.get(platform, 0) + subtotal
    daily = [by_day[key] for key in sorted(by_day)]
    models = [{"model": name, "tokens": by_model[name]}
              for name in sorted(by_model, key=lambda name: -by_model[name]) if by_model[name] > 0]
    platforms = [{"platform": name, "tokens": by_platform[name]}
                 for name in sorted(by_platform, key=lambda name: -by_platform[name]) if by_platform[name] > 0]
    return daily, models, platforms


def claude_daily():
    return claude_scan()[0]


# --- payload assembly (whitelisted fields only, docs/PUSH_PROTOCOL.md "POST /push/v1/usage") ---

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


# --- adapter c: codex usage from `codex app-server` -------------------------------------
# Codex has no OAuth usage endpoint like Claude's; its ChatGPT plan windows and account-wide
# daily token totals come from the app-server JSON-RPC (account/rateLimits/read + usage/read) —
# the same source the KVM's SSH collector uses. Run locally here so a push-only device (no SSH)
# still refreshes codex limits and TODAY TOKENS instead of leaving them blank/stale.

def codex_binary():
    found = shutil.which("codex")
    if found:
        return found
    fallback = "/Applications/Codex.app/Contents/Resources/codex"
    return fallback if os.access(fallback, os.X_OK) else None


def _finite_number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _plan_label(value):
    if not isinstance(value, str) or not value:
        return None
    return re.sub(r"\b\w", lambda match: match.group(0).upper(), re.sub(r"[_-]+", " ", value))


def _codex_limit(value):
    if not isinstance(value, dict) or _finite_number(value.get("usedPercent")) is None:
        return None
    minutes = _finite_number(value.get("windowDurationMins"))
    entry = {"label": "Current session" if minutes and minutes <= 360 else "Weekly limit",
             "usedPercent": max(0.0, min(100.0, _finite_number(value.get("usedPercent"))))}
    if minutes is not None and minutes > 0:
        entry["windowMinutes"] = minutes
    resets = _finite_number(value.get("resetsAt"))
    if resets is not None:
        entry["resetsAt"] = datetime.datetime.fromtimestamp(
            resets, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    return entry


def codex_usage_payload():
    """Codex plan/limits/daily-tokens via `codex app-server`, or None when codex is absent or the
    handshake yields nothing. Never raises — usage collection must not break the push cycle."""
    codex = codex_binary()
    if not codex:
        return None
    messages = (
        {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "kvm-ai-monitor", "title": "KVM AI Monitor", "version": "1.0.0"}, "capabilities": {"experimentalApi": True}}},
        {"method": "initialized", "params": {}},
        {"id": 2, "method": "account/rateLimits/read", "params": {}},
        {"id": 3, "method": "account/usage/read", "params": {}},
    )
    body = "\n".join(json.dumps(message, separators=(",", ":")) for message in messages) + "\n"
    # Keep stdin open and read incrementally: app-server shuts down on stdin EOF, so closing it
    # (as communicate does) drops the rateLimits/usage replies before they are sent. select() is
    # POSIX-only, which is fine — codex usage is a macOS/Linux concern; on Windows this raises and
    # we return None. Mirrors the KVM SSH collector's codex_native().
    process = None
    responses = {}
    try:
        process = subprocess.Popen(
            (codex, "app-server", "--stdio"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        process.stdin.write(body)
        process.stdin.flush()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and len(responses) < 2:
            ready, _, _ = select.select((process.stdout,), (), (), max(0, deadline - time.monotonic()))
            if not ready:
                break
            line = process.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") in (2, 3) and isinstance(message.get("result"), dict):
                responses[message["id"]] = message["result"]
    except Exception:
        return None
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=2)
            except Exception:
                process.kill()
    rate = responses.get(2, {}).get("rateLimits")
    rate = rate if isinstance(rate, dict) else {}
    usage = responses.get(3, {})
    if not rate and not usage:
        return None
    limits = [entry for entry in (_codex_limit(rate.get("primary")), _codex_limit(rate.get("secondary"))) if entry]
    buckets = usage.get("dailyUsageBuckets") if isinstance(usage.get("dailyUsageBuckets"), list) else []
    daily = [
        {"date": item.get("startDate"), "totalTokens": _finite_number(item.get("tokens")) or 0}
        for item in buckets
        if isinstance(item, dict) and isinstance(item.get("startDate"), str)
    ]
    payload = {"schemaVersion": SCHEMA_VERSION, "provider": "codex", "collectedAt": now_iso(), "loggedIn": True}
    plan = _plan_label(rate.get("planType"))
    if plan:
        payload["plan"] = plan
    if limits:
        payload["limits"] = limits[:8]
    if daily:
        payload["daily"] = daily[-31:]
    return payload


def claude_usage_payload():
    """Claude usage: auth/plan + account limits + local daily token totals."""
    logged_in, plan = claude_auth_status()
    limits = account_limits()
    payload = {"schemaVersion": SCHEMA_VERSION, "provider": "claude", "collectedAt": now_iso()}
    if logged_in is not None:
        payload["loggedIn"] = logged_in
    if plan:
        payload["plan"] = plan
    if limits:
        payload["limits"] = limits[:8]
    daily, models, platforms = claude_scan()
    if daily:
        payload["daily"] = daily[-31:]
    if models:
        payload["models"] = models
    if platforms:
        payload["platforms"] = platforms
    return payload


# Provider usage collectors. Each returns a push payload (provider/plan/limits/daily) or None when
# that provider isn't installed/authed here. `send-usage` pushes every collector that returns
# data, so the KVM holds usage for whichever provider the user selects on the web UI — none is
# privileged. Adding a provider means adding its collector here and nothing else. (gemini/grok/
# copilot expose no supported quota command, so they have no collector — their tiles show
# connection state only, exactly as the KVM's own SSH collector treats them.)
USAGE_COLLECTORS = {
    "claude": claude_usage_payload,
    "codex": codex_usage_payload,
}


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
    for provider_id, collect in USAGE_COLLECTORS.items():
        try:
            payload = collect()
        except Exception as error:
            print(f"# {provider_id}: collection failed: {error}", file=sys.stderr)
            continue
        if payload:
            print(json.dumps(payload, indent=2, sort_keys=True))


def cmd_app_usage(_args):
    """Emit one JSON object for the menu-bar companion: each provider's usage payload (plan,
    limits, daily token history) plus the current local working state. Read-only — never pushes.
    Errors go to stderr so stdout stays a single parseable JSON line."""
    providers = []
    for provider_id, collect in USAGE_COLLECTORS.items():
        try:
            payload = collect()
        except Exception as error:
            print(f"# {provider_id}: {error}", file=sys.stderr)
            continue
        if payload:
            providers.append(payload)
    try:
        working = {key: bool(value) for key, value in detect_working().items()}
    except Exception:
        working = {}
    print(json.dumps({"providers": providers, "working": working, "generatedAt": now_iso()}))


def cmd_store_secret(args):
    # Read bytes, not text: PowerShell prefixes a UTF-8 BOM when it pipes into a native
    # process, and the locale-dependent text decoding of stdin turns that into mojibake or
    # lone surrogates that later fail to re-encode.
    raw = sys.stdin.buffer.readline()
    secret = raw.decode("utf-8-sig", errors="replace").strip()
    if not secret:
        print("kvm-ai-monitor: no secret on stdin", file=sys.stderr)
        sys.exit(1)
    try:
        store_secret(args.kvm, secret)
    except Exception as error:
        print(f"kvm-ai-monitor: storing secret failed: {error}", file=sys.stderr)
        sys.exit(1)


def push_to_targets(backend_path, payload, timeout):
    """Push one payload to every configured KVM. Returns (target_count, error_lines);
    error text never contains payload or credential data."""
    targets = load_targets()
    errors = []
    for target in targets:
        try:
            secret = load_secret(target["kvmHost"])
            post(target["kvmHost"], backend_path, payload, secret, target["deviceId"], timeout=timeout)
        except Exception as error:
            errors.append(f"{target['kvmHost']}: {error}")
    return len(targets), errors


def cmd_send_usage(_args):
    # Collect and push every provider that has data. Each collector is independent — one failing
    # (or being absent) never blocks the others — so the KVM stays current for whichever provider
    # the user selects.
    considered = 0
    pushed = 0
    for provider_id, collect in USAGE_COLLECTORS.items():
        try:
            payload = collect()
        except Exception as error:
            print(f"kvm-ai-monitor: {provider_id} usage collection failed: {error}", file=sys.stderr)
            continue
        if not payload:
            continue
        considered += 1
        try:
            total, errors = push_to_targets("/push/v1/usage", payload, timeout=10)
        except Exception as error:
            print(f"kvm-ai-monitor: {provider_id} usage push failed: {error}", file=sys.stderr)
            continue
        for line in errors:
            print(f"kvm-ai-monitor: {provider_id} usage push failed: {line}", file=sys.stderr)
        if not (errors and len(errors) == total):
            pushed += 1
    # Record the last successful push so the menu-bar app can show an accurate "last update" time
    # (the launchd log only changes on output, so its mtime is not a reliable signal).
    if pushed:
        try:
            marker = pathlib.Path.home() / ".kvm-ai-monitor" / "last-usage-push"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(now_iso() + "\n")
        except OSError:
            pass
    # Fail (for launchd) only when we had data but none of it reached any KVM.
    if considered and not pushed:
        sys.exit(1)


def cmd_send_activity(args):
    if args.event == "active" and throttled():
        return
    provider = getattr(args, "provider", None) or DEFAULT_ACTIVITY_PROVIDER
    payload = {"schemaVersion": SCHEMA_VERSION, "provider": provider, "event": args.event}
    try:
        total, errors = push_to_targets("/push/v1/activity", payload, timeout=3)
    except Exception as error:
        print(f"kvm-ai-monitor: activity push failed: {error}", file=sys.stderr)
        sys.exit(1)
    if args.event == "active" and len(errors) < total:
        mark_activity_sent()
    for line in errors:
        print(f"kvm-ai-monitor: activity push failed: {line}", file=sys.stderr)
    if errors and len(errors) == total:
        sys.exit(1)


# --- local activity poll (push-mode detection for CLIs without a lifecycle hook) --------
# Claude Code reports working/idle through its own hooks (kvm-ai-claude-hook.sh). Codex has no
# such hook, so on a device where the KVM cannot SSH in to run its own probe, codex would never
# report a working state. This polls the same signals the KVM's SSH probe uses — a busy process
# or a freshly written session log — and pushes `active`/`stop` itself. Runs from its own
# LaunchAgent on a short interval; the KVM holds each working state for its own 120 s window and
# we send an explicit `stop` on the first idle poll, so the tile lapses promptly once codex stops.

# How recently codex's session transcript must have been written to count as "working". Codex
# streams turn events (response_item/event_msg) into the rollout jsonl as it works, so during a
# turn this file is written every few seconds; at an idle prompt it goes stale within seconds.
# Kept short so the tile clears quickly once codex stops, but long enough to bridge normal gaps
# between streamed events. (NB: codex's *.sqlite-wal churns every few seconds even when idle, so
# it is NOT a working signal — only the rollout transcript is.)
SESSION_ACTIVE_WINDOW_SECONDS = 60

# Providers detected by polling. Codex has a session transcript to key on; the rest (including
# Claude) fall back to "a matching process is busy" — exactly like the KVM's SSH probe. Add a
# provider by adding a spec here; `sessions` is optional.
#
# Claude is polled process-only so the working animation works even when the opt-in Claude Code
# lifecycle hooks are NOT installed (the default). When the hooks ARE installed they push the same
# active/stop events with tighter timing; the redundant poll pushes agree and are harmless. Using
# the process signal (rather than transcript recency) keeps the tile from lingering after a turn.
POLL_PROVIDERS = {
    "claude": {
        "process": re.compile(r"(?:^|/)claude(?:\s|$)", re.I),
        "process_exclude": (" mcp", " --help", " --version"),
    },
    "codex": {
        "process": re.compile(r"(?:^|/)codex(?:\s|$)", re.I),
        "process_exclude": (" app-server", " --help", " --version"),
        "sessions": pathlib.Path.home() / ".codex" / "sessions",
    },
    "copilot": {"process": re.compile(r"(?:^|/)(?:copilot|github-copilot)(?:\s|$)", re.I)},
    "gemini": {"process": re.compile(r"(?:^|/)gemini(?:\s|$)", re.I)},
    "grok": {"process": re.compile(r"(?:^|/)(?:grok|grok-build)(?:\s|$)", re.I)},
}


def _busy_processes():
    """provider_id -> [running, busy] from one ps scan. POSIX only; returns empty where ps is
    unavailable (Windows), leaving session-log recency as the sole signal."""
    ps_bin = "/bin/ps" if os.path.exists("/bin/ps") else "ps"
    try:
        output = subprocess.run(
            (ps_bin, "-axo", "pcpu=,command="), capture_output=True, text=True,
            timeout=3, check=False,
        ).stdout
    except Exception:
        return {}
    result = {provider_id: [False, False] for provider_id in POLL_PROVIDERS}
    for line in output.splitlines():
        match = re.match(r"^\s*([\d.]+)\s+(.*\S)\s*$", line)
        if not match:
            continue
        command = match.group(2)
        try:
            cpu = float(match.group(1))
        except ValueError:
            cpu = 0.0
        lower = command.lower()
        for provider_id, spec in POLL_PROVIDERS.items():
            if not spec["process"].search(command):
                continue
            if any(token in lower for token in spec.get("process_exclude", ())):
                continue
            result[provider_id][0] = True
            if cpu >= 0.2:
                result[provider_id][1] = True
    return result


def _newest_session_mtime(root):
    """(newest .jsonl mtime, any-.jsonl-found) under root, time/visit bounded so a large session
    history can't stall the poll."""
    root = pathlib.Path(root).expanduser()
    if not root.is_dir():
        return None, False
    newest = None
    found = False
    visited = 0
    deadline = time.monotonic() + 0.4
    stack = [root]
    while stack and visited < 4096 and time.monotonic() < deadline:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for entry in entries:
            visited += 1
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(pathlib.Path(entry.path))
                elif entry.name.endswith(".jsonl") and entry.is_file(follow_symlinks=False):
                    found = True
                    mtime = entry.stat(follow_symlinks=False).st_mtime
                    newest = mtime if newest is None else max(newest, mtime)
            except OSError:
                continue
    return newest, found


def detect_working():
    processes = _busy_processes()
    now = time.time()
    states = {}
    for provider_id, spec in POLL_PROVIDERS.items():
        _running, busy = processes.get(provider_id, (False, False))
        # A freshly-written session transcript is the signal a turn is in progress (codex); for
        # providers without one, a briefly-busy process is the only local signal. Idle background
        # sqlite churn is deliberately ignored — only the transcript counts as work.
        recent = False
        sessions = spec.get("sessions")
        if sessions is not None:
            mtime, _files = _newest_session_mtime(sessions)
            recent = mtime is not None and now - mtime <= SESSION_ACTIVE_WINDOW_SECONDS
        states[provider_id] = recent or busy
    return states


def poll_state_path():
    return config_dir() / "poll-activity-state.json"


def _load_poll_state():
    try:
        return json.loads(poll_state_path().read_text())
    except Exception:
        return {}


def _save_poll_state(state):
    try:
        config_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
        path = poll_state_path()
        path.write_text(json.dumps(state))
        os.chmod(path, 0o600)
    except Exception:
        pass


def cmd_poll_activity(_args):
    states = detect_working()
    previous = _load_poll_state()
    next_state = {}
    for provider_id, working in states.items():
        was_working = bool(previous.get(provider_id))
        # `active` refreshes the KVM's working window each poll; `stop` clears it promptly on the
        # first idle poll instead of waiting for the window to lapse.
        if working:
            event = "active"
        elif was_working:
            event = "stop"
        else:
            next_state[provider_id] = False
            continue
        payload = {"schemaVersion": SCHEMA_VERSION, "provider": provider_id, "event": event}
        try:
            push_to_targets("/push/v1/activity", payload, timeout=3)
        except Exception as error:
            print(f"kvm-ai-monitor: activity poll push failed: {error}", file=sys.stderr)
        next_state[provider_id] = working
    _save_poll_state(next_state)


# --- claude code lifecycle hooks (opt-in) --------------------------------------------------
# Hooks give the tightest "Claude is working" timing but require editing the user's
# ~/.claude/settings.json. They are OFF by default: the poller above already covers Claude, so
# usage and a live working animation work without them. These subcommands let the menu-bar app's
# Settings toggle enable/disable hooks without shipping the repo. Only entries whose command points
# at our installed hook script are touched; everything else in settings.json is preserved.
CLAUDE_SETTINGS_PATH = pathlib.Path.home() / ".claude" / "settings.json"
HOOK_SCRIPT_PATH = pathlib.Path(__file__).resolve().with_name("kvm-ai-claude-hook.sh")
HOOK_EVENTS = {
    "SessionStart": "start", "UserPromptSubmit": "active", "PostToolUse": "active",
    "Stop": "stop", "SessionEnd": "stop",
}
HOOK_QUOTE = '"' if sys.platform == "win32" else "'"


def _hook_command(event):
    return f"{HOOK_QUOTE}{HOOK_SCRIPT_PATH}{HOOK_QUOTE} {HOOK_EVENTS[event]}"


def _hook_is_ours(command):
    return isinstance(command, str) and "kvm-ai-claude-hook" in command


def _load_claude_settings():
    if CLAUDE_SETTINGS_PATH.exists():
        try:
            return json.loads(CLAUDE_SETTINGS_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_claude_settings(settings):
    CLAUDE_SETTINGS_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if CLAUDE_SETTINGS_PATH.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        CLAUDE_SETTINGS_PATH.with_name(f"settings.json.backup-{stamp}").write_text(CLAUDE_SETTINGS_PATH.read_text())
    with CLAUDE_SETTINGS_PATH.open("w") as stream:
        json.dump(settings, stream, indent=2)
        stream.write("\n")


def hooks_installed():
    hooks = _load_claude_settings().get("hooks")
    if not isinstance(hooks, dict):
        return False
    return any(
        isinstance(entry, dict) and any(
            isinstance(item, dict) and _hook_is_ours(item.get("command"))
            for item in entry.get("hooks", [])
        )
        for entries in hooks.values() if isinstance(entries, list)
        for entry in entries
    )


def cmd_install_hooks(args):
    settings = _load_claude_settings()
    hooks = settings.setdefault("hooks", {})
    changed = False
    for event in HOOK_EVENTS:
        entries = hooks.setdefault(event, [])
        command = _hook_command(event)
        if any(isinstance(e, dict) and any(isinstance(i, dict) and i.get("command") == command
                                           for i in e.get("hooks", [])) for e in entries):
            continue
        entries.append({"hooks": [{"type": "command", "command": command}]})
        changed = True
    if changed:
        _save_claude_settings(settings)
    print("on")


def cmd_uninstall_hooks(args):
    settings = _load_claude_settings()
    hooks = settings.get("hooks")
    if isinstance(hooks, dict):
        changed = False
        for event in list(hooks.keys()):
            entries = hooks[event]
            if not isinstance(entries, list):
                continue
            kept_entries = []
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                    kept_entries.append(entry)
                    continue
                kept = [i for i in entry["hooks"] if not (isinstance(i, dict) and _hook_is_ours(i.get("command")))]
                if len(kept) != len(entry["hooks"]):
                    changed = True
                if kept:
                    kept_entries.append({**entry, "hooks": kept})
            if kept_entries:
                hooks[event] = kept_entries
            else:
                del hooks[event]
        if not hooks:
            settings.pop("hooks", None)
        if changed:
            _save_claude_settings(settings)
    print("off")


def cmd_hooks_status(args):
    print("on" if hooks_installed() else "off")


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
    activity_parser.add_argument("--provider", default=DEFAULT_ACTIVITY_PROVIDER,
                                 help="provider this event is for (default: %(default)s)")
    activity_parser.set_defaults(func=cmd_send_activity)

    sub.add_parser("poll-activity",
                   help="detect local CLI activity (codex) and push active/stop events"
                   ).set_defaults(func=cmd_poll_activity)

    sub.add_parser("print-payload", help="print the usage payload without sending it").set_defaults(func=cmd_print_payload)
    sub.add_parser("app-usage", help="print usage + working state as one JSON blob for the menu-bar app").set_defaults(func=cmd_app_usage)

    store_parser = sub.add_parser("store-secret", help="store the device push secret (read from stdin)")
    store_parser.add_argument("--kvm", required=True)
    store_parser.set_defaults(func=cmd_store_secret)

    sub.add_parser("install-hooks", help="enable opt-in Claude Code working-state hooks").set_defaults(func=cmd_install_hooks)
    sub.add_parser("uninstall-hooks", help="disable Claude Code working-state hooks").set_defaults(func=cmd_uninstall_hooks)
    sub.add_parser("hooks-status", help="print 'on' or 'off' for Claude hooks").set_defaults(func=cmd_hooks_status)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
