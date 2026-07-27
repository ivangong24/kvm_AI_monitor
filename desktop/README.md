# KVM AI Monitor — menu bar companion

A native macOS menu bar control center (macOS 13+) with a compact status dashboard, Comet cards,
push health, and one-click actions:

- Open each configured KVM's AI Usage page.
- Send a usage push now.
- Run the guided setup wizard in Terminal.
- Show whether the helper LaunchAgent is scheduled and when it last pushed.
- Start the companion automatically at login.

It reads the same files the CLI and helper use (`~/.kvm-ai-monitor`, the helper LaunchAgent,
`/tmp/kvm-ai-helper.log`) and stores no credentials of its own.

## Build

```bash
./desktop/build.sh
open "desktop/dist/KVM AI Monitor.app"
```

Requires Xcode or the Xcode Command Line Tools. The build is universal (Apple Silicon + Intel)
and ad-hoc signed, which is suitable for the Mac it was built on.

## Install with Homebrew

The cask lives in the same personal tap as the CLI formula. After the cask has been copied from
`packaging/homebrew/Casks/kvm-ai-monitor.rb` into that tap and the matching release archive has
been published:

```bash
brew tap ivangong24/kvm-ai-monitor
brew install --cask kvm-ai-monitor
```

Updates and removal use the standard cask commands:

```bash
brew upgrade --cask kvm-ai-monitor
brew uninstall --cask kvm-ai-monitor
```

## Distributing (signing and notarization)

Signing needs the paid **Apple Developer Program** ($99/yr) for a *Developer ID Application*
certificate. The build already signs with the hardened runtime, a secure timestamp, and
`entitlements.plist` (Apple-events, for the terminal-automation setup) whenever
`KVM_CODESIGN_IDENTITY` is set — everything notarization requires.

Locally:

```bash
KVM_CODESIGN_IDENTITY="Developer ID Application: <name> (<TEAMID>)" ./desktop/package-release.sh
xcrun notarytool submit desktop/dist/KVM-AI-Monitor-v*.zip --keychain-profile <profile> --wait
xcrun stapler staple "desktop/dist/KVM AI Monitor.app"
```

### CI (automatic on every tag)

The `Release macOS companion` workflow signs and notarizes automatically **once these repo secrets
exist** (Settings → Secrets and variables → Actions). Until they do, every signing step is skipped
and the build ships ad-hoc signed, exactly as before — so adding them is the only step needed to
turn on notarized releases.

| Secret | What it is |
| --- | --- |
| `MACOS_CERTIFICATE` | base64 of the Developer ID Application cert exported as `.p12` (`base64 -i cert.p12`) |
| `MACOS_CERTIFICATE_PWD` | the password you set when exporting that `.p12` |
| `MACOS_SIGN_IDENTITY` | the identity name, e.g. `Developer ID Application: Your Name (TEAMID)` |
| `AC_API_KEY_ID` | App Store Connect API key ID (Users and Access → Integrations → App Store Connect API) |
| `AC_API_ISSUER_ID` | the issuer ID shown on that same page |
| `AC_API_KEY_P8` | base64 of the downloaded `AuthKey_<id>.p8` |

The App Store Connect API key needs only the **Developer** role for notarization. The workflow
imports the cert into a throwaway keychain, builds with `KVM_CODESIGN_IDENTITY`, submits the zip to
`notarytool --wait`, staples the ticket into the app, and re-zips the stapled bundle as the release
asset the cask installs.
