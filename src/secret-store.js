// Cross-platform storage for the Comet admin session token.
//
// macOS keeps secrets in the login Keychain via /usr/bin/security. Windows has no equivalent
// CLI that can read a secret back out, so we talk to the Credential Manager API (advapi32)
// directly through a short PowerShell shim. That keeps the dependency list empty: every
// Windows install already has powershell.exe and advapi32.
//
// Secrets are passed to PowerShell through the environment, never argv, so they do not show
// up in the process list.

import { execFileSync } from "node:child_process";

const CRED_TYPE_GENERIC = 1;
const CRED_PERSIST_LOCAL_MACHINE = 2;

// --- macOS ------------------------------------------------------------------------------

function macRun(args) {
  return execFileSync("/usr/bin/security", args, {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
  }).trim();
}

const macStore = {
  get(service, account) {
    return macRun(["find-generic-password", "-a", account, "-s", service, "-w"]);
  },
  set(service, account, secret) {
    macRun(["add-generic-password", "-a", account, "-s", service, "-w", secret, "-U"]);
  },
  delete(service, account) {
    macRun(["delete-generic-password", "-a", account, "-s", service]);
  },
};

// --- Windows ----------------------------------------------------------------------------

// One script handles all three operations; KVMSS_OP selects which. Compiled once per call,
// which costs ~200ms — fine for a setup wizard that touches the store a handful of times.
const WINDOWS_SHIM = String.raw`
$ErrorActionPreference = 'Stop'
# Without this, PowerShell emits a CLIXML progress record ("Preparing modules for first use")
# onto stderr and muddies our error reporting.
$ProgressPreference = 'SilentlyContinue'
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;

public class KvmCred {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct CREDENTIAL {
        public uint Flags;
        public uint Type;
        public IntPtr TargetName;
        public IntPtr Comment;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
        public uint CredentialBlobSize;
        public IntPtr CredentialBlob;
        public uint Persist;
        public uint AttributeCount;
        public IntPtr Attributes;
        public IntPtr TargetAlias;
        public IntPtr UserName;
    }

    // CharSet.Unicode is required: without it .NET marshals the target name as ANSI and the
    // W entry point silently looks up the wrong name (fails with ERROR_NOT_FOUND / 1168).
    [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode, EntryPoint = "CredReadW")]
    private static extern bool CredRead(string target, uint type, uint flags, out IntPtr cred);

    [DllImport("advapi32.dll", SetLastError = true, EntryPoint = "CredWriteW")]
    private static extern bool CredWrite(ref CREDENTIAL cred, uint flags);

    [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode, EntryPoint = "CredDeleteW")]
    private static extern bool CredDelete(string target, uint type, uint flags);

    [DllImport("advapi32.dll")]
    private static extern void CredFree(IntPtr cred);

    public static string Read(string target, uint type) {
        IntPtr handle;
        if (!CredRead(target, type, 0, out handle)) {
            throw new Exception("credential not found (error " + Marshal.GetLastWin32Error() + ")");
        }
        try {
            CREDENTIAL cred = (CREDENTIAL)Marshal.PtrToStructure(handle, typeof(CREDENTIAL));
            byte[] blob = new byte[cred.CredentialBlobSize];
            Marshal.Copy(cred.CredentialBlob, blob, 0, (int)cred.CredentialBlobSize);
            return Encoding.UTF8.GetString(blob);
        } finally {
            CredFree(handle);
        }
    }

    public static void Write(string target, string user, string secret, uint type, uint persist) {
        byte[] blob = Encoding.UTF8.GetBytes(secret);
        CREDENTIAL cred = new CREDENTIAL();
        cred.Type = type;
        cred.Persist = persist;
        cred.TargetName = Marshal.StringToCoTaskMemUni(target);
        cred.UserName = Marshal.StringToCoTaskMemUni(user);
        cred.CredentialBlob = Marshal.AllocCoTaskMem(blob.Length);
        cred.CredentialBlobSize = (uint)blob.Length;
        Marshal.Copy(blob, 0, cred.CredentialBlob, blob.Length);
        try {
            if (!CredWrite(ref cred, 0)) {
                throw new Exception("could not save credential (error " + Marshal.GetLastWin32Error() + ")");
            }
        } finally {
            Marshal.FreeCoTaskMem(cred.TargetName);
            Marshal.FreeCoTaskMem(cred.UserName);
            Marshal.FreeCoTaskMem(cred.CredentialBlob);
        }
    }

    public static void Delete(string target, uint type) {
        if (!CredDelete(target, type, 0)) {
            throw new Exception("could not delete credential (error " + Marshal.GetLastWin32Error() + ")");
        }
    }
}
'@

$target = $env:KVMSS_TARGET
switch ($env:KVMSS_OP) {
    'get'    { [Console]::Out.Write([KvmCred]::Read($target, KVMSS_TYPE)) }
    'set'    { [KvmCred]::Write($target, $env:KVMSS_USER, $env:KVMSS_SECRET, KVMSS_TYPE, KVMSS_PERSIST) }
    'delete' { [KvmCred]::Delete($target, KVMSS_TYPE) }
    default  { throw "unknown operation" }
}
`
  .replaceAll("KVMSS_TYPE", String(CRED_TYPE_GENERIC))
  .replaceAll("KVMSS_PERSIST", String(CRED_PERSIST_LOCAL_MACHINE));

// PowerShell reports failures as CLIXML on stderr. Pull out the human-readable part so callers
// see "credential not found", not a 4KB base64 command echo.
function readableError(stderr) {
  const messages = [...`${stderr ?? ""}`.matchAll(/<S S="Error">([^<]*)<\/S>/g)]
    .map((match) => match[1].replaceAll("_x000D_", "").replaceAll("_x000A_", "").trim())
    .filter(Boolean);
  const summary = messages.find((line) => !line.startsWith("+") && !line.startsWith("At line:"));
  const fallback = `${stderr ?? ""}`.trim() || "unknown error";
  return (summary ?? messages[0] ?? fallback)
    .replace(/^Exception calling "\w+" with "\d+" argument\(s\): "?/, "")
    .replace(/"$/, "");
}

function windowsRun(op, service, account, secret) {
  // -EncodedCommand takes UTF-16LE base64, which sidesteps every layer of shell quoting.
  const encoded = Buffer.from(WINDOWS_SHIM, "utf16le").toString("base64");
  try {
    return execFileSync(
      "powershell.exe",
      ["-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
      {
        encoding: "utf8",
        stdio: ["ignore", "pipe", "pipe"],
        env: {
          ...process.env,
          KVMSS_OP: op,
          KVMSS_TARGET: service,
          KVMSS_USER: account,
          KVMSS_SECRET: secret ?? "",
        },
      },
    ).trim();
  } catch (error) {
    throw new Error(readableError(error.stderr));
  }
}

const windowsStore = {
  get: (service, account) => windowsRun("get", service, account),
  set: (service, account, secret) => void windowsRun("set", service, account, secret),
  delete: (service, account) => void windowsRun("delete", service, account),
};

// --- dispatch ---------------------------------------------------------------------------

function store() {
  if (process.platform === "darwin") return macStore;
  if (process.platform === "win32") return windowsStore;
  throw new Error(
    `No supported secret store on ${process.platform}. Set KVM_TOKEN in the environment instead.`,
  );
}

/** Read a secret. Throws if it is missing — callers treat that as "not authorized yet". */
export function getSecret(service, account = "admin") {
  return store().get(service, account);
}

/** Create or replace a secret. */
export function setSecret(service, secret, account = "admin") {
  store().set(service, account, secret);
}

/** Remove a secret, ignoring the case where it was never stored. */
export function deleteSecret(service, account = "admin") {
  try {
    store().delete(service, account);
  } catch {
    // Nothing saved under this service, or the platform has no store: nothing to clean up.
  }
}

/** Human-readable name for the backing store, for messages like "saved to your Keychain". */
export function secretStoreName() {
  if (process.platform === "darwin") return "Keychain";
  if (process.platform === "win32") return "Windows Credential Manager";
  return "secret store";
}
