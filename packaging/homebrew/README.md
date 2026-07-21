# Homebrew packaging

The CLI formula lives in `ivangong24/homebrew-kvm-ai-monitor`. The companion app uses the cask
in `Casks/kvm-ai-monitor.rb` from this directory.

For a release:

1. Bump `package.json`, `kvm-agent/agent.py`, `desktop/Info.plist`, and the cask version together.
2. Build, Developer ID sign, and notarize with `desktop/package-release.sh` plus the commands in
   `desktop/README.md`.
3. Publish `desktop/dist/KVM-AI-Monitor-v<version>.zip` on the matching GitHub release. The tag
   workflow attaches the archive automatically; configure signing secrets before a public build.
4. Copy `Casks/kvm-ai-monitor.rb` into the tap repository and push it.

Users can then install with:

```bash
brew tap ivangong24/kvm-ai-monitor
brew install --cask kvm-ai-monitor
```

The template uses `sha256 :no_check` so the release workflow and cask do not need a cross-repo
commit race. For stricter supply-chain pinning, replace it with the SHA-256 printed by
`desktop/package-release.sh` for the exact published archive.
