#!/bin/sh

ROOT=/etc/kvmd/user/ai-usage
EXTENSION=/usr/share/kvmd/extras/ai-usage
PID_FILE=/run/kvm-ai-usage.pid
LOG_FILE=/tmp/kvm-ai-usage.log
PYTHON=/usr/bin/python3

sync_extension() {
    mkdir -p "$EXTENSION"
    cp "$ROOT/extension/manifest.yaml" "$EXTENSION/manifest.yaml"
    cp "$ROOT/extension/nginx.ctx-http.conf" "$EXTENSION/nginx.ctx-http.conf"
    cp "$ROOT/extension/nginx.ctx-server.conf" "$EXTENSION/nginx.ctx-server.conf"
    cp "$ROOT/icon.svg" "$EXTENSION/icon.svg"
    chmod 644 "$EXTENSION/"*
}

start_agent() {
    sync_extension
    if [ -s "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        return 0
    fi
    rm -f "$PID_FILE"
    touch "$LOG_FILE"
    start-stop-daemon -S -b -m -p "$PID_FILE" -x "$PYTHON" -O "$LOG_FILE" -- "$ROOT/agent.py"
}

stop_agent() {
    if [ -s "$PID_FILE" ]; then
        start-stop-daemon -K -p "$PID_FILE" -R 3 2>/dev/null || true
        rm -f "$PID_FILE"
    fi
}

case "${1:-start}" in
    start) start_agent ;;
    stop) stop_agent ;;
    restart) stop_agent; start_agent ;;
    *) echo "Usage: $0 {start|stop|restart}" >&2; exit 2 ;;
esac
