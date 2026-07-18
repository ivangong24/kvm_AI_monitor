#!/bin/sh
# Claude Code lifecycle hook: forwards start/active/stop to the push helper in the background.
# Must never slow down or break Claude Code, so it never waits and always exits 0.
set -u

HELPER_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
EVENT="${1:-active}"

python3 "$HELPER_DIR/kvm_ai_push.py" send-activity "$EVENT" >/dev/null 2>&1 &

exit 0
