#!/bin/sh
# Linux enrollment for the KVM AI Monitor push helper.
# Usage: install-helper-linux.sh --kvm <host> --device <device-id> [--secret-stdin] | --update
# Requires python3 and a systemd user session; the secret goes to libsecret when secret-tool
# is available, otherwise to a 0600 file under ~/.kvm-ai-monitor/secrets/.
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CONFIG_DIR="$HOME/.kvm-ai-monitor"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/kvm-ai-monitor"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
KVM_HOST=""
DEVICE_ID=""
UPDATE=0
SECRET_STDIN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --kvm) KVM_HOST="$2"; shift 2 ;;
    --device) DEVICE_ID="$2"; shift 2 ;;
    --secret-stdin) SECRET_STDIN=1; shift ;;
    --update) UPDATE=1; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [ "$UPDATE" -eq 1 ]; then
  [ -f "$CONFIG_DIR/helper.json" ] || { echo "--update requires an existing installation." >&2; exit 1; }
elif [ -z "$KVM_HOST" ] || [ -z "$DEVICE_ID" ]; then
  echo "Usage: install-helper-linux.sh --kvm <host> --device <device-id> [--secret-stdin] | --update" >&2
  exit 1
fi

mkdir -p "$DATA_DIR"
cp "$PROJECT_DIR/helper/kvm_ai_push.py" "$DATA_DIR/kvm_ai_push.py"
cp "$PROJECT_DIR/helper/kvm-ai-claude-hook.sh" "$DATA_DIR/kvm-ai-claude-hook.sh"
chmod 644 "$DATA_DIR/kvm_ai_push.py"
chmod 755 "$DATA_DIR/kvm-ai-claude-hook.sh"

mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"

if [ "$UPDATE" -eq 0 ]; then
  if [ "$SECRET_STDIN" -eq 1 ]; then
    IFS= read -r SECRET
  else
    printf 'One-time device secret from the KVM AI Usage page: '
    stty -echo 2>/dev/null || true
    IFS= read -r SECRET
    stty echo 2>/dev/null || true
    echo
  fi
  [ -n "$SECRET" ] || { echo "Secret cannot be empty." >&2; exit 1; }
  printf '%s\n' "$SECRET" | python3 "$DATA_DIR/kvm_ai_push.py" store-secret --kvm "$KVM_HOST"
  unset SECRET

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

mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/kvm-ai-helper.service" <<UNIT
[Unit]
Description=KVM AI Monitor usage push

[Service]
Type=oneshot
ExecStart=/usr/bin/env python3 $DATA_DIR/kvm_ai_push.py send-usage
UNIT
cat > "$UNIT_DIR/kvm-ai-helper.timer" <<UNIT
[Unit]
Description=KVM AI Monitor usage push every minute

[Timer]
OnBootSec=30
OnUnitActiveSec=60
AccuracySec=5

[Install]
WantedBy=timers.target
UNIT

if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  systemctl --user daemon-reload
  systemctl --user enable --now kvm-ai-helper.timer
  echo "systemd user timer enabled (kvm-ai-helper.timer, every 60 s)."
else
  echo "No systemd user session detected. Schedule this yourself (e.g. cron):"
  echo "  * * * * * python3 $DATA_DIR/kvm_ai_push.py send-usage"
fi

echo "Running an initial usage push..."
if python3 "$DATA_DIR/kvm_ai_push.py" send-usage; then
  echo "Initial push succeeded."
else
  echo "Initial push failed; check the KVM address and secret." >&2
fi

echo
echo "To also send exact working/idle events from Claude Code on this device, run:"
echo "  npm run helper:hooks"
echo "(or directly: python3 $PROJECT_DIR/helper/claude_hooks.py install \"$DATA_DIR/kvm-ai-claude-hook.sh\")"
