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

To ship the app without a Gatekeeper warning, set `KVM_CODESIGN_IDENTITY` to a Developer ID
Application identity while building, then notarize with an Apple Developer account:

```bash
KVM_CODESIGN_IDENTITY="Developer ID Application: <name>" ./desktop/package-release.sh
xcrun notarytool submit desktop/dist/KVM-AI-Monitor-v*.zip --keychain-profile <profile> --wait
xcrun stapler staple "desktop/dist/KVM AI Monitor.app"
```

The tag release workflow builds and attaches `KVM-AI-Monitor-v<version>.zip`, which is the asset
the cask installs. For a public release, configure signing/notarization in that workflow before
publishing the tag.
