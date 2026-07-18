#!/bin/zsh
set -euo pipefail

PROJECT_DIR="${0:A:h:h}"
KVM_HOST=""
DEVICE_ID=""
UPDATE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --kvm) KVM_HOST="$2"; shift 2 ;;
    --device) DEVICE_ID="$2"; shift 2 ;;
    --update) UPDATE=1; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

CONFIG_DIR="$HOME/.kvm-ai-monitor"
if [[ "$UPDATE" -eq 1 && ( -z "$KVM_HOST" || -z "$DEVICE_ID" ) ]]; then
  if [[ ! -f "$CONFIG_DIR/helper.json" ]]; then
    echo "--update requires an existing installation ($CONFIG_DIR/helper.json not found)." >&2
    exit 1
  fi
  KVM_HOST=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["kvmHost"])' "$CONFIG_DIR/helper.json")
  DEVICE_ID=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["deviceId"])' "$CONFIG_DIR/helper.json")
fi

if [[ -z "$KVM_HOST" || -z "$DEVICE_ID" ]]; then
  echo "Usage: install-helper.sh --kvm <host-or-ip> --device <device-id> | --update" >&2
  exit 1
fi

APP_SUPPORT="$HOME/Library/Application Support/kvm-ai-monitor"
LABEL="com.kvm-ai-monitor.helper"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ "$UPDATE" -eq 0 ]]; then
  read -r -s "SECRET?One-time device secret from the KVM AI Usage page: "
  echo
  if [[ -z "$SECRET" ]]; then
    echo "Secret cannot be empty." >&2
    exit 1
  fi

  # Item created by the security CLI, so scheduled runs of `security find-generic-password`
  # can read it back without a GUI consent prompt.
  security add-generic-password -a device -s "kvm-ai-monitor-push:$KVM_HOST" -w "$SECRET" -U >/dev/null
  unset SECRET
fi

mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"
printf '{"kvmHost": "%s", "deviceId": "%s"}\n' "$KVM_HOST" "$DEVICE_ID" > "$CONFIG_DIR/helper.json"
chmod 600 "$CONFIG_DIR/helper.json"

mkdir -p "$APP_SUPPORT"
chmod 755 "$APP_SUPPORT"
cp "$PROJECT_DIR/mac-helper/kvm_ai_push.py" "$APP_SUPPORT/kvm_ai_push.py"
cp "$PROJECT_DIR/mac-helper/kvm-ai-claude-hook.sh" "$APP_SUPPORT/kvm-ai-claude-hook.sh"
chmod 644 "$APP_SUPPORT/kvm_ai_push.py"
chmod 755 "$APP_SUPPORT/kvm-ai-claude-hook.sh"

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/env</string>
    <string>python3</string>
    <string>$APP_SUPPORT/kvm_ai_push.py</string>
    <string>send-usage</string>
  </array>
  <key>StartInterval</key>
  <integer>60</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/kvm-ai-helper.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/kvm-ai-helper.log</string>
</dict>
</plist>
PLIST_EOF

UID_NUM=$(id -u)
launchctl bootout "gui/$UID_NUM/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST"

echo "LaunchAgent loaded. Running an initial usage push..."
if python3 "$APP_SUPPORT/kvm_ai_push.py" send-usage; then
  echo "Initial push succeeded."
else
  echo "Initial push failed. Check /tmp/kvm-ai-helper.log or run: npm run helper:status" >&2
fi

echo
echo "NOTE: reading Claude's local usage may show a one-time Keychain consent dialog for"
echo "\"Claude Code-credentials\" — choose Always Allow so the scheduled push never prompts again."
echo
echo "To also send exact working/idle events from Claude Code on this device, run:"
echo "  npm run helper:hooks"
