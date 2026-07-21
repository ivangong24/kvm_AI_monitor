#!/usr/bin/env node
// kvm-ai-monitor: one-command setup and management CLI.
//
// `kvm-ai-monitor` (or `kvm-ai-monitor setup`) walks a user with no terminal experience from an
// unconfigured Comet Pro to a working AI-usage wallpaper: discover the KVM on the LAN, authorize,
// install the on-device agent, switch the touchscreen to Wallpaper Only, enroll this computer as a
// push device, and run a health check. Every step reuses the same scripts the repo has always
// shipped, so the wizard and the manual path cannot drift apart.

import { spawnSync } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync, existsSync, chmodSync } from "node:fs";
import { homedir, hostname, networkInterfaces } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { createInterface } from "node:readline";
import https from "node:https";
import { CometClient } from "../src/comet-client.js";
import { getSecret, setSecret, secretStoreName } from "../src/secret-store.js";
import { posixShell, findPython, installedHookShim } from "../src/platform.js";

const PROJECT_DIR = dirname(dirname(fileURLToPath(import.meta.url)));
const CONFIG_DIR = join(homedir(), ".kvm-ai-monitor");
const VERSION = JSON.parse(readFileSync(join(PROJECT_DIR, "package.json"), "utf8")).version;
const MARKER_BEGIN = "@@KVM_AI_BEGIN@@";
const MARKER_END = "@@KVM_AI_END@@";

// --- small console helpers --------------------------------------------------------------

const ok = (text) => console.log(`  ✓ ${text}`);
const info = (text) => console.log(`  • ${text}`);
const fail = (text) => console.log(`  ✗ ${text}`);

function ask(question, { hidden = false, fallback = "" } = {}) {
  return new Promise((resolve) => {
    const rl = createInterface({ input: process.stdin, output: process.stdout, terminal: true });
    if (hidden) {
      rl._writeToOutput = (chunk) => {
        if (chunk.includes(question)) rl.output.write(question);
      };
    }
    rl.question(question, (answer) => {
      rl.close();
      if (hidden) process.stdout.write("\n");
      resolve(answer.trim() || fallback);
    });
  });
}

async function confirm(question, byDefault = true) {
  const suffix = byDefault ? " [Y/n] " : " [y/N] ";
  const answer = (await ask(question + suffix)).toLowerCase();
  if (!answer) return byDefault;
  return answer.startsWith("y");
}

// --- local state ------------------------------------------------------------------------

function readRegistry() {
  try {
    const parsed = JSON.parse(readFileSync(join(CONFIG_DIR, "kvms.json"), "utf8"));
    if (Array.isArray(parsed?.kvms)) return parsed.kvms.filter((h) => typeof h === "string");
  } catch {
    // Fall through to the single-host pointer used by older installs.
  }
  try {
    const legacy = readFileSync(join(CONFIG_DIR, "kvm-host"), "utf8").trim();
    if (legacy) return [legacy];
  } catch {
    // First run.
  }
  return [];
}

function rememberKvm(host) {
  mkdirSync(CONFIG_DIR, { recursive: true, mode: 0o700 });
  const kvms = readRegistry();
  if (!kvms.includes(host)) kvms.push(host);
  const registry = join(CONFIG_DIR, "kvms.json");
  writeFileSync(registry, JSON.stringify({ kvms }, null, 2) + "\n");
  chmodSync(registry, 0o600);
  // kvm-host stays the "active" KVM for the classic npm scripts.
  const pointer = join(CONFIG_DIR, "kvm-host");
  writeFileSync(pointer, host + "\n");
  chmodSync(pointer, 0o600);
}

const tokenService = (host) => `kvm-ai-monitor-token:${host}`;

function hasToken(host) {
  try {
    getSecret(tokenService(host));
    return true;
  } catch {
    return false;
  }
}

// --- network discovery ------------------------------------------------------------------

function probeGlkvm(ip, timeoutMs = 900) {
  return new Promise((resolve) => {
    const request = https.request(
      { hostname: ip, port: 443, path: "/", method: "GET", rejectUnauthorized: false, timeout: timeoutMs },
      (response) => {
        const chunks = [];
        response.on("data", (chunk) => {
          chunks.push(chunk);
          if (Buffer.concat(chunks).length > 4096) response.destroy();
        });
        response.on("end", () => resolve(Buffer.concat(chunks).toString("utf8").includes("GLKVM")));
        response.on("close", () => resolve(Buffer.concat(chunks).toString("utf8").includes("GLKVM")));
      },
    );
    request.on("timeout", () => { request.destroy(); resolve(false); });
    request.on("error", () => resolve(false));
    request.end();
  });
}

async function discover() {
  const candidates = new Set();
  for (const entries of Object.values(networkInterfaces())) {
    for (const entry of entries ?? []) {
      if (entry.family !== "IPv4" || entry.internal) continue;
      const prefix = Number(entry.cidr?.split("/")[1] ?? 24);
      if (prefix < 24) continue; // Cap the scan at a /24 per interface.
      const base = entry.address.split(".").slice(0, 3).join(".");
      for (let i = 1; i <= 254; i += 1) {
        const ip = `${base}.${i}`;
        if (ip !== entry.address) candidates.add(ip);
      }
    }
  }
  const found = [];
  const queue = [...candidates];
  const workers = Array.from({ length: 64 }, async () => {
    while (queue.length) {
      const ip = queue.pop();
      if (await probeGlkvm(ip)) found.push(ip);
    }
  });
  await Promise.all(workers);
  return found.sort();
}

// --- KVM command channel (reuses the web-terminal script and its saved token) -----------

function webterm(host, command, { timeoutMs = 120_000 } = {}) {
  const result = spawnSync(
    process.execPath,
    [join(PROJECT_DIR, "scripts", "kvm-webterm-command.mjs"), "--stdin"],
    {
      input: `echo ${MARKER_BEGIN}\n${command}\necho ${MARKER_END}\n`,
      encoding: "utf8",
      env: { ...process.env, KVM_IP: host, KVM_COMMAND_TIMEOUT_MS: String(timeoutMs) },
      timeout: timeoutMs + 15_000,
    },
  );
  const output = `${result.stdout ?? ""}`;
  const begin = output.lastIndexOf(MARKER_BEGIN);
  const end = output.lastIndexOf(MARKER_END);
  const body = begin >= 0 && end > begin ? output.slice(begin + MARKER_BEGIN.length, end) : output;
  return { status: result.status ?? 1, output: body.trim(), stderr: `${result.stderr ?? ""}`.trim() };
}

function runScript(script, { args = [], env = {}, input } = {}) {
  const result = spawnSync(script, args, {
    cwd: PROJECT_DIR,
    encoding: "utf8",
    env: { ...process.env, ...env },
    input,
    stdio: input === undefined ? ["inherit", "pipe", "pipe"] : ["pipe", "pipe", "pipe"],
  });
  return { status: result.status ?? 1, stdout: `${result.stdout ?? ""}`, stderr: `${result.stderr ?? ""}` };
}

// --- wizard steps -----------------------------------------------------------------------

async function chooseHost(preset) {
  if (preset) return preset;
  const known = readRegistry();
  console.log("\nScanning your network for a GL.iNet Comet KVM (about 10 seconds)...");
  const found = await discover();
  const options = [...new Set([...found, ...known])];
  if (options.length === 1) {
    if (await confirm(`Found a Comet at ${options[0]}. Use it?`)) return options[0];
  } else if (options.length > 1) {
    console.log("Found these Comet KVMs:");
    options.forEach((ip, index) => console.log(`  ${index + 1}) ${ip}`));
    const pick = await ask(`Which one? [1-${options.length}] `, { fallback: "1" });
    const index = Number(pick) - 1;
    if (options[index]) return options[index];
  } else {
    console.log("No Comet found automatically (it may be on another subnet).");
  }
  const manual = await ask("Comet IP address: ");
  if (!/^([0-9]{1,3}\.){3}[0-9]{1,3}$/.test(manual)) throw new Error("That is not an IPv4 address.");
  return manual;
}

async function authorize(host) {
  if (hasToken(host) && webterm(host, "true", { timeoutMs: 20_000 }).status === 0) {
    ok("Already authorized (saved session still valid)");
    return;
  }
  console.log("\nSign in with the Comet's admin credentials (from the GL.iNet app or web console).");
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    const password = await ask("Admin password: ", { hidden: true });
    const totp = await ask("6-digit 2FA code (press Enter if 2FA is off): ");
    let token;
    try {
      const client = new CometClient({ host, password });
      token = (await client.login(totp)).token;
    } catch (error) {
      fail(`Sign-in failed: ${error.message}`);
      if (attempt === 3) throw new Error("Could not sign in to the Comet.");
      continue;
    }
    // Storing the token is a separate failure mode from signing in. Retrying with a different
    // password cannot fix a broken secret store, so report it plainly and stop.
    try {
      setSecret(tokenService(host), token);
    } catch (error) {
      throw new Error(
        `Signed in successfully, but could not save the session token to your ` +
        `${secretStoreName()}: ${error.message}`,
      );
    }
    ok(`Authorized; session token saved to your ${secretStoreName()}`);
    return;
  }
}

function installAgent(host) {
  const result = runScript(posixShell(), { args: [join(PROJECT_DIR, "scripts", "install-kvm-agent.sh")], env: { KVM_IP: host } });
  if (result.status !== 0) {
    throw new Error(`Agent install failed: ${result.stderr.split("\n").filter(Boolean).at(-1) ?? "unknown error"}`);
  }
  ok("AI usage agent installed on the KVM");
}

function enableWallpaperMode(host) {
  const script = [
    "python3 - <<'PY'",
    "import json",
    "path = '/etc/glinet/kvm-gui.conf'",
    "data = json.load(open(path))",
    "screen = data.setdefault('CustomScreen', {})",
    "if screen.get('ScreenMode') == 2:",
    "    print('WALLPAPER_ALREADY')",
    "else:",
    "    screen['ScreenMode'] = 2",
    "    json.dump(data, open(path, 'w'))",
    "    print('WALLPAPER_SET')",
    "PY",
  ].join("\n");
  const first = webterm(host, script);
  if (first.output.includes("WALLPAPER_ALREADY")) {
    ok("Touchscreen already in Wallpaper Only mode");
    return;
  }
  if (first.output.includes("WALLPAPER_SET")) {
    webterm(host, "/etc/init.d/S39gl-kvm-gui restart >/dev/null 2>&1 || true");
    ok("Touchscreen switched to Wallpaper Only");
    return;
  }
  info("Could not switch the screen mode automatically. In the Comet console choose");
  info("Settings > System > Screen Display > Wallpaper Only, then Apply.");
}

function readHelperTargets() {
  try {
    const parsed = JSON.parse(readFileSync(join(CONFIG_DIR, "helper.json"), "utf8"));
    if (Array.isArray(parsed?.targets)) return parsed.targets;
    if (parsed?.kvmHost && parsed?.deviceId) return [{ kvmHost: parsed.kvmHost, deviceId: parsed.deviceId }];
  } catch {
    // Not enrolled yet.
  }
  return [];
}

// kvm_ai_push.py is already cross-platform; only the scheduling differs, so each platform has
// its own thin installer: launchd (install-helper.sh), Task Scheduler (install-helper.ps1),
// and a systemd user timer (install-helper-linux.sh).
const HELPER_UNSUPPORTED =
  `Enrolling this computer as a push device is supported on macOS, Windows, and Linux only, so ` +
  `this machine's own AI usage will not appear on the KVM. The KVM agent and wallpaper still ` +
  `work, and any enrolled device keeps pushing.`;

function helperSupported() {
  return ["darwin", "win32", "linux"].includes(process.platform);
}

const deviceLabel = () =>
  process.platform === "win32" ? "PC" : process.platform === "darwin" ? "Mac" : "computer";

// Runs the platform's helper installer with the same argument shape across platforms.
function runHelperInstaller({ update = false, host, deviceId, secret } = {}) {
  const args = update
    ? (process.platform === "win32" ? ["-Update"] : ["--update"])
    : process.platform === "win32"
      ? ["-Kvm", host, "-Device", deviceId, "-SecretStdin"]
      : ["--kvm", host, "--device", deviceId, "--secret-stdin"];
  const input = update ? "" : secret + "\n";

  if (process.platform === "win32") {
    return runScript("powershell.exe", {
      args: [
        "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", join(PROJECT_DIR, "helper", "install-helper.ps1"),
        ...args,
      ],
      input,
    });
  }
  if (process.platform === "linux") {
    return runScript("sh", { args: [join(PROJECT_DIR, "helper", "install-helper-linux.sh"), ...args], input });
  }
  return runScript(join(PROJECT_DIR, "helper", "install-helper.sh"), { args, input });
}

// claude_hooks.py edits ~/.claude/settings.json the same way on every platform; only the shim
// it points the hooks at differs. macOS keeps its zsh wrapper for its extra summary output.
function installClaudeHooks() {
  let status;
  if (process.platform === "darwin") {
    status = runScript(join(PROJECT_DIR, "helper", "install-claude-hooks.sh"), { input: "" }).status;
  } else {
    const shim = installedHookShim();
    const python = findPython();
    if (!python || !existsSync(shim)) {
      info("Could not install hooks automatically; run later with: npm run helper:hooks");
      return;
    }
    status = runScript(python, {
      args: [join(PROJECT_DIR, "helper", "claude_hooks.py"), "install", shim],
      input: "",
    }).status;
  }
  if (status === 0) ok("Claude Code hooks installed");
  else info("Hook install failed; run later with: npm run helper:hooks");
}

async function enrollThisDevice(host) {
  if (!helperSupported()) throw new Error(HELPER_UNSUPPORTED);
  const label = deviceLabel();
  if (readHelperTargets().some((target) => target?.kvmHost === host)) {
    if (runHelperInstaller({ update: true }).status === 0) {
      ok(`This ${label} is already enrolled; helper refreshed`);
      return;
    }
  }
  const name = hostname().replace(/\.local$/i, "").slice(0, 48) || label;
  const create = webterm(
    host,
    `curl -s -X POST http://127.0.0.1:8199/api/devices -H 'Content-Type: application/json' -d '${JSON.stringify({ name }).replaceAll("'", "'\\''")}'`,
  );
  const match = create.output.match(/\{"id":\s*"(d-[0-9a-f]{8})",\s*"name":.*?"secret":\s*"([0-9a-f]{48})"\}/);
  if (!match) {
    // A failed command channel leaves output empty, so fall back to stderr rather than
    // reporting a blank reason.
    const reason = create.output.trim() || create.stderr.trim() || "no response from the KVM";
    throw new Error(`Device enrollment failed: ${reason.slice(0, 200)}`);
  }
  const [, deviceId, secret] = match;
  const install = runHelperInstaller({ host, deviceId, secret });
  if (install.status !== 0) {
    const detail = [install.stderr, install.stdout]
      .flatMap((stream) => stream.split("\n"))
      .filter(Boolean)
      .at(-1) ?? "unknown";
    throw new Error(`Helper install failed: ${detail}`);
  }
  ok(`This ${label} enrolled as "${name}" (${deviceId}); usage now pushes every minute`);
  if (await confirm(`Also send exact working/idle events from Claude Code on this ${label}?`)) {
    installClaudeHooks();
  }
}

function healthCheck(host) {
  const status = webterm(host, "curl -s http://127.0.0.1:8199/api/status");
  try {
    // The web terminal interleaves shell prompts around the payload; take the outermost braces.
    const begin = status.output.indexOf("{");
    const end = status.output.lastIndexOf("}");
    const parsed = JSON.parse(status.output.slice(begin, end + 1));
    ok(`Agent healthy (wallpaper ${parsed.wallpaperReady ? "rendering" : "pending"}, ` +
       `${parsed.pushDevices?.filter((d) => !d.revoked).length ?? 0} push device(s))`);
  } catch {
    info("Could not read agent status; open the AI Usage page to verify.");
  }
  console.log(`\nDone. Manage everything at: https://${host}/extras/ai-usage/`);
}

// --- commands ---------------------------------------------------------------------------

async function cmdSetup(kvmArg) {
  console.log(`KVM AI Monitor setup (v${VERSION})`);
  const host = await chooseHost(kvmArg);
  await authorize(host);
  rememberKvm(host);
  installAgent(host);
  enableWallpaperMode(host);
  if (!helperSupported()) {
    info(HELPER_UNSUPPORTED);
  } else if (await confirm(
    `Enroll this ${deviceLabel()} so its AI provider usage shows on the KVM?`,
  )) {
    await enrollThisDevice(host);
  }
  healthCheck(host);
}

async function cmdDiscover(asJson) {
  const found = await discover();
  if (asJson) console.log(JSON.stringify({ kvms: found }));
  else if (found.length) found.forEach((ip) => console.log(ip));
  else console.log("No GLKVM device found on the local network.");
}

function cmdStatus() {
  const kvms = readRegistry();
  console.log(`Configured KVMs: ${kvms.length ? kvms.join(", ") : "none (run: kvm-ai-monitor setup)"}`);
  const targets = readHelperTargets();
  const enrollment = targets.length
    ? targets.map((t) => `${t.kvmHost} (${t.deviceId})`).join(", ")
    : helperSupported() ? "not enrolled" : "not enrolled (push helper needs macOS, Windows, or Linux)";
  console.log(`This computer pushes to: ${enrollment}`);
  for (const host of kvms) {
    console.log(`Admin session for ${host}: ${hasToken(host) ? "saved" : "missing (rerun setup)"}`);
  }
}

function usage() {
  console.log(`kvm-ai-monitor v${VERSION} — AI usage wallpaper for the GL.iNet Comet Pro

Usage:
  kvm-ai-monitor [setup] [--kvm <ip>]   guided setup (discover, authorize, install, enroll)
  kvm-ai-monitor authorize [--kvm <ip>] sign in to a KVM and save the admin session token
  kvm-ai-monitor enroll [--kvm <ip>]    enroll this computer as a push device on a configured KVM
  kvm-ai-monitor install-agent [--kvm <ip>]  redeploy the on-device agent
  kvm-ai-monitor discover [--json]      list Comet KVMs found on the local network
  kvm-ai-monitor status                 show configured KVMs and this computer's enrollment
  kvm-ai-monitor version | help`);
}

async function main() {
  const argv = process.argv.slice(2);
  const kvmIndex = argv.indexOf("--kvm");
  const kvmArg = kvmIndex >= 0 ? argv[kvmIndex + 1] : undefined;
  const command = argv.find((arg) => !arg.startsWith("--") && arg !== kvmArg) ?? "setup";
  switch (command) {
    case "setup": await cmdSetup(kvmArg); break;
    case "authorize": {
      // The cross-platform stand-in for scripts/configure-kvm.sh (zsh + macOS Keychain).
      const host = kvmArg ?? readRegistry().at(-1) ?? await ask("Comet IP address: ");
      if (!host) throw new Error("No KVM given. Run: kvm-ai-monitor authorize --kvm <ip>");
      await authorize(host);
      rememberKvm(host);
      break;
    }
    case "discover": await cmdDiscover(argv.includes("--json")); break;
    case "status": cmdStatus(); break;
    case "install-agent": {
      const host = kvmArg ?? readRegistry().at(-1);
      if (!host) throw new Error("No KVM configured. Run: kvm-ai-monitor setup");
      installAgent(host);
      break;
    }
    case "enroll": {
      const host = kvmArg ?? readRegistry().at(-1);
      if (!host) throw new Error("No KVM configured. Run: kvm-ai-monitor setup");
      await enrollThisDevice(host);
      break;
    }
    case "version": console.log(VERSION); break;
    case "help": case "--help": usage(); break;
    default: usage(); process.exitCode = 2;
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});
