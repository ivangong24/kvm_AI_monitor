# KVM AI Monitor Push Protocol v1

This document is the implementation contract for the outbound-only device push architecture
recommended in `PROJECT_CHECKPOINT_2026-07-18.md`. It covers device enrollment, request signing,
the two push endpoints, receiver merge rules, and the privacy filter. Provider credentials never
leave the monitored Mac; only whitelisted aggregate fields reach the KVM.

## Roles

- **KVM receiver** (`kvm-agent/push_receiver.py`, wired into `kvm-agent/agent.py`): verifies and
  stores pushed aggregates, merges them into the display snapshot, and expires working state.
- **macOS helper** (`mac-helper/`): a LaunchAgent in the logged-in GUI session that reads Claude
  usage natively (Keychain consent works there), reduces it to the strict schema below, signs, and
  POSTs it to the KVM once per minute. The Mac listens on no port.
- **Claude Code hooks** (`mac-helper/`): lifecycle hook commands that send minimal signed activity
  events (`start` / `active` / `stop`) so the KVM can animate exact working state from any enrolled
  device, including devices without Remote Login.

## Enrollment

- Devices are enrolled on the AI Usage admin page. The KVM generates:
  - `id`: `d-` + 8 lowercase hex characters (`secrets.token_hex(4)`).
  - `secret`: 48 lowercase hex characters (`secrets.token_hex(24)`), shown exactly once at
    creation/rotation and stored on the KVM in `/etc/kvmd/user/ai-usage/devices.json` (mode 0600).
- `devices.json` schema:

```json
{
  "devices": [
    {
      "id": "d-1a2b3c4d",
      "name": "Mac mini",
      "secret": "…48 hex…",
      "createdAt": "2026-07-18T00:00:00Z",
      "revoked": false,
      "lastSeenAt": null,
      "lastUsageAt": null,
      "lastActivityAt": null
    }
  ]
}
```

- Device `name` is free text, 1–48 characters, control characters stripped.
- Revoked devices are rejected with 401 but their record is retained until deleted.
- On the Mac, the secret is stored only in the login Keychain
  (service `kvm-ai-monitor-push:<kvm-host>`, account `device`), written via
  `security add-generic-password`. It is never written to disk or logs.

## Transport

- Public URL: `https://<comet-address>/extras/ai-usage/push/v1/<endpoint>`.
- nginx location `/extras/ai-usage/push/` proxies to the agent **without** `loc-login.conf` and
  with `auth_request off;` — the GL firmware's `gl.ctx-server.conf` enables `auth_request
  /auth_check;` at the server level, so every unauthenticated location must opt out explicitly.
  Authentication for pushes is the HMAC signature below.
- The Comet uses a self-signed certificate; clients send `curl -k` style requests. HMAC provides
  integrity/authenticity independent of TLS; payloads contain only aggregate numbers.
- The agent's HTTP server receives the path with the `/extras/ai-usage` prefix stripped, i.e.
  `/push/v1/usage` or `/push/v1/activity`. **The signed path is always this backend path.**

## Request signing

Headers on every push request:

| Header | Value |
| --- | --- |
| `X-KVM-Device` | device `id` |
| `X-KVM-Timestamp` | Unix seconds, integer decimal |
| `X-KVM-Nonce` | 16–64 lowercase hex characters, fresh random per request |
| `X-KVM-Signature` | 64 lowercase hex characters, computed below |

```
string_to_sign = "v1\n" + timestamp + "\n" + nonce + "\n" + method + "\n" + path + "\n"
                 + sha256_hex(exact_body_bytes)
signature      = hex(HMAC_SHA256(key = ASCII bytes of the secret, msg = string_to_sign))
```

- `method` is uppercase (`POST`). `path` is the backend path (`/push/v1/usage`).
- Receiver checks, in order: known non-revoked device; `|now - timestamp| <= 120` seconds; nonce
  not seen for this device within the last 10 minutes; signature matches via
  `hmac.compare_digest`. Any failure → `401 {"error": "unauthorized"}` with no further detail.
- Body limit 64 KB → `413`. Malformed or non-whitelisted schema → `400`. Per-device rate limit
  120 requests per 60 s → `429`. Success → `200 {"ok": true}`.
- Nonce cache and rate limiter are in-memory; the 120 s timestamp window bounds replay after a
  receiver restart.

### Test vector (both sides must reproduce)

```
secret     = "0123456789abcdef0123456789abcdef0123456789abcdef"
timestamp  = 1752800000
nonce      = "abcdef0123456789"
method     = "POST"
path       = "/push/v1/activity"
body       = {"event":"active","provider":"claude","schemaVersion":1}   (exact bytes, no newline)
sha256(body) = 9eeaf4fb5632bdd74905036336c2fc7440ab02d657fc97f711ff32d288712979
signature    = 4d23e79d847fee9e540fa1b24e8328681fab6e77043fff7d0503cef0f2832ead
```

## `POST /push/v1/usage`

Body (all fields optional except `schemaVersion` and `provider`; unknown fields are dropped,
never stored, never echoed):

```json
{
  "schemaVersion": 1,
  "provider": "claude",
  "collectedAt": "2026-07-18T01:00:00Z",
  "plan": "Max",
  "loggedIn": true,
  "limits": [
    {"label": "Current session", "usedPercent": 34, "windowMinutes": 300,
     "resetsAt": "2026-07-18T04:00:00Z"}
  ],
  "daily": [
    {"date": "2026-07-18", "totalTokens": 1234, "inputTokens": 400, "outputTokens": 300,
     "cacheReadTokens": 500, "cacheCreationTokens": 34}
  ]
}
```

Receiver whitelist / sanitation:

- `provider` ∈ {claude, codex, copilot, gemini, grok}.
- `plan`: string ≤ 40 chars, control characters stripped.
- `limits`: ≤ 8 entries; `label` string ≤ 40 chars; `usedPercent` clamped 0–100;
  `windowMinutes` positive finite number; `resetsAt` ISO-8601 string or dropped.
- `daily`: ≤ 31 entries; `date` must parse as `YYYY-MM-DD`, within the last 30 days through
  today; token fields non-negative finite numbers, missing → 0.
- Forbidden anywhere: emails, tokens, prompts, paths, project names, session ids — anything not
  named above is discarded at ingest and must have a test proving non-disclosure.

Storage: latest accepted snapshot per (device, provider) is persisted atomically to
`/etc/kvmd/user/ai-usage/push-state.json` (mode 0600) so a KVM restart retains the last usage.

## `POST /push/v1/activity`

```json
{"schemaVersion": 1, "provider": "claude", "event": "start" | "active" | "stop"}
```

- `start` / `active` → the receiver sets `workingUntil[(device, provider)] = now + 120 s`.
- `stop` → clears that entry immediately.
- Activity state is in-memory only and expires on its own; a lost `stop` costs at most 120 s of
  animation.

## Merge rules on the KVM

1. **Working state**: a provider is `working` when the SSH probe reports it on the primary or an
   enrolled SSH activity host, **or** any push device has an unexpired `workingUntil` for it.
   Push-sourced work is reported as `authorizedDeviceWorking` (`workingSource:
   "authorized_device"`) unless the SSH primary is also working. SSH probe failure must not
   suppress unexpired push activity, and vice versa.
2. **Usage overlay** (per provider, applied after `build_usage_snapshot`): when at least one
   device has pushed usage for the provider —
   - `plan` and `limits` come from the most recently pushed snapshot that includes them.
   - `daily` becomes the per-date sum across the latest snapshot of each device. SSH-collected
     local daily totals are used only when no push supplies `daily` (prevents double counting;
     install the helper on the primary Mac too for its tokens to be counted).
   - When pushed data includes `loggedIn: true` and any usage, the provider's
     `connectionState` becomes `ready` and `source` becomes `"Device helper push"`.
3. **Degraded rendering**: `verification_required` with local token totals available renders the
   normal usage panel (status text stays `CHECK`), not the setup screen.
4. Last successful aggregates are retained when a device goes offline; only working state expires.

## Admin API (behind the existing Comet-authenticated location)

- `GET  /api/devices` → `{"devices": [{id, name, createdAt, revoked, lastSeenAt, lastUsageAt,
  lastActivityAt}]}` — never includes secrets.
- `POST /api/devices` `{"name": "Mac mini"}` → `{id, name, secret}` (only time the secret is
  returned).
- `POST /api/devices/rotate` `{"id"}` → `{id, secret}`.
- `POST /api/devices/revoke` `{"id"}` → `{"ok": true}`.
- `POST /api/devices/delete` `{"id"}` → `{"ok": true}`.

## SSH activity host addressing (fix shipped alongside)

`activityHosts` entries accept `[user@]host[:port]`; user and port default to the primary
device's values. A trailing `:NNN` is a port only when `NNN` is all digits (IPv6 literals keep
their colons). The probe must use each entry's own user/port.

## Security constraints (unchanged from the checkpoint)

No inbound listener on any Mac; no iCloud or cloud relay; no CodexBar; no Keychain ACL changes;
no credential export; no prompts/paths/projects/identity on the KVM; quota-delta inference is
never described as live working state; enrollment stays explicit and revocable.
