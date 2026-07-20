#!/bin/zsh
set -euo pipefail

PROJECT_DIR="${0:A:h:h}"
KVM_HOST=""
DEVICE_ID=""
UPDATE=0

SECRET_STDIN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --kvm) KVM_HOST="$2"; shift 2 ;;
    --device) DEVICE_ID="$2"; shift 2 ;;
    --secret-stdin) SECRET_STDIN=1; shift ;;
    --update) UPDATE=1; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

CONFIG_DIR="$HOME/.kvm-ai-monitor"
if [[ "$UPDATE" -eq 1 ]]; then
  if [[ ! -f "$CONFIG_DIR/helper.json" ]]; then
    echo "--update requires an existing installation ($CONFIG_DIR/helper.json not found)." >&2
    exit 1
  fi
elif [[ -z "$KVM_HOST" || -z "$DEVICE_ID" ]]; then
  echo "Usage: install-helper.sh --kvm <host-or-ip> --device <device-id> | --update" >&2
  exit 1
fi

APP_SUPPORT="$HOME/Library/Application Support/kvm-ai-monitor"
LABEL="com.kvm-ai-monitor.helper"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
ACTIVITY_LABEL="com.kvm-ai-monitor.activity"
ACTIVITY_PLIST="$HOME/Library/LaunchAgents/$ACTIVITY_LABEL.plist"

if [[ "$UPDATE" -eq 0 ]]; then
  if [[ "$SECRET_STDIN" -eq 1 ]]; then
    IFS= read -r SECRET  # piped by the setup wizard; never echoed or logged
  else
    read -r -s "SECRET?One-time device secret from the KVM AI Usage page: "
    echo
  fi
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
if [[ "$UPDATE" -eq 0 ]]; then
  # Merge this KVM into the target list so one Mac can push to several KVMs.
  python3 - "$CONFIG_DIR/helper.json" "$KVM_HOST" "$DEVICE_ID" <<'PY'
import json, os, pathlib, sys
path, host, device = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
config = {}
if path.is_file():
    try:
        config = json.load(path.open())
    except ValueError:
        config = {}
targets = config.get("targets")
if not isinstance(targets, list):
    targets = []
    if config.get("kvmHost") and config.get("deviceId"):
        targets.append({"kvmHost": config["kvmHost"], "deviceId": config["deviceId"]})
targets = [t for t in targets if isinstance(t, dict) and t.get("kvmHost") != host]
targets.append({"kvmHost": host, "deviceId": device})
path.write_text(json.dumps({"targets": targets}, indent=2) + "\n")
os.chmod(path, 0o600)
PY
fi

mkdir -p "$APP_SUPPORT"
chmod 755 "$APP_SUPPORT"
cp "$PROJECT_DIR/helper/kvm_ai_push.py" "$APP_SUPPORT/kvm_ai_push.py"
cp "$PROJECT_DIR/helper/kvm-ai-claude-hook.sh" "$APP_SUPPORT/kvm-ai-claude-hook.sh"
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

# Activity poller: pushes working/idle for CLIs without a lifecycle hook (codex). Short
# interval so the tile animates promptly; the KVM holds each working state for 120 s.
cat > "$ACTIVITY_PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$ACTIVITY_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/env</string>
    <string>python3</string>
    <string>$APP_SUPPORT/kvm_ai_push.py</string>
    <string>poll-activity</string>
  </array>
  <key>StartInterval</key>
  <integer>30</integer>
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
launchctl bootout "gui/$UID_NUM/$ACTIVITY_LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$UID_NUM" "$ACTIVITY_PLIST"

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
