# Device helper (macOS, Linux, Windows)

Implements the outbound-only side of `docs/PUSH_PROTOCOL.md`: a scheduled per-user task that
reduces Claude usage to the whitelisted aggregate schema, HMAC-signs it, and pushes it to the
KVM. Nothing here opens a listening port. The same stdlib-only Python helper runs on all three
platforms; only the scheduler and secret vault differ.

| | macOS | Linux | Windows |
|---|---|---|---|
| Installer | `install-helper.sh` | `install-helper-linux.sh` | `install-helper.ps1` |
| Uninstaller | `uninstall-helper.sh` | `uninstall-helper-linux.sh` | `uninstall-helper.ps1` |
| Scheduler | LaunchAgent (60 s) | systemd user timer (60 s) | Task Scheduler (1 min) |
| Push secret | login Keychain | libsecret (`secret-tool`), else 0600 file | user-scoped DPAPI file |
| Claude credentials | Keychain item (consent once) | `~/.claude/.credentials.json` | `~/.claude/.credentials.json` |
| Claude hook shim | `kvm-ai-claude-hook.sh` | `kvm-ai-claude-hook.sh` | `kvm-ai-claude-hook.cmd` |
| Status | `helper-status.sh` | `systemctl --user status` + `print-payload` | `helper-status.ps1` |

Every scheduler runs in the logged-in user's session (LaunchAgent GUI domain / systemd `--user`
/ Task Scheduler `InteractiveToken`), so pushes pause while that user is signed out and resume
at the next sign-in. The npm scripts (`npm run helper:*`) dispatch to the right script for the
current OS.

Linux/Windows enrollment, with the device ID + one-time secret from the AI Usage page:

```bash
# Linux (python3 + systemd user session)
./helper/install-helper-linux.sh --kvm <kvm-ip> --device <device-id>

# Windows (Python 3 on PATH, from PowerShell)
powershell -ExecutionPolicy Bypass -File helper\install-helper.ps1 -Kvm <kvm-ip> -Device <device-id>
```

Both print the command that adds Claude Code activity hooks at the end. The
`KVM_AI_SECRET_BACKEND` environment variable (`keychain` / `secret-tool` / `dpapi` / `file`)
overrides the automatic secret-storage choice, e.g. for headless Linux boxes without a secret
service. CI runs the helper test suite on macOS, Ubuntu, and Windows on every push.

## What runs where

- `kvm_ai_push.py` — stdlib-only Python, both a library and the CLI invoked by the LaunchAgent
  and the hook script. Subcommands: `sign`, `send-usage`, `send-activity {start|active|stop}`,
  `print-payload`.
- `com.kvm-ai-monitor.helper` LaunchAgent — runs `kvm_ai_push.py send-usage` once at load and
  every 60 seconds via `StartInterval`. Logs to `/tmp/kvm-ai-helper.log`.
- `kvm-ai-claude-hook.sh` — installed into Claude Code's `SessionStart` / `UserPromptSubmit` /
  `PostToolUse` / `Stop` / `SessionEnd` hooks (via `claude_hooks.py`, driven by
  `install-claude-hooks.sh`). Backgrounds `send-activity` and always exits 0 so it can never
  slow down or break Claude Code.
- Installed copies live in `~/Library/Application Support/kvm-ai-monitor/`; the LaunchAgent
  plist lives in `~/Library/LaunchAgents/`.

## Install / uninstall / status

These npm scripts work the same on macOS, Linux, and Windows (the dispatcher picks the right
installer and resolves Git Bash / a runnable Python on Windows):

```bash
npm run helper:install -- --kvm <kvm-host-or-ip> --device <device-id>   # enroll + schedule the push
npm run helper:install -- --update                                     # refresh installed files after git pull (no secret needed)
npm run helper:hooks                                                   # add Claude Code activity hooks
npm run helper:status                                                  # audit what is configured/sent
npm run helper:uninstall                                               # stop + remove installed files
npm run helper:uninstall -- --purge                                    # also remove config + the push secret
npm run helper:test                                                    # offline unit tests
```

`<device-id>` and the one-time secret come from the KVM's AI Usage admin page (`POST
/api/devices`), which shows the secret exactly once. The secret is only ever entered once, at
install time.

An additional device (e.g. a Mac mini used with the same subscription) needs only this
directory — no Remote Login, no SSH key. Copy the repo (or just `helper/`) to it and run
the same `helper:install` / `helper:hooks` commands with its own enrolled `--device` id.

## Privacy model

Sent (only): `schemaVersion`, `provider`, `collectedAt`, `plan`, `loggedIn`, `limits[]`
(`label`, `usedPercent`, `windowMinutes`, `resetsAt`), `daily[]` (`date` plus token counts), and
for activity pushes just `{schemaVersion, provider, event}`. Run `npm run helper:status` (or
`kvm_ai_push.py print-payload`) at any time to see the exact usage JSON before it is ever sent.

Never sent, logged, or written to disk by this code: prompts, responses, file paths, project
names, session ids, emails, or any credential/token. The OAuth access token read from Keychain
(for account limits) and Anthropic's response to `/api/oauth/usage` are held in memory only,
used to compute `limits`, and discarded — never printed or persisted.

The per-device push secret lives in the platform vault — login Keychain on macOS (service
`kvm-ai-monitor-push:<kvm-host>`, account `device`), libsecret on Linux, a user-scoped
DPAPI-encrypted file on Windows — and is never written to `helper.json`. The only exception is
the documented Linux fallback for machines without a secret service: a 0600 file under
`~/.kvm-ai-monitor/secrets/`. There is no iCloud sync, no cloud relay, and no CodexBar. The
device never listens on a network port; every push is an outbound HTTPS request the helper
initiates.

## Keychain consent

The first time the helper reads the `Claude Code-credentials` item (to compute account usage
limits), macOS may show a one-time consent dialog. Choose **Always Allow** so the scheduled
push (every 60 seconds) never prompts again. `install-helper.sh` prints this reminder after
running the first `send-usage`.

## Additional devices (e.g. a Mac mini)

A device used only for activity animation (no local Claude usage of interest) needs just the
helper + Claude Code hooks installed above — no Remote Login, no SSH key, no admin session on
that machine. Its `send-activity` pushes let the KVM animate "working" state for it; install
`helper:install` there too if you also want its local daily token totals counted (see the merge
rule in `docs/PUSH_PROTOCOL.md`: SSH-collected daily totals are only used when no device has
pushed `daily`, so install the helper on the primary Mac as well for its tokens to count).
