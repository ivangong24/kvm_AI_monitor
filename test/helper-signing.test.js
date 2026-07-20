import assert from "node:assert/strict";
import test from "node:test";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const helperPath = path.join(here, "..", "mac-helper", "kvm_ai_push.py");

// On Windows `python`/`python3` are usually the Microsoft Store alias stubs, which exit 9009
// without running anything, and uv-managed interpreters are not on PATH at all. Probe until
// one actually executes.
function findPython() {
  if (process.env.KVM_PYTHON) return process.env.KVM_PYTHON;
  const candidates = process.platform === "win32" ? ["py", "python", "python3"] : ["python3", "python"];
  if (process.platform === "win32") {
    const uvRoot = path.join(process.env.APPDATA ?? "", "uv", "python");
    if (fs.existsSync(uvRoot)) {
      for (const dir of fs.readdirSync(uvRoot).sort().reverse()) {
        candidates.push(path.join(uvRoot, dir, "python.exe"));
      }
    }
  }
  for (const candidate of candidates) {
    const probe = spawnSync(candidate, ["-c", "print(1)"], { encoding: "utf8" });
    if (probe.status === 0 && probe.stdout.trim() === "1") return candidate;
  }
  return null;
}

const python = findPython();

test("device helper reproduces the push protocol HMAC test vector", { skip: python ? false : "no usable Python interpreter found" }, () => {
  const body = '{"event":"active","provider":"claude","schemaVersion":1}';
  const result = spawnSync(
    python,
    [
      helperPath,
      "sign",
      "--secret", "0123456789abcdef0123456789abcdef0123456789abcdef",
      "--timestamp", "1752800000",
      "--nonce", "abcdef0123456789",
      "--method", "POST",
      "--path", "/push/v1/activity",
    ],
    { input: body, encoding: "utf8" },
  );
  assert.equal(result.status, 0);
  assert.equal(
    result.stdout.trim(),
    "4d23e79d847fee9e540fa1b24e8328681fab6e77043fff7d0503cef0f2832ead",
  );
});
