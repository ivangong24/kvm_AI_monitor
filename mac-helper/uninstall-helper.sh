#!/bin/zsh
set -euo pipefail

PROJECT_DIR="${0:A:h:h}"
LABEL="com.kvm-ai-monitor.helper"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
APP_SUPPORT="$HOME/Library/Application Support/kvm-ai-monitor"
CONFIG_DIR="$HOME/.kvm-ai-monitor"
PURGE=0

for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    *) echo "Unknown argument: $arg" >&2; exit 1 ;;
  esac
done

if [[ -x "$PROJECT_DIR/mac-helper/uninstall-claude-hooks.sh" ]]; then
  "$PROJECT_DIR/mac-helper/uninstall-claude-hooks.sh" || true
fi

UID_NUM=$(id -u)
launchctl bootout "gui/$UID_NUM/$LABEL" >/dev/null 2>&1 || true
rm -f "$PLIST"
rm -rf "$APP_SUPPORT"
rm -f "$CONFIG_DIR/last-activity"

if [[ "$PURGE" -eq 1 ]]; then
  if [[ -f "$CONFIG_DIR/helper.json" ]]; then
    KVM_HOST=$(python3 -c "
import json, sys
try:
    print(json.load(open(sys.argv[1])).get('kvmHost', ''))
except Exception:
    print('')
" "$CONFIG_DIR/helper.json")
    if [[ -n "$KVM_HOST" ]]; then
      security delete-generic-password -a device -s "kvm-ai-monitor-push:$KVM_HOST" >/dev/null 2>&1 || true
    fi
  fi
  rm -rf "$CONFIG_DIR"
  echo "Purged helper config and the Keychain push secret."
else
  echo "Left $CONFIG_DIR/helper.json and the Keychain push secret in place (use --purge to remove)."
fi

echo "KVM AI Monitor helper uninstalled."
