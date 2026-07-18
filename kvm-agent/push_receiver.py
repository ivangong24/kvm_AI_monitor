#!/usr/bin/env python3
"""Signed device push receiver: enrollment, verification, and aggregate storage."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


PROVIDER_IDS = ("claude", "codex", "copilot", "gemini", "grok")
TIMESTAMP_SKEW_SECONDS = 120
NONCE_TTL_SECONDS = 600
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_MAX_REQUESTS = 120
ACTIVITY_WINDOW_SECONDS = 120
NONCE_RE = re.compile(r"[0-9a-f]{16,64}")
SIGNATURE_RE = re.compile(r"[0-9a-f]{64}")
TIMESTAMP_RE = re.compile(r"[0-9]{1,20}")
ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _strip_control(value: str) -> str:
    return CONTROL_RE.sub("", value)


def _sanitize_name(value: object) -> str:
    text = value if isinstance(value, str) else ""
    return _strip_control(text).strip()[:48]


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or abs(number) == float("inf"):
        return None
    return number


def _valid_iso(value: object, limit: int = 40) -> str | None:
    if not isinstance(value, str) or not ISO_RE.fullmatch(value):
        return None
    return value[:limit]


class DeviceStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock = threading.Lock()
        self.devices = self._load()

    def _load(self) -> dict[str, dict[str, object]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        result: dict[str, dict[str, object]] = {}
        for item in raw.get("devices", []) if isinstance(raw, dict) else []:
            if not (isinstance(item, dict) and isinstance(item.get("id"), str)
                    and isinstance(item.get("secret"), str)):
                continue
            result[item["id"]] = {
                "id": item["id"],
                "name": str(item.get("name") or ""),
                "secret": item["secret"],
                "createdAt": item.get("createdAt") or _utc_now(),
                "revoked": item.get("revoked") is True,
                "lastSeenAt": item.get("lastSeenAt"),
                "lastUsageAt": item.get("lastUsageAt"),
                "lastActivityAt": item.get("lastActivityAt"),
            }
        return result

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix="devices.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"devices": list(self.devices.values())}, stream, indent=2)
                stream.write("\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def list(self) -> list[dict[str, object]]:
        with self.lock:
            return [
                {key: value for key, value in device.items() if key != "secret"}
                for device in self.devices.values()
            ]

    def get(self, device_id: str) -> dict[str, object] | None:
        with self.lock:
            device = self.devices.get(device_id)
            return dict(device) if device else None

    def create(self, name: object) -> dict[str, object]:
        clean = _sanitize_name(name)
        if not clean:
            raise ValueError("device name is required")
        with self.lock:
            device_id = "d-" + secrets.token_hex(4)
            while device_id in self.devices:
                device_id = "d-" + secrets.token_hex(4)
            secret = secrets.token_hex(24)
            self.devices[device_id] = {
                "id": device_id, "name": clean, "secret": secret,
                "createdAt": _utc_now(), "revoked": False,
                "lastSeenAt": None, "lastUsageAt": None, "lastActivityAt": None,
            }
            self._save_locked()
        return {"id": device_id, "name": clean, "secret": secret}

    def rotate(self, device_id: str) -> dict[str, object] | None:
        with self.lock:
            device = self.devices.get(device_id)
            if device is None:
                return None
            secret = secrets.token_hex(24)
            device["secret"] = secret
            self._save_locked()
        return {"id": device_id, "secret": secret}

    def revoke(self, device_id: str) -> bool:
        with self.lock:
            device = self.devices.get(device_id)
            if device is None:
                return False
            device["revoked"] = True
            self._save_locked()
        return True

    def delete(self, device_id: str) -> bool:
        with self.lock:
            if device_id not in self.devices:
                return False
            del self.devices[device_id]
            self._save_locked()
        return True

    def touch(self, device_id: str, kind: str | None = None) -> None:
        with self.lock:
            device = self.devices.get(device_id)
            if device is None:
                return
            now = _utc_now()
            device["lastSeenAt"] = now
            if kind == "usage":
                device["lastUsageAt"] = now
            elif kind == "activity":
                device["lastActivityAt"] = now
            self._save_locked()


class PushReceiver:
    def __init__(self, devices: DeviceStore, state_path: Path, clock=time.time) -> None:
        self.devices = devices
        self.state_path = Path(state_path)
        self.clock = clock
        self.lock = threading.Lock()
        self.nonces: dict[str, dict[str, float]] = {}
        self.request_times: dict[str, list[float]] = {}
        self.working_until: dict[tuple[str, str], float] = {}
        self.usage: dict[tuple[str, str], dict[str, object]] = {}
        self._load_state()

    def _load_state(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for entry in raw.get("usage", []) if isinstance(raw, dict) else []:
            if not isinstance(entry, dict):
                continue
            device_id, provider, snapshot = entry.get("deviceId"), entry.get("provider"), entry.get("snapshot")
            if isinstance(device_id, str) and isinstance(provider, str) and isinstance(snapshot, dict):
                self.usage[(device_id, provider)] = snapshot

    def _save_state_locked(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "usage": [
                {"deviceId": device_id, "provider": provider, "snapshot": snapshot}
                for (device_id, provider), snapshot in self.usage.items()
            ]
        }
        descriptor, temporary = tempfile.mkstemp(prefix="push-state.", suffix=".tmp", dir=self.state_path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2)
                stream.write("\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def verify(self, device_id: object, timestamp: object, nonce: object, signature: object,
              method: str, path: str, body_bytes: bytes) -> dict[str, object] | None:
        if not isinstance(device_id, str) or not isinstance(nonce, str) or not isinstance(signature, str):
            return None
        if not NONCE_RE.fullmatch(nonce) or not SIGNATURE_RE.fullmatch(signature):
            return None
        timestamp_text = timestamp if isinstance(timestamp, str) else str(timestamp)
        if not TIMESTAMP_RE.fullmatch(timestamp_text):
            return None
        device = self.devices.get(device_id)
        if device is None or device.get("revoked") is True:
            return None
        now = self.clock()
        if abs(now - int(timestamp_text)) > TIMESTAMP_SKEW_SECONDS:
            return None
        with self.lock:
            seen = nonce in self.nonces.get(device_id, {})
        if seen:
            return None
        body_hash = hashlib.sha256(body_bytes).hexdigest()
        string_to_sign = f"v1\n{timestamp_text}\n{nonce}\n{method.upper()}\n{path}\n{body_hash}"
        expected = hmac.new(device["secret"].encode("ascii"), string_to_sign.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return None
        with self.lock:
            cache = self.nonces.setdefault(device_id, {})
            for stale, seen_at in list(cache.items()):
                if now - seen_at > NONCE_TTL_SECONDS:
                    del cache[stale]
            if nonce in cache:
                return None
            cache[nonce] = now
        self.devices.touch(device_id)
        return device

    def rate_limited(self, device_id: str) -> bool:
        now = self.clock()
        with self.lock:
            window = self.request_times.setdefault(device_id, [])
            window[:] = [seen for seen in window if now - seen <= RATE_LIMIT_WINDOW_SECONDS]
            if len(window) >= RATE_LIMIT_MAX_REQUESTS:
                return True
            window.append(now)
            return False

    def handle_usage(self, device: dict[str, object], body: dict[str, object]) -> bool:
        provider = body.get("provider")
        if provider not in PROVIDER_IDS or body.get("schemaVersion") != 1:
            return False
        plan = body.get("plan")
        plan = _strip_control(plan)[:40] if isinstance(plan, str) else None
        limits: list[dict[str, object]] = []
        for item in (body.get("limits") if isinstance(body.get("limits"), list) else [])[:8]:
            if not isinstance(item, dict):
                continue
            entry: dict[str, object] = {}
            label = item.get("label")
            if isinstance(label, str):
                entry["label"] = _strip_control(label)[:40]
            used_percent = _finite(item.get("usedPercent"))
            if used_percent is not None:
                entry["usedPercent"] = max(0.0, min(100.0, used_percent))
            window_minutes = _finite(item.get("windowMinutes"))
            if window_minutes is not None and window_minutes > 0:
                entry["windowMinutes"] = window_minutes
            resets_at = _valid_iso(item.get("resetsAt"))
            if resets_at is not None:
                entry["resetsAt"] = resets_at
            if entry:
                limits.append(entry)
        daily: list[dict[str, object]] = []
        today = datetime.now().astimezone().date()
        cutoff = today - timedelta(days=29)
        for item in (body.get("daily") if isinstance(body.get("daily"), list) else [])[:31]:
            if not isinstance(item, dict):
                continue
            date_text = item.get("date")
            if not isinstance(date_text, str):
                continue
            try:
                date_value = datetime.strptime(date_text, "%Y-%m-%d").date()
            except ValueError:
                continue
            if date_value < cutoff or date_value > today:
                continue
            entry = {"date": date_text}
            for key in ("totalTokens", "inputTokens", "outputTokens", "cacheReadTokens", "cacheCreationTokens"):
                value = _finite(item.get(key))
                entry[key] = value if value is not None and value >= 0 else 0
            daily.append(entry)
        if not limits:
            # A device whose limits fetch transiently failed still pushes plan/daily data;
            # keep the last known limits instead of blanking the display's quota bars.
            with self.lock:
                previous = self.usage.get((device["id"], provider))
            if previous and isinstance(previous.get("limits"), list):
                limits = previous["limits"]
        snapshot = {
            "collectedAt": _valid_iso(body.get("collectedAt")),
            "plan": plan,
            "loggedIn": body.get("loggedIn") is True,
            "limits": limits,
            "daily": daily,
            "receivedAt": _utc_now(),
            "receivedAtEpoch": self.clock(),
        }
        with self.lock:
            self.usage[(device["id"], provider)] = snapshot
            self._save_state_locked()
        self.devices.touch(device["id"], "usage")
        return True

    def handle_activity(self, device: dict[str, object], body: dict[str, object]) -> bool:
        provider = body.get("provider")
        if provider not in PROVIDER_IDS or body.get("schemaVersion") != 1:
            return False
        event = body.get("event")
        if event not in ("start", "active", "stop"):
            return False
        key = (device["id"], provider)
        with self.lock:
            if event == "stop":
                self.working_until.pop(key, None)
            else:
                self.working_until[key] = self.clock() + ACTIVITY_WINDOW_SECONDS
        self.devices.touch(device["id"], "activity")
        return True

    def active_providers(self) -> dict[str, bool]:
        now = self.clock()
        result = {provider_id: False for provider_id in PROVIDER_IDS}
        with self.lock:
            for (_, provider), until in self.working_until.items():
                if until > now and provider in result:
                    result[provider] = True
        return result

    def active_map(self) -> dict[str, dict[str, bool]]:
        now = self.clock()
        result: dict[str, dict[str, bool]] = {}
        with self.lock:
            for (device_id, provider), until in self.working_until.items():
                if until > now:
                    result.setdefault(device_id, {})[provider] = True
        return result

    def usage_overlay(self) -> dict[str, dict[str, object] | None]:
        with self.lock:
            snapshots = dict(self.usage)
        by_provider: dict[str, list[dict[str, object]]] = {}
        for (_, provider), snapshot in snapshots.items():
            by_provider.setdefault(provider, []).append(snapshot)
        result: dict[str, dict[str, object] | None] = {provider_id: None for provider_id in PROVIDER_IDS}
        for provider, snaps in by_provider.items():
            if provider not in PROVIDER_IDS:
                continue
            ordered = sorted(snaps, key=lambda item: item.get("receivedAtEpoch") or 0)
            plan = next((s.get("plan") for s in reversed(ordered) if s.get("plan")), None)
            limits = next((s.get("limits") for s in reversed(ordered) if s.get("limits")), [])
            totals: dict[str, dict[str, object]] = {}
            for snapshot in snaps:
                for day in snapshot.get("daily") or []:
                    date = day.get("date")
                    bucket = totals.setdefault(date, {
                        "date": date, "totalTokens": 0, "inputTokens": 0,
                        "outputTokens": 0, "cacheReadTokens": 0, "cacheCreationTokens": 0,
                    })
                    for key in ("totalTokens", "inputTokens", "outputTokens", "cacheReadTokens", "cacheCreationTokens"):
                        bucket[key] += day.get(key) or 0
            result[provider] = {
                "plan": plan,
                "limits": limits,
                "daily": [totals[key] for key in sorted(totals)],
                "loggedIn": any(s.get("loggedIn") for s in snaps),
                "lastPushAt": ordered[-1].get("receivedAt") if ordered else None,
            }
        return result
