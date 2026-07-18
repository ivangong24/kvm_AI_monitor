#!/bin/zsh
set -uo pipefail

PROJECT_DIR="${0:A:h:h}"
CONFIG_DIR="$HOME/.kvm-ai-monitor"
CONFIG_PATH="$CONFIG_DIR/helper.json"
LABEL="com.kvm-ai-monitor.helper"
LOG="/tmp/kvm-ai-helper.log"

echo "== Config =="
KVM_HOST=""
if [[ -f "$CONFIG_PATH" ]]; then
  KVM_HOST=$(python3 -c "
import json, sys
try:
    data = json.load(open(sys.argv[1]))
    print(data['kvmHost'] if data.get('kvmHost') and data.get('deviceId') else '')
except Exception:
    print('')
" "$CONFIG_PATH")
  if [[ -n "$KVM_HOST" ]]; then
    echo "present and valid: $CONFIG_PATH"
  else
    echo "present but invalid (missing kvmHost/deviceId): $CONFIG_PATH"
  fi
else
  echo "missing: $CONFIG_PATH"
fi

echo
echo "== Keychain secret =="
if [[ -n "$KVM_HOST" ]] && security find-generic-password -s "kvm-ai-monitor-push:$KVM_HOST" -a device -w >/dev/null 2>&1; then
  echo "present"
else
  echo "not found"
fi

echo
echo "== LaunchAgent =="
UID_NUM=$(id -u)
if ! launchctl print "gui/$UID_NUM/$LABEL" 2>&1 | head -20; then
  echo "not loaded"
fi

echo
echo "== Recent log ($LOG) =="
if [[ -f "$LOG" ]]; then
  tail -n 20 "$LOG"
else
  echo "no log yet"
fi

echo
echo "== Payload that would be sent (print-payload) =="
python3 "$PROJECT_DIR/mac-helper/kvm_ai_push.py" print-payload
