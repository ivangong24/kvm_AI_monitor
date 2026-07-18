#!/bin/sh
set -eu

ROOT=/etc/kvmd/user/ai-usage
STARTUP=/etc/kvmd/user/scripts/S60kvm-ai-usage
EXTENSION=/usr/share/kvmd/extras/ai-usage
GUI_BACKGROUND_DIRS="/etc/glinet/gui/custom/background /etc/rm10-gui/picture/custom/background"

[ ! -x "$STARTUP" ] || "$STARTUP" stop
rm -f "$STARTUP"
rm -rf "$EXTENSION"
# Drop the tmpfs shields so the GUI's background copies persist on flash again.
for dir in $GUI_BACKGROUND_DIRS; do
    umount "$dir" 2>/dev/null || true
done
nohup sh -c 'sleep 2; /etc/init.d/S99kvmd-nginx restart' >/tmp/kvm-ai-usage-nginx.log 2>&1 &
echo "KVM AI Usage stopped and removed from the web console."
echo "Configuration remains in $ROOT; remove it manually only if no longer needed."
