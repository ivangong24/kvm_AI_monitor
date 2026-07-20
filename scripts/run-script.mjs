#!/usr/bin/env node
// Cross-platform launcher behind the npm scripts.
//
// The repo's operational scripts come in per-platform flavors: zsh for macOS, PowerShell for
// Windows, POSIX sh for Linux and the KVM-facing scripts. npm scripts used to point straight
// at the Unix flavor, which made most of them fail on Windows. This dispatcher picks the right
// implementation for the current platform (finding Git's bash.exe for the POSIX scripts and a
// Python that actually runs for the Python entry points), so `npm run helper:status` and
// friends mean the same thing everywhere.

import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { posixShell, findPython, installedHookShim } from "../src/platform.js";

const PROJECT_DIR = dirname(dirname(fileURLToPath(import.meta.url)));
const HELPER_DIR = join(PROJECT_DIR, "helper");
const SCRIPTS_DIR = join(PROJECT_DIR, "scripts");

function run(command, args = []) {
  const result = spawnSync(command, args, { cwd: PROJECT_DIR, stdio: "inherit" });
  return result.status ?? 1;
}

// POSIX scripts run directly on Unix (their shebangs pick zsh/sh) and through Git's bash.exe
// on Windows, which also puts tar/base64/mktemp on PATH for them.
const runSh = (script, args = []) =>
  process.platform === "win32" ? run(posixShell(), [script, ...args]) : run(script, args);

const runPs1 = (script, args = []) =>
  run("powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, ...args]);

// npm scripts document the POSIX flag spelling; translate it for the PowerShell installers so
// `npm run helper:install -- --update` works unchanged on Windows.
function ps1Flags(args) {
  const map = {
    "--kvm": "-Kvm",
    "--device": "-Device",
    "--update": "-Update",
    "--secret-stdin": "-SecretStdin",
    "--purge": "-Purge",
  };
  return args.map((arg) => map[arg] ?? arg);
}

function requirePython() {
  const python = findPython();
  if (python) return python;
  console.error(
    "No usable Python 3 found. `python`/`python3` on PATH are usually Microsoft Store stubs " +
    "that do not run. Install one (https://www.python.org/downloads/ or `uv python install`), " +
    "or point KVM_PYTHON at a python executable.",
  );
  process.exit(1);
}

function installHooks() {
  if (process.platform === "darwin") {
    // The zsh wrapper adds the "installed in ~/.claude/settings.json" summary output.
    return run(join(HELPER_DIR, "install-claude-hooks.sh"));
  }
  const hookScript = installedHookShim();
  if (!existsSync(hookScript)) {
    console.error(`Hook script not found at: ${hookScript}`);
    console.error("Run the helper installer first: npm run helper:install");
    return 1;
  }
  const status = run(requirePython(), [join(HELPER_DIR, "claude_hooks.py"), "install", hookScript]);
  if (status === 0) console.log(`Claude Code lifecycle hooks installed (-> ${hookScript})`);
  return status;
}

function linuxStatus() {
  console.log("== systemd user timer ==");
  run("systemctl", ["--user", "--no-pager", "status", "kvm-ai-helper.timer"]);
  console.log("\n== Payload that would be sent (print-payload) ==");
  return run(requirePython(), [join(HELPER_DIR, "kvm_ai_push.py"), "print-payload"]);
}

const [name, ...args] = process.argv.slice(2);

// Each entry maps platform -> a function returning an exit status.
const commands = {
  "kvm:configure": {
    darwin: () => run(join(SCRIPTS_DIR, "configure-kvm.sh"), args),
    // configure-kvm.sh is zsh + macOS Keychain; elsewhere the CLI's authorize flow is the
    // equivalent (prompts for the password, saves the session token to the platform store).
    default: () => run(process.execPath, [join(PROJECT_DIR, "bin", "kvm-ai-monitor.mjs"), "authorize", ...args]),
  },
  "kvm:agent:install": { default: () => runSh(join(SCRIPTS_DIR, "install-kvm-agent.sh"), args) },
  "kvm:agent:uninstall": { default: () => runSh(join(SCRIPTS_DIR, "uninstall-kvm-agent.sh"), args) },
  "helper:install": {
    darwin: () => run(join(HELPER_DIR, "install-helper.sh"), args),
    win32: () => runPs1(join(HELPER_DIR, "install-helper.ps1"), ps1Flags(args)),
    default: () => runSh(join(HELPER_DIR, "install-helper-linux.sh"), args),
  },
  "helper:uninstall": {
    darwin: () => run(join(HELPER_DIR, "uninstall-helper.sh"), args),
    win32: () => runPs1(join(HELPER_DIR, "uninstall-helper.ps1"), ps1Flags(args)),
    default: () => runSh(join(HELPER_DIR, "uninstall-helper-linux.sh"), args),
  },
  "helper:status": {
    darwin: () => run(join(HELPER_DIR, "helper-status.sh"), args),
    win32: () => runPs1(join(HELPER_DIR, "helper-status.ps1"), args),
    default: linuxStatus,
  },
  "helper:hooks": { default: installHooks },
  "helper:test": { default: () => run(requirePython(), [join(HELPER_DIR, "test_helper.py")]) },
};

const command = commands[name];
if (!command) {
  console.error(`Unknown script: ${name ?? "(none)"}. Known: ${Object.keys(commands).join(", ")}`);
  process.exit(2);
}
process.exit((command[process.platform] ?? command.default)());
