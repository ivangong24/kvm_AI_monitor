import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pythonOrgInstallCandidates } from "../src/platform.js";

test("python.org per-user and all-users installs are found outside PATH", () => {
  const root = mkdtempSync(join(tmpdir(), "kvm-python-org-"));
  try {
    const local = join(root, "LocalAppData");
    const programs = join(root, "ProgramFiles");
    const python312 = join(local, "Programs", "Python", "Python312");
    const python313 = join(programs, "Python313");
    mkdirSync(python312, { recursive: true });
    mkdirSync(python313, { recursive: true });
    writeFileSync(join(python312, "python.exe"), "");
    writeFileSync(join(python313, "python.exe"), "");

    assert.deepEqual(
      pythonOrgInstallCandidates({ LOCALAPPDATA: local, ProgramFiles: programs }),
      [join(python312, "python.exe"), join(python313, "python.exe")],
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
