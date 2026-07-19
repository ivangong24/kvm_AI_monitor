# KVM AI Monitor

Turn a GL.iNet Comet Pro KVM's touchscreen into a live AI-usage dashboard. The KVM renders a
480×160 wallpaper showing your AI subscription usage — Claude Code's account-accurate current
session, weekly, and model-scoped limits, Codex plan windows and daily tokens — plus an
animated working indicator whenever an agent is actively processing on any enrolled device.

- Everything renders on the KVM itself; your computers never run a dashboard, server, or open
  port. Enrolled devices push signed, whitelisted usage aggregates outbound; credentials never
  leave the device they live on.
- Works across devices and accounts: enroll any number of macOS, Linux, or Windows machines,
  and one machine can push to several KVMs.
- The KVM's primary job always wins: the animation pauses automatically while anyone is
  remote-viewing, and the agent runs at low priority.
- Copilot, Gemini CLI, and Grok get installation/sign-in detection; their vendors expose no
  supported consumer quota API yet.

![Wallpaper rendered on the KVM touchscreen (sample data)](docs/images/wallpaper-claude.png)

## Prerequisites

- A GL.iNet Comet Pro with its admin password (2FA supported), reachable on your LAN.
- A macOS machine with Node.js 22+ to run the setup wizard (Linux/Windows *monitored* devices
  are supported; the setup machine itself is macOS-only for now).
- On each monitored device: Python 3 and Claude Code signed in (`claude` CLI) for Claude data.

## Install

One command on a Mac on the same network:

```bash
npx github:ivangong24/kvm_AI_monitor        # or: brew install ivangong24/kvm-ai-monitor/kvm-ai-monitor && kvm-ai-monitor
```

The wizard discovers the Comet, signs in (only a revocable session token is kept, in your
Keychain), installs the on-device agent, switches the touchscreen to Wallpaper Only, enrolls
the Mac it runs on as a push device (with optional Claude Code hooks for exact working-state
animation), and finishes with a health check. From a clone: `npm run setup`.

### Enrolling more devices

On the AI Usage page (`https://<comet-ip>/extras/ai-usage/`), use **Enroll a device** to get a
device ID and one-time secret, then on that device run:

```bash
# macOS
./mac-helper/install-helper.sh --kvm <comet-ip> --device <device-id>

# Linux (systemd user session)
./mac-helper/install-helper-linux.sh --kvm <comet-ip> --device <device-id>

# Windows (PowerShell, Python 3 on PATH)
powershell -ExecutionPolicy Bypass -File mac-helper\install-helper.ps1 -Kvm <comet-ip> -Device <device-id>
```

Each installer schedules a per-minute usage push (LaunchAgent / systemd timer / Task
Scheduler), stores the secret in the platform vault (Keychain / libsecret / Windows DPAPI),
and prints the command that adds Claude Code activity hooks. Details:
[`mac-helper/README.md`](mac-helper/README.md).

Optionally, a device can instead be read over SSH ("Connected device" on the page): enable
Remote Login, authorize the KVM's public key, and enter the username. SSH devices provide
install/auth detection and working-state presence; push devices provide full usage and are the
recommended path.

## Usage

Manage everything at `https://<comet-ip>/extras/ai-usage/`:

- **Display provider** — choose which subscription the touchscreen shows (Claude, Codex, …),
  each with its own brand colors and working-glyph animation.
- **Appearance** — customize the selected provider's wallpaper colors, working-glyph style, and
  whether limit rows lead with percent used or time to reset, with an instant live preview;
  themes are validated JSON stored on the KVM (export/import supported, one-click reset).
- **Layouts** — pick a wallpaper arrangement (Classic, Detailed with a 7-day sparkline and
  reset countdown, Compact with a clock, Multi-agent showing every provider's usage at once) or
  build a custom one by assigning widgets — limit bars, token totals, sparkline, countdown,
  clock, provider grid, plan — to named slots.
- **Push devices** — enroll, rotate secrets, revoke, or delete devices; last-seen times shown.
- **Display settings** — enable/disable the wallpaper, working animation, and refresh interval.
  A live wallpaper preview and health status are on the same page.
- **Updates** — the page shows the agent version and checks GitHub releases on demand; update
  with `npx github:ivangong24/kvm_AI_monitor install-agent` (or `brew upgrade` + the same).

The wallpaper shows current-session and weekly limit bars with reset times, today's and
30-day token totals, and animates while the selected agent is working on any enrolled device
(120-second activity window; exact per-turn state when Claude hooks are installed). Usage data
is retained while a device is offline; the animation pauses during active remote viewing
(`pauseWhenStreaming`).

A menu bar companion app for macOS (enrollment health, one-click actions) can be built with
`./desktop/build.sh` — see [`desktop/README.md`](desktop/README.md).

## Privacy

Push payloads contain only plan label, quota percentages, reset times, and daily token
counts — never prompts, responses, paths, project names, emails, or credentials. Every push is
HMAC-signed with a revocable per-device secret; the OAuth token used to read Claude's account
limits stays in memory on the device that owns it. Inspect exactly what would be sent with
`npm run helper:status`. Full protocol: [`docs/PUSH_PROTOCOL.md`](docs/PUSH_PROTOCOL.md).

## Uninstall

```bash
npm run kvm:agent:uninstall    # remove the KVM extension (config preserved on the KVM)
npm run helper:uninstall       # remove this Mac's helper (--purge also removes secrets)
```

## Development

```bash
npm test                          # Node: Comet client, CLI, cross-language HMAC vector
python3 mac-helper/test_helper.py # helper unit tests (also run on Linux/Windows in CI)
python3 kvm-agent/test_push_receiver.py
python3 kvm-agent/test_ssh_collector.py
```

CI runs the suite on macOS, Ubuntu, and Windows. Design history and device internals are in
[`docs/PROJECT_CHECKPOINT_2026-07-18.md`](docs/PROJECT_CHECKPOINT_2026-07-18.md); future
directions in [`docs/ROADMAP.md`](docs/ROADMAP.md).
