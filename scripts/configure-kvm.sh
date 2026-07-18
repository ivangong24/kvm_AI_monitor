#!/bin/zsh
set -euo pipefail

PROJECT_DIR="${0:A:h:h}"
KVM_IP="${KVM_IP:-}"
if [[ -z "$KVM_IP" ]]; then
  read -r "KVM_IP?Comet Pro IP address: "
fi
if [[ ! "$KVM_IP" =~ '^([0-9]{1,3}\.){3}[0-9]{1,3}$' ]]; then
  echo "Enter the Comet Pro IPv4 address or set KVM_IP." >&2
  exit 1
fi
SERVICE="kvm-ai-monitor:$KVM_IP"
CONFIG_DIR="$HOME/.kvm-ai-monitor"

mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"
printf '%s\n' "$KVM_IP" > "$CONFIG_DIR/kvm-host"
chmod 600 "$CONFIG_DIR/kvm-host"

read -r -s "PASSWORD?Comet Pro admin password: "
echo
read -r "TOTP?Current 6-digit 2FA code (leave empty if 2FA is disabled): "

if [[ -z "$PASSWORD" ]]; then
  echo "Password cannot be empty." >&2
  exit 1
fi

security add-generic-password \
  -a admin \
  -s "$SERVICE" \
  -w "$PASSWORD" \
  -U >/dev/null
unset PASSWORD

echo "Password stored temporarily in macOS Keychain for authorization"
KVM_IP="$KVM_IP" KVM_TOTP="$TOTP" node "$PROJECT_DIR/scripts/authorize-kvm.js"
unset TOTP
echo
echo "In the Comet console, select Settings -> System -> Screen Display -> Wallpaper Only, then Apply."
