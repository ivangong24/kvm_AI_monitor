import assert from "node:assert/strict";
import test from "node:test";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const helperPath = path.join(here, "..", "mac-helper", "kvm_ai_push.py");

test("mac helper reproduces the push protocol HMAC test vector", () => {
  const body = '{"event":"active","provider":"claude","schemaVersion":1}';
  const result = spawnSync(
    "python3",
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
