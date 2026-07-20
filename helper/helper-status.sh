#!/bin/zsh
set -uo pipefail

PROJECT_DIR="${0:A:h:h}"
CONFIG_DIR="$HOME/.kvm-ai-monitor"
CONFIG_PATH="$CONFIG_DIR/helper.json"
LABEL="com.kvm-ai-monitor.helper"
LOG="/tmp/kvm-ai-helper.log"

echo "== Config =="
KVM_HOSTS=""
if [[ -f "$CONFIG_PATH" ]]; then
  KVM_HOSTS=$(python3 -c "
import json, sys
try:
    data = json.load(open(sys.argv[1]))
    targets = data.get('targets')
    if not isinstance(targets, list):
        targets = [{'kvmHost': data.get('kvmHost'), 'deviceId': data.get('deviceId')}]
    print(' '.join(t['kvmHost'] for t in targets
                   if isinstance(t, dict) and t.get('kvmHost') and t.get('deviceId')))
except Exception:
    print('')
" "$CONFIG_PATH")
  if [[ -n "$KVM_HOSTS" ]]; then
    echo "present and valid: $CONFIG_PATH (KVMs: $KVM_HOSTS)"
  else
    echo "present but invalid (no usable targets): $CONFIG_PATH"
  fi
else
  echo "missing: $CONFIG_PATH"
fi

echo
echo "== Keychain secrets =="
for host in ${(z)KVM_HOSTS}; do
  if security find-generic-password -s "kvm-ai-monitor-push:$host" -a device -w >/dev/null 2>&1; then
    echo "$host: present"
  else
    echo "$host: not found"
  fi
done
[[ -n "$KVM_HOSTS" ]] || echo "not found"

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
python3 "$PROJECT_DIR/helper/kvm_ai_push.py" print-payload
