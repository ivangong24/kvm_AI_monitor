#!/bin/zsh
set -euo pipefail

PROJECT_DIR="${0:A:h:h}"
APP_SUPPORT="$HOME/Library/Application Support/kvm-ai-monitor"
HOOK_SCRIPT="$APP_SUPPORT/kvm-ai-claude-hook.sh"

if [[ ! -x "$HOOK_SCRIPT" ]]; then
  echo "Hook script not found at: $HOOK_SCRIPT" >&2
  echo "Run install-helper.sh (npm run helper:install) first." >&2
  exit 1
fi

python3 "$PROJECT_DIR/mac-helper/claude_hooks.py" install "$HOOK_SCRIPT"
echo "Claude Code lifecycle hooks installed in ~/.claude/settings.json"
echo "(SessionStart/UserPromptSubmit/PostToolUse/Stop/SessionEnd -> $HOOK_SCRIPT)"
