import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));

test("macOS app and Homebrew cask versions match the package", () => {
  const plist = readFileSync(join(root, "desktop", "Info.plist"), "utf8");
  const cask = readFileSync(join(root, "packaging", "homebrew", "Casks", "kvm-ai-monitor.rb"), "utf8");
  assert.match(plist, new RegExp(`<string>${pkg.version.replaceAll(".", "\\.")}</string>`));
  assert.match(cask, new RegExp(`version "${pkg.version.replaceAll(".", "\\.")}"`));
  assert.match(cask, /KVM-AI-Monitor-v#\{version\}\.zip/);
});

test("desktop release packager remains executable", () => {
  const mode = statSync(join(root, "desktop", "package-release.sh")).mode;
  assert.notEqual(mode & 0o111, 0);
});
