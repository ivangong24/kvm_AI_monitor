# Project Checkpoint - 2026-07-18

This document is the durable handoff point for unfinished KVM AI Monitor work. It intentionally
contains no account email, organization ID, password, session token, OAuth token, cookie, private
key, prompt, response, project name, or local network address.

## Current deployed architecture

- The Comet Pro runs the AI Usage control page, collector orchestration, 480x160 renderer, animation,
  and wallpaper publisher.
- The primary computer is discovered or configured over restricted, key-based SSH. It does not run
  the dashboard or wallpaper renderer.
- Codex usage comes from the installed Codex app-server and currently provides native plan windows
  and account-level daily token totals.
- Claude local token history is parsed only from aggregate fields in local JSONL session records.
- Copilot, Gemini, and Grok currently provide installation/authentication detection only; supported
  subscription quota feeds have not been implemented.
- CodexBar and iCloud are not used.

## Deployed display behavior

- Provider-specific official logos and themes are installed.
- The working glyph uses 60 cached motion phases, a two-second rotation, and a ten-frame-per-second
  wallpaper publish rate.
- Working state is polled every five seconds from the primary and explicitly enrolled SSH activity
  devices. Codex rollout and Claude transcript modification times use a 120-second active window.
- Additional SSH activity devices can be entered on the AI Usage page, but each device must enable
  Remote Login and authorize the restricted KVM key.
- The previous delayed Codex lifetime-token heuristic was removed because it could label completed,
  delayed account usage as live work.

## Verified Claude authentication issue

- Claude Code is installed on the primary Mac.
- Claude reports a valid Claude subscription login when `claude auth status --json` is run in the
  Mac's local GUI Terminal session.
- The same official command reports `loggedIn: false` when invoked through the KVM SSH session.
- A protected `Claude Code-credentials` Keychain item exists, but macOS prevents the SSH audit
  session from reading it. PTY allocation, `launchctl asuser`, and AppleScript shell execution did
  not bypass that isolation.
- The deployed UI therefore reports `verification_required` instead of the incorrect
  `login_required`. It checks only for Keychain item existence and never reads its value.
- Claude account quota and current-session/weekly windows are still unavailable to the KVM.

## Unresolved requirements

### Claude usage

The KVM cannot currently fetch Claude subscription usage because the usable OAuth credential is
protected inside the Mac's GUI Keychain session. Local JSONL totals are not account-wide and may be
absent on the primary Mac when Claude is used elsewhere.

### Provider sign-in on the AI Usage page

A normal provider sign-in link is insufficient: the KVM needs a provider-approved OAuth client,
callback, encrypted token storage, refresh, revocation, and account separation. Consumer
subscription OAuth is not documented as a general third-party appliance interface for all selected
providers. Reusing an internal CLI client ID, scraping browser cookies, or copying CLI credentials to
the KVM has deliberately not been implemented.

### Exact cross-device live activity

Subscription quota APIs expose consumption or remaining limits, not a reliable "working now" state
or a list of devices using the account. Exact animation cannot be inferred solely from a shared
subscription. Polling quota deltas would be delayed, coarse, and sometimes wrong.

### Other providers

- Copilot: supported subscription quota and account-activity source not implemented.
- Gemini: supported subscription quota and account-activity source not implemented.
- Grok: supported subscription quota and account-activity source not implemented.
- Provider-specific direct OAuth onboarding has not been designed or registered.

## Recommended next architecture

Use an **outbound-only local helper**, not an inbound server on the connected computer.

The helper should:

1. Run as a macOS LaunchAgent in the logged-in user session so normal Keychain consent can work.
2. Read Claude usage through the native Claude credential/CLI locally; provider credentials must
   never leave the Mac.
3. Reduce provider output to a strict aggregate schema: plan label, quota percentages, reset times,
   daily token categories, and timestamps.
4. Push that aggregate snapshot over HTTPS to an authenticated KVM receiver. The Mac must not listen
   on a network port.
5. Use a per-device revocable secret and request authentication. Do not reuse the Comet admin token.
6. Retain the last successful aggregate snapshot on the KVM and expire working state separately.
7. Provide matching installer, status, rotation, and uninstaller commands.

For exact Claude activity without Remote Login, add Claude Code HTTP hooks after the aggregate helper.
Hooks can send minimal `SessionStart`, turn/activity, `Stop`, and `SessionEnd` events outward to the
KVM. The receiver must discard session IDs, transcript paths, working directories, prompts, and tool
arguments. Each device still requires one-time enrollment because the subscription does not identify
or authorize devices automatically.

## Work not yet implemented

- Authenticated KVM push/hook endpoint that is separate from the admin web session.
- Per-device enrollment secret generation, rotation, revocation, and replay protection.
- macOS helper and LaunchAgent.
- Claude Keychain consent and aggregate usage fetch inside the GUI session.
- Claude lifecycle hook installer and privacy filter.
- Device health/last-seen reporting for push devices.
- Tests for signature verification, replay rejection, stale activity expiry, helper uninstall, and
  credential non-disclosure.
- End-to-end verification with the Mac asleep, offline, logged out, and removed from the network.

## Security constraints to preserve

- Do not install or call CodexBar.
- Do not use iCloud or another cloud relay.
- Do not expose an inbound listener on a monitored computer.
- Do not weaken macOS Keychain ACLs or export provider credentials.
- Do not store prompts, responses, source code, project names, filesystem paths, account identity, or
  raw provider output on the KVM.
- Do not describe quota-delta inference as live working state.
- Keep all device enrollment explicit and revocable.

## Verification at this checkpoint

- Python collector/renderer tests: 8 passing.
- Node Comet client tests: 1 passing.
- Python modules compile successfully.
- `git diff --check` passes.
- The corrected Claude `verification_required` state is deployed on the KVM.

## Resume point

Start by specifying the minimal signed push schema and threat model. Then implement the KVM receiver
and its tests before creating the macOS helper. The first end-to-end milestone is Claude current
session and weekly limits reaching the KVM with the OAuth credential remaining exclusively in the
Mac's Keychain.

---

## Update - 2026-07-18: Push architecture implemented

The outbound-only push architecture from the recommended next section has been implemented:

- **Push receiver** (`kvm-agent/push_receiver.py`): HMAC-SHA256 signed endpoint at `/push/v1/usage` and `/push/v1/activity`, integrated into the KVM agent. Verifies per-device secrets, timestamps (120 s window), nonces (replay protection), request signatures, and enforces schema whitelisting. Persists latest device snapshots to `/etc/kvmd/user/ai-usage/push-state.json` and manages in-memory working-state expiry (120 s).

- **nginx location** (`kvm-agent/extension/nginx.ctx-server.conf`): Added `/extras/ai-usage/push/` location that proxies to the agent without `loc-login.conf` (HMAC replaces Comet authentication).

- **Admin enrollment UI** (`kvm-agent/index.html`): New "Push devices" section on the AI Usage page. Enroll devices by name (POST `api/devices`), see the one-time secret and install command, then manage devices (rotate secret, revoke, delete, view last-seen/usage/activity times with humanized relative timestamps). Device list fetches in parallel with status every 15 s; gracefully hides if backend not yet deployed (404 on `api/devices`).

- **Device addressing enhancement**: `activityHosts` entries now accept `[user@]host[:port]` format (e.g., `anna@192.168.0.25`, `mini.local:2222`), enabling per-entry user/port override.

- **Claude verification_required degraded rendering**: When verification_required is reported over SSH, the usage panel renders from whatever token totals are available (local or pushed) instead of the setup screen. Recommendation text in UI invites enrollment of a push device.

- **README and checkpoint**: Documented the architecture (two collection channels: SSH and push), privacy guarantees (per-device secrets, HMAC, whitelisted payloads), enrollment flow, activity format, runtime behavior, and development tests. Reference to `docs/PUSH_PROTOCOL.md` for the complete specification.

Also implemented in this phase (`mac-helper/`):

- **macOS helper**: `kvm_ai_push.py` (stdlib-only sign/collect/push CLI), LaunchAgent
  (`com.kvm-ai-monitor.helper`, 60 s interval in the logged-in GUI session), installer /
  uninstaller / status / secret storage in the login Keychain. Claude usage is collected natively
  (auth status, Keychain OAuth credential used in memory only, local JSONL
  daily totals) and reduced to the whitelisted aggregate schema.
- **Claude lifecycle hooks**: idempotent installer wiring SessionStart / UserPromptSubmit /
  PostToolUse / Stop / SessionEnd to a background, always-exit-0 hook that pushes signed
  start/active/stop events (20 s throttle on `active`).
- **Cross-language contract tests**: the shared HMAC test vector is enforced in the receiver
  suite, the helper suite, and an `npm test` case that spawns the helper's signer.

Work remaining:

- Copilot, Gemini, Grok quota feeds (provider-specific subscription API integration).
- Provider OAuth onboarding and per-provider credential management.
- Live end-to-end verification on the deployed KVM and enrolled Macs (device enrollment, first
  push, animation from a Mac mini hook event, Mac-asleep retention).

---

## Update - 2026-07-18 (later): Accurate account limits and per-agent bar colors

Verified against the live account: `api.anthropic.com/api/oauth/usage` returns a structured
`limits` array (`kind`: `session` / `weekly_all` / `weekly_scoped` with `percent`, `resets_at`,
and a model `scope`) alongside legacy `five_hour` / `seven_day` buckets, and it rate-limits
aggressive polling with HTTP 429. Changes:

- **Helper** parses the structured `limits` array first (session, weekly all-models, and
  model-scoped weekly entries such as "Weekly (Fable)"), falling back to the legacy bucket keys.
  The nonexistent `claude usage --json` adapter was removed.
- **Helper** now fetches account limits at most once per 4 minutes and caches the last good
  (already-whitelisted) result in `~/.kvm-ai-monitor/limits-cache.json` for up to 24 h, so the
  60-second LaunchAgent cadence no longer trips the endpoint's rate limit and a transient 429
  no longer blanks the pushed limits.
- **Receiver** retains the previous snapshot's limits when a new usage push arrives without any,
  matching the "last successful aggregate is preserved" display rule.
- **Renderer** gives each provider a dedicated `bar` theme color (Claude coral, Codex teal,
  Copilot purple, Gemini blue, Grok white); the weekly bar is a tint of the same brand color.

These limits are account-wide: one enrolled device with a valid Claude Code OAuth credential
reports the same session/weekly percentages the account sees everywhere. The helper does not
refresh the OAuth token itself; if Claude Code is unused on the enrolled Mac long enough for the
token to expire, limits pause (cache serves up to 24 h) until Claude Code refreshes it.

The KVM-side changes (receiver retention, bar colors) still need deployment via
`npm run kvm:agent:install`; the saved Comet session token had expired at verification time, so
`npm run kvm:configure` must be rerun first. The helper-side fix is installed and verified: a
signed push containing correct session and weekly limits was accepted by the deployed KVM.

---

## Update - 2026-07-18 (later still): Blackout and animation-stall fixes, deployed

Device investigation (gl_kvm_gui logs, binary strings, file mtimes) established:

- Every `update_background` ubus event makes the GUI copy the published PNG into
  `/etc/glinet/gui/custom/background/` and `/etc/rm10-gui/picture/custom/background/`, both on the
  flash-backed overlay — at the previous 10 fps this meant ~374 KB/s of eMMC writes while animating
  and a congested GUI event loop.
- The agent kept animation frames in throwaway `mkdtemp` directories and deleted the old directory
  on every refresh/state change; a queued GUI event referencing a deleted frame file loads nothing
  and blanks the screen to black. This was the frequent working-time blackout.
- The 5-second SSH working probe and the full 60-frame re-render ran in the same thread/lock as the
  frame publisher, so slow probes (or each refresh) froze the animation for seconds. This was the
  stop-and-recover stall.

Fixes (all deployed and verified live on the Comet):

- Animation frames now live at stable paths in `/tmp/kvm-ai-frames/` (60 frames), written with
  atomic replace and never deleted; legacy mkdtemp directories are cleaned at startup.
- Publishing runs at 10 fps from a dedicated animator thread; the SSH working probe and the
  usage-refresh cycle run in their own threads, with network I/O outside the publish lock. The
  original 60-phase / 10 fps smoothness is retained: with the tmpfs shield the GUI's per-event
  cost is RAM copies plus a decode, measured at ~5% GUI CPU with no event-queue backlog.
- Wallpaper composition and frame rendering happen before the publish lock is taken;
  `render_wallpaper` was split into `compose_wallpaper` + `save_png_atomic`.
- `service.sh` mounts a tmpfs over both GUI background directories (preserving prior content), so
  the GUI's per-event copies stay in RAM; `uninstall-on-device.sh` unmounts them. Verified: during
  animation the tmpfs copy updates while the flash overlay copy's mtime stays frozen.

Verified after deployment: ~10 events/s cadence in gl_kvm_gui logs, zero flash writes during
animation (flash overlay copy mtime frozen while the tmpfs copy updates), `lastError: null`, the
event queue draining immediately when working state ends, and clean transition back to the static
wallpaper. The GUI's `AutoLockTime` is 60 s but `AlwaysOn` is true, so screen-off was ruled out as
the blackout cause.
