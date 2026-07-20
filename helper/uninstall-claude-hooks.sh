#!/bin/zsh
set -euo pipefail

PROJECT_DIR="${0:A:h:h}"
APP_SUPPORT="$HOME/Library/Application Support/kvm-ai-monitor"
HOOK_SCRIPT="$APP_SUPPORT/kvm-ai-claude-hook.sh"

python3 "$PROJECT_DIR/helper/claude_hooks.py" uninstall "$HOOK_SCRIPT"
echo "Claude Code lifecycle hooks removed from ~/.claude/settings.json"
