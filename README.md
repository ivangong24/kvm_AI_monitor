# KVM AI Monitor

The project is feature-complete for its supported providers; the design history and current
status are recorded in
[`docs/PROJECT_CHECKPOINT_2026-07-18.md`](docs/PROJECT_CHECKPOINT_2026-07-18.md).

KVM AI Monitor is a GL.iNet Comet Pro extension that discovers an authorized computer, reads AI
subscription usage over SSH, and renders a 480x160 wallpaper entirely on the KVM. No computer runs a
dashboard, telemetry server, scheduled exporter, or wallpaper renderer, and the display continues to
operate without an HDMI source.

## Architecture

The persistent agent at `/etc/kvmd/user/ai-usage` owns the complete display pipeline. Usage is collected via two channels:

1. **SSH collector** (primary): The KVM generates a dedicated Ed25519 key and scans its local subnet for a device that accepts it. The KVM opens a short, noninteractive SSH session and runs a read-only in-memory collector. The collector detects Claude Code, Codex, GitHub Copilot, Gemini CLI, and Grok installations and invokes supported read-only interfaces in the installed provider CLI or desktop bundle.

2. **Push devices** (outbound-only): Enrolled Macs run a local helper in the GUI session that reads Claude usage natively via Keychain consent, signs the aggregate schema with a per-device secret, and POSTs it over HTTPS to an unauthenticated-but-signed KVM endpoint. Push devices do not require Remote Login or a network-facing listener.

The KVM accepts only whitelisted aggregate fields, renders the selected provider wallpaper, and publishes it through the Comet GUI. The connected computer's Remote Login remains the only inbound server; there is no KVM AI Monitor process running on that computer. Automatic discovery selects only a host that accepts the KVM's dedicated key, so an address change does not require reconfiguration.

## Privacy

- The KVM key is generated and stored only on the KVM with mode `0600`.
- Each push device gets a revocable per-device secret (48 hex chars) stored on the KVM and in the device's Keychain.
- Push requests are signed with HMAC-SHA256; the signature includes the request path, method, timestamp, and body hash.
- Passwords, tokens, provider credentials, prompts, responses, source code, session records, project
  names, filesystem paths, account emails, and raw provider output are not returned or persisted.
- Push payloads contain only: plan label, quota percentages, reset times, daily token categories by date, and timestamps. Session IDs, transcript paths, working directories, and prompts are discarded at ingest.
- Authentication files are checked only for existence; their contents are never opened.
- Only quota percentages, reset times, daily token categories, equivalent cost totals, installation
  booleans, and aggregate working booleans enter the display snapshot.
- There is no iCloud sync and no cross-device usage exporter. Additional activity devices are polled
  directly by the KVM and return no account identity, prompt, response, project, or path data.

Codex's native app-server supplies account-wide quota windows and daily token buckets. Claude Code's
native logs supply connected-device token totals; Anthropic does not expose a documented equivalent
account-wide token-history command. Push device usage is collected natively on the Mac via Keychain
without SSH. API-equivalent cost is not estimated.

## Install

Requirements for deployment are Node.js 22 or newer on the setup Mac. The Comet agent itself uses the
firmware's Python, Dropbear SSH client, Pillow, nginx extension support, and persistent user scripts.

```bash
git clone git@github.com:ivangong24/kvm_AI_monitor.git
cd kvm_AI_monitor
npm run kvm:configure
npm run kvm:agent:install
```

`kvm:configure` asks for the Comet admin password and current 2FA code. It saves the resulting
revocable Comet session token in macOS Keychain and immediately deletes the admin password.

### Primary device with SSH

1. On the computer to monitor, enable **System Settings > General > Sharing > Remote Login**.
2. Open `https://<comet-address>/extras/ai-usage/` after signing in to the Comet.
3. Copy the displayed KVM public key into `~/.ssh/authorized_keys` for that macOS user. Restrict it to
   the KVM address and disable forwarding and PTY allocation where practical.
4. Enter the macOS username, leave the device address as `auto`, and select **Save**.
5. Under the Comet's **Settings > System > Screen Display**, choose **Wallpaper Only** and apply it.

### Alternate: push device without Remote Login

Instead of or in addition to SSH, enroll a push device on the AI Usage page:

1. Enter a device name (e.g., "Mac mini") and select **Add device**.
2. Copy the install command and device secret.
3. On the enrolled Mac, run: `./mac-helper/install-helper.sh --kvm <comet-address> --device <id>` (paste the secret when prompted).
4. Optionally, for exact working-state animation without Remote Login, also run: `./mac-helper/install-claude-hooks.sh`.

Push devices do not require Remote Login and run only in the logged-in GUI session. You can revoke,
rotate the secret, or delete a device at any time from the AI Usage page.

### Additional activity devices

To include live activity from another device using the same subscription, enable Remote Login there,
authorize the same restricted KVM key, and add its address under **Additional activity devices** (format: `[user@]host[:port]`, e.g., `anna@192.168.0.25` or `mini.local:2222`). The primary connected device remains the only source for installation, authentication, and usage totals.

The AI Usage page provides provider selection, connected-device diagnostics, refresh controls, status,
and a wallpaper preview. The wallpaper is retained if the device is temporarily offline.

CodexBar is not read or invoked. Codex uses the installed CLI or the helper embedded in Codex.app.
Claude requires Claude Code CLI authentication (`claude auth login`) for native account state; the
consumer Claude desktop app does not expose the same read-only usage interface. When a push device is
enrolled and pushing usage, Claude's plan, limits, and daily token totals come from the device helper
without requiring the SSH probe to read the Keychain. Account quota limits therefore arrive via the
device helper running in the GUI session. Other providers are reported as installed and authenticated
only when their native artifacts support that detection. When Claude reports `verification_required`
over SSH, the display still renders any locally collected token totals instead of the setup screen.
The agent checks the protected `Claude Code-credentials` Keychain item only for existence and never
reads or copies its protected value. The complete push protocol — enrollment, HMAC signing, replay
protection, payload whitelist, and merge rules — is specified in
[`docs/PUSH_PROTOCOL.md`](docs/PUSH_PROTOCOL.md); the helper's commands are documented in
[`mac-helper/README.md`](mac-helper/README.md).

## Runtime

The agent refreshes once per minute by default. Its configuration is stored on the KVM at
`/etc/kvmd/user/ai-usage/config.json`; its private key is
`/etc/kvmd/user/ai-usage/device-key`. The KVM performs a lightweight activity probe over SSH every
five seconds, independently of the slower account refresh. The probe combines agent process state
with Codex rollout and Claude transcript modification times using a 120-second active window. Push
devices send activity events (`start` / `active` / `stop`) that also trigger animation with a 120-second expiry;
push-sourced work is distinct from SSH-sourced work. While
the selected native agent is working on the primary or an enrolled activity device or any push device,
the indicator uses 60 cached motion phases with a two-second rotation and publishes at 10 frames per
second, which the GUI event loop sustains once its background copies are kept off flash. The KVM's
primary function always wins: the agent runs at nice 15, animation frames are cached and only
re-rendered when the wallpaper content changes, and while anyone is actively viewing the web
console's video stream the animation pauses on the static wallpaper (`pauseWhenStreaming`, on by
default) so remote view and control never compete with wallpaper publishing. Animation frames live at
stable paths under `/tmp/kvm-ai-frames/` and are only ever replaced atomically, never deleted, so a
queued GUI event can never reference a missing file (which would blank the screen). The Comet GUI
copies every published background into two directories on its flash-backed overlay; the agent's
service script mounts a tmpfs over both so animation does not wear the eMMC or slow the GUI event
loop (the uninstaller unmounts them, restoring stock behavior). Activity probing, usage collection,
and frame publishing run in separate threads, so a slow SSH probe cannot pause the animation.

Push device usage is retained when the device goes offline; only working state expires. Last successful
aggregates from the device are preserved in the display until deleted or revoked.

No provider currently exposes a supported subscription API for real-time work occurring on arbitrary
devices. Cross-device animation therefore covers only explicitly enrolled, online SSH devices and push
devices with active working events. The remote SSH command is ephemeral and read-only; CodexBar, a daemon, exporter, renderer, and scheduler are
not installed or invoked on those devices.

Remove the KVM extension while preserving its configuration with:

```bash
npm run kvm:agent:uninstall
```

## Development

```bash
python3 kvm-agent/test_ssh_collector.py
python3 kvm-agent/test_push_receiver.py
python3 mac-helper/test_helper.py
npm test
```

`npm test` covers Comet authentication payload handling. The Python tests verify aggregate conversion,
HMAC signature verification, replay protection, stale activity expiry, and ensure sensitive or
content-bearing fields are dropped at the push receiver. The mac-helper tests verify the native
Claude usage parsing and schema compliance.
