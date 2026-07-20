#!/bin/sh
# Linux counterpart to uninstall-helper.sh: stops the systemd user timer and removes the
# installed files. --purge also removes the config, the libsecret push secrets, and the
# file-backend fallbacks under ~/.kvm-ai-monitor/secrets/.
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CONFIG_DIR="$HOME/.kvm-ai-monitor"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/kvm-ai-monitor"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PURGE=0

for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    *) echo "Unknown argument: $arg" >&2; exit 1 ;;
  esac
done

if [ -f "$DATA_DIR/kvm-ai-claude-hook.sh" ]; then
  python3 "$PROJECT_DIR/helper/claude_hooks.py" uninstall "$DATA_DIR/kvm-ai-claude-hook.sh" || true
fi

if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  systemctl --user disable --now kvm-ai-helper.timer >/dev/null 2>&1 || true
  systemctl --user daemon-reload || true
fi
rm -f "$UNIT_DIR/kvm-ai-helper.timer" "$UNIT_DIR/kvm-ai-helper.service"
rm -rf "$DATA_DIR"
rm -f "$CONFIG_DIR/last-activity"

if [ "$PURGE" -eq 1 ]; then
  if [ -f "$CONFIG_DIR/helper.json" ] && command -v secret-tool >/dev/null 2>&1; then
    KVM_HOSTS=$(python3 -c "
import json, sys
try:
    data = json.load(open(sys.argv[1]))
    targets = data.get('targets')
    if not isinstance(targets, list):
        targets = [{'kvmHost': data.get('kvmHost')}]
    print(' '.join(t['kvmHost'] for t in targets if isinstance(t, dict) and t.get('kvmHost')))
except Exception:
    print('')
" "$CONFIG_DIR/helper.json")
    for host in $KVM_HOSTS; do
      secret-tool clear service "kvm-ai-monitor-push:$host" account device >/dev/null 2>&1 || true
    done
  fi
  rm -rf "$CONFIG_DIR"
  echo "Purged helper config and the stored push secrets."
else
  echo "Left $CONFIG_DIR/helper.json and the push secret in place (use --purge to remove)."
fi

echo "KVM AI Monitor helper uninstalled."
