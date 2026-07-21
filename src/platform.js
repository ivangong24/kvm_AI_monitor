// Platform lookups shared by the setup wizard, the npm-script dispatcher, and the tests.
//
// Two things are surprisingly hard to find on Windows: a POSIX shell (the `bash.exe` in
// system32 is the WSL stub and fails when no distro is installed) and a Python interpreter
// that actually runs (`python`/`python3` on PATH are usually the Microsoft Store alias stubs,
// which exist on disk but exit 9009 without running anything; uv-managed interpreters are
// never on PATH at all).

import { spawnSync } from "node:child_process";
import { existsSync, readdirSync } from "node:fs";
import { homedir } from "node:os";
import { join, dirname } from "node:path";

// The install scripts are portable POSIX, but Windows has no `sh` on PATH. Git for Windows
// ships one (along with tar/base64/mktemp), so locate it rather than requiring WSL.
export function posixShell() {
  if (process.env.KVM_SH) return process.env.KVM_SH;
  if (process.platform !== "win32") return "sh";

  const candidates = [];
  const where = spawnSync("where.exe", ["git"], { encoding: "utf8" });
  for (const line of `${where.stdout ?? ""}`.split(/\r?\n/)) {
    const gitExe = line.trim();
    // ...\Git\cmd\git.exe -> ...\Git\bin\bash.exe
    // Prefer bin\bash.exe over usr\bin\sh.exe: only the former sets up the MSYS PATH, so
    // sh.exe cannot find tar/base64/mktemp when launched straight from Windows.
    if (gitExe.toLowerCase().endsWith("git.exe")) {
      candidates.push(join(dirname(dirname(gitExe)), "bin", "bash.exe"));
    }
  }
  for (const root of [process.env.ProgramFiles, process.env["ProgramFiles(x86)"], "C:\\Program Files"]) {
    if (root) candidates.push(join(root, "Git", "bin", "bash.exe"));
  }

  const found = candidates.find((candidate) => existsSync(candidate));
  if (found) return found;
  throw new Error(
    "This step needs a POSIX shell. Install Git for Windows (https://git-scm.com/download/win), " +
    "or point KVM_SH at an sh.exe.",
  );
}

// Mirrors Find-Python in helper/find-python.ps1: probe candidates and keep the first that
// actually executes, skipping the Store stubs under WindowsApps. Returns null when nothing
// usable exists — callers decide whether that is fatal.
function versionParts(name) {
  const uv = name.match(/cpython-(\d+)\.(\d+)(?:\.(\d+))?/i);
  if (uv) return [Number(uv[1]), Number(uv[2]), Number(uv[3] ?? 0)];
  // python.org's default directories are named Python313, Python312, etc.
  const pythonOrg = name.match(/^Python(\d)(\d+)/i);
  return pythonOrg ? [Number(pythonOrg[1]), Number(pythonOrg[2]), 0] : [0, 0, 0];
}

function byVersionDesc(a, b) {
  const [a1, a2, a3] = versionParts(a);
  const [b1, b2, b3] = versionParts(b);
  return b1 - a1 || b2 - a2 || b3 - a3;
}

// A python.org install is usable even when the user leaves "Add python.exe to PATH"
// unchecked. Cover the per-user and all-users default locations; KVM_PYTHON remains the
// escape hatch for a custom installation directory.
export function pythonOrgInstallCandidates(env = process.env) {
  const roots = [];
  if (env.LOCALAPPDATA) {
    roots.push(join(env.LOCALAPPDATA, "Programs", "Python"));
  }
  for (const root of [env.ProgramFiles, env["ProgramFiles(x86)"]]) {
    if (root) roots.push(root);
  }

  const found = [];
  for (const root of roots) {
    if (!existsSync(root)) continue;
    let entries;
    try {
      entries = readdirSync(root, { withFileTypes: true });
    } catch {
      continue;
    }
    const directories = entries
      .filter((item) => item.isDirectory())
      .sort((a, b) => byVersionDesc(a.name, b.name));
    for (const entry of directories) {
      if (!/^Python\d+/i.test(entry.name)) continue;
      found.push(join(root, entry.name, "python.exe"));
    }
  }
  return found;
}

export function findPython() {
  if (process.env.KVM_PYTHON) return process.env.KVM_PYTHON;

  const candidates = [];
  if (process.platform === "win32") {
    const uvRoot = join(process.env.APPDATA ?? "", "uv", "python");
    if (existsSync(uvRoot)) {
      // Sort by parsed version, not by name: a string sort ranks "cpython-3.9" above
      // "cpython-3.14" and would pick the oldest interpreter installed.
      for (const dir of readdirSync(uvRoot).sort(byVersionDesc)) {
        candidates.push(join(uvRoot, dir, "python.exe"));
      }
    }
    candidates.push(...pythonOrgInstallCandidates());
    const where = spawnSync("where.exe", ["python.exe", "python3.exe", "py.exe"], { encoding: "utf8" });
    for (const line of `${where.stdout ?? ""}`.split(/\r?\n/)) {
      const exe = line.trim();
      if (exe && !/\\WindowsApps\\/i.test(exe)) candidates.push(exe);
    }
  } else {
    candidates.push("python3", "python");
  }

  for (const candidate of candidates) {
    const probe = spawnSync(candidate, ["-c", "print(1)"], { encoding: "utf8" });
    if (probe.status === 0 && probe.stdout.trim() === "1") return candidate;
  }
  return null;
}

// Where each platform's helper installer places the Claude Code hook shim (see the
// install-helper.* scripts). Used both by the setup wizard and the npm-script dispatcher when
// wiring up Claude Code lifecycle hooks.
export function installedHookShim() {
  if (process.platform === "win32") {
    return join(process.env.LOCALAPPDATA ?? "", "kvm-ai-monitor", "kvm-ai-claude-hook.cmd");
  }
  if (process.platform === "darwin") {
    return join(homedir(), "Library", "Application Support", "kvm-ai-monitor", "kvm-ai-claude-hook.sh");
  }
  const dataHome = process.env.XDG_DATA_HOME || join(homedir(), ".local", "share");
  return join(dataHome, "kvm-ai-monitor", "kvm-ai-claude-hook.sh");
}
