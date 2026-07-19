# KVM AI Monitor — menu bar companion

A small macOS menu bar app (macOS 13+) that shows this Mac's enrollment and push health and
offers one-click actions:

- Open each configured KVM's AI Usage page.
- Send a usage push now.
- Run the guided setup wizard (`npx github:ivangong24/kvm_AI_monitor`) in Terminal.
- Show whether the helper LaunchAgent is scheduled and when it last pushed.

It reads the same files the CLI and helper use (`~/.kvm-ai-monitor`, the helper LaunchAgent,
`/tmp/kvm-ai-helper.log`) and stores no credentials of its own.

## Build

```bash
./desktop/build.sh
open "desktop/dist/KVM AI Monitor.app"
```

Requires the Xcode Command Line Tools. The build is ad-hoc signed, which is fine for the Mac it
was built on. Launch it at login by adding it to System Settings → General → Login Items.

## Distributing (signing and notarization)

To ship the app to other Macs (directly or via a Homebrew cask), it must be Developer ID signed
and notarized with an Apple Developer account:

```bash
codesign --force --options runtime --sign "Developer ID Application: <name>" "desktop/dist/KVM AI Monitor.app"
ditto -c -k --keepParent "desktop/dist/KVM AI Monitor.app" "KVM-AI-Monitor.zip"
xcrun notarytool submit KVM-AI-Monitor.zip --keychain-profile <profile> --wait
xcrun stapler staple "desktop/dist/KVM AI Monitor.app"
```

Until then, the supported cross-Mac installation paths are the CLI (`npx` or Homebrew formula).
