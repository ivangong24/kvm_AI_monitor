# KVM AI Monitor — Future Directions

Status: proposal only. Nothing in this document is committed work; it exists to capture agreed
directions for the next development phases. The current version is feature-complete and
deployable to any unconfigured Comet Pro of the same model (see "Deployability baseline").

## Deployability baseline (verified 2026-07-18)

The shipped scripts contain no machine-specific values. A fresh installation needs:

1. A Comet Pro with its admin password (and current 2FA code if enabled), reachable on the LAN.
2. A macOS setup machine with Node.js 22+ (`git clone` → `npm run kvm:configure` →
   `npm run kvm:agent:install`).
3. One manual firmware step: Settings → System → Screen Display → Wallpaper Only.
4. Per monitored Mac: either Remote Login + the KVM key (SSH path) or the push-device helper
   (`./helper/install-helper.sh`), plus optional Claude hooks.

Known constraints to address in future work rather than today: the CLI tooling manages a single
Comet per setup computer (`~/.kvm-ai-monitor/kvm-host` holds one address and the helper pushes to
one `kvmHost` — note this does **not** limit how many computers one Comet monitors, which already
works); the setup machine and helper now run on macOS, Windows, and Linux (Keychain / Credential
Manager / libsecret and LaunchAgent / Task Scheduler / systemd user timer); the firmware
paths (`/etc/kvmd/user`, extras/nginx extension, ubus `custom_screen`, ttyd web terminal, GUI
background directories) are verified against the current RM10 firmware and should be re-verified
after firmware updates.

---

## 1. Zero-terminal onboarding (highest impact)

> **Status (v1.1.0):** largely shipped — `kvm-ai-monitor` guided CLI (discovery, authorize,
> agent install, automatic Wallpaper Only, one-step enrollment), first-run wizard on the AI
> Usage page, browser-side update check against GitHub releases, multi-KVM registry and
> multi-target helper, and a menu bar companion app. Remaining: on-KVM one-click self-update
> download, native-dialog setup app replacing the Terminal step.

Goal: a user who has never opened a terminal can go from unboxing to a working wallpaper.

- **Native setup app (macOS menu bar app).** One "Set up my KVM" flow that:
  - Discovers the Comet on the LAN automatically (the firmware runs mDNS responders; verify the
    advertised service type, fall back to subnet scan of the admin port).
  - Asks for the admin password/2FA in a native dialog, performs the existing authorize flow
    internally, and stores the session token in Keychain exactly as today.
  - Pushes the agent bundle through the web terminal channel (same mechanism as
    `install-kvm-agent.sh`, reimplemented in the app).
  - Sets "Wallpaper Only" automatically via the firmware API/ubus (`update_screen_mode` exists in
    the GUI binary; verify the corresponding admin API) — removing the last manual step.
  - Enrolls the Mac it is running on as a push device in the same flow: create the device via
    `api/devices`, store the secret in Keychain, install the helper LaunchAgent and Claude hooks.
  - Shows a live wallpaper preview and a green/red health checklist at the end.
- **First-run wizard on the AI Usage page.** For users who start from the Comet web console:
  step-by-step cards (choose provider → enroll this Mac → copy one command or scan a QR code that
  deep-links the desktop app) instead of the current single dense page.
- **Self-update.** The agent checks a GitHub release feed (signature-verified tarball) and offers
  one-click update from the AI Usage page; the desktop app updates itself via Sparkle/standard
  channels. Removes the need to ever re-run installers.
- **Multi-KVM support** (enabler): replace the single `kvm-host` file and single-target helper
  config with a list of enrolled KVMs; the helper signs and pushes to each.

## 2. Distribution and installation options

> **Status (v1.1.0):** shipped — `npx github:ivangong24/kvm_AI_monitor` one-shot, Homebrew tap
> (`brew install ivangong24/kvm-ai-monitor/kvm-ai-monitor`), and a buildable menu bar app
> (`desktop/build.sh`, ad-hoc signed). Remaining: Developer ID signing + notarization for a
> downloadable .dmg / Homebrew cask, and any GL.iNet marketplace channel.

- **Homebrew**: `brew install --cask kvm-ai-monitor` delivering the menu bar app; a `--formula`
  CLI-only variant for headless/scripted setups. Requires codesigning + notarization.
- **Signed .dmg download** for users without Homebrew (same app bundle, Developer ID signed,
  notarized; Sparkle for updates).
- **`npx kvm-ai-monitor`** one-shot for CLI-comfortable users — no clone, prompts inline.
- **Implementation note**: fold the setup logic (currently zsh + Node scripts) into one
  distributable core (either a Swift implementation inside the app, or a bundled Node/Bun single
  binary) so all channels share one code path. The Python helper stays a standalone stdlib file —
  it already has no dependencies and can be embedded in the app bundle.
- Longer shot: publish through GL.iNet's extension/marketplace channel if one becomes available
  for the Comet line, which would remove the setup computer entirely.

## 3. Visual refinement of the current design

- **Wallpaper rendering**: bundle a proper display font (e.g., Inter or IBM Plex, subset to the
  glyphs used) instead of firmware DejaVu; render at 2x and downsample for crisper text (the
  glyph already does this — extend to the whole canvas); rounded progress bars with subtle
  track/border contrast; tightened spacing grid; refined setup/error screens so even
  "verification required" looks intentional; small polish like thousands separators and
  reset-time phrasing.
- **AI Usage page**: modern layout (cards, consistent spacing scale, dark mode following
  `prefers-color-scheme`), a large live wallpaper preview with provider switcher, toast
  notifications instead of inline error text, device cards showing last-push freshness at a
  glance, and mobile-friendly responsive behavior for phone-based setup.
- Keep the existing constraint: everything renders on-device at 480x160; no external assets at
  runtime.

## 4. Themes, widgets, and user customization

> **Status (v1.5.0): complete.** Theme system and editor (v1.4.0): per-provider color editing
> with live rendered preview, glyph-style selection, percent vs. time-to-reset emphasis, JSON
> import/export, strict validation with built-in fallback, instant republish. Widget/layout
> system (v1.5.0): seven widgets (limit bars, token totals, 7-day sparkline, reset countdown,
> clock, all-provider grid, plan) rendered from data-driven placements; four presets (Classic,
> Detailed, Compact, Multi-agent) plus custom slot-based arrangements from the page, all through
> the same sanitizer, with the brand/status header fixed so the animation pipeline is untouched.

- **Theme system**: extract the hardcoded `PROVIDERS` styling into versioned JSON theme files on
  the KVM (colors, bar colors, fonts, background treatment per provider). Ship the current look
  as the default theme set. The AI Usage page gets a theme editor with live rendered preview
  (the agent already exposes rendering; add a `?preview=` endpoint that renders a candidate theme
  without publishing). Import/export as a single JSON for sharing.
- **Widget/layout system**: decompose the fixed layout into widgets placed on a slot grid —
  limits bars, today/30-day tokens, 7-day sparkline, cost equivalent, reset countdown, working
  glyph, clock, multi-provider mini-grid. Users pick a layout (compact / detailed / multi-agent)
  or arrange widgets in the web UI; the agent renders from the layout description. Graceful
  fallback to the built-in layout if a custom one fails validation.
- **Display options**: font family/size choices from a bundled set; per-provider animation style
  selection (offer each provider's glyph as a choice); provider auto-rotation on an interval;
  scheduled night dimming (the GUI exposes brightness); percentage vs. remaining-time emphasis.
- **Safety rails**: themes/layouts are data, not code — strict schema validation on the KVM, no
  file paths or commands in theme JSON, size-capped, and the default theme always available.

## 5. Cross-platform support (Linux and Windows)

> **Status (v1.2.0):** helper port shipped — platform secret backends (Keychain / libsecret /
> DPAPI / file fallback), `~/.claude/.credentials.json` credential source, systemd-timer and
> Task Scheduler installers, Windows hook shim, and a macOS/Ubuntu/Windows CI matrix.
> Remaining: cross-platform *setup* tooling (the wizard still assumes a macOS setup machine).

Goal: monitored devices and the setup machine can run Linux or Windows, not just macOS.

- **Helper port (highest value, lowest effort).** The push helper is a single stdlib Python file;
  the macOS-specific pieces are isolated and each has a per-OS equivalent:
  - Push-secret storage: Keychain → libsecret/`secret-tool` (Linux), Windows Credential Manager
    via `keyring`-style APIs (Windows), with an encrypted-file fallback where no vault exists.
  - Claude credential source: on Linux and Windows, Claude Code stores credentials in
    `~/.claude/.credentials.json` instead of a keychain — simpler than macOS (verify current
    location per platform at implementation time).
  - Scheduling: LaunchAgent → systemd user timer (Linux), Task Scheduler (Windows).
  - Claude hooks are already cross-platform (Claude Code `settings.json`); the hook shim needs a
    `.cmd`/PowerShell variant on Windows.
- **Setup tooling.** Reimplement `configure-kvm.sh` (zsh + `security`) in the shared setup core
  from section 2 so it runs on all three platforms; the desktop app ships as a cross-platform
  build (Tauri/Electron) or per-OS native apps. `install-kvm-agent.sh` and the web-terminal
  channel are already portable Node/POSIX logic.
- **SSH activity devices.** The remote probe already works against Linux hosts with Remote
  Login/sshd; verify the probed file paths per platform and document per-OS enrollment. Windows
  activity probing arrives with the Windows helper (hooks/push) rather than SSH.
- **Documentation matrix.** Per-OS install pages; CI running the helper test suite on macOS,
  Linux, and Windows.

## Suggested sequencing

1. Multi-KVM config + self-update plumbing (enables everything else to ship incrementally).
2. Linux/Windows helper port (small, isolated, immediately widens who can enroll devices).
3. Theme extraction to JSON + page dark mode (small, immediately visible, unblocks the editor).
4. Cross-platform setup app with auto-discovery and one-click enrollment (largest UX win).
5. Homebrew/DMG and per-OS distribution of that app.
6. Widget/layout system and the theme editor UI.
7. Wallpaper typography/rendering polish alongside the widget work (shared rendering changes).
