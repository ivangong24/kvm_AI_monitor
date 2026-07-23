#!/usr/bin/env python3
"""On-device AI usage wallpaper renderer and local control API."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from io import BytesIO
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFont
from push_receiver import DeviceStore, PushReceiver
from ssh_collector import SshCollector, build_usage_snapshot


AGENT_VERSION = "0.8.0"  # Keep in step with package.json; surfaced on /api/status for updates.

ROOT = Path(os.environ.get("KVM_AI_USAGE_ROOT", "/etc/kvmd/user/ai-usage"))
CONFIG_PATH = Path(os.environ.get("KVM_AI_USAGE_CONFIG", ROOT / "config.json"))
DEVICES_PATH = Path(os.environ.get("KVM_AI_USAGE_DEVICES", ROOT / "devices.json"))
PUSH_STATE_PATH = Path(os.environ.get("KVM_AI_USAGE_PUSH_STATE", ROOT / "push-state.json"))
WALLPAPER_PATH = Path(
    os.environ.get("KVM_AI_USAGE_WALLPAPER", "/tmp/kvm-ai-usage-wallpaper.png")
)
ANIMATION_FRAMES_ROOT = Path(
    os.environ.get("KVM_AI_USAGE_ANIMATION_FRAMES", "/tmp")
)
PROVIDERS_PATH = Path(os.environ.get("KVM_AI_USAGE_PROVIDERS", ROOT / "providers"))
FONTS_PATH = Path(os.environ.get("KVM_AI_USAGE_FONTS", ROOT / "fonts"))
THEME_PATH = Path(os.environ.get("KVM_AI_USAGE_THEME", ROOT / "themes" / "default.json"))
INDEX_PATH = Path(os.environ.get("KVM_AI_USAGE_INDEX", ROOT / "index.html"))
ICON_PATH = Path(os.environ.get("KVM_AI_USAGE_ICON", ROOT / "icon.svg"))
COMET_IMAGE_PATH = Path(os.environ.get("KVM_AI_USAGE_COMET_IMAGE", ROOT / "comet-pro.jpg"))
SSH_KEY_PATH = Path(os.environ.get("KVM_AI_USAGE_SSH_KEY", ROOT / "device-key"))
PORT = int(os.environ.get("KVM_AI_USAGE_PORT", "8199"))
PUBLISH_ENABLED = os.environ.get("KVM_AI_USAGE_NO_PUBLISH") != "1"

# --- console authentication -------------------------------------------------------------------
# The ai-usage console can be served without the main KVM web-login gate (nginx `auth_request off`)
# so it is reachable at a direct, bookmarkable URL. To keep it protected we do our own auth here:
# a login form takes the Comet admin password (+ optional 2FA) and we VERIFY it by relaying to the
# Comet's own login API — we never store the password or reimplement its hashing. On success we set
# a signed, expiring, HttpOnly session cookie; every /api/* route (except login and the HMAC push
# endpoints) requires a valid cookie. When the console is left behind the KVM gate instead, these
# checks are simply never exercised because nginx blocks unauthenticated requests upstream.
AUTH_SECRET_PATH = Path(os.environ.get("KVM_AI_USAGE_AUTH_SECRET", ROOT / "auth-secret"))
COMET_LOGIN_URL = os.environ.get("KVM_AI_USAGE_COMET_LOGIN_URL", "https://127.0.0.1/api/auth/login")
SESSION_COOKIE = "ai_usage_session"
SESSION_TTL_SECONDS = int(os.environ.get("KVM_AI_USAGE_SESSION_TTL", str(12 * 3600)))
# When set to "0" the console runs behind the main KVM login (nginx gate) and the agent skips its
# own auth entirely — a valid escape hatch for anyone who prefers the original behavior.
AUTH_ENABLED = os.environ.get("KVM_AI_USAGE_AUTH", "1") != "0"

_auth_secret_cache: bytes | None = None


def auth_secret() -> bytes:
    """A persistent random key for signing session cookies. Persisted (0600) so a restart does not
    invalidate every open console; regenerated only if the file is missing/unreadable."""
    global _auth_secret_cache
    if _auth_secret_cache is not None:
        return _auth_secret_cache
    try:
        data = AUTH_SECRET_PATH.read_bytes()
        if len(data) >= 16:
            _auth_secret_cache = data
            return data
    except OSError:
        pass
    secret = secrets.token_bytes(32)
    try:
        AUTH_SECRET_PATH.write_bytes(secret)
        os.chmod(AUTH_SECRET_PATH, 0o600)
    except OSError:
        pass
    _auth_secret_cache = secret
    return secret


def issue_session_cookie() -> str:
    """A `<payload>.<sig>` token where payload carries the expiry; the HMAC binds it to our secret."""
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(time.time()) + SESSION_TTL_SECONDS}).encode()
    ).decode().rstrip("=")
    signature = hmac.new(auth_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def session_cookie_valid(token: str) -> bool:
    if not isinstance(token, str) or token.count(".") != 1:
        return False
    payload, signature = token.split(".", 1)
    expected = hmac.new(auth_secret(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        padded = payload + "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return False
    return isinstance(data, dict) and time.time() < (data.get("exp") or 0)


def verify_admin_credentials(password: str, totp: str = "") -> bool:
    """Validate the admin password (+ optional 2FA) by relaying to the Comet's own login API, exactly
    as the setup CLI does (multipart `user`/`passwd`, passwd = password concatenated with the 2FA
    code). A returned token means the credentials are valid. We never persist the password."""
    if not password:
        return False
    boundary = "----kvm-ai-" + secrets.token_hex(8)
    parts = []
    for name, value in (("user", "admin"), ("passwd", f"{password}{totp}")):
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n"
        )
    body = ("".join(parts) + f"--{boundary}--\r\n").encode("utf-8")
    request = urllib.request.Request(
        COMET_LOGIN_URL, data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Accept": "application/json"},
    )
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(request, timeout=15, context=context) as response:
            data = json.loads(response.read() or b"{}")
    except Exception:
        return False
    if not isinstance(data, dict) or data.get("ok") is False:
        return False
    result = data.get("result") if isinstance(data.get("result"), dict) else data
    return bool(isinstance(result, dict) and result.get("token"))


def request_session_token(handler: BaseHTTPRequestHandler) -> str:
    """The session cookie value from the request's Cookie header, or ''."""
    raw = handler.headers.get("Cookie", "")
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name == SESSION_COOKIE:
            return value
    return ""

DEFAULT_CONFIG = {
    "enabled": True,
    "animateWorking": True,
    "pauseWhenStreaming": True,
    "intervalSeconds": 60,
    "deviceUser": "",
    "deviceHost": "auto",
    "devicePort": 22,
    "activityHosts": "",
    "selectedProvider": "claude",
}
MIN_INTERVAL = 30
MAX_INTERVAL = 3600
ACTIVE_WINDOW_SECONDS = 120
ANIMATION_FRAME_COUNT = 60
ANIMATION_ROTATION_SECONDS = 2.0
# The GUI copies every published background before drawing it. With the tmpfs shield keeping
# those copies off flash (service.sh), the per-event cost is RAM copies plus a decode, which
# the event loop sustains at 10 fps without building a queue backlog.
ANIMATION_PUBLISH_FPS = 10
ANIMATION_INTERVAL_SECONDS = 1 / ANIMATION_PUBLISH_FPS
ANIMATION_RENDER_SCALE = 4
# Frames live at stable paths and are only ever atomically replaced, never deleted: the GUI
# processes update_background events asynchronously, and a queued event that references a
# deleted frame file blanks the screen to black.
ANIMATION_FRAMES_DIR = ANIMATION_FRAMES_ROOT / "kvm-ai-frames"
WORKING_POLL_SECONDS = 5.0
PUSH_BODY_LIMIT = 64 * 1024
KVM_MODEL_NAMES = {"RM10": "GL.iNet Comet Pro"}

# Built-in provider themes; kvm-agent/themes/default.json ships the same values as editable
# data and load_providers() validates and overlays it, falling back here on any problem.
BUILTIN_PROVIDERS = {
    "claude": {
        "name": "Claude Code", "brand": "CLAUDE", "subbrand": "CODE",
        "background": "#0d1112", "text": "#f4f5f2", "muted": "#939b98",
        "line": "#303637", "track": "#293031", "accent": "#d97757",
        "secondary": "#53d59c", "bar": "#d97757", "logo": "claude.png",
    },
    "codex": {
        "name": "Codex", "brand": "CODEX", "subbrand": "OPENAI",
        "background": "#f5f5f2", "text": "#101312", "muted": "#68716e",
        "line": "#cbd1ce", "track": "#dfe3e1", "accent": "#101312",
        "secondary": "#10a37f", "bar": "#10a37f", "logo": "codex.png",
    },
    "copilot": {
        "name": "GitHub Copilot", "brand": "COPILOT", "subbrand": "GITHUB",
        "background": "#f6f8fa", "text": "#24292f", "muted": "#68717c",
        "line": "#d0d7de", "track": "#d8dee4", "accent": "#8534f3",
        "secondary": "#fe4c25", "bar": "#8534f3", "logo": "copilot.png", "wideLogo": True,
    },
    "gemini": {
        "name": "Gemini CLI", "brand": "GEMINI", "subbrand": "CLI",
        "background": "#f7f9fc", "text": "#202124", "muted": "#6c727b",
        "line": "#d2d8e2", "track": "#e0e5ec", "accent": "#5684d1",
        "secondary": "#9168c0", "bar": "#4285f4", "logo": "gemini.png",
    },
    "grok": {
        "name": "Grok Build", "brand": "GROK", "subbrand": "BUILD",
        "background": "#050505", "text": "#f7f7f7", "muted": "#a0a0a0",
        "line": "#353535", "track": "#292929", "accent": "#f7f7f7",
        "secondary": "#8d99a1", "bar": "#f7f7f7", "logo": "grok.png",
    },
}
THEME_COLOR_KEYS = (
    "background", "backgroundBottom", "text", "muted", "line", "track",
    "accent", "secondary", "bar",
)
THEME_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
GLYPH_STYLES = frozenset(BUILTIN_PROVIDERS)
DEFAULT_DISPLAY = {"limitEmphasis": "percent"}


def _key_value_file(path: Path) -> dict[str, str]:
    """Read the small KEY=value files shipped by the Comet firmware."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"\'')
    return values


def read_kvm_identity(root: Path = Path("/")) -> dict[str, str]:
    """Return non-sensitive hardware/firmware facts for the dashboard device drawer."""
    version = _key_value_file(root / "etc/version")
    os_release = _key_value_file(root / "etc/os-release")
    model_code = version.get("RK_MODEL", "RM10")
    return {
        "model": KVM_MODEL_NAMES.get(model_code, model_code or "GL.iNet Comet Pro"),
        "modelCode": model_code,
        "firmwareVersion": version.get("RK_VERSION") or os_release.get("VERSION") or "Unknown",
        "platformVersion": os_release.get("PRETTY_NAME") or os_release.get("NAME") or "Buildroot",
        "hostname": socket.gethostname(),
    }

# --- widget layouts: everything below the fixed brand/status header is composed from widget
# --- placements (rects in 1x canvas units). Presets ship the arrangements; custom layouts
# --- come from the theme document and pass the same validation.
WIDGET_TYPES = frozenset((
    "tokensToday", "limitBar", "sparkline", "resetCountdown", "clock", "providerGrid", "plan",
))
LAYOUT_PRESETS: dict[str, dict[str, object]] = {
    "classic": {"divider": True, "widgets": [
        {"widget": "tokensToday", "x": 0, "y": 44, "w": 196, "h": 116},
        {"widget": "limitBar", "x": 218, "y": 10, "w": 246, "h": 66, "limit": "primary"},
        {"widget": "limitBar", "x": 218, "y": 84, "w": 246, "h": 66, "limit": "secondary"},
    ]},
    "detailed": {"divider": True, "widgets": [
        {"widget": "tokensToday", "x": 0, "y": 44, "w": 196, "h": 116},
        {"widget": "limitBar", "x": 218, "y": 10, "w": 246, "h": 66, "limit": "primary"},
        {"widget": "sparkline", "x": 218, "y": 84, "w": 118, "h": 66},
        {"widget": "resetCountdown", "x": 346, "y": 84, "w": 118, "h": 66, "limit": "secondary"},
    ]},
    "compact": {"divider": True, "widgets": [
        {"widget": "tokensToday", "x": 0, "y": 44, "w": 196, "h": 116},
        {"widget": "limitBar", "x": 218, "y": 40, "w": 246, "h": 66, "limit": "secondary"},
        {"widget": "clock", "x": 218, "y": 114, "w": 246, "h": 40},
    ]},
    "multiAgent": {"divider": False, "widgets": [
        {"widget": "tokensToday", "x": 0, "y": 44, "w": 196, "h": 116},
        {"widget": "providerGrid", "x": 212, "y": 6, "w": 262, "h": 148},
    ]},
}
DEFAULT_LAYOUT: dict[str, object] = {"preset": "classic"}


def _sanitize_layout(raw: object) -> dict[str, object] | None:
    if not isinstance(raw, dict):
        return None
    preset = raw.get("preset")
    if isinstance(preset, str) and preset in LAYOUT_PRESETS and "widgets" not in raw:
        return {"preset": preset}
    widgets_raw = raw.get("widgets")
    if not isinstance(widgets_raw, list):
        return None
    widgets: list[dict[str, object]] = []
    for item in widgets_raw[:10]:
        if not isinstance(item, dict) or item.get("widget") not in WIDGET_TYPES:
            continue
        try:
            x, y = int(item.get("x")), int(item.get("y"))
            w, h = int(item.get("w")), int(item.get("h"))
        except (TypeError, ValueError):
            continue
        if not (0 <= x < 480 and 0 <= y < 160 and 24 <= w <= 480 and 20 <= h <= 160):
            continue
        if x + w > 480 or y + h > 160:
            continue
        entry: dict[str, object] = {"widget": item["widget"], "x": x, "y": y, "w": w, "h": h}
        if item.get("limit") in ("primary", "secondary"):
            entry["limit"] = item["limit"]
        widgets.append(entry)
    if not widgets:
        return None
    clean: dict[str, object] = {"widgets": widgets}
    if isinstance(raw.get("divider"), bool):
        clean["divider"] = raw["divider"]
    return clean


def resolve_layout(layout: dict[str, object] | None) -> dict[str, object]:
    """A concrete {divider, widgets} arrangement from a sanitized layout document."""
    if layout and isinstance(layout.get("preset"), str) and layout["preset"] in LAYOUT_PRESETS:
        preset = LAYOUT_PRESETS[str(layout["preset"])]
        return {"divider": preset.get("divider", True),
                "widgets": [dict(widget) for widget in preset["widgets"]]}
    if layout and isinstance(layout.get("widgets"), list):
        return {"divider": layout.get("divider", True),
                "widgets": [dict(widget) for widget in layout["widgets"]]}
    return resolve_layout(dict(DEFAULT_LAYOUT))


def sanitize_theme(raw: object) -> dict[str, object] | None:
    """Reduce arbitrary input to a safe theme document, or None if it isn't one at all.
    Themes are data, never code: only known providers, known color keys with strict hex
    values, a known glyph style, and known display options survive."""
    if not isinstance(raw, dict) or raw.get("schemaVersion") != 1:
        return None
    clean: dict[str, object] = {"schemaVersion": 1, "providers": {}}
    providers = raw.get("providers")
    for provider_id, overrides in (providers.items() if isinstance(providers, dict) else ()):
        if provider_id not in BUILTIN_PROVIDERS or not isinstance(overrides, dict):
            continue
        entry: dict[str, object] = {}
        for key, value in overrides.items():
            if key in THEME_COLOR_KEYS and isinstance(value, str) and THEME_COLOR_RE.match(value):
                entry[key] = value
            elif key == "glyph" and value in GLYPH_STYLES:
                entry[key] = value
        if entry:
            clean["providers"][provider_id] = entry
    display = raw.get("display")
    if isinstance(display, dict) and display.get("limitEmphasis") in ("percent", "time"):
        clean["display"] = {"limitEmphasis": display["limitEmphasis"]}
    layout = _sanitize_layout(raw.get("layout"))
    if layout is not None:
        clean["layout"] = layout
    return clean


def read_theme_file() -> dict[str, object] | None:
    try:
        raw = json.loads(THEME_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return sanitize_theme(raw)


def load_providers(theme: dict[str, object] | None = None,
                   ) -> tuple[dict[str, dict[str, object]], dict[str, object], dict[str, object]]:
    """(provider themes, display options, layout): built-ins overlaid with a sanitized theme
    document (the saved file when none is given). Any problem keeps the built-in look."""
    themes = {provider_id: dict(entry) for provider_id, entry in BUILTIN_PROVIDERS.items()}
    display = dict(DEFAULT_DISPLAY)
    layout = dict(DEFAULT_LAYOUT)
    clean = theme if theme is not None else read_theme_file()
    if not clean:
        return themes, display, layout
    for provider_id, overrides in clean.get("providers", {}).items():
        themes[provider_id].update(overrides)
    display.update(clean.get("display", {}))
    if clean.get("layout"):
        layout = dict(clean["layout"])
    return themes, display, layout


PROVIDERS: dict[str, dict[str, object]] = {}
DISPLAY: dict[str, object] = {}
LAYOUT: dict[str, object] = {}


def apply_theme(theme: dict[str, object] | None = None) -> None:
    """Swap the active theme in place so every reference (renderer, frames) sees it."""
    providers, display, layout = load_providers(theme)
    PROVIDERS.clear()
    PROVIDERS.update(providers)
    DISPLAY.clear()
    DISPLAY.update(display)
    LAYOUT.clear()
    LAYOUT.update(layout)


apply_theme()
PROVIDER_IDS = frozenset(BUILTIN_PROVIDERS)

FONT_REGULAR = (
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/ttf-dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/DejaVuSans.ttf",
)
FONT_BOLD = (
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/ttf-dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/DejaVuSans-Bold.ttf",
)
# Bundled Inter (OFL) with firmware DejaVu as fallback.
FONT_UI_BOLD = (str(FONTS_PATH / "Inter-Bold.ttf"),) + FONT_BOLD
FONT_UI_SEMIBOLD = (str(FONTS_PATH / "Inter-SemiBold.ttf"), str(FONTS_PATH / "Inter-Bold.ttf")) + FONT_BOLD
FONT_UI_MEDIUM = (str(FONTS_PATH / "Inter-Medium.ttf"),) + FONT_REGULAR
FONT_UI_DISPLAY = (str(FONTS_PATH / "InterDisplay-Bold.ttf"), str(FONTS_PATH / "Inter-Bold.ttf")) + FONT_BOLD


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_font(candidates: tuple[str, ...], size: int) -> ImageFont.ImageFont:
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def compact_number(value: object) -> str:
    try:
        number = max(0.0, float(value or 0))
    except (TypeError, ValueError):
        number = 0
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if number >= divisor:
            result = number / divisor
            return f"{result:.1f}".rstrip("0").rstrip(".") + suffix
    return str(int(number))


def reset_label(value: object, now: datetime | None = None) -> str:
    reset = parse_timestamp(value)
    if reset is None:
        return "WAITING FOR DATA"
    current = now or datetime.now(timezone.utc)
    minutes = max(0, round((reset - current).total_seconds() / 60))
    if minutes >= 2880:
        return f"RESETS IN {round(minutes / 1440)}D"
    if minutes >= 120:
        return f"RESETS IN {round(minutes / 60)}H"
    return f"RESETS IN {minutes}M"


def window_label(limit: dict[str, object] | None, fallback: str) -> str:
    try:
        minutes = int(limit.get("windowMinutes")) if limit else 0
    except (TypeError, ValueError):
        minutes = 0
    if minutes and minutes % 10080 == 0:
        return f"{minutes // 10080 * 7}-DAY WINDOW"
    if minutes and minutes % 1440 == 0:
        return f"{minutes // 1440}-DAY WINDOW"
    if minutes and minutes % 60 == 0:
        return f"{minutes // 60}-HOUR WINDOW"
    return fallback


def normalize_config(value: object) -> dict[str, object]:
    raw = value if isinstance(value, dict) else {}
    enabled = raw.get("enabled", DEFAULT_CONFIG["enabled"])
    animate_working = raw.get("animateWorking", DEFAULT_CONFIG["animateWorking"])
    pause_when_streaming = raw.get("pauseWhenStreaming", DEFAULT_CONFIG["pauseWhenStreaming"])
    interval = raw.get("intervalSeconds", DEFAULT_CONFIG["intervalSeconds"])
    device_user = raw.get("deviceUser", DEFAULT_CONFIG["deviceUser"])
    device_host = raw.get("deviceHost", DEFAULT_CONFIG["deviceHost"])
    device_port = raw.get("devicePort", DEFAULT_CONFIG["devicePort"])
    activity_hosts = raw.get("activityHosts", DEFAULT_CONFIG["activityHosts"])
    selected_provider = raw.get("selectedProvider", DEFAULT_CONFIG["selectedProvider"])
    try:
        interval = int(interval)
    except (TypeError, ValueError):
        interval = DEFAULT_CONFIG["intervalSeconds"]
    if not isinstance(enabled, bool):
        enabled = DEFAULT_CONFIG["enabled"]
    if not isinstance(animate_working, bool):
        animate_working = DEFAULT_CONFIG["animateWorking"]
    if not isinstance(pause_when_streaming, bool):
        pause_when_streaming = DEFAULT_CONFIG["pauseWhenStreaming"]
    try:
        device_port = int(device_port)
    except (TypeError, ValueError):
        device_port = DEFAULT_CONFIG["devicePort"]
    if not isinstance(device_user, str) or not re.fullmatch(r"[A-Za-z0-9._-]{0,64}", device_user):
        device_user = DEFAULT_CONFIG["deviceUser"]
    if not isinstance(device_host, str) or not re.fullmatch(r"(?:auto|[A-Za-z0-9._:-]{1,253})", device_host):
        device_host = DEFAULT_CONFIG["deviceHost"]
    if not 1 <= device_port <= 65535:
        device_port = DEFAULT_CONFIG["devicePort"]
    if not isinstance(activity_hosts, str):
        activity_hosts = DEFAULT_CONFIG["activityHosts"]
    normalized_hosts = []
    for host in activity_hosts.split(","):
        host = host.strip()
        if (host and host != "auto" and len(host) <= 253
                and re.fullmatch(r"(?:[A-Za-z0-9._-]{1,64}@)?[A-Za-z0-9._:-]{1,253}", host)
                and host not in normalized_hosts):
            normalized_hosts.append(host)
        if len(normalized_hosts) >= 8:
            break
    if selected_provider not in PROVIDER_IDS:
        selected_provider = DEFAULT_CONFIG["selectedProvider"]
    return {
        "enabled": enabled,
        "animateWorking": animate_working,
        "pauseWhenStreaming": pause_when_streaming,
        "intervalSeconds": min(MAX_INTERVAL, max(MIN_INTERVAL, interval)),
        "deviceUser": device_user,
        "deviceHost": device_host,
        "devicePort": device_port,
        "activityHosts": ", ".join(normalized_hosts),
        "selectedProvider": selected_provider,
    }


def read_config() -> dict[str, object]:
    try:
        return normalize_config(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return dict(DEFAULT_CONFIG)


def write_config(config: dict[str, object]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="config.", suffix=".tmp", dir=CONFIG_PATH.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(normalize_config(config), stream, indent=2)
            stream.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, CONFIG_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def find_provider(snapshot: dict[str, object], provider_id: str) -> dict[str, object]:
    providers = snapshot.get("providers", [])
    for provider in providers if isinstance(providers, list) else []:
        if isinstance(provider, dict) and provider.get("id") == provider_id:
            return provider
    raise RuntimeError(f"usage response has no {PROVIDERS[provider_id]['name']} provider")


Color = "str | tuple[int, int, int]"
SS = 2  # The base wallpaper is composed at 2x and downsampled once for crisp text and curves.
GLYPH_CENTER = (146, 26)


def draw_text(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, font: ImageFont.ImageFont,
              fill: object, anchor: str = "la") -> None:
    draw.text(xy, text, font=font, fill=fill, anchor=anchor, spacing=0)


def _rgb(color: object) -> tuple[int, int, int]:
    return ImageColor.getrgb(color) if isinstance(color, str) else tuple(color)


def blend_color(foreground: object, background: object, strength: float) -> tuple[int, int, int]:
    front = _rgb(foreground)
    back = _rgb(background)
    amount = max(0.0, min(1.0, strength))
    return tuple(round(back[index] + (front[index] - back[index]) * amount) for index in range(3))


def vertical_gradient(size: tuple[int, int], top: object, bottom: object) -> Image.Image:
    image = Image.new("RGB", size, _rgb(top))
    draw = ImageDraw.Draw(image)
    top_rgb, bottom_rgb = _rgb(top), _rgb(bottom)
    for y in range(size[1]):
        amount = y / max(1, size[1] - 1)
        color = tuple(round(top_rgb[i] + (bottom_rgb[i] - top_rgb[i]) * amount) for i in range(3))
        draw.line((0, y, size[0], y), fill=color)
    return image


def fit_text(draw: ImageDraw.ImageDraw, text: str, candidates: tuple[str, ...],
             max_size: int, min_size: int, max_width: int) -> ImageFont.ImageFont:
    for size in range(max_size, min_size - 1, -2):
        font = load_font(candidates, size)
        if draw.textlength(text, font=font) <= max_width:
            return font
    return load_font(candidates, min_size)


def short_reset(value: object) -> str:
    label = reset_label(value)
    return label.removeprefix("RESETS IN ") if label.startswith("RESETS IN ") else "--"


def pick_limit(ctx: dict[str, object], which: str) -> tuple[dict[str, object] | None, str, str]:
    """(limit, title, window text) for the primary (session) or secondary (weekly) slot."""
    by_label, limits = ctx["by_label"], ctx["limits"]
    primary = by_label.get("Current session") or by_label.get("5-hour window")
    secondary = by_label.get("Weekly limit") or by_label.get("Weekly window")
    if primary is None and secondary is None:
        primary = limits[0] if limits else None
        secondary = limits[1] if len(limits) > 1 else None
    if which == "secondary":
        title = str(secondary.get("label") if secondary else "WEEKLY LIMIT").upper()
        return secondary, title, window_label(secondary, "7-DAY WINDOW")
    title = str(primary.get("label") if primary else "CURRENT SESSION").upper()
    return primary, title, window_label(primary, "5-HOUR WINDOW")


def limit_percent(limit: dict[str, object] | None) -> tuple[float, bool]:
    try:
        return min(100.0, max(0.0, float(limit.get("usedPercent")))), True
    except (TypeError, ValueError, AttributeError):
        return 0.0, False


def rounded_bar(draw: ImageDraw.ImageDraw, x0: int, y0: int, x1: int, height: int,
                percent: float, has_percent: bool, track: object, fill: object) -> None:
    radius = height // 2
    draw.rounded_rectangle((x0, y0, x1, y0 + height), radius=radius, fill=track)
    if has_percent and percent > 0:
        width = max(height, round((x1 - x0) * percent / 100))
        draw.rounded_rectangle((x0, y0, x0 + width, y0 + height), radius=radius, fill=fill)


# --- widgets: each renders inside rect (x, y, w, h in 1x units; multiply by s to draw) ---

def widget_limit_bar(draw, image, rect, ctx, theme, s, options):
    which = str(options.get("limit", "primary"))
    limit, title, window = pick_limit(ctx, which)
    bar_color = str(theme.get("bar", theme["accent"]))
    if which == "secondary":
        bar_color = blend_color(bar_color, str(theme["background"]), 0.62)
    percent, has_percent = limit_percent(limit)
    resets_at = limit.get("resetsAt") if limit else None
    if ctx["emphasis"] == "time":
        hero_value = short_reset(resets_at)
        footer_right = f"{round(percent)}% USED" if has_percent else "WAITING FOR DATA"
    else:
        hero_value = f"{round(percent)}%" if has_percent else "--"
        footer_right = reset_label(resets_at)
    x, y, w = rect["x"], rect["y"], rect["w"]
    x1 = x + w
    draw_text(draw, (x * s, (y + 6) * s), title, load_font(FONT_UI_SEMIBOLD, 11 * s),
              str(theme["text"]), "la")
    draw_text(draw, (x1 * s, (y + 1) * s), hero_value, load_font(FONT_UI_DISPLAY, 27 * s),
              str(theme["text"]), "ra")
    rounded_bar(draw, x * s, (y + 33) * s, x1 * s, 12 * s, percent, has_percent,
                str(theme["track"]), bar_color)
    footer_font = load_font(FONT_UI_MEDIUM, 9 * s)
    draw_text(draw, (x * s, (y + 51) * s), window, footer_font, str(theme["muted"]), "la")
    draw_text(draw, (x1 * s, (y + 51) * s), footer_right, footer_font, str(theme["muted"]), "ra")


def tokens_panel_content(ctx) -> tuple[str, str, str, str]:
    provider, activity, today, limits = ctx["provider"], ctx["activity"], ctx["today"], ctx["limits"]
    provider_id = str(provider.get("id"))
    if provider.get("trackedTokenTotalsAvailable") is True or not (
            provider_id not in ("claude", "codex") and (
                limits or provider.get("creditsRemaining") is not None
                or provider.get("providerCostUSD") is not None)
            or provider.get("accountTokenTotalsAvailable") is False):
        bottom_value = compact_number(activity.get("last30DaysTokens"))
        cost_value = activity.get("last30DaysCostUSD")
        try:
            if cost_value is not None:
                bottom_value = f"{bottom_value} · ${float(cost_value):.0f}"
        except (TypeError, ValueError):
            pass
        return "TODAY TOKENS", compact_number(today.get("tokens")), "LAST 30 DAYS", bottom_value
    if provider.get("accountTokenTotalsAvailable") is False:
        return ("NATIVE ACCOUNT", str(provider.get("plan") or "CONNECTED").upper()[:16],
                "TOKEN HISTORY", "UNAVAILABLE")
    hero = str(provider.get("plan") or "CONNECTED").upper()[:18]
    credits = provider.get("creditsRemaining")
    provider_cost = provider.get("providerCostUSD")
    if credits is not None:
        return "SUBSCRIPTION", hero, "CREDITS LEFT", compact_number(credits)
    if provider_cost is not None:
        return "SUBSCRIPTION", hero, "PROVIDER COST", f"${float(provider_cost):.2f}"
    return "SUBSCRIPTION", hero, "ACCOUNT LIMITS", "CONNECTED"


def widget_tokens_today(draw, image, rect, ctx, theme, s, options):
    top_label, hero, bottom_label, bottom_value = tokens_panel_content(ctx)
    x, y, w, h = rect["x"], rect["y"], rect["w"], rect["h"]
    draw_text(draw, ((x + 16) * s, (y + 12) * s), top_label,
              load_font(FONT_UI_SEMIBOLD, 10 * s), str(theme["muted"]), "la")
    hero_font = fit_text(draw, hero, FONT_UI_DISPLAY, 42 * s, 20 * s, (w - 20) * s)
    draw_text(draw, ((x + 15) * s, (y + 26) * s), hero, hero_font, str(theme["text"]), "la")
    draw.line(((x + 16) * s, (y + h - 36) * s, (x + w - 8) * s, (y + h - 36) * s),
              fill=str(theme["line"]), width=s)
    draw_text(draw, ((x + 16) * s, (y + h - 26) * s), bottom_label,
              load_font(FONT_UI_SEMIBOLD, 9 * s), str(theme["muted"]), "la")
    bottom_font = fit_text(draw, bottom_value, FONT_UI_BOLD, 15 * s, 10 * s, (w - 88) * s)
    draw_text(draw, ((x + w - 8) * s, (y + h - 20) * s), bottom_value, bottom_font,
              str(theme.get("bar", theme["accent"])), "rm")


def widget_plan(draw, image, rect, ctx, theme, s, options):
    provider = ctx["provider"]
    x, y = rect["x"], rect["y"]
    draw_text(draw, ((x + 16) * s, (y + 12) * s), "SUBSCRIPTION",
              load_font(FONT_UI_SEMIBOLD, 10 * s), str(theme["muted"]), "la")
    hero = str(provider.get("plan") or "CONNECTED").upper()[:18]
    hero_font = fit_text(draw, hero, FONT_UI_DISPLAY, 34 * s, 18 * s, (rect["w"] - 20) * s)
    draw_text(draw, ((x + 15) * s, (y + 28) * s), hero, hero_font, str(theme["text"]), "la")


def widget_sparkline(draw, image, rect, ctx, theme, s, options):
    activity = ctx["activity"]
    days = [day for day in (activity.get("last7Days") or []) if isinstance(day, dict)]
    values = [max(0.0, float(day.get("totalTokens") or 0)) for day in days]
    x, y, w, h = rect["x"], rect["y"], rect["w"], rect["h"]
    draw_text(draw, (x * s, (y + 6) * s), "7-DAY TOKENS",
              load_font(FONT_UI_SEMIBOLD, 10 * s), str(theme["text"]), "la")
    if len(values) < 2 or not max(values):
        draw_text(draw, (x * s, (y + 30) * s), "WAITING FOR DATA",
                  load_font(FONT_UI_MEDIUM, 9 * s), str(theme["muted"]), "la")
        return
    draw_text(draw, ((x + w) * s, (y + 7) * s), compact_number(max(values)),
              load_font(FONT_UI_MEDIUM, 9 * s), str(theme["muted"]), "ra")
    top_value = max(values)
    chart_top, chart_bottom = (y + 24) * s, (y + h - 8) * s
    points = []
    for index, value in enumerate(values):
        px = x * s + round(index * (w * s) / (len(values) - 1))
        py = chart_bottom - round((value / top_value) * (chart_bottom - chart_top))
        points.append((px, py))
    bar_color = str(theme.get("bar", theme["accent"]))
    area = points + [(points[-1][0], chart_bottom), (points[0][0], chart_bottom)]
    draw.polygon(area, fill=blend_color(bar_color, str(theme["background"]), 0.28))
    draw.line(points, fill=bar_color, width=2 * s, joint="curve")


def widget_reset_countdown(draw, image, rect, ctx, theme, s, options):
    which = str(options.get("limit", "secondary"))
    limit, title, _ = pick_limit(ctx, which)
    percent, has_percent = limit_percent(limit)
    x, y, w = rect["x"], rect["y"], rect["w"]
    draw_text(draw, (x * s, (y + 6) * s), title, load_font(FONT_UI_SEMIBOLD, 10 * s),
              str(theme["text"]), "la")
    hero = short_reset(limit.get("resetsAt") if limit else None)
    draw_text(draw, (x * s, (y + 20) * s), hero, load_font(FONT_UI_DISPLAY, 26 * s),
              str(theme.get("bar", theme["accent"])), "la")
    footer = f"{round(percent)}% USED" if has_percent else "WAITING FOR DATA"
    draw_text(draw, (x * s, (y + 51) * s), footer, load_font(FONT_UI_MEDIUM, 9 * s),
              str(theme["muted"]), "la")


def widget_clock(draw, image, rect, ctx, theme, s, options):
    now = datetime.now().astimezone()
    x, y, w, h = rect["x"], rect["y"], rect["w"], rect["h"]
    draw_text(draw, (x * s, (y + h // 2) * s), now.strftime("%H:%M"),
              load_font(FONT_UI_DISPLAY, min(30, h - 8) * s), str(theme["text"]), "lm")
    draw_text(draw, ((x + w) * s, (y + h // 2) * s), now.strftime("%a %b %d").upper(),
              load_font(FONT_UI_MEDIUM, 10 * s), str(theme["muted"]), "rm")


def widget_provider_grid(draw, image, rect, ctx, theme, s, options):
    snapshot = ctx["snapshot"]
    providers = [
        provider for provider in snapshot.get("providers", [])
        if isinstance(provider, dict) and provider.get("id") in PROVIDER_IDS
    ][:5]
    if not providers:
        return
    x, y, w, h = rect["x"], rect["y"], rect["w"], rect["h"]
    row_height = h // len(providers)
    for index, provider in enumerate(providers):
        provider_id = str(provider["id"])
        row_theme = PROVIDERS.get(provider_id, theme)
        row_y = y + index * row_height + row_height // 2
        try:
            logo = Image.open(PROVIDERS_PATH / str(row_theme["logo"])).convert("RGBA")
            logo.thumbnail((14 * s, 14 * s), Image.Resampling.LANCZOS)
            image.paste(logo, ((x + 2) * s, row_y * s - logo.height // 2), logo)
        except OSError:
            pass
        draw_text(draw, ((x + 22) * s, row_y * s), str(row_theme["brand"]),
                  load_font(FONT_UI_SEMIBOLD, 10 * s), str(theme["text"]), "lm")
        row_ctx = {
            "by_label": {item.get("label"): item for item in provider.get("limits", [])
                         if isinstance(item, dict) and item.get("label")},
            "limits": provider.get("limits") if isinstance(provider.get("limits"), list) else [],
        }
        limit, _, _ = pick_limit(row_ctx, "secondary")
        if limit is None:
            limit, _, _ = pick_limit(row_ctx, "primary")
        percent, has_percent = limit_percent(limit)
        bar_x0, bar_x1 = (x + w - 118) * s, (x + w - 34) * s
        rounded_bar(draw, bar_x0, row_y * s - 3 * s, bar_x1, 6 * s, percent, has_percent,
                    str(theme["track"]), str(row_theme.get("bar", row_theme["accent"])))
        value = f"{round(percent)}%" if has_percent else "--"
        draw_text(draw, ((x + w) * s, row_y * s), value,
                  load_font(FONT_UI_BOLD, 11 * s), str(theme["text"]), "rm")
        if provider.get("working") is True:
            draw.ellipse(((x + 12) * s, (row_y - 7) * s, (x + 16) * s, (row_y - 3) * s),
                         fill=str(row_theme["secondary"]))


WIDGET_RENDERERS = {
    "limitBar": widget_limit_bar,
    "tokensToday": widget_tokens_today,
    "plan": widget_plan,
    "sparkline": widget_sparkline,
    "resetCountdown": widget_reset_countdown,
    "clock": widget_clock,
    "providerGrid": widget_provider_grid,
}


def paste_provider_logo(image: Image.Image, provider_id: str, theme: dict[str, object],
                        s: int = 1) -> None:
    try:
        logo = Image.open(PROVIDERS_PATH / str(theme["logo"])).convert("RGBA")
        if theme.get("wideLogo"):
            logo.thumbnail((112 * s, 30 * s), Image.Resampling.LANCZOS)
            position = (15 * s, 12 * s + (30 * s - logo.height) // 2)
        else:
            logo.thumbnail((26 * s, 26 * s), Image.Resampling.LANCZOS)
            position = (16 * s + (26 * s - logo.width) // 2, 13 * s + (26 * s - logo.height) // 2)
        image.paste(logo, position, logo)
    except OSError:
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((16 * s, 13 * s, 42 * s, 39 * s), radius=6 * s,
                               fill=str(theme["accent"]))


def draw_activity_glyph(image: Image.Image, provider_id: str, center: tuple[int, int],
                        frame: int, color: str, background: object, working: bool) -> None:
    size = 20
    scale = ANIMATION_RENDER_SCALE
    glyph = Image.new("RGB", (size * scale, size * scale), background)
    draw = ImageDraw.Draw(glyph)
    x = y = size * scale // 2

    def point(dx: float, dy: float) -> tuple[int, int]:
        return x + round(dx * scale), y + round(dy * scale)

    def box(left: float, top: float, right: float, bottom: float) -> tuple[int, int, int, int]:
        return (*point(left, top), *point(right, bottom))

    if not working:
        draw.ellipse(box(-4, -4, 4, 4),
                     outline=blend_color(color, background, 0.45), width=1)
        draw.ellipse(box(-1, -1, 1, 1), fill=color)
    else:
        phase = frame % ANIMATION_FRAME_COUNT
        angle = math.tau * phase / ANIMATION_FRAME_COUNT
        if provider_id == "claude":
            for index in range(5):
                wave = (math.sin(angle - index * 0.8) + 1) / 2
                height = 3 + wave * 7
                bar_color = blend_color(color, background, 0.45 + wave * 0.55)
                left = -6 + index * 3
                draw.rounded_rectangle(box(left, 5 - height, left + 1.25, 5),
                                       radius=scale, fill=bar_color)
        elif provider_id == "codex":
            for index in range(12):
                point_angle = angle - index * math.tau / 12 - math.pi / 2
                strength = max(0.16, 1 - index * 0.075)
                px = math.cos(point_angle) * 6
                py = math.sin(point_angle) * 6
                radius = 1.65 if index < 2 else 0.85
                draw.ellipse(box(px - radius, py - radius, px + radius, py + radius),
                             fill=blend_color(color, background, strength))
        elif provider_id == "copilot":
            outline = blend_color(color, background, 0.62)
            draw.rounded_rectangle(box(-7, -4, -1, 3), radius=3 * scale,
                                   outline=outline, width=scale)
            draw.rounded_rectangle(box(1, -4, 7, 3), radius=3 * scale,
                                   outline=outline, width=scale)
            gaze_x = math.sin(angle) * 2
            gaze_y = math.sin(angle * 2)
            draw.ellipse(box(-5 + gaze_x, -1 + gaze_y, -3 + gaze_x, 1 + gaze_y),
                         fill=color)
            draw.ellipse(box(3 + gaze_x, -1 + gaze_y, 5 + gaze_x, 1 + gaze_y),
                         fill=color)
        elif provider_id == "gemini":
            pulse = (math.sin(angle) + 1) / 2
            vertical = 5 + pulse * 2
            horizontal = 3 + (1 - pulse) * 2
            draw.polygon((point(0, -vertical), point(1, -1), point(horizontal, 0),
                          point(1, 1), point(0, vertical), point(-1, 1),
                          point(-horizontal, 0), point(-1, -1)), fill=color)
            orbit_x = math.cos(angle) * 7
            orbit_y = math.sin(angle) * 4
            draw.ellipse(box(orbit_x - 1, orbit_y - 1, orbit_x + 1, orbit_y + 1),
                         fill=blend_color(color, background, 0.7))
        else:
            draw.line((point(-6, 5), point(6, -5)),
                      fill=blend_color(color, background, 0.5), width=scale)
            sweep = (math.sin(angle - math.pi / 2) + 1) / 2
            px = -6 + sweep * 12
            py = 5 - sweep * 10
            draw.ellipse(box(px - 2, py - 2, px + 2, py + 2), fill=color)

    glyph = glyph.resize((size, size), Image.Resampling.LANCZOS)
    image.paste(glyph, (center[0] - size // 2, center[1] - size // 2))


def summarize_usage(provider: dict[str, object]) -> dict[str, object]:
    """Compact numeric usage for the web console's live on-screen reproduction. The touchscreen
    itself is rendered server-side to a PNG; the browser rebuilds the same content as crisp vectors
    from this block instead of upscaling that image."""
    now = datetime.now(timezone.utc)
    limits_raw = provider.get("limits") if isinstance(provider.get("limits"), list) else []
    limits: list[dict[str, object]] = []
    for item in limits_raw:
        if not isinstance(item, dict):
            continue
        percent, has_percent = limit_percent(item)
        window = window_label(item, "")
        limits.append({
            "label": item.get("label"),
            "usedPercent": round(percent) if has_percent else None,
            "windowLabel": window or None,
            "resetsAt": item.get("resetsAt"),
            "resetLabel": short_reset(item.get("resetsAt")) if item.get("resetsAt") else None,
        })
    activity = provider.get("activity") if isinstance(provider.get("activity"), dict) else {}
    today = activity.get("today") if isinstance(activity.get("today"), dict) else {}
    # Compact daily series (date + per-type token counts) for the console's usage chart. Kept to the
    # last 30 entries and only the fields the chart needs so the status payload stays small.
    daily_source = activity.get("last30Days") if isinstance(activity.get("last30Days"), list) else []
    daily = [
        {
            "date": day.get("date"),
            "totalTokens": day.get("totalTokens", 0),
            "inputTokens": day.get("inputTokens", 0),
            "outputTokens": day.get("outputTokens", 0),
            "cacheReadTokens": day.get("cacheReadTokens", 0),
            "cacheCreationTokens": day.get("cacheCreationTokens", 0),
        }
        for day in daily_source[-30:]
        if isinstance(day, dict) and day.get("date")
    ]
    return {
        "trackedTokenTotalsAvailable": provider.get("trackedTokenTotalsAvailable") is True,
        "creditsRemaining": provider.get("creditsRemaining"),
        "limits": limits,
        "todayTokens": today.get("tokens"),
        "todayInputTokens": today.get("inputTokens"),
        "todayOutputTokens": today.get("outputTokens"),
        "todayCacheReadTokens": today.get("cacheReadTokens"),
        "todayCacheWriteTokens": today.get("cacheWriteTokens"),
        "last30DaysTokens": activity.get("last30DaysTokens"),
        "daily": daily,
        "lastUsedAt": activity.get("lastUsedAt"),
        "generatedAt": now.isoformat(),
    }


def summarize_provider(provider: dict[str, object]) -> dict[str, object]:
    installation = provider.get("installation") if isinstance(provider.get("installation"), dict) else {}
    authentication = provider.get("authentication") if isinstance(provider.get("authentication"), dict) else {}
    return {
        "id": provider.get("id"),
        "name": provider.get("name"),
        "plan": provider.get("plan"),
        "connectionState": provider.get("connectionState", "not_installed"),
        "usageAvailable": bool(provider.get("usageAvailable")),
        "working": provider.get("working") is True,
        "workingSource": provider.get("workingSource"),
        "deviceWorking": provider.get("deviceWorking") is True,
        "authorizedDeviceWorking": provider.get("authorizedDeviceWorking") is True,
        "activityState": provider.get("activityState", "standby"),
        "capabilityNote": provider.get("capabilityNote"),
        "installation": installation,
        "authentication": authentication,
        "usage": summarize_usage(provider),
    }


def setup_copy(provider: dict[str, object]) -> tuple[str, str, str]:
    state = str(provider.get("connectionState", "not_installed"))
    installation = provider.get("installation") if isinstance(provider.get("installation"), dict) else {}
    authentication = provider.get("authentication") if isinstance(provider.get("authentication"), dict) else {}
    if state == "not_installed":
        return "INSTALL REQUIRED", "CONNECTED DEVICE", str(installation.get("installCommand", "Open AI Usage for setup"))
    if state == "login_required":
        return "SIGN IN REQUIRED", "RUN ON CONNECTED DEVICE", str(authentication.get("loginCommand", "Open AI Usage for setup"))
    if state == "verification_required":
        return "VERIFY SIGN-IN", "CREDENTIALS PROTECTED", "Open Claude Code locally and run /status"
    if state == "usage_unavailable":
        return "CONNECTED", "USAGE FEED UNAVAILABLE", "Installation and sign-in detected"
    return "WAITING FOR USAGE", "CONNECTED DEVICE", "Start a new agent session"


def render_setup(draw: ImageDraw.ImageDraw, provider: dict[str, object], theme: dict[str, object],
                 s: int) -> None:
    headline, label, detail = setup_copy(provider)
    headline_font = load_font(FONT_UI_DISPLAY, 23 * s)
    label_font = load_font(FONT_UI_SEMIBOLD, 10 * s)
    detail_font = load_font(FONT_UI_MEDIUM, 11 * s)
    draw_text(draw, (16 * s, 60 * s), headline, headline_font, str(theme["text"]), "la")
    draw.line((16 * s, 101 * s, 188 * s, 101 * s), fill=str(theme["line"]), width=s)
    draw_text(draw, (16 * s, 114 * s), str(provider.get("plan") or "NO PLAN DATA").upper(),
              label_font, str(theme["muted"]), "la")
    draw_text(draw, (218 * s, 32 * s), label, label_font, str(theme["muted"]), "la")
    words = detail.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > 31 and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    for index, line in enumerate(lines[:3]):
        draw_text(draw, (218 * s, (53 + index * 19) * s), line, detail_font, str(theme["text"]), "la")
    bar_y = 120 * s
    draw.rounded_rectangle((218 * s, bar_y, 464 * s, bar_y + 6 * s), radius=3 * s,
                           fill=str(theme["track"]))
    draw.rounded_rectangle((218 * s, bar_y, 280 * s, bar_y + 6 * s), radius=3 * s,
                           fill=str(theme.get("bar", theme["accent"])))
    draw_text(draw, (218 * s, 136 * s), "MANAGE AT /EXTRAS/AI-USAGE/", label_font,
              str(theme["muted"]), "la")


def usage_panel_visible(provider: dict[str, object], limits: list[object]) -> bool:
    connection_state = provider.get("connectionState")
    if connection_state == "ready":
        return True
    if connection_state is None:
        activity = provider.get("activity") if isinstance(provider.get("activity"), dict) else {}
        return bool(provider.get("usageAvailable") or limits or activity.get("lastUsedAt"))
    if connection_state == "verification_required":
        return bool(provider.get("trackedTokenTotalsAvailable") or limits)
    return False


def save_png_atomic(image: Image.Image, output_path: Path, compress_level: int | None = None) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".png.", suffix=".png", dir=output_path.parent)
    os.close(descriptor)
    try:
        if compress_level is None:
            image.save(temporary, format="PNG", optimize=True)
        else:
            image.save(temporary, format="PNG", compress_level=compress_level)
        os.replace(temporary, output_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


# Filesystem types that are not real persistent storage (RAM, pseudo, or the read-only firmware
# image) and so must not be reported as the device's disk.
NON_STORAGE_FSTYPES = frozenset({
    "tmpfs", "devtmpfs", "ramfs", "overlay", "squashfs", "proc", "sysfs", "cgroup", "cgroup2",
    "devpts", "mqueue", "debugfs", "tracefs", "securityfs", "pstore", "autofs", "configfs",
    "fuse.gvfsd-fuse", "nsfs", "bpf", "efivarfs",
})


def primary_storage_stats() -> dict[str, object]:
    """The device's real storage, not the tiny writable overlay that backs `/`. On the Comet Pro `/`
    is `overlay:/overlay` (~1 GB) while the actual eMMC storage is a separate, much larger partition
    (e.g. /userdata/media ≈ 27 GB). Pick the largest real, writable, block-backed filesystem so the
    disk gauge reflects the ~30 GB the device actually has; fall back to `/` if none is found."""
    best = None  # (total_bytes, target, statvfs)
    try:
        with open("/proc/mounts") as stream:
            for line in stream:
                fields = line.split()
                if len(fields) < 4:
                    continue
                source, target, fstype, options = fields[0], fields[1], fields[2], fields[3]
                if not source.startswith("/dev/") or fstype in NON_STORAGE_FSTYPES:
                    continue
                if "ro" in options.split(","):  # read-only (e.g. the squashfs firmware) is not health
                    continue
                try:
                    info = os.statvfs(target)
                except OSError:
                    continue
                total = info.f_blocks * info.f_frsize
                if total <= 0:
                    continue
                if best is None or total > best[0]:
                    best = (total, target, info)
    except OSError:
        pass
    if best is None:
        try:
            usage = shutil.disk_usage("/")
            return {
                "diskTotalGb": round(usage.total / 1e9, 1),
                "diskUsedGb": round((usage.total - usage.free) / 1e9, 1),
                "diskPercent": round(100 * (usage.total - usage.free) / usage.total) if usage.total else 0,
                "diskMount": "/",
            }
        except OSError:
            return {}
    total, target, info = best
    total_bytes = info.f_blocks * info.f_frsize
    free_bytes = info.f_bavail * info.f_frsize
    used_bytes = max(0, total_bytes - free_bytes)
    return {
        "diskTotalGb": round(total_bytes / 1e9, 1),
        "diskUsedGb": round(used_bytes / 1e9, 1),
        "diskPercent": round(100 * used_bytes / total_bytes) if total_bytes else 0,
        "diskMount": target,
    }


def read_system_stats() -> dict[str, object]:
    """Best-effort Comet Pro (Linux) health — CPU %, memory, disk, temperature, load, uptime.
    Returns only the fields it can read; on a non-Linux host (e.g. a dev Mac) it returns an empty
    dict, so the web console simply hides the panel there."""
    stats: dict[str, object] = {}
    try:
        meminfo: dict[str, int] = {}
        with open("/proc/meminfo") as stream:
            for line in stream:
                key, _, rest = line.partition(":")
                meminfo[key.strip()] = int(rest.strip().split()[0])
        total = meminfo.get("MemTotal", 0)
        available = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
        if total:
            used = max(0, total - available)
            stats["memTotalMb"] = round(total / 1024)
            stats["memUsedMb"] = round(used / 1024)
            stats["memPercent"] = round(100 * used / total)
    except (OSError, ValueError, IndexError):
        pass
    try:
        def sample() -> tuple[int, int]:
            with open("/proc/stat") as stream:
                fields = [int(value) for value in stream.readline().split()[1:]]
            idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
            return sum(fields), idle
        total1, idle1 = sample()
        time.sleep(0.12)
        total2, idle2 = sample()
        delta = total2 - total1
        if delta > 0:
            stats["cpuPercent"] = max(0, min(100, round(100 * (delta - (idle2 - idle1)) / delta)))
    except (OSError, ValueError, IndexError):
        pass
    try:
        temperatures: list[float] = []
        thermal_root = "/sys/class/thermal"
        for zone in os.listdir(thermal_root):
            if not zone.startswith("thermal_zone"):
                continue
            try:
                with open(os.path.join(thermal_root, zone, "temp")) as stream:
                    temperatures.append(int(stream.read().strip()) / 1000)
            except (OSError, ValueError):
                continue
        if temperatures:
            stats["tempC"] = round(max(temperatures), 1)
    except OSError:
        pass
    try:
        stats["load1"] = round(os.getloadavg()[0], 2)
    except OSError:
        pass
    try:
        with open("/proc/uptime") as stream:
            stats["uptimeSec"] = int(float(stream.read().split()[0]))
    except (OSError, ValueError):
        pass
    stats.update(primary_storage_stats())
    return stats


def compose_wallpaper(snapshot: dict[str, object], provider_id: str = "claude",
                      animation_frame: int = 0, theme_override: dict[str, object] | None = None,
                      display_override: dict[str, object] | None = None,
                      layout_override: dict[str, object] | None = None,
                      ) -> tuple[Image.Image, dict[str, object]]:
    provider = find_provider(snapshot, provider_id)
    theme = theme_override or PROVIDERS[provider_id]
    emphasis = str((display_override or DISPLAY).get("limitEmphasis", "percent"))
    activity = provider.get("activity") if isinstance(provider.get("activity"), dict) else {}
    today = activity.get("today") if isinstance(activity.get("today"), dict) else {}
    limits = provider.get("limits") if isinstance(provider.get("limits"), list) else []
    by_label = {
        item.get("label"): item for item in limits if isinstance(item, dict) and item.get("label")
    }

    s = SS
    top_color = str(theme["background"])
    bottom_color = theme.get("backgroundBottom") or blend_color(
        str(theme.get("bar", theme["accent"])), top_color, 0.08)
    image = vertical_gradient((480 * s, 160 * s), top_color, bottom_color)
    draw = ImageDraw.Draw(image)

    paste_provider_logo(image, provider_id, theme, s)
    if not theme.get("wideLogo"):
        draw_text(draw, (48 * s, 11 * s), str(theme["brand"]),
                  load_font(FONT_UI_BOLD, 16 * s), str(theme["text"]), "la")
        draw_text(draw, (48 * s, 31 * s), str(theme["subbrand"]),
                  load_font(FONT_UI_SEMIBOLD, 9 * s), str(theme["muted"]), "la")

    is_active = provider.get("working") is True
    connection_state = str(provider.get("connectionState", ""))
    if connection_state and connection_state != "ready":
        status_text = "SETUP" if connection_state == "not_installed" else "CHECK"
    else:
        status_text = "WORK" if is_active else "READY"
    status_color = str(theme["secondary"]) if is_active else str(theme["muted"])
    # Working-status chip: a rounded pill with a leading dot behind the WORK/READY label, mirroring
    # the web console's live-view design so the two surfaces match.
    status_font = load_font(FONT_UI_SEMIBOLD, 8 * s)
    status_x = (GLYPH_CENTER[0] + 18) * s
    status_y = GLYPH_CENTER[1] * s
    status_w = draw.textlength(status_text, font=status_font)
    pill_half = 7 * s
    draw.rounded_rectangle(
        ((GLYPH_CENTER[0] + 11) * s, status_y - pill_half, status_x + status_w + 6 * s, status_y + pill_half),
        radius=pill_half, fill=blend_color(status_color, top_color, 0.20))
    dot_r = 2 * s
    dot_cx = (GLYPH_CENTER[0] + 14.5) * s
    draw.ellipse((dot_cx - dot_r, status_y - dot_r, dot_cx + dot_r, status_y + dot_r), fill=status_color)
    draw_text(draw, (status_x, status_y), status_text, status_font, status_color, "lm")
    # Current subscription plan (e.g. PRO / PLUS / MAX), drawn in the always-visible header so it
    # shows for whichever provider is selected and updates automatically as the plan push changes.
    plan_text = str(provider.get("plan") or "").strip().upper()[:14]
    if plan_text and connection_state in ("", "ready"):
        draw_text(draw, ((GLYPH_CENTER[0] + 18) * s, (GLYPH_CENTER[1] + 12) * s), plan_text,
                  load_font(FONT_UI_SEMIBOLD, 8 * s), str(theme.get("bar", theme["accent"])), "lm")

    if usage_panel_visible(provider, limits):
        ctx = {"provider": provider, "activity": activity, "today": today,
               "limits": limits, "by_label": by_label, "snapshot": snapshot,
               "emphasis": emphasis}
        layout = resolve_layout(layout_override if layout_override is not None else LAYOUT)
        if layout.get("divider"):
            draw.line((204 * s, 14 * s, 204 * s, 146 * s), fill=str(theme["line"]), width=s)
        for placement in layout["widgets"]:
            renderer = WIDGET_RENDERERS.get(str(placement.get("widget")))
            if renderer:
                renderer(draw, image, placement, ctx, theme, s, placement)
    else:
        draw.line((204 * s, 14 * s, 204 * s, 146 * s), fill=str(theme["line"]), width=s)
        render_setup(draw, provider, theme, s)

    image = image.resize((480, 160), Image.Resampling.LANCZOS)
    # The glyph is drawn on the final 1x image so animation frames can redraw exactly the
    # same box; its tile background is sampled beside the box to blend with the gradient.
    glyph_background = image.getpixel((GLYPH_CENTER[0] - 14, GLYPH_CENTER[1]))
    glyph_style = str(theme.get("glyph", provider_id))
    draw_activity_glyph(image, glyph_style, GLYPH_CENTER, animation_frame, status_color,
                        glyph_background, is_active)

    return image, {"active": is_active, "generatedAt": snapshot.get("generatedAt"),
                   "provider": summarize_provider(provider)}


def render_wallpaper(snapshot: dict[str, object], provider_id: str = "claude",
                     output_path: Path = WALLPAPER_PATH, animation_frame: int = 0) -> dict[str, object]:
    image, rendered = compose_wallpaper(snapshot, provider_id, animation_frame)
    save_png_atomic(image, output_path)
    return rendered


_last_frames_signature: str | None = None


def render_animation_frames(snapshot: dict[str, object], provider_id: str,
                            base_image: Image.Image | None = None) -> list[Path]:
    global _last_frames_signature
    provider = find_provider(snapshot, provider_id)
    theme = PROVIDERS[provider_id]
    working = provider.get("working") is True
    status_color = str(theme["secondary"]) if working else str(theme["muted"])
    ANIMATION_FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    if base_image is None:
        with Image.open(WALLPAPER_PATH) as source:
            base_image = source.convert("RGB")
    paths = [ANIMATION_FRAMES_DIR / f"frame-{frame:02d}.png" for frame in range(ANIMATION_FRAME_COUNT)]
    # Rendering 60 PNGs is the agent's costliest operation and working-state flips can request
    # it every few seconds; when the base wallpaper is unchanged the existing files are reused.
    signature = "|".join((provider_id, str(working), hashlib.sha256(base_image.tobytes()).hexdigest()))
    if signature == _last_frames_signature and all(path.is_file() for path in paths):
        return paths
    # Sampled beside the glyph box so the redrawn tile blends with the gradient background.
    glyph_background = base_image.getpixel((GLYPH_CENTER[0] - 14, GLYPH_CENTER[1]))
    glyph_style = str(theme.get("glyph", provider_id))
    for frame, path in enumerate(paths):
        frame_image = base_image.copy()
        draw_activity_glyph(frame_image, glyph_style, GLYPH_CENTER, frame, status_color,
                            glyph_background, working)
        save_png_atomic(frame_image, path, compress_level=1)
    _last_frames_signature = signature
    return paths


def build_preview_snapshot(provider_id: str) -> dict[str, object]:
    """Representative sample data for theme previews when no live snapshot exists yet."""
    now = datetime.now(timezone.utc)
    series = []
    for offset in range(29, -1, -1):
        total = int(1_500_000 + 3_600_000 * (0.5 + 0.5 * math.sin(offset * 0.7)) * (0.55 + (offset % 4) * 0.15))
        series.append({
            "date": (now - timedelta(days=offset)).date().isoformat(),
            "totalTokens": total,
            "inputTokens": int(total * 0.08), "outputTokens": int(total * 0.11),
            "cacheReadTokens": int(total * 0.74), "cacheCreationTokens": int(total * 0.07),
        })
    latest = series[-1]
    return {"generatedAt": utc_now(), "providers": [{
        "id": provider_id, "name": provider_id, "plan": "Pro", "connectionState": "ready",
        "usageAvailable": True, "working": True, "trackedTokenTotalsAvailable": True,
        "limits": [
            {"label": "Current session", "usedPercent": 42, "windowMinutes": 300,
             "resetsAt": (now + timedelta(hours=2, minutes=40)).isoformat()},
            {"label": "Weekly limit", "usedPercent": 63, "windowMinutes": 10080,
             "resetsAt": (now + timedelta(days=2, hours=15)).isoformat()},
        ],
        "activity": {
            "today": {"tokens": latest["totalTokens"], "inputTokens": latest["inputTokens"],
                      "outputTokens": latest["outputTokens"], "cacheReadTokens": latest["cacheReadTokens"],
                      "cacheWriteTokens": latest["cacheCreationTokens"]},
            "last7Days": series[-7:], "last30Days": series,
            "last30DaysTokens": sum(day["totalTokens"] for day in series), "lastUsedAt": utc_now(),
        },
    }]}


def write_theme_file(theme: dict[str, object]) -> None:
    THEME_PATH.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".theme.", suffix=".json", dir=THEME_PATH.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(theme, stream, indent=2)
            stream.write("\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, THEME_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def cleanup_legacy_frame_dirs() -> None:
    """Old agent versions kept frames in throwaway mkdtemp directories; remove leftovers."""
    try:
        for path in ANIMATION_FRAMES_ROOT.glob("kvm-ai-frames.*"):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


STREAMER_STATE_SOCKET = os.environ.get("KVM_AI_USAGE_STREAMER_SOCKET", "/run/kvmd/ustreamer.sock")
STREAM_CHECK_SECONDS = 3.0


def stream_clients_active() -> bool:
    """True while the web console is streaming the captured display to someone. The wallpaper
    animation yields then: remote view/control must keep the CPU, and concurrent GUI redraws
    plus capture-encode load is what wedged the vendor video pipeline into D-state once."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            sock.connect(STREAMER_STATE_SOCKET)
            sock.sendall(b"GET /state HTTP/1.0\r\nHost: localhost\r\n\r\n")
            data = b""
            while len(data) < 65536:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        body = data.split(b"\r\n\r\n", 1)[1]
        sinks = json.loads(body).get("result", {}).get("sinks", {})
        return any(isinstance(sink, dict) and sink.get("has_clients") is True
                   for sink in sinks.values())
    except Exception:
        return False


def publish_wallpaper(path: Path = WALLPAPER_PATH, timeout: float = 10) -> None:
    if not PUBLISH_ENABLED:
        return
    event_data = json.dumps({"path": str(path)}, separators=(",", ":"))
    payload = json.dumps(
        {
            "event_module": "custom_screen",
            "event_type": "update_background",
            "event_data": event_data,
        },
        separators=(",", ":"),
    )
    result = subprocess.run(["ubus", "send", "gui", payload], capture_output=True, text=True,
                            timeout=timeout, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeError(f"wallpaper refresh failed: {detail}")


class Agent:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        # refresh_lock guards publishing plus the animation frame state; collect_lock
        # serializes full refresh cycles. Slow network I/O (SSH collect, activity probes)
        # deliberately runs under neither, so the animation publisher is never starved.
        self.refresh_lock = threading.Lock()
        self.collect_lock = threading.Lock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.config = read_config()
        self.collector = SshCollector(SSH_KEY_PATH)
        self.devices = DeviceStore(DEVICES_PATH)
        self.push = PushReceiver(self.devices, PUSH_STATE_PATH)
        self.kvm_identity = read_kvm_identity()
        self.resolved_device_host: str | None = None
        self.snapshot: dict[str, object] | None = None
        self.animation_frames: list[Path] = []
        self.animation_frame = 0
        self.animation_started_at = time.monotonic()
        cleanup_legacy_frame_dirs()
        write_config(self.config)
        self.state: dict[str, object] = {
            "running": True,
            "deviceOnline": False,
            "lastAttemptAt": None,
            "lastSuccessAt": None,
            "snapshotGeneratedAt": None,
            "selectedProviderActive": False,
            "activityDeviceCount": 0,
            "activityDeviceConfiguredCount": 0,
            "pushDeviceCount": 0,
            "lastPushAt": None,
            "providers": [],
            "lastError": None,
        }

    def status(self) -> dict[str, object]:
        system = read_system_stats()  # samples CPU over ~0.12s; done outside the lock
        with self.lock:
            return {
                **self.config,
                **self.state,
                "agentVersion": AGENT_VERSION,
                "kvmIdentity": self.kvm_identity,
                "system": system,
                "resolvedDeviceHost": self.resolved_device_host,
                "sshPublicKey": self.collector.public_key(),
                "collectionMode": "kvm-ssh-pull",
                "wallpaperReady": WALLPAPER_PATH.is_file(),
                "pushDevices": self.devices.list(),
            }

    def update_config(self, changes: dict[str, object]) -> dict[str, object]:
        allowed = {
            key: changes[key]
            for key in (
                "enabled", "animateWorking", "pauseWhenStreaming", "intervalSeconds",
                "selectedProvider", "deviceUser", "deviceHost", "devicePort", "activityHosts",
            )
            if key in changes
        }
        with self.lock:
            self.config = normalize_config({**self.config, **allowed})
            if {"deviceUser", "deviceHost", "devicePort"} & allowed.keys():
                self.resolved_device_host = None
            write_config(self.config)
            result = dict(self.config)
        self.wake.set()
        return result

    def refresh(self, publish: bool = True) -> dict[str, object]:
        with self.collect_lock:
            with self.lock:
                device_user = str(self.config["deviceUser"])
                device_host = str(self.config["deviceHost"])
                device_port = int(self.config["devicePort"])
                selected_provider = str(self.config["selectedProvider"])
                animate_working = bool(self.config.get("animateWorking"))
                self.state["lastAttemptAt"] = utc_now()
            collect_error: str | None = None
            device_online = True
            resolved_device_host = self.resolved_device_host
            try:
                snapshot, resolved_device_host = self.collector.collect(
                    device_user, device_host, device_port, self.resolved_device_host,
                )
            except Exception as error:
                collect_error = str(error)
                device_online = False
                snapshot = build_usage_snapshot({"providers": []})
                if device_host == "auto":
                    resolved_device_host = None
            self.resolved_device_host = resolved_device_host
            activity_hosts = self.configured_activity_hosts(resolved_device_host)
            try:
                activity_results = (
                    self.collector.probe_activity_hosts(activity_hosts, device_user, device_port)
                    if activity_hosts else {}
                )
            except RuntimeError:
                activity_results = {}
            merged_activity = {**activity_results, **self.push.active_map()}
            self.apply_activity_states(snapshot, merged_activity, resolved_device_host or "")
            self.apply_usage_overlay(snapshot, self.push.usage_overlay())
            push_device_count, last_push_at = self.push_health()
            no_activity_source = not activity_results and not any(
                not device.get("revoked") for device in self.devices.list()
            )
            has_usage = any(
                isinstance(provider, dict)
                and (provider.get("usageAvailable") or provider.get("connectionState") == "ready")
                for provider in snapshot.get("providers", [])
            )
            if collect_error and not has_usage:
                with self.lock:
                    self.state.update({"deviceOnline": False, "lastError": collect_error})
                return self.status()
            try:
                # Compose and pre-render frames outside refresh_lock: the snapshot is still
                # local to this call, and frame files are replaced atomically in place.
                image, rendered = compose_wallpaper(snapshot, selected_provider)
                animation_frames = (
                    render_animation_frames(snapshot, selected_provider, base_image=image)
                    if animate_working and rendered.get("active") else []
                )
                with self.refresh_lock:
                    save_png_atomic(image, WALLPAPER_PATH)
                    if publish:
                        publish_wallpaper(animation_frames[0] if animation_frames else WALLPAPER_PATH)
                    with self.lock:
                        self.snapshot = snapshot
                        self.animation_frames = animation_frames
                        self.animation_frame = 0
                        self.animation_started_at = time.monotonic()
                        self.state.update(
                            {
                                "deviceOnline": device_online,
                                "lastSuccessAt": utc_now(),
                                "snapshotGeneratedAt": rendered.get("generatedAt"),
                                "selectedProviderActive": rendered.get("active", False),
                                "activityDeviceCount": len(activity_results),
                                "activityDeviceConfiguredCount": len(activity_hosts),
                                "pushDeviceCount": push_device_count,
                                "lastPushAt": last_push_at,
                                "providers": [
                                    summarize_provider(provider)
                                    for provider in snapshot.get("providers", [])
                                    if isinstance(provider, dict) and provider.get("id") in PROVIDER_IDS
                                ],
                                "lastError": collect_error or (
                                    "no activity device or push device configured" if no_activity_source else None
                                ),
                            }
                        )
            except Exception as error:  # Keep the last successful wallpaper on transient failures.
                with self.lock:
                    if str(self.config["deviceHost"]) == "auto":
                        self.resolved_device_host = None
                    self.state.update({"deviceOnline": False, "lastError": str(error)})
        return self.status()

    def configured_activity_hosts(self, primary_host: str | None) -> list[str]:
        configured = str(self.config.get("activityHosts", ""))
        extra = [host.strip() for host in configured.split(",") if host.strip()]
        return list(dict.fromkeys(([primary_host] if primary_host else []) + extra))

    def push_health(self) -> tuple[int, str | None]:
        devices = self.devices.list()
        pushed = [device for device in devices if device.get("lastUsageAt") or device.get("lastActivityAt")]
        last_push_at = max(
            (device.get("lastUsageAt") or device.get("lastActivityAt") or "" for device in pushed),
            default="",
        )
        return len(pushed), (last_push_at or None)

    @staticmethod
    def apply_activity_states(snapshot: dict[str, object],
                              results: dict[str, dict[str, bool]], primary_host: str) -> None:
        for provider in snapshot.get("providers", []):
            if not isinstance(provider, dict) or provider.get("id") not in PROVIDER_IDS:
                continue
            provider_id = str(provider["id"])
            device_working = results.get(primary_host, {}).get(provider_id, False)
            authorized_working = any(
                host != primary_host and states.get(provider_id, False)
                for host, states in results.items()
            )
            provider["deviceWorking"] = device_working
            provider["authorizedDeviceWorking"] = authorized_working
            provider["working"] = device_working or authorized_working
            provider["workingSource"] = (
                "connected_device" if device_working
                else "authorized_device" if authorized_working else None
            )
            provider["activityState"] = "working" if provider["working"] else "standby"

    @staticmethod
    def apply_usage_overlay(snapshot: dict[str, object],
                            overlay: dict[str, dict[str, object] | None]) -> None:
        today_key = datetime.now().astimezone().date().isoformat()
        for provider in snapshot.get("providers", []):
            if not isinstance(provider, dict) or provider.get("id") not in PROVIDER_IDS:
                continue
            pushed = overlay.get(str(provider["id"]))
            if not pushed:
                continue
            if pushed.get("plan"):
                provider["plan"] = pushed["plan"]
            if pushed.get("limits"):
                provider["limits"] = pushed["limits"]
                provider["exactSubscriptionUsage"] = True
            daily = pushed.get("daily")
            if daily:
                today = next((day for day in daily if day.get("date") == today_key), {
                    "date": today_key, "totalTokens": 0, "inputTokens": 0, "outputTokens": 0,
                    "cacheReadTokens": 0, "cacheCreationTokens": 0,
                })
                activity = provider.get("activity") if isinstance(provider.get("activity"), dict) else {}
                activity["today"] = {
                    "date": today.get("date"),
                    "tokens": today.get("totalTokens", 0),
                    "inputTokens": today.get("inputTokens", 0),
                    "outputTokens": today.get("outputTokens", 0),
                    "cacheReadTokens": today.get("cacheReadTokens", 0),
                    "cacheWriteTokens": today.get("cacheCreationTokens", 0),
                    "costUSD": None,
                }
                activity["last7Days"] = daily[-7:]
                activity["last30Days"] = daily
                activity["last30DaysTokens"] = sum(day.get("totalTokens", 0) for day in daily)
                activity["last30DaysCostUSD"] = None
                provider["activity"] = activity
                provider["trackedTokenTotalsAvailable"] = True
                provider["tokenTotalsScope"] = "account"
                provider["accountTokenTotalsAvailable"] = True
                provider["usageAvailable"] = True
            if pushed.get("loggedIn") and (pushed.get("limits") or daily):
                provider["connectionState"] = "ready"
                provider["source"] = "Device helper push"
                provider["usageAvailable"] = True

    def republish(self) -> None:
        """Re-render and publish the wallpaper with the currently applied theme."""
        with self.lock:
            snapshot = self.snapshot
            selected_provider = str(self.config["selectedProvider"])
            animate_working = bool(self.config.get("animateWorking"))
        if not snapshot:
            self.wake.set()
            return
        image, rendered = compose_wallpaper(snapshot, selected_provider)
        animation_frames = (
            render_animation_frames(snapshot, selected_provider, base_image=image)
            if animate_working and rendered.get("active") else []
        )
        with self.refresh_lock:
            save_png_atomic(image, WALLPAPER_PATH)
            publish_wallpaper(animation_frames[0] if animation_frames else WALLPAPER_PATH)
            with self.lock:
                self.animation_frames = animation_frames
                self.animation_frame = 0
                self.animation_started_at = time.monotonic()

    def refresh_working_state(self) -> None:
        with self.lock:
            host = self.resolved_device_host
            snapshot = self.snapshot
            user = str(self.config["deviceUser"])
            port = int(self.config["devicePort"])
            selected_provider = str(self.config["selectedProvider"])
            animate_working = bool(self.config.get("animateWorking"))
        if not snapshot:
            return
        activity_hosts = self.configured_activity_hosts(host)
        try:
            results = (
                self.collector.probe_activity_hosts(activity_hosts, user, port)
                if activity_hosts else {}
            )
        except RuntimeError:
            results = {}
        merged = {**results, **self.push.active_map()}
        with self.lock:
            if self.snapshot is not snapshot:
                return  # A full refresh replaced the snapshot while we probed.
        previous_states = {
            str(provider.get("id")): provider.get("working") is True
            for provider in snapshot.get("providers", []) if isinstance(provider, dict)
        }
        self.apply_activity_states(snapshot, merged, host or "")
        selected_changed = False
        for provider in snapshot.get("providers", []):
            if not isinstance(provider, dict) or provider.get("id") not in PROVIDER_IDS:
                continue
            provider_id = str(provider["id"])
            working = provider.get("working") is True
            if provider_id == selected_provider and previous_states.get(provider_id) != working:
                selected_changed = True
            provider["status"] = "active" if working else (
                "available" if provider.get("usageAvailable") else "unavailable"
            )
        if selected_changed:
            image, rendered = compose_wallpaper(snapshot, selected_provider)
            animation_frames = (
                render_animation_frames(snapshot, selected_provider, base_image=image)
                if animate_working and rendered.get("active") else []
            )
            with self.refresh_lock:
                with self.lock:
                    stale = self.snapshot is not snapshot
                if not stale:
                    save_png_atomic(image, WALLPAPER_PATH)
                    publish_wallpaper(animation_frames[0] if animation_frames else WALLPAPER_PATH)
                    with self.lock:
                        self.animation_frames = animation_frames
                        self.animation_frame = 0
                        self.animation_started_at = time.monotonic()
                        self.state["selectedProviderActive"] = rendered.get("active", False)
        push_device_count, last_push_at = self.push_health()
        with self.lock:
            self.state["providers"] = [
                summarize_provider(provider)
                for provider in snapshot.get("providers", [])
                if isinstance(provider, dict) and provider.get("id") in PROVIDER_IDS
            ]
            self.state["lastActivityProbeAt"] = utc_now()
            self.state["activityDeviceCount"] = len(results)
            self.state["activityDeviceConfiguredCount"] = len(activity_hosts)
            self.state["pushDeviceCount"] = push_device_count
            self.state["lastPushAt"] = last_push_at

    def run(self) -> None:
        threading.Thread(target=self.working_probe_loop, name="working-probe", daemon=True).start()
        threading.Thread(target=self.animation_loop, name="animator", daemon=True).start()
        while not self.stop.is_set():
            with self.lock:
                enabled = bool(self.config["enabled"])
                interval = int(self.config["intervalSeconds"])
            if enabled:
                self.refresh()
            self.wake.wait(interval if enabled else 3600)
            self.wake.clear()

    def working_probe_loop(self) -> None:
        while not self.stop.is_set():
            with self.lock:
                enabled = bool(self.config["enabled"])
            if enabled:
                try:
                    self.refresh_working_state()
                except Exception as error:
                    with self.lock:
                        self.state["lastError"] = f"activity: {error}"
            self.stop.wait(WORKING_POLL_SECONDS)

    def animation_loop(self) -> None:
        streaming = False
        last_stream_check = 0.0
        while not self.stop.is_set():
            with self.lock:
                animate = bool(self.config.get("animateWorking"))
                pause_when_streaming = bool(self.config.get("pauseWhenStreaming"))
                active = bool(self.state.get("selectedProviderActive"))
                animation_frames = self.animation_frames
            if not (animate and active and animation_frames):
                self.stop.wait(ANIMATION_INTERVAL_SECONDS)
                continue
            if pause_when_streaming:
                now = time.monotonic()
                if now - last_stream_check >= STREAM_CHECK_SECONDS:
                    was_streaming = streaming
                    streaming = stream_clients_active()
                    last_stream_check = now
                    if streaming and not was_streaming:
                        try:  # Freeze on the static wallpaper while someone is viewing.
                            with self.refresh_lock:
                                publish_wallpaper(WALLPAPER_PATH, timeout=3)
                        except Exception:
                            pass
                if streaming:
                    self.stop.wait(STREAM_CHECK_SECONDS)
                    continue
            elapsed = time.monotonic() - self.animation_started_at
            next_frame = int(
                elapsed % ANIMATION_ROTATION_SECONDS
                / ANIMATION_ROTATION_SECONDS
                * ANIMATION_FRAME_COUNT
            )
            if next_frame != self.animation_frame:
                try:
                    with self.refresh_lock:
                        with self.lock:
                            animation_frames = self.animation_frames
                        if animation_frames:
                            self.animation_frame = next_frame % len(animation_frames)
                            publish_wallpaper(animation_frames[self.animation_frame], timeout=3)
                except Exception as error:
                    with self.lock:
                        self.state["lastError"] = f"animation: {error}"
            # A full interval between wake-ups caps publishing at ANIMATION_PUBLISH_FPS;
            # the frame index is derived from elapsed time, so skipped phases stay in step.
            self.stop.wait(ANIMATION_INTERVAL_SECONDS)


class Handler(BaseHTTPRequestHandler):
    server_version = "KvmAiUsage/1"

    @property
    def agent(self) -> Agent:
        return self.server.agent  # type: ignore[attr-defined]

    def log_message(self, message: str, *args: object) -> None:
        print(f"{self.address_string()} - {message % args}", flush=True)

    def send_bytes(self, status: int, body: bytes, content_type: str, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status: int, value: object) -> None:
        self.send_bytes(status, json.dumps(value).encode("utf-8"), "application/json; charset=utf-8")

    def serve_file(self, path: Path, content_type: str, cache: str = "no-store") -> None:
        try:
            self.send_bytes(HTTPStatus.OK, path.read_bytes(), content_type, cache)
        except OSError:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_HEAD(self) -> None:
        self.do_GET()

    def authed(self) -> bool:
        """True when console auth is disabled (nginx gate handles it) or a valid session cookie is
        present. Static shell assets are public so the login screen can render; everything else and
        every /api/* route goes through here."""
        return not AUTH_ENABLED or session_cookie_valid(request_session_token(self))

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        # The HTML shell, icon and provider logos are public so the login screen can render; the
        # live wallpaper and all /api data require a session.
        public = path in ("/", "/index.html", "/icon.svg", "/comet-pro.jpg") or (
            path.startswith("/providers/") and path.endswith(".png")
        )
        if not public and not self.authed():
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        if path in ("/", "/index.html"):
            self.serve_file(INDEX_PATH, "text/html; charset=utf-8")
        elif path == "/icon.svg":
            self.serve_file(ICON_PATH, "image/svg+xml", "public, max-age=3600")
        elif path == "/comet-pro.jpg":
            self.serve_file(COMET_IMAGE_PATH, "image/jpeg", "public, max-age=86400")
        elif path.startswith("/providers/") and path.endswith(".png"):
            name = path.removeprefix("/providers/")
            if name.removesuffix(".png") in PROVIDER_IDS and "/" not in name:
                self.serve_file(PROVIDERS_PATH / name, "image/png", "public, max-age=3600")
            else:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        elif path == "/api/session":
            self.send_json(HTTPStatus.OK, {"authenticated": True, "authRequired": AUTH_ENABLED})
        elif path == "/wallpaper.png":
            self.serve_file(WALLPAPER_PATH, "image/png")
        elif path == "/api/status":
            self.send_json(HTTPStatus.OK, self.agent.status())
        elif path == "/api/devices":
            self.send_json(HTTPStatus.OK, {"devices": self.agent.devices.list()})
        elif path == "/api/theme":
            builtin = {
                provider_id: {key: theme[key] for key in THEME_COLOR_KEYS if key in theme}
                for provider_id, theme in BUILTIN_PROVIDERS.items()
            }
            self.send_json(HTTPStatus.OK, {
                "theme": read_theme_file() or {"schemaVersion": 1, "providers": {}},
                "builtin": builtin,
                "glyphStyles": sorted(GLYPH_STYLES),
                "layoutPresets": sorted(LAYOUT_PRESETS),
                "widgetTypes": sorted(WIDGET_TYPES),
            })
        else:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def read_json(self) -> dict[str, object]:
        try:
            length = min(16_384, int(self.headers.get("Content-Length", "0")))
            value = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("Invalid JSON body") from error
        if not isinstance(value, dict):
            raise RuntimeError("JSON body must be an object")
        return value

    def send_json_cookie(self, status: int, value: object, cookie: str) -> None:
        body = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Set-Cookie", cookie)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def handle_login(self) -> None:
        try:
            body = self.read_json()
        except RuntimeError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid body"})
            return
        password = str(body.get("password") or "")
        totp = re.sub(r"\s+", "", str(body.get("totp") or ""))
        if not password:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "password required"})
            return
        if not verify_admin_credentials(password, totp):
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid credentials"})
            return
        cookie = (
            f"{SESSION_COOKIE}={issue_session_cookie()}; Path=/; HttpOnly; "
            f"SameSite=Strict; Secure; Max-Age={SESSION_TTL_SECONDS}"
        )
        self.send_json_cookie(HTTPStatus.OK, {"ok": True}, cookie)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/push/v1/usage", "/push/v1/activity"):
            self.handle_push(path)
            return
        if path == "/api/login":
            self.handle_login()
            return
        if path == "/api/logout":
            expired = f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Secure; Max-Age=0"
            self.send_json_cookie(HTTPStatus.OK, {"ok": True}, expired)
            return
        if not self.authed():
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        try:
            if path == "/api/config":
                self.send_json(HTTPStatus.OK, self.agent.update_config(self.read_json()))
            elif path == "/api/refresh":
                self.send_json(HTTPStatus.OK, self.agent.refresh())
            elif path == "/api/theme":
                clean = sanitize_theme(self.read_json().get("theme"))
                if clean is None:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid theme document"})
                    return
                write_theme_file(clean)
                apply_theme(clean)
                self.agent.republish()
                self.send_json(HTTPStatus.OK, {"ok": True, "theme": clean})
            elif path == "/api/theme/preview":
                body = self.read_json()
                provider_id = body.get("provider")
                if provider_id not in PROVIDER_IDS:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "unknown provider"})
                    return
                clean = sanitize_theme(body.get("theme"))
                providers, display, layout = load_providers(clean)
                with self.agent.lock:
                    snapshot = self.agent.snapshot
                try:
                    if snapshot:
                        find_provider(snapshot, provider_id)
                except RuntimeError:
                    snapshot = None
                image, _ = compose_wallpaper(
                    snapshot or build_preview_snapshot(provider_id), provider_id,
                    animation_frame=18, theme_override=providers[provider_id],
                    display_override=display, layout_override=layout,
                )
                buffer = BytesIO()
                image.save(buffer, format="PNG")
                self.send_bytes(HTTPStatus.OK, buffer.getvalue(), "image/png")
            elif path == "/api/devices":
                self.send_json(HTTPStatus.OK, self.agent.devices.create(self.read_json().get("name")))
            elif path == "/api/devices/rotate":
                result = self.agent.devices.rotate(str(self.read_json().get("id", "")))
                self.send_json(HTTPStatus.OK if result else HTTPStatus.NOT_FOUND,
                               result or {"error": "Not found"})
            elif path == "/api/devices/revoke":
                ok = self.agent.devices.revoke(str(self.read_json().get("id", "")))
                self.send_json(HTTPStatus.OK if ok else HTTPStatus.NOT_FOUND,
                               {"ok": True} if ok else {"error": "Not found"})
            elif path == "/api/devices/delete":
                ok = self.agent.devices.delete(str(self.read_json().get("id", "")))
                self.send_json(HTTPStatus.OK if ok else HTTPStatus.NOT_FOUND,
                               {"ok": True} if ok else {"error": "Not found"})
            else:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except (RuntimeError, ValueError) as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})

    def handle_push(self, path: str) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid body"})
            return
        if length > PUSH_BODY_LIMIT:
            self.close_connection = True
            self.send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "payload too large"})
            return
        body = self.rfile.read(length) if length else b""
        device = self.agent.push.verify(
            self.headers.get("X-KVM-Device", ""),
            self.headers.get("X-KVM-Timestamp", ""),
            self.headers.get("X-KVM-Nonce", ""),
            self.headers.get("X-KVM-Signature", ""),
            "POST", path, body,
        )
        if device is None:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        if self.agent.push.rate_limited(device["id"]):
            self.send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "rate_limited"})
            return
        try:
            value = json.loads(body or b"{}")
        except json.JSONDecodeError:
            value = None
        if not isinstance(value, dict):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid body"})
            return
        handler = self.agent.push.handle_usage if path.endswith("/usage") else self.agent.push.handle_activity
        if not handler(device, value):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid body"})
            return
        # Return Comet Pro health on the usage push so the enrolled companion app can show it without
        # a separate (session-authenticated) console request — the device is already HMAC-verified.
        if path.endswith("/usage"):
            self.send_json(HTTPStatus.OK, {"ok": True, "agentVersion": AGENT_VERSION,
                                           "kvmIdentity": self.agent.kvm_identity,
                                           "system": read_system_stats()})
        else:
            self.send_json(HTTPStatus.OK, {"ok": True})


class ControlServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], agent: Agent) -> None:
        self.agent = agent
        super().__init__(address, Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render-once", action="store_true", help="fetch and render once")
    parser.add_argument("--no-publish", action="store_true", help="do not notify the touchscreen GUI")
    args = parser.parse_args()
    agent = Agent()
    if args.render_once:
        status = agent.refresh(publish=not args.no_publish)
        print(json.dumps(status, indent=2))
        raise SystemExit(0 if status["lastError"] is None else 1)

    worker = threading.Thread(target=agent.run, name="wallpaper-worker", daemon=True)
    worker.start()
    server = ControlServer(("127.0.0.1", PORT), agent)
    print(f"KVM AI Usage listening on 127.0.0.1:{PORT}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        agent.stop.set()
        agent.wake.set()
        server.server_close()


if __name__ == "__main__":
    main()
