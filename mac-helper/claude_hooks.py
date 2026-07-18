#!/usr/bin/env python3
"""Idempotent merge/removal of KVM AI Monitor lifecycle hooks in ~/.claude/settings.json.
Used by install-claude-hooks.sh / uninstall-claude-hooks.sh. Touches only hook entries whose
command points at our installed hook script; everything else in settings.json is preserved."""

from __future__ import annotations

import json
import pathlib
import sys
import time

SETTINGS_PATH = pathlib.Path.home() / ".claude" / "settings.json"

EVENT_ARGS = {
    "SessionStart": "start",
    "UserPromptSubmit": "active",
    "PostToolUse": "active",
    "Stop": "stop",
    "SessionEnd": "stop",
}


def load_settings():
    if SETTINGS_PATH.exists():
        with SETTINGS_PATH.open() as stream:
            return json.load(stream)
    return {}


def backup_settings():
    if SETTINGS_PATH.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_path = SETTINGS_PATH.with_name("settings.json.backup-" + stamp)
        backup_path.write_text(SETTINGS_PATH.read_text())


def save_settings(settings):
    SETTINGS_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with SETTINGS_PATH.open("w") as stream:
        json.dump(settings, stream, indent=2)
        stream.write("\n")


def command_for(hook_path, event):
    # Single-quoted so a path containing spaces (e.g. "Application Support") stays one argument.
    return "'" + hook_path + "' " + EVENT_ARGS[event]


def points_to_hook(command, hook_path):
    if not isinstance(command, str):
        return False
    quoted = "'" + hook_path + "'"
    return command == quoted or command.startswith(quoted + " ")


def has_command(entry, command):
    return isinstance(entry, dict) and any(
        isinstance(item, dict) and item.get("type") == "command" and item.get("command") == command
        for item in entry.get("hooks", [])
    )


def install(hook_path):
    settings = load_settings()
    hooks = settings.setdefault("hooks", {})
    changed = False
    for event in EVENT_ARGS:
        entries = hooks.setdefault(event, [])
        command = command_for(hook_path, event)
        if any(has_command(entry, command) for entry in entries):
            continue
        entries.append({"hooks": [{"type": "command", "command": command}]})
        changed = True
    if changed:
        backup_settings()
        save_settings(settings)


def uninstall(hook_path):
    settings = load_settings()
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    changed = False
    for event in list(hooks.keys()):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        new_entries = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                new_entries.append(entry)
                continue
            kept = [
                item for item in entry["hooks"]
                if not (isinstance(item, dict) and item.get("type") == "command" and points_to_hook(item.get("command"), hook_path))
            ]
            if len(kept) != len(entry["hooks"]):
                changed = True
            if kept:
                new_entries.append({**entry, "hooks": kept})
        hooks[event] = new_entries
        if not hooks[event]:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    if changed:
        backup_settings()
        save_settings(settings)


def main(argv):
    if len(argv) != 3 or argv[1] not in ("install", "uninstall"):
        print("usage: claude_hooks.py {install|uninstall} <hook-path>", file=sys.stderr)
        return 1
    action, hook_path = argv[1], argv[2]
    (install if action == "install" else uninstall)(hook_path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
