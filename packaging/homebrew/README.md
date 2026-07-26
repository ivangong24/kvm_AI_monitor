# Homebrew packaging

The CLI formula lives in `ivangong24/homebrew-kvm-ai-monitor`. The companion app uses the cask
in `Casks/kvm-ai-monitor.rb` from this directory.

For a release:

1. Bump `package.json`, `kvm-agent/agent.py`, `desktop/Info.plist`, and the cask version together.
2. Build, Developer ID sign, and notarize with `desktop/package-release.sh` plus the commands in
   `desktop/README.md`.
3. Push the `v<version>` tag. The `Release macOS companion` workflow builds the universal app
   archive and attaches `KVM-AI-Monitor-v<version>.zip` to the GitHub release.

The Homebrew tap then updates **itself**: a scheduled `Sync from upstream release` workflow in
`ivangong24/homebrew-kvm-ai-monitor` polls this repo's latest release hourly and, when it changes,
repoints its own `Formula/kvm-ai-monitor.rb` (url + `sha256`) and bumps `Casks/kvm-ai-monitor.rb`.
It runs entirely on that repo's built-in `GITHUB_TOKEN`, so there is no cross-repo push and no PAT
to manage. A new release shows up in `brew install`/`brew upgrade` within about an hour, or
immediately if you run the tap's workflow manually (`gh workflow run` / the Actions tab). The cask
in this directory stays the source of truth for the cask's shape; keep its version in step 1 in sync
with the release.

Users can then install with:

```bash
brew tap ivangong24/kvm-ai-monitor
brew install --cask kvm-ai-monitor
```

The template uses `sha256 :no_check` so the release workflow and cask do not need a cross-repo
commit race. For stricter supply-chain pinning, replace it with the SHA-256 printed by
`desktop/package-release.sh` for the exact published archive.
