#!/usr/bin/env node
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { CometClient } from "../src/comet-client.js";

function configuredHost() {
  if (process.env.KVM_IP) return process.env.KVM_IP;
  try {
    const value = readFileSync(join(homedir(), ".kvm-ai-monitor", "kvm-host"), "utf8").trim();
    if (value) return value;
  } catch {
    // Report a single actionable error below.
  }
  throw new Error("KVM address is not configured. Run: npm run kvm:configure");
}

function keychainSecret(service) {
  try {
    return execFileSync(
      "/usr/bin/security",
      ["find-generic-password", "-a", "admin", "-s", service, "-w"],
      { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] },
    ).trim();
  } catch {
    throw new Error("No KVM password was found in Keychain. Run: npm run kvm:configure");
  }
}

function saveToken(service, token) {
  execFileSync(
    "/usr/bin/security",
    ["add-generic-password", "-a", "admin", "-s", service, "-w", token, "-U"],
    { stdio: "ignore" },
  );
}

function deleteKeychainSecret(service) {
  try {
    execFileSync(
      "/usr/bin/security",
      ["delete-generic-password", "-a", "admin", "-s", service],
      { stdio: "ignore" },
    );
  } catch {
    // The password may have been supplied only through KVM_PASSWORD.
  }
}

async function main() {
  const host = configuredHost();
  const twoFactorCode = process.env.KVM_TOTP ?? "";
  if (twoFactorCode && !/^\d{6}$/.test(twoFactorCode)) {
    throw new Error("The 2FA code must contain exactly six digits");
  }
  const passwordService = `kvm-ai-monitor:${host}`;
  const password = process.env.KVM_PASSWORD ?? keychainSecret(passwordService);
  const client = new CometClient({ host, password });
  const result = await client.login(twoFactorCode);
  saveToken(`kvm-ai-monitor-token:${host}`, result.token);
  deleteKeychainSecret(passwordService);
  console.log("Authorized Comet Pro, saved its session token, and removed the admin password");
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : error);
  process.exitCode = 1;
});
