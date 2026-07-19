#!/usr/bin/env python3
"""On-device AI usage wallpaper renderer and local control API."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFont
from push_receiver import DeviceStore, PushReceiver
from ssh_collector import SshCollector, build_usage_snapshot


AGENT_VERSION = "1.3.0"  # Keep in step with package.json; surfaced on /api/status for updates.

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
SSH_KEY_PATH = Path(os.environ.get("KVM_AI_USAGE_SSH_KEY", ROOT / "device-key"))
PORT = int(os.environ.get("KVM_AI_USAGE_PORT", "8199"))
PUBLISH_ENABLED = os.environ.get("KVM_AI_USAGE_NO_PUBLISH") != "1"

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


def load_providers() -> dict[str, dict[str, object]]:
    """Built-in themes overlaid with validated color overrides from the theme JSON file.
    Themes are data, never code: only known color keys with strict hex values are accepted,
    and any parse or schema problem silently keeps the built-in look."""
    themes = {provider_id: dict(theme) for provider_id, theme in BUILTIN_PROVIDERS.items()}
    try:
        raw = json.loads(THEME_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return themes
    if not isinstance(raw, dict) or raw.get("schemaVersion") != 1:
        return themes
    providers = raw.get("providers")
    for provider_id, overrides in (providers.items() if isinstance(providers, dict) else ()):
        if provider_id not in themes or not isinstance(overrides, dict):
            continue
        for key, value in overrides.items():
            if key in THEME_COLOR_KEYS and isinstance(value, str) and THEME_COLOR_RE.match(value):
                themes[provider_id][key] = value
    return themes


PROVIDERS = load_providers()
PROVIDER_IDS = frozenset(PROVIDERS)

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


def render_limit_row(draw: ImageDraw.ImageDraw, limit: dict[str, object] | None, y: int,
                     title: str, window: str, bar_color: object,
                     theme: dict[str, object], s: int) -> None:
    label_font = load_font(FONT_UI_SEMIBOLD, 11 * s)
    value_font = load_font(FONT_UI_DISPLAY, 27 * s)
    footer_font = load_font(FONT_UI_MEDIUM, 9 * s)
    raw_percent = limit.get("usedPercent") if limit else None
    try:
        percent = min(100.0, max(0.0, float(raw_percent)))
        has_percent = True
    except (TypeError, ValueError):
        percent = 0
        has_percent = False

    x0, x1 = 218 * s, 464 * s
    draw_text(draw, (x0, y * s), title, label_font, str(theme["text"]), "la")
    draw_text(draw, (x1, (y - 5) * s), f"{round(percent)}%" if has_percent else "--", value_font,
              str(theme["text"]), "ra")
    bar_y = (y + 27) * s
    bar_height = 12 * s
    radius = bar_height // 2
    draw.rounded_rectangle((x0, bar_y, x1, bar_y + bar_height), radius=radius,
                           fill=str(theme["track"]))
    if has_percent and percent > 0:
        width = max(bar_height, round((x1 - x0) * percent / 100))
        draw.rounded_rectangle((x0, bar_y, x0 + width, bar_y + bar_height), radius=radius,
                               fill=bar_color)
    draw_text(draw, (x0, (y + 45) * s), window, footer_font, str(theme["muted"]), "la")
    draw_text(draw, (x1, (y + 45) * s), reset_label(limit.get("resetsAt") if limit else None),
              footer_font, str(theme["muted"]), "ra")


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


def compose_wallpaper(snapshot: dict[str, object], provider_id: str = "claude",
                      animation_frame: int = 0) -> tuple[Image.Image, dict[str, object]]:
    provider = find_provider(snapshot, provider_id)
    theme = PROVIDERS[provider_id]
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
    draw_text(draw, ((GLYPH_CENTER[0] + 15) * s, GLYPH_CENTER[1] * s), status_text,
              load_font(FONT_UI_SEMIBOLD, 8 * s), status_color, "lm")

    draw.line((204 * s, 14 * s, 204 * s, 146 * s), fill=str(theme["line"]), width=s)
    if usage_panel_visible(provider, limits):
        # Left panel: one consistent hierarchy — small label, hero value, bottom detail row.
        if provider.get("trackedTokenTotalsAvailable") is True:
            top_label, hero = "TODAY TOKENS", compact_number(today.get("tokens"))
            bottom_label = "LAST 30 DAYS"
            bottom_value = compact_number(activity.get("last30DaysTokens"))
            cost_value = activity.get("last30DaysCostUSD")
            try:
                if cost_value is not None:
                    bottom_value = f"{bottom_value} · ${float(cost_value):.0f}"
            except (TypeError, ValueError):
                pass
        elif provider_id not in ("claude", "codex") and (
                limits or provider.get("creditsRemaining") is not None
                or provider.get("providerCostUSD") is not None):
            top_label = "SUBSCRIPTION"
            hero = str(provider.get("plan") or "CONNECTED").upper()[:18]
            credits = provider.get("creditsRemaining")
            provider_cost = provider.get("providerCostUSD")
            if credits is not None:
                bottom_label, bottom_value = "CREDITS LEFT", compact_number(credits)
            elif provider_cost is not None:
                bottom_label, bottom_value = "PROVIDER COST", f"${float(provider_cost):.2f}"
            else:
                bottom_label, bottom_value = "ACCOUNT LIMITS", "CONNECTED"
        elif provider.get("accountTokenTotalsAvailable") is False:
            top_label = "NATIVE ACCOUNT"
            hero = str(provider.get("plan") or "CONNECTED").upper()[:16]
            bottom_label, bottom_value = "TOKEN HISTORY", "UNAVAILABLE"
        else:
            top_label, hero = "TODAY TOKENS", compact_number(today.get("tokens"))
            cost_value = activity.get("last30DaysCostUSD")
            bottom_label = "LAST 30 DAYS"
            bottom_value = compact_number(activity.get("last30DaysTokens"))
            try:
                if cost_value is not None:
                    bottom_value = f"{bottom_value} · ${float(cost_value):.0f}"
            except (TypeError, ValueError):
                pass

        draw_text(draw, (16 * s, 56 * s), top_label,
                  load_font(FONT_UI_SEMIBOLD, 10 * s), str(theme["muted"]), "la")
        hero_font = fit_text(draw, hero, FONT_UI_DISPLAY, 42 * s, 20 * s, 176 * s)
        draw_text(draw, (15 * s, 70 * s), hero, hero_font, str(theme["text"]), "la")
        draw.line((16 * s, 124 * s, 188 * s, 124 * s), fill=str(theme["line"]), width=s)
        draw_text(draw, (16 * s, 134 * s), bottom_label,
                  load_font(FONT_UI_SEMIBOLD, 9 * s), str(theme["muted"]), "la")
        bottom_font = fit_text(draw, bottom_value, FONT_UI_BOLD, 15 * s, 10 * s, 108 * s)
        draw_text(draw, (188 * s, 140 * s), bottom_value, bottom_font,
                  str(theme.get("bar", theme["accent"])), "rm")

        primary = by_label.get("Current session") or by_label.get("5-hour window")
        secondary = by_label.get("Weekly limit") or by_label.get("Weekly window")
        if primary is None and secondary is None:
            primary = limits[0] if limits else None
            secondary = limits[1] if len(limits) > 1 else None
        primary_title = str(primary.get("label") if primary else "CURRENT SESSION").upper()
        secondary_title = str(secondary.get("label") if secondary else "WEEKLY LIMIT").upper()
        bar_color = str(theme.get("bar", theme["accent"]))
        render_limit_row(draw, primary, 16, primary_title,
                         window_label(primary, "5-HOUR WINDOW"), bar_color, theme, s)
        render_limit_row(draw, secondary, 90, secondary_title,
                         window_label(secondary, "7-DAY WINDOW"),
                         blend_color(bar_color, top_color, 0.62), theme, s)
    else:
        render_setup(draw, provider, theme, s)

    image = image.resize((480, 160), Image.Resampling.LANCZOS)
    # The glyph is drawn on the final 1x image so animation frames can redraw exactly the
    # same box; its tile background is sampled beside the box to blend with the gradient.
    glyph_background = image.getpixel((GLYPH_CENTER[0] - 14, GLYPH_CENTER[1]))
    draw_activity_glyph(image, provider_id, GLYPH_CENTER, animation_frame, status_color,
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
    for frame, path in enumerate(paths):
        frame_image = base_image.copy()
        draw_activity_glyph(frame_image, provider_id, GLYPH_CENTER, frame, status_color,
                            glyph_background, working)
        save_png_atomic(frame_image, path, compress_level=1)
    _last_frames_signature = signature
    return paths


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
        with self.lock:
            return {
                **self.config,
                **self.state,
                "agentVersion": AGENT_VERSION,
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

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self.serve_file(INDEX_PATH, "text/html; charset=utf-8")
        elif path == "/icon.svg":
            self.serve_file(ICON_PATH, "image/svg+xml", "public, max-age=3600")
        elif path.startswith("/providers/") and path.endswith(".png"):
            name = path.removeprefix("/providers/")
            if name.removesuffix(".png") in PROVIDER_IDS and "/" not in name:
                self.serve_file(PROVIDERS_PATH / name, "image/png", "public, max-age=3600")
            else:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        elif path == "/wallpaper.png":
            self.serve_file(WALLPAPER_PATH, "image/png")
        elif path == "/api/status":
            self.send_json(HTTPStatus.OK, self.agent.status())
        elif path == "/api/devices":
            self.send_json(HTTPStatus.OK, {"devices": self.agent.devices.list()})
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

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/push/v1/usage", "/push/v1/activity"):
            self.handle_push(path)
            return
        try:
            if path == "/api/config":
                self.send_json(HTTPStatus.OK, self.agent.update_config(self.read_json()))
            elif path == "/api/refresh":
                self.send_json(HTTPStatus.OK, self.agent.refresh())
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
