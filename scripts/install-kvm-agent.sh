#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ARCHIVE=$(mktemp -t kvm-ai-usage.XXXXXX.tar.gz)
trap 'rm -f "$ARCHIVE"' EXIT INT TERM

tar -C "$PROJECT_DIR" -czf "$ARCHIVE" kvm-agent
PAYLOAD=$(base64 < "$ARCHIVE" | tr -d '\n')

{
    printf '%s\n' "umask 077"
    printf '%s\n' "rm -rf /tmp/kvm-ai-usage-install /tmp/kvm-ai-usage.tar.gz"
    printf '%s\n' "mkdir -p /tmp/kvm-ai-usage-install"
    printf "printf '%%s' '%s' | base64 -d > /tmp/kvm-ai-usage.tar.gz\n" "$PAYLOAD"
    printf '%s\n' "tar -tzf /tmp/kvm-ai-usage.tar.gz >/dev/null"
    printf '%s\n' "tar -xzf /tmp/kvm-ai-usage.tar.gz -C /tmp/kvm-ai-usage-install"
    printf '%s\n' "python3 -m py_compile /tmp/kvm-ai-usage-install/kvm-agent/agent.py /tmp/kvm-ai-usage-install/kvm-agent/ssh_collector.py /tmp/kvm-ai-usage-install/kvm-agent/push_receiver.py"
    printf '%s\n' "/tmp/kvm-ai-usage-install/kvm-agent/install-on-device.sh"
    printf '%s\n' "rm -rf /tmp/kvm-ai-usage-install /tmp/kvm-ai-usage.tar.gz"
} | node "$PROJECT_DIR/scripts/kvm-webterm-command.mjs" --stdin

sleep 7
node "$PROJECT_DIR/scripts/kvm-webterm-command.mjs" \
    "curl -fsS http://127.0.0.1:8199/api/status"
