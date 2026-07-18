#!/bin/sh
set -eu

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=/etc/kvmd/user/ai-usage
STARTUP=/etc/kvmd/user/scripts/S60kvm-ai-usage
EXTENSION=/usr/share/kvmd/extras/ai-usage
BACKUP_ROOT=/etc/kvmd/user/backups
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="$BACKUP_ROOT/kvm-ai-usage-$STAMP"

if [ -e "$ROOT" ] || [ -e "$STARTUP" ] || [ -e "$EXTENSION" ]; then
    mkdir -p "$BACKUP"
    [ ! -e "$ROOT/config.json" ] || cp -a "$ROOT/config.json" "$BACKUP/config.json"
    [ ! -e "$STARTUP" ] || cp -a "$STARTUP" "$BACKUP/startup-script"
    [ ! -e "$EXTENSION" ] || cp -a "$EXTENSION" "$BACKUP/extension"
    echo "Previous non-secret configuration backed up to $BACKUP"
fi

mkdir -p "$ROOT/extension" "$ROOT/providers" "$(dirname "$STARTUP")"
rm -f "$ROOT/claude-logo.png"
cp "$SOURCE_DIR/agent.py" "$ROOT/agent.py"
cp "$SOURCE_DIR/ssh_collector.py" "$ROOT/ssh_collector.py"
cp "$SOURCE_DIR/push_receiver.py" "$ROOT/push_receiver.py"
cp "$SOURCE_DIR/index.html" "$ROOT/index.html"
cp "$SOURCE_DIR/providers/"*.png "$ROOT/providers/"
cp "$SOURCE_DIR/icon.svg" "$ROOT/icon.svg"
cp "$SOURCE_DIR/extension/manifest.yaml" "$ROOT/extension/manifest.yaml"
cp "$SOURCE_DIR/extension/nginx.ctx-http.conf" "$ROOT/extension/nginx.ctx-http.conf"
cp "$SOURCE_DIR/extension/nginx.ctx-server.conf" "$ROOT/extension/nginx.ctx-server.conf"
cp "$SOURCE_DIR/service.sh" "$STARTUP"
cp "$SOURCE_DIR/uninstall-on-device.sh" "$ROOT/uninstall-on-device.sh"

if [ ! -e "$ROOT/config.json" ]; then
    cp "$SOURCE_DIR/config.example.json" "$ROOT/config.json"
fi

chmod 755 "$ROOT/agent.py" "$ROOT/uninstall-on-device.sh" "$STARTUP"
chmod 600 "$ROOT/config.json"
chmod 644 "$ROOT/index.html" "$ROOT/icon.svg" "$ROOT/ssh_collector.py" "$ROOT/push_receiver.py" "$ROOT/extension/"*
chmod 644 "$ROOT/providers/"*.png

"$STARTUP" restart
nohup sh -c 'sleep 2; /etc/init.d/S99kvmd-nginx restart' >/tmp/kvm-ai-usage-nginx.log 2>&1 &
echo "KVM AI Usage installed. Configuration backups: $BACKUP_ROOT"
