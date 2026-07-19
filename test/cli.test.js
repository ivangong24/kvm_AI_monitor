import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { join, dirname } from "node:path";

const BIN = join(dirname(dirname(fileURLToPath(import.meta.url))), "bin", "kvm-ai-monitor.mjs");

function run(args) {
  return spawnSync(process.execPath, [BIN, ...args], { encoding: "utf8", timeout: 30_000 });
}

test("help prints usage and exits 0", () => {
  const result = run(["help"]);
  assert.equal(result.status, 0);
  assert.match(result.stdout, /kvm-ai-monitor .* setup/s);
});

test("version prints a semver", () => {
  const result = run(["version"]);
  assert.equal(result.status, 0);
  assert.match(result.stdout.trim(), /^\d+\.\d+\.\d+$/);
});

test("unknown command exits 2 with usage", () => {
  const result = run(["frobnicate"]);
  assert.equal(result.status, 2);
  assert.match(result.stdout, /Usage:/);
});
