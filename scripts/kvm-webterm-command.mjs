#!/usr/bin/env node
import { execFileSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { connect } from "node:tls";

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

const host = configuredHost();
const token = process.env.KVM_TOKEN ?? execFileSync(
  "/usr/bin/security",
  ["find-generic-password", "-a", "admin", "-s", `kvm-ai-monitor-token:${host}`, "-w"],
  { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] },
).trim();
const command = process.argv[2] === "--stdin"
  ? readFileSync(0, "utf8")
  : process.argv.slice(2).join(" ");

if (!command.trim()) {
  console.error("Usage: kvm-webterm-command.mjs <command> | --stdin");
  process.exit(2);
}

const marker = `__KVM_COMMAND_${randomBytes(10).toString("hex")}__`;
const timeoutMs = Number(process.env.KVM_COMMAND_TIMEOUT_MS ?? 120_000);

function clientFrame(payload, opcode = 2) {
  const body = Buffer.from(payload);
  const mask = randomBytes(4);
  let header;
  if (body.length < 126) {
    header = Buffer.from([0x80 | opcode, 0x80 | body.length]);
  } else if (body.length <= 0xffff) {
    header = Buffer.alloc(4);
    header[0] = 0x80 | opcode;
    header[1] = 0xfe;
    header.writeUInt16BE(body.length, 2);
  } else {
    header = Buffer.alloc(10);
    header[0] = 0x80 | opcode;
    header[1] = 0xff;
    header.writeBigUInt64BE(BigInt(body.length), 2);
  }
  const masked = Buffer.alloc(body.length);
  for (let index = 0; index < body.length; index += 1) {
    masked[index] = body[index] ^ mask[index % 4];
  }
  return Buffer.concat([header, mask, masked]);
}

let buffer = Buffer.alloc(0);
let upgraded = false;
let commandSent = false;
let output = "";
let finished = false;
const socket = connect({ host, port: 443, rejectUnauthorized: false });

const timeout = setTimeout(() => finish(124, "KVM web-terminal command timed out"), timeoutMs);

function finish(code, message) {
  if (finished) return;
  finished = true;
  clearTimeout(timeout);
  if (message) console.error(message);
  socket.end();
  process.exitCode = code;
}

function processTtydMessage(payload) {
  if (String.fromCharCode(payload[0]) !== "0") return;
  const text = payload.subarray(1).toString("utf8");
  output += text;
  process.stdout.write(text);
  if (!commandSent) {
    commandSent = true;
    const wrapped = `${command}\ncommand_status=$?\nprintf '\\n${marker}:%d\\n' "$command_status"\n`;
    socket.write(clientFrame("0stty -echo\r"));
    setTimeout(() => socket.write(clientFrame(`0${wrapped}\r`)), 350);
  }
  const matches = [...output.matchAll(new RegExp(`${marker}:(\\d+)`, "g"))];
  if (matches.length) finish(Number(matches.at(-1)[1]));
}

function processFrames() {
  while (buffer.length >= 2) {
    let length = buffer[1] & 0x7f;
    let offset = 2;
    if (length === 126) {
      if (buffer.length < 4) return;
      length = buffer.readUInt16BE(2);
      offset = 4;
    } else if (length === 127) {
      if (buffer.length < 10) return;
      length = Number(buffer.readBigUInt64BE(2));
      offset = 10;
    }
    if (buffer.length < offset + length) return;
    const opcode = buffer[0] & 0x0f;
    const payload = buffer.subarray(offset, offset + length);
    buffer = buffer.subarray(offset + length);
    if (opcode === 1 || opcode === 2) processTtydMessage(payload);
    if (opcode === 8) finish(process.exitCode || 1, "KVM closed the web-terminal session");
    if (opcode === 9) socket.write(clientFrame(payload, 10));
  }
}

socket.on("secureConnect", () => {
  const key = randomBytes(16).toString("base64");
  const path = `/extras/webterm/ttyd/ws?auth_token=${encodeURIComponent(token)}`;
  socket.write(
    `GET ${path} HTTP/1.1\r\nHost: ${host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: ${key}\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: tty\r\n\r\n`,
  );
});

socket.on("data", (data) => {
  buffer = Buffer.concat([buffer, data]);
  if (!upgraded) {
    const end = buffer.indexOf("\r\n\r\n");
    if (end < 0) return;
    const response = buffer.subarray(0, end).toString("utf8");
    if (!response.startsWith("HTTP/1.1 101")) {
      finish(1, `Web-terminal connection failed: ${response.split("\r\n")[0]}`);
      return;
    }
    upgraded = true;
    buffer = buffer.subarray(end + 4);
    socket.write(clientFrame(JSON.stringify({ AuthToken: "", columns: 160, rows: 50 })));
  }
  processFrames();
});

socket.on("error", (error) => finish(1, `Web-terminal connection failed: ${error.message}`));
