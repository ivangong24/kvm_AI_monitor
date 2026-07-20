#!/usr/bin/env node
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { CometClient } from "../src/comet-client.js";
import { getSecret, setSecret, deleteSecret, secretStoreName } from "../src/secret-store.js";

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
    return getSecret(service);
  } catch {
    throw new Error(
      `No KVM password was found in your ${secretStoreName()}. Run: npm run kvm:configure`,
    );
  }
}

function saveToken(service, token) {
  setSecret(service, token);
}

function deleteKeychainSecret(service) {
  // The password may have been supplied only through KVM_PASSWORD; a missing entry is fine.
  deleteSecret(service);
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
