// Exercises helper/merge-helper-config.ps1 — the multi-KVM helper.json merge used by the
// Windows installer — under a real PowerShell. Runs against powershell.exe on Windows (so CI
// covers the same interpreter the installer uses, Windows PowerShell 5.1) and pwsh elsewhere
// when installed; skips with a reason when neither exists.

import assert from "node:assert/strict";
import test from "node:test";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const mergeScript = path.join(here, "..", "helper", "merge-helper-config.ps1");

function findPowerShell() {
  const candidates = process.platform === "win32" ? ["powershell.exe", "pwsh"] : ["pwsh"];
  for (const candidate of candidates) {
    const probe = spawnSync(candidate, ["-NoProfile", "-Command", "1"], { encoding: "utf8" });
    if (probe.status === 0) return candidate;
  }
  return null;
}

const powershell = findPowerShell();
const skip = powershell ? false : "no PowerShell (powershell.exe/pwsh) available";

function merge(configPath, kvm, device) {
  const result = spawnSync(
    powershell,
    ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", mergeScript,
     "-ConfigPath", configPath, "-Kvm", kvm, "-Device", device],
    { encoding: "utf8" },
  );
  assert.equal(result.status, 0, `merge failed: ${result.stderr}`);
  return fs.readFileSync(configPath); // raw bytes, so the BOM check sees the real first byte
}

function targetsIn(buffer) {
  return JSON.parse(buffer.toString("utf8")).targets;
}

function tempConfig(contents) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "kvm-merge-test-"));
  const configPath = path.join(dir, "helper.json");
  if (contents !== undefined) fs.writeFileSync(configPath, contents);
  return configPath;
}

test("merge creates helper.json for a fresh install", { skip }, () => {
  const raw = merge(tempConfig(), "192.0.2.10", "d-00000001");
  assert.deepEqual(targetsIn(raw), [{ kvmHost: "192.0.2.10", deviceId: "d-00000001" }]);
});

test("merge output is BOM-less UTF-8 with a trailing newline", { skip }, () => {
  // Both Python's json.load and Node's JSON.parse reject a leading BOM.
  const raw = merge(tempConfig(), "192.0.2.10", "d-00000001");
  assert.equal(raw[0], "{".charCodeAt(0));
  assert.ok(raw.toString("utf8").endsWith("\n"));
});

test("merge appends a second KVM and keeps the first", { skip }, () => {
  const configPath = tempConfig();
  merge(configPath, "192.0.2.10", "d-00000001");
  const raw = merge(configPath, "192.0.2.20", "d-00000002");
  assert.deepEqual(targetsIn(raw), [
    { kvmHost: "192.0.2.10", deviceId: "d-00000001" },
    { kvmHost: "192.0.2.20", deviceId: "d-00000002" },
  ]);
});

test("re-enrolling a known KVM replaces its entry instead of duplicating it", { skip }, () => {
  const configPath = tempConfig();
  merge(configPath, "192.0.2.10", "d-00000001");
  merge(configPath, "192.0.2.20", "d-00000002");
  const raw = merge(configPath, "192.0.2.10", "d-00000003");
  assert.deepEqual(targetsIn(raw), [
    { kvmHost: "192.0.2.20", deviceId: "d-00000002" },
    { kvmHost: "192.0.2.10", deviceId: "d-00000003" },
  ]);
});

test("merge upgrades the legacy single-target shape", { skip }, () => {
  const configPath = tempConfig('{"kvmHost": "192.0.2.10", "deviceId": "d-00000001"}\n');
  const raw = merge(configPath, "192.0.2.20", "d-00000002");
  assert.deepEqual(targetsIn(raw), [
    { kvmHost: "192.0.2.10", deviceId: "d-00000001" },
    { kvmHost: "192.0.2.20", deviceId: "d-00000002" },
  ]);
});

test("re-enrolling the KVM from a legacy config does not duplicate it", { skip }, () => {
  const configPath = tempConfig('{"kvmHost": "192.0.2.10", "deviceId": "d-00000001"}\n');
  const raw = merge(configPath, "192.0.2.10", "d-00000009");
  assert.deepEqual(targetsIn(raw), [{ kvmHost: "192.0.2.10", deviceId: "d-00000009" }]);
});

test("merge recovers from an unparseable helper.json", { skip }, () => {
  const configPath = tempConfig("not json at all {{{");
  const raw = merge(configPath, "192.0.2.10", "d-00000001");
  assert.deepEqual(targetsIn(raw), [{ kvmHost: "192.0.2.10", deviceId: "d-00000001" }]);
});
