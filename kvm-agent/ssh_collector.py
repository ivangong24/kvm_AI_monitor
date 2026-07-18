#!/usr/bin/env python3
"""KVM-owned discovery and read-only connected-device telemetry collection."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path


PROVIDER_IDS = ("claude", "codex", "copilot", "gemini", "grok")
PROVIDER_INFO = {
    "claude": {
        "name": "Claude Code",
        "installCommand": "curl -fsSL https://claude.ai/install.sh | bash",
        "loginCommand": "claude auth login",
        "installUrl": "https://code.claude.com/docs/en/setup",
        "capabilityNote": "Claude CLI authentication and local session logs are read natively; account-wide token history is not exposed.",
    },
    "codex": {
        "name": "Codex",
        "installCommand": "npm install -g @openai/codex",
        "loginCommand": "codex login",
        "installUrl": "https://developers.openai.com/codex/cli/",
        "capabilityNote": "Codex app-server supplies native ChatGPT plan windows and account-wide daily token totals.",
    },
    "copilot": {
        "name": "GitHub Copilot",
        "installCommand": "npm install -g @github/copilot",
        "loginCommand": "copilot login",
        "installUrl": "https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli",
        "capabilityNote": "Installation and native authentication are detected; Copilot does not expose a supported quota command.",
    },
    "gemini": {
        "name": "Gemini CLI",
        "installCommand": "npm install -g @google/gemini-cli",
        "loginCommand": "gemini",
        "installUrl": "https://github.com/google-gemini/gemini-cli",
        "capabilityNote": "Installation and native authentication are detected; Gemini CLI does not expose a supported quota command.",
    },
    "grok": {
        "name": "Grok Build",
        "installCommand": "curl -fsSL https://x.ai/cli/install.sh | bash",
        "loginCommand": "grok",
        "installUrl": "https://x.ai/cli",
        "capabilityNote": "Installation and native authentication are detected; Grok does not expose a supported quota command.",
    },
}

WORKING_ALIASES = {
    "claude": ("claude",),
    "codex": ("codex",),
    "copilot": ("copilot", "github copilot"),
    "gemini": ("gemini",),
    "grok": ("grok", "grok-build"),
}

def parse_working_processes(output: str) -> dict[str, bool]:
    states = {provider_id: False for provider_id in PROVIDER_IDS}
    for line in output.splitlines():
        match = re.match(r"^(.*\S)\s+([\d.]+)$", line)
        if not match:
            continue
        try:
            cpu = float(match.group(2))
        except ValueError:
            continue
        if cpu < 0.2:
            continue
        name = Path(match.group(1)).name.lower()
        for provider_id, values in WORKING_ALIASES.items():
            if any(name == value or name.startswith(value + " ") for value in values):
                states[provider_id] = True
    return states


REMOTE_ACTIVITY_PROBE = r'''
import json
import os
import pathlib
import re
import subprocess
import time

IDS = ("claude", "codex", "copilot", "gemini", "grok")
ACTIVE_WINDOW_SECONDS = 120

try:
    process_output = subprocess.run(
        ("/bin/ps", "-axo", "pcpu=,command="), capture_output=True, text=True,
        timeout=3, check=False,
    ).stdout
except Exception:
    process_output = ""

processes = {provider_id: False for provider_id in IDS}
busy = {provider_id: False for provider_id in IDS}
patterns = {
    "claude": re.compile(r"(?:^|/)claude(?:\s|$)|Application Support/Claude/claude-code/claude", re.I),
    "codex": re.compile(r"(?:^|/)codex(?:\s|$)", re.I),
    "copilot": re.compile(r"(?:^|/)(?:copilot|github-copilot)(?:\s|$)", re.I),
    "gemini": re.compile(r"(?:^|/)gemini(?:\s|$)", re.I),
    "grok": re.compile(r"(?:^|/)(?:grok|grok-build)(?:\s|$)", re.I),
}
for line in process_output.splitlines():
    match = re.match(r"^\s*([\d.]+)\s+(.*\S)\s*$", line)
    if not match:
        continue
    command = match.group(2)
    try:
        cpu = float(match.group(1))
    except ValueError:
        cpu = 0
    for provider_id, pattern in patterns.items():
        if not pattern.search(command):
            continue
        lower = command.lower()
        if provider_id == "codex" and any(value in lower for value in (" app-server", " --help", " --version")):
            continue
        if (provider_id == "codex" and ".app/" in lower
                and not lower.startswith("/applications/codex.app/contents/resources/codex ")):
            continue
        if provider_id == "claude" and any(value in lower for value in (" --help", " --version", "claude-code-acp")):
            continue
        if (provider_id == "claude" and ".app/" in lower
                and "application support/claude/claude-code/claude" not in lower):
            continue
        processes[provider_id] = True
        if cpu >= 0.2:
            busy[provider_id] = True

def newest_jsonl(root):
    root = pathlib.Path(root).expanduser()
    if not root.is_dir():
        return None, False
    newest = None
    found = False
    visited = 0
    deadline = time.monotonic() + 0.4
    stack = [(root, 0)]
    while stack and visited < 1024 and time.monotonic() < deadline:
        directory, depth = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        def modified(entry):
            try:
                return entry.stat(follow_symlinks=False).st_mtime
            except OSError:
                return 0
        entries.sort(key=modified)
        for entry in entries:
            visited += 1
            if visited > 1024 or time.monotonic() >= deadline:
                break
            try:
                if entry.is_dir(follow_symlinks=False) and depth < 6:
                    stack.append((pathlib.Path(entry.path), depth + 1))
                elif entry.is_file(follow_symlinks=False) and entry.name.endswith(".jsonl"):
                    found = True
                    value = entry.stat(follow_symlinks=False).st_mtime
                    newest = value if newest is None else max(newest, value)
            except OSError:
                continue
    return newest, found

now = time.time()
codex_mtime, codex_files = newest_jsonl(pathlib.Path.home() / ".codex/sessions")
claude_mtime, claude_files = newest_jsonl(pathlib.Path.home() / ".claude/projects")
codex_recent = codex_mtime is not None and now - codex_mtime <= ACTIVE_WINDOW_SECONDS
claude_recent = claude_mtime is not None and now - claude_mtime <= ACTIVE_WINDOW_SECONDS

states = {
    "claude": processes["claude"] and (claude_recent or busy["claude"] or not claude_files),
    "codex": codex_recent or (processes["codex"] and (busy["codex"] or not codex_files)),
    "copilot": busy["copilot"],
    "gemini": busy["gemini"],
    "grok": busy["grok"],
}
print(json.dumps(states, separators=(",", ":")))
'''


def parse_activity_entry(entry: str, default_user: str, default_port: int) -> tuple[str, str, int]:
    text = entry.strip()
    user = default_user
    at = text.find("@")
    if at != -1:
        candidate, text = text[:at], text[at + 1:]
        if candidate:
            user = candidate
    host, port = text, default_port
    last_colon = text.rfind(":")
    if last_colon != -1:
        prefix, suffix = text[:last_colon], text[last_colon + 1:]
        if suffix.isdigit() and ":" not in prefix:
            host, port = prefix, int(suffix)
    return host, user, port


def parse_activity_probe(output: str) -> dict[str, bool]:
    try:
        value = json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError("activity device returned invalid state") from error
    if not isinstance(value, dict):
        raise RuntimeError("activity device returned invalid state")
    return {provider_id: value.get(provider_id) is True for provider_id in PROVIDER_IDS}


REMOTE_COLLECTOR = r'''
import concurrent.futures
import datetime
import json
import os
import pathlib
import re
import select
import shutil
import subprocess
import time

IDS = ("claude", "codex", "copilot", "gemini", "grok")
os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")
DEFINITIONS = {
    "claude": {"cli": ("claude",), "apps": ("Claude.app",), "auth": (".claude/.credentials.json",), "extensions": ()},
    "codex": {"cli": ("codex",), "apps": ("Codex.app",), "auth": (".codex/auth.json",), "extensions": ()},
    "copilot": {"cli": ("copilot",), "apps": ("GitHub Copilot for Xcode.app",), "auth": (".copilot",), "extensions": ("github.copilot-", "github.copilot-chat-")},
    "gemini": {"cli": ("gemini",), "apps": ("Gemini.app",), "auth": (".gemini/oauth_creds.json", ".gemini/google_accounts.json"), "extensions": ("google.geminicodeassist-",)},
    "grok": {"cli": ("grok", "grok-build"), "apps": ("Grok.app",), "auth": (".grok", ".xai"), "extensions": ()},
}
HOME = pathlib.Path.home()
CODEX = shutil.which("codex")
if not CODEX and os.access("/Applications/Codex.app/Contents/Resources/codex", os.X_OK):
    CODEX = "/Applications/Codex.app/Contents/Resources/codex"
def run(args, timeout, input_text=None):
    try:
        return subprocess.run(args, input=input_text, capture_output=True, text=True, timeout=timeout, check=False).stdout
    except Exception:
        return ""

def number(value):
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else None
    except (TypeError, ValueError):
        return None

def plan_label(value):
    if not isinstance(value, str) or not value:
        return None
    return re.sub(r"\b\w", lambda match: match.group(0).upper(), re.sub(r"[_-]+", " ", value))

def native_limit(value):
    if not isinstance(value, dict) or number(value.get("usedPercent")) is None:
        return None
    minutes = number(value.get("windowDurationMins"))
    label = "Current session" if minutes and minutes <= 360 else "Weekly limit"
    result = {"label": label, "usedPercent": max(0, min(100, number(value.get("usedPercent"))))}
    if minutes is not None:
        result["windowMinutes"] = minutes
    resets = number(value.get("resetsAt"))
    if resets is not None:
        result["resetsAt"] = datetime.datetime.fromtimestamp(resets, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    return result

def codex_native():
    if not CODEX:
        return None, [], False
    messages = (
        {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "kvm-ai-monitor", "title": "KVM AI Monitor", "version": "1.0.0"}, "capabilities": {"experimentalApi": True}}},
        {"method": "initialized", "params": {}},
        {"id": 2, "method": "account/rateLimits/read", "params": {}},
        {"id": 3, "method": "account/usage/read", "params": {}},
    )
    payload = "\n".join(json.dumps(message, separators=(",", ":")) for message in messages) + "\n"
    responses = {}
    process = None
    try:
        process = subprocess.Popen((CODEX, "app-server", "--stdio"), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True, bufsize=1)
        process.stdin.write(payload)
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
                if message.get("id") in (2, 3) and isinstance(message.get("result"), dict):
                    responses[message["id"]] = message["result"]
            except Exception:
                continue
    except Exception:
        pass
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=2)
            except Exception:
                process.kill()
    rate_result = responses.get(2, {})
    rate = rate_result.get("rateLimits") if isinstance(rate_result.get("rateLimits"), dict) else {}
    limits = [native_limit(rate.get("primary")), native_limit(rate.get("secondary"))]
    credits = rate.get("credits") if isinstance(rate.get("credits"), dict) else {}
    account = None
    if rate:
        account = {
            "source": "Codex app-server",
            "updatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            "plan": plan_label(rate.get("planType")),
            "limits": [value for value in limits if value],
            "creditsRemaining": number(credits.get("balance")) if credits.get("hasCredits") else None,
            "providerCostUSD": None,
        }
    usage = responses.get(3, {})
    buckets = usage.get("dailyUsageBuckets") if isinstance(usage.get("dailyUsageBuckets"), list) else []
    if usage and not account:
        account = {
            "source": "Codex app-server",
            "updatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            "plan": None,
            "limits": [],
            "creditsRemaining": None,
            "providerCostUSD": None,
        }
    daily = [{"date": item.get("startDate"), "totalTokens": number(item.get("tokens"))}
             for item in buckets if isinstance(item, dict) and isinstance(item.get("startDate"), str)]
    return account, daily, bool(rate or usage)

def claude_daily():
    root = HOME / ".claude/projects"
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

def claude_native():
    if not shutil.which("claude"):
        return None, [], False
    authenticated = False
    plan = None
    try:
        status = json.loads(run(("claude", "auth", "status", "--json"), 10))
        authenticated = status.get("loggedIn") is True
        plan = plan_label(status.get("subscriptionType"))
    except Exception:
        pass
    account = {
        "source": "Claude CLI",
        "updatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "plan": plan,
        "limits": [],
        "creditsRemaining": None,
        "providerCostUSD": None,
    } if authenticated else None
    return account, claude_daily(), authenticated

def installation(provider_id):
    definition = DEFINITIONS[provider_id]
    cli = any(shutil.which(name) for name in definition["cli"])
    desktop = any((pathlib.Path("/Applications") / name).exists() or (HOME / "Applications" / name).exists() for name in definition["apps"])
    extensions_root = HOME / ".vscode/extensions"
    integration = extensions_root.is_dir() and any(entry.name.startswith(definition["extensions"]) for entry in extensions_root.iterdir()) if definition["extensions"] else False
    authenticated = any((HOME / value).exists() for value in definition["auth"])
    keychain = False
    if provider_id == "claude" and pathlib.Path("/usr/bin/security").is_file():
        try:
            keychain = subprocess.run(
                ("/usr/bin/security", "find-generic-password", "-s", "Claude Code-credentials"),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3, check=False,
            ).returncode == 0
        except Exception:
            pass
    return {"cliInstalled": cli, "desktopInstalled": desktop, "integrationInstalled": integration, "authenticated": authenticated, "protectedCredentialDetected": keychain}

def working_states():
    result = {provider_id: False for provider_id in IDS}
    patterns = {
        "claude": re.compile(r"(?:^|/)claude(?:\s|$)", re.I),
        "codex": re.compile(r"(?:^|/)codex(?:\s|$)", re.I),
        "copilot": re.compile(r"(?:^|/)copilot(?:\s|$)", re.I),
        "gemini": re.compile(r"(?:^|/)gemini(?:\s|$)", re.I),
        "grok": re.compile(r"(?:^|/)(?:grok|grok-build)(?:\s|$)", re.I),
    }
    for line in run(("/bin/ps", "-axo", "comm=,pcpu="), 3).splitlines():
        match = re.match(r"^(.*\S)\s+([\d.]+)$", line)
        if not match or number(match.group(2)) is None or number(match.group(2)) < 0.2:
            continue
        for provider_id, pattern in patterns.items():
            if pattern.search(match.group(1)):
                result[provider_id] = True
    return result

with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
    codex_future = executor.submit(codex_native)
    claude_future = executor.submit(claude_native)
    native = {"codex": codex_future.result(), "claude": claude_future.result()}

working = working_states()
providers = []
for provider_id in IDS:
    detected = installation(provider_id)
    account, daily, native_authenticated = native.get(provider_id, (None, [], False))
    providers.append({
        "id": provider_id,
        "installation": detected,
        "authenticated": detected["authenticated"] or native_authenticated,
        "working": working[provider_id],
        "account": account,
        "daily": daily,
        "tokenScope": "account" if provider_id == "codex" else "connected_device",
    })
print(json.dumps({"generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"), "providers": providers}, separators=(",", ":")))
'''


def _number(value: object) -> float:
    try:
        result = float(value or 0)
        return result if result == result and abs(result) != float("inf") else 0
    except (TypeError, ValueError):
        return 0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_usage_snapshot(raw: dict[str, object]) -> dict[str, object]:
    by_id = {
        item.get("id"): item for item in raw.get("providers", [])
        if isinstance(item, dict) and item.get("id") in PROVIDER_IDS
    }
    providers = []
    today_key = datetime.now().astimezone().date().isoformat()
    cutoff_key = (datetime.now().astimezone().date() - timedelta(days=29)).isoformat()
    for provider_id in PROVIDER_IDS:
        item = by_id.get(provider_id, {})
        info = PROVIDER_INFO[provider_id]
        account = item.get("account") if isinstance(item.get("account"), dict) else {}
        installation = item.get("installation") if isinstance(item.get("installation"), dict) else {}
        daily = item.get("daily") if isinstance(item.get("daily"), list) else []
        safe_daily = []
        for day in daily:
            if (not isinstance(day, dict) or not isinstance(day.get("date"), str)
                    or day["date"] < cutoff_key or day["date"] > today_key):
                continue
            cost = _number(day.get("totalCost")) if day.get("totalCost") is not None else None
            safe_daily.append({
                "date": day["date"],
                "tokens": _number(day.get("totalTokens")),
                "inputTokens": _number(day.get("inputTokens")),
                "outputTokens": _number(day.get("outputTokens")),
                "cacheReadTokens": _number(day.get("cacheReadTokens")),
                "cacheWriteTokens": _number(day.get("cacheCreationTokens")),
                "costUSD": cost,
            })
        safe_daily.sort(key=lambda value: value["date"])
        today = next((day for day in safe_daily if day["date"] == today_key), {
            "date": today_key, "tokens": 0, "inputTokens": 0, "outputTokens": 0,
            "cacheReadTokens": 0, "cacheWriteTokens": 0, "costUSD": None,
        })
        limits = account.get("limits") if isinstance(account.get("limits"), list) else []
        has_usage = bool(limits or safe_daily or account.get("creditsRemaining") is not None
                         or account.get("providerCostUSD") is not None)
        installed = any(installation.get(key) is True for key in (
            "cliInstalled", "desktopInstalled", "integrationInstalled",
        ))
        authenticated = item.get("authenticated") is True
        protected_credential = installation.get("protectedCredentialDetected") is True
        if not installed:
            connection_state = "not_installed"
        elif not authenticated and protected_credential:
            connection_state = "verification_required"
        elif not authenticated:
            connection_state = "login_required"
        elif has_usage:
            connection_state = "ready"
        else:
            connection_state = "usage_unavailable"
        working = item.get("working") is True
        last_used_at = _utc_now() if working else None
        if not last_used_at and safe_daily:
            used_day = next((day for day in reversed(safe_daily) if day["tokens"] > 0), None)
            if used_day:
                last_used_at = f"{used_day['date']}T12:00:00Z"
        costs = [day["costUSD"] for day in safe_daily if day["costUSD"] is not None]
        token_scope = item.get("tokenScope") if item.get("tokenScope") in ("account", "connected_device") else "connected_device"
        providers.append({
            "id": provider_id,
            "name": info["name"],
            "status": "active" if working else "available" if has_usage else "unavailable",
            "plan": account.get("plan") or ("Detected locally" if installed else "Not detected"),
            "usageKind": "limits" if limits else "activity",
            "source": account.get("source") or "KVM SSH pull",
            "exactSubscriptionUsage": bool(limits),
            "limits": limits,
            "activity": {
                "today": today,
                "last7Days": safe_daily[-7:],
                "last30Days": safe_daily,
                "last30DaysTokens": sum(day["tokens"] for day in safe_daily),
                "last30DaysCostUSD": sum(costs) if costs else None,
                "lastUsedAt": last_used_at,
                "model": "Not collected",
            },
            "trackedTokenTotalsAvailable": bool(safe_daily),
            "tokenTotalsScope": token_scope,
            "accountTokenTotalsAvailable": bool(safe_daily) and token_scope == "account",
            "creditsRemaining": account.get("creditsRemaining"),
            "providerCostUSD": account.get("providerCostUSD"),
            "working": working,
            "deviceWorking": working,
            "authorizedDeviceWorking": False,
            "workingSource": "device" if working else None,
            "activityState": "working" if working else "standby",
            "usageAvailable": has_usage,
            "connectionState": connection_state,
            "capabilityNote": (
                "Claude credentials were detected in macOS Keychain, but Claude Code reports no active login to the KVM SSH session. Verify with /status or /login on the connected device."
                if provider_id == "claude" and connection_state == "verification_required"
                else info["capabilityNote"]
            ),
            "installation": {
                "installed": installed,
                "cliInstalled": installation.get("cliInstalled") is True,
                "desktopInstalled": installation.get("desktopInstalled") is True,
                "integrationInstalled": installation.get("integrationInstalled") is True,
                "installCommand": info["installCommand"],
                "installUrl": info["installUrl"],
            },
            "authentication": {
                "authenticated": authenticated,
                "protectedCredentialDetected": protected_credential,
                "loginCommand": info["loginCommand"],
            },
        })
    return {"generatedAt": raw.get("generatedAt") or _utc_now(), "providers": providers}


class SshCollector:
    def __init__(self, key_path: Path) -> None:
        self.key_path = key_path
        self.ensure_key()

    def ensure_key(self) -> None:
        if self.key_path.is_file():
            os.chmod(self.key_path, 0o600)
            return
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["dropbearkey", "-t", "ed25519", "-f", str(self.key_path)],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("could not create the KVM device key")
        os.chmod(self.key_path, 0o600)

    def public_key(self) -> str:
        result = subprocess.run(
            ["dropbearkey", "-y", "-f", str(self.key_path)],
            capture_output=True, text=True, timeout=10, check=False,
        )
        for line in result.stdout.splitlines():
            if line.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-")):
                return f"{line} kvm-ai-monitor"
        return ""

    def _ssh(self, host: str, user: str, port: int, command: str,
             input_text: str | None = None, timeout: int = 8) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "/bin/ssh", "-i", str(self.key_path), "-T", "-q", "-y",
                "-o", "BatchMode=yes", "-o", "PasswordAuthentication=no",
                "-p", str(port), f"{user}@{host}", command,
            ],
            input=input_text, capture_output=True, text=True, timeout=timeout, check=False,
        )

    def _authorized(self, host: str, user: str, port: int) -> bool:
        try:
            result = self._ssh(host, user, port, "printf __KVM_AI_DEVICE__", timeout=5)
            return result.returncode == 0 and result.stdout == "__KVM_AI_DEVICE__"
        except (OSError, subprocess.SubprocessError):
            return False

    def probe_activity(self, host: str, user: str, port: int = 22) -> dict[str, bool]:
        try:
            result = self._ssh(
                host, user, port, "/usr/bin/env python3 -",
                input_text=REMOTE_ACTIVITY_PROBE, timeout=8,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError("activity-device probe failed") from error
        if result.returncode != 0:
            raise RuntimeError("activity-device probe failed")
        return parse_activity_probe(result.stdout)

    def probe_activity_hosts(self, hosts: list[str], user: str,
                             port: int = 22) -> dict[str, dict[str, bool]]:
        unique_entries = list(dict.fromkeys(entry for entry in hosts if entry))
        results: dict[str, dict[str, bool]] = {}
        if not unique_entries:
            return results
        with ThreadPoolExecutor(max_workers=min(6, len(unique_entries))) as executor:
            futures = {}
            for entry in unique_entries:
                host, entry_user, entry_port = parse_activity_entry(entry, user, port)
                futures[executor.submit(self.probe_activity, host, entry_user, entry_port)] = entry
            for future in as_completed(futures):
                try:
                    results[futures[future]] = future.result()
                except RuntimeError:
                    continue
        return results

    @staticmethod
    def _networks() -> list[ipaddress.IPv4Network]:
        try:
            result = subprocess.run(
                ["ip", "-4", "-o", "addr", "show", "scope", "global"],
                capture_output=True, text=True, timeout=5, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        networks = []
        for line in result.stdout.splitlines():
            fields = line.split()
            try:
                interface = ipaddress.IPv4Interface(fields[fields.index("inet") + 1])
            except (ValueError, IndexError, ipaddress.AddressValueError):
                continue
            network = interface.network
            if network.prefixlen < 24:
                network = ipaddress.IPv4Network(f"{interface.ip}/24", strict=False)
            if network.prefixlen <= 30 and network not in networks:
                networks.append(network)
        return networks

    @staticmethod
    def _port_open(address: str, port: int) -> bool:
        try:
            with socket.create_connection((address, port), timeout=0.25):
                return True
        except OSError:
            return False

    def discover(self, user: str, port: int = 22, preferred: str | None = None) -> str:
        if preferred and self._authorized(preferred, user, port):
            return preferred
        addresses = []
        for network in self._networks():
            addresses.extend(str(address) for address in network.hosts())
        with ThreadPoolExecutor(max_workers=64) as executor:
            futures = {executor.submit(self._port_open, address, port): address for address in addresses}
            ssh_hosts = [futures[future] for future in as_completed(futures) if future.result()]
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {executor.submit(self._authorized, host, user, port): host for host in ssh_hosts}
            for future in as_completed(futures):
                if future.result():
                    return futures[future]
        raise RuntimeError("no authorized connected device was found")

    def collect(self, user: str, host: str = "auto", port: int = 22,
                preferred: str | None = None) -> tuple[dict[str, object], str]:
        if not user:
            raise RuntimeError("connected-device username is not configured")
        resolved = self.discover(user, port, preferred) if host == "auto" else host
        if host != "auto" and not self._authorized(resolved, user, port):
            raise RuntimeError("the connected device did not accept the KVM key")
        try:
            result = self._ssh(
                resolved, user, port, "/usr/bin/env python3 -",
                input_text=REMOTE_COLLECTOR, timeout=55,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("connected-device collection timed out") from error
        if result.returncode != 0:
            raise RuntimeError("connected-device collection failed")
        try:
            raw = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("connected device returned invalid telemetry") from error
        if not isinstance(raw, dict) or not isinstance(raw.get("providers"), list):
            raise RuntimeError("connected device returned no provider data")
        return build_usage_snapshot(raw), resolved
