#!/usr/bin/env python3
"""Local preview of the AI Usage web console — serves index.html with realistic mock API data so
you can see the new live touchscreen animation without deploying to the Comet Pro.

    python3 kvm-agent/preview-web.py     # then open http://127.0.0.1:8787/

This is a dev aid only; it never talks to a real KVM or any device."""

import http.server
import json
import os
import pathlib

import agent  # module import is safe; we never construct Agent() (which needs KVM tooling)

HERE = pathlib.Path(__file__).resolve().parent
PORT = int(os.environ.get("PREVIEW_PORT", "8787"))

# Brand palette so the mock looks like the real Claude wallpaper (bars in Claude orange).
CLAUDE_THEME = {
    "background": "#141210", "text": "#f4efe9", "muted": "#9a8f83", "line": "#332b24",
    "track": "#2a231d", "bar": "#d97757", "accent": "#d97757", "secondary": "#39e08f",
}


def mock_status(working=True):
    provider = agent.summarize_provider(agent.build_preview_snapshot("claude")["providers"][0])
    provider.update({"id": "claude", "name": "Claude Code", "working": working})
    return {
        "selectedProvider": "claude", "enabled": True, "animateWorking": True, "intervalSeconds": 60,
        "providers": [provider],
        "kvmIdentity": {"model": "GL.iNet Comet Pro", "modelCode": "RM10", "hostname": "glkvm",
                        "firmwareVersion": "V1.9.1 release1", "platformVersion": "Buildroot 2024.02"},
        "deviceUser": "you", "deviceHost": "auto", "activityHosts": "", "sshPublicKey": "ssh-ed25519 AAAA…preview",
        "deviceOnline": True, "resolvedDeviceHost": "mac.local", "running": True,
        "activityDeviceCount": 1, "activityDeviceConfiguredCount": 1,
        "lastSuccessAt": agent.utc_now(), "agentVersion": "preview", "wallpaperReady": False,
        "pushDevices": [], "lastError": None,
        "system": {"cpuPercent": 37, "memPercent": 54, "memUsedMb": 545, "memTotalMb": 1010,
                   "tempC": 52.4, "load1": 0.42, "uptimeSec": 189342,
                   "diskPercent": 63, "diskUsedGb": 4.7, "diskTotalGb": 7.4},
    }


LOGGED_IN = {"ok": False}


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(HERE), **kwargs)

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # Set PREVIEW_REQUIRE_LOGIN=1 to preview the agent-hosted login screen (any password works).
        if os.environ.get("PREVIEW_REQUIRE_LOGIN") == "1" and not LOGGED_IN["ok"] \
                and self.path.split("?", 1)[0] in ("/api/status", "/api/devices", "/api/theme"):
            return self._json({"error": "unauthorized"}, status=401)
        if self.path.startswith("/api/status"):
            return self._json(mock_status())
        if self.path.startswith("/api/theme"):
            return self._json({"theme": {"schemaVersion": 1, "providers": {}}, "builtin": {"claude": CLAUDE_THEME}})
        if self.path.startswith("/api/devices"):
            return self._json({"devices": []})
        if self.path in ("/", "/index.html"):
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/login"):
            LOGGED_IN["ok"] = True
            return self._json({"ok": True})
        if self.path.startswith("/api/refresh"):
            return self._json(mock_status())
        return self._json({})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"AI Usage web preview: http://127.0.0.1:{PORT}/  (Ctrl+C to stop)")
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
