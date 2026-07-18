#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
printf '%s\n' "/etc/kvmd/user/ai-usage/uninstall-on-device.sh" \
  | "$PROJECT_DIR/scripts/kvm-webterm-command.mjs" --stdin
