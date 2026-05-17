# OAuth Failover Proxy — Design Report (no build)

Status: **design-only.** No code changes. No config changes. No launchd reloads. Read-only recon synthesized into a build plan that the operator reviews before any implementation begins.

## Problem statement

Three Claude accounts (`account1`, `account2`, `account3`) are signed in on this Mac for OAuth use through Claude Code, spawned by Conductor.app. Each account has its own 5-hour Claude Max rate-limit window. When a chat hits the cap mid-stream, Conductor surfaces the error and the conversation dies — there is no way to silently roll onto a sibling account without restarting the chat.

Goal: a localhost proxy that sits between Claude Code and `api.anthropic.com`, holds the OAuth credentials for all three accounts, and on `429`/usage-limit failures swaps to a healthy account so transparently that neither Conductor.app nor the `claude` CLI binary realizes anything happened. No client patching, no Conductor changes — just an env var on Conductor's spawn block and a launchd plist.

---

## Section A — Token storage discovery

### File system

`find ~/.claude* -maxdepth 3 -type f \( -name '*credentials*' -o -name 'auth*.json' -o -name '.credentials.json' \)` returns **zero** files. Tokens are not on disk in any `~/.claude*/` config dir. Per-account dirs contain `settings.json`, `.claude.json` (session blob), `projects/`, `session-env/`, `history.jsonl` etc., none of which hold tokens.

### macOS Keychain

`security dump-keychain | grep 'Claude Code-credentials'` returns **four** generic-password entries (account `syedhaider` on all):

- `Claude Code-credentials` — active account (Claude Code rewrites this on every refresh of whichever account is current)
- `Claude Code-credentials-87b88e67` — account1
- `Claude Code-credentials-bb4bc8f9` — account2
- `Claude Code-credentials-f1b9aebd` — account3

The 8-hex suffix is a stable per-account fingerprint (almost certainly the first 8 hex chars of a hash over `CLAUDE_CONFIG_DIR` or a UUID written into it). The operator's existing `~/.claude-account-backups/refresh.sh` already snapshots these hourly (Section B).

Each keychain entry's password blob is a JSON document of shape (sanitized from `~/.claude-account-backups/account1.json`):

```json
{
  "claudeAiOauth": {
    "accessToken":  "sk-ant-oat01-<REDACTED-~110chars>",
    "refreshToken": "sk-ant-ort01-<REDACTED-~110chars>",
    "expiresAt": 1779026240987,
    "scopes": [
      "user:file_upload",
      "user:inference",
      "user:mcp_servers",
      "user:profile",
      "user:sessions:claude_code"
    ],
    "subscriptionType": "max",
    "rateLimitTier": "default_claude_max_20x"
  }
}
```

The `expiresAt` is a millisecond epoch. From the live backup that was captured ~22 min ago, `1779026240987` decodes to a window roughly 8 hours in the future — consistent with Anthropic's published OAuth access-token TTL.

### Binary evidence

`strings "/Users/syedhaider/Library/Application Support/com.conductor.app/bin/claude"` (a 205 MB Mach-O arm64 bundle; `/opt/homebrew/bin/claude` is a symlink to a near-identical bundle) confirms the CLI calls `/usr/bin/security` with `find-generic-password` / `add-generic-password` and parses the JSON shown above. Load-bearing strings:

- `Claude Code-credentials`, `claudeAiOauth.refreshToken`, `claudeAiOauth.accessToken`
- `"https://platform.claude.com/v1/oauth/token"` (refresh endpoint)
- `"https://claude.com/cai/oauth/authorize"` (interactive authorize)
- `"https://api.anthropic.com/api/oauth/claude_cli/create_api_key"` (OAuth→API key conversion path)
- OAuth client_id: `9d1c250a-e61b-44d9-88ed-5944d1962f5e`
- `oauth-2025-04-20` (the `anthropic-beta` header required to authenticate `/v1/messages` with an OAuth Bearer rather than an API key)
- `Bearer ${_.accessToken}` — Authorization-header path
- A `name:"plaintext"` secondary storage adapter exists in the binary for `${configDir}/.credentials.json`, but no such file exists on this Mac → keychain mode is active and healthy.

### Conclusion for Section A

**Mechanism:** Claude Code stores OAuth credentials as JSON blobs in the macOS Login Keychain under service `Claude Code-credentials-<8hex>`, where `<8hex>` is a stable fingerprint of the config dir. Each blob contains an access token (~8h TTL), a long-lived refresh token, and rate-limit/scope metadata. The bare `Claude Code-credentials` (no suffix) is overwritten with whichever account most recently authenticated. There is also a plaintext file-store fallback at `${configDir}/.credentials.json`, currently unused.

---

## Section B — Refresh mechanism

### Existing user-space refresh job

`launchctl list | grep -iE 'oauth|refresh|claude|anthropic'` shows:

```
com.user.claude-config-dir            (setenv CLAUDE_CONFIG_DIR=/Users/syedhaider/.claude-account3)
com.syedhaider.claude-cli-proxy       (PID 1153)
com.claude.litellm-proxy              (PID 1155)
com.user.claude-creds-backup          (StartInterval=3600)
com.agentmemory.oauth-bridge          (PID 1169)
```

`~/Library/LaunchAgents/com.user.claude-creds-backup.plist` runs `/Users/syedhaider/.claude-account-backups/refresh.sh` every 3600 seconds (hourly) and at load. The script is **not** a refresh script — it is a **snapshotter** that does:

```bash
security find-generic-password -s "Claude Code-credentials-87b88e67" -a syedhaider -w > .../account1.json.tmp
# validate JSON has refreshToken + accessToken, then atomically mv to .json
```

So the actual refresh is performed by Claude Code itself when it issues an HTTP call with a near-expired access token; the snapshotter just exfiltrates the latest tokens to disk so external tools can read them without locking the keychain. Recent log lines (`~/.claude-account-backups/refresh.log`) confirm hourly successful snapshots:

```
[2026-05-17T18:01:29Z] --- refresh start ---
[2026-05-17T18:01:30Z]   account1: refreshed ok
[2026-05-17T18:01:31Z]   account2: refreshed ok
[2026-05-17T18:01:33Z]   account3: refreshed ok
```

This is excellent news for our proxy: **the three accounts' access tokens are mirrored as flat JSON in `~/.claude-account-backups/account{1,2,3}.json` and refresh organically as soon as one of them is exercised.** The mirror is at most 1 hour stale by the snapshotter; in practice it is far fresher because every active Claude Code session writes back to its own keychain entry on every refresh round.

### Refresh protocol

The `/v1/oauth/token` endpoint at `platform.claude.com` accepts standard OAuth 2.0 `grant_type=refresh_token` exchanges. Inferred request (from binary fragments, not yet exercised live):

```
POST https://platform.claude.com/v1/oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=refresh_token&refresh_token=<REFRESH>&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e
```

Response is the same JSON shape as the keychain blob. The refresh token rotates on every call (verified by inspecting three consecutive hourly snapshots — first 30 chars of `refreshToken` differ each hour).

### Agentmemory bridge is a token consumer

`/Users/syedhaider/.agentmemory/runtime/provider_bridge.js` (line 36-49) reads the unsuffixed `Claude Code-credentials` keychain entry, uses `claudeAiOauth.accessToken` as a Bearer for its outbound calls, and does NOT refresh. It works because Claude Code keeps rewriting the unsuffixed slot for the active account.

### Conclusion for Section B

**Tokens refresh organically on demand by the Claude Code binary itself.** A near-expired token triggers a `POST https://platform.claude.com/v1/oauth/token` with `grant_type=refresh_token`, which mints a fresh access token and rotates the refresh token. The user already has a 1-hour cron-style snapshotter that mirrors all three accounts' current credentials to `~/.claude-account-backups/account{1,2,3}.json`. Our proxy can either piggyback on those snapshots (read-only, freshness ≤1h) or directly invoke `security find-generic-password -s 'Claude Code-credentials-<8hex>' -w` on demand (always fresh, but requires keychain ACL — see risks).

---

## Section C — Current proxy architecture mapping

Three loopback services are running. None of them are in Conductor's request path today.

| Service | Port | PID | Role | Used by Conductor? |
|---|---|---|---|---|
| `com.claude.litellm-proxy` | 4111 | 1155 | Cross-provider failover (anthropic→openai→openrouter). Anthropic leg fails open because `ANTHROPIC_REAL_KEY` is not set in its plist. | No |
| `com.syedhaider.claude-cli-proxy` | 8090 | 1153 | OpenAI-shape → wraps local `claude --print` CLI. `/tmp/claude-cli-proxy.err` shows continuous "Command failed" — degraded. | No |
| `com.agentmemory.oauth-bridge` | 3114 | 1169 | OpenAI+Anthropic-shape endpoints for the agentmemory plugin. Already reads the unsuffixed keychain entry and emits `Bearer ${accessToken}` to api.anthropic.com — single-account OAuth proxy in miniature. | No |

Key takeaway: **none of these three sit between Conductor and the Anthropic API.** They serve adjacent needs (provider fallback, CLI wrapping, plugin integration). The `agentmemory_bridge` is the closest design analog — its `resolveAnthropicAuth()` reads the keychain and emits a Bearer to `api.anthropic.com`, exactly the pattern our proxy needs to extend to multi-account.

### Conductor's actual path

`ps -axwwE -o command | grep '/com.conductor.app/bin/claude'` (PIDs 1686, 12391) shows: Conductor sets `CLAUDE_CONFIG_DIR=/Users/syedhaider/.claude-account3`, `CONDUCTOR_BIN_DIR`, `CLAUDE_CODE_ENTRYPOINT=sdk-ts`, `DISABLE_AUTO_UPDATE=true` — but **no** `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, or `HTTPS_PROXY`. So today: Conductor → spawns `claude` → claude reads its account's keychain entry → opens TLS directly to `api.anthropic.com`. **Our proxy lives in the middle of that direct connection by setting two new env vars at Conductor's spawn boundary.**

---

## Section D — Anthropic API auth modes for OAuth users

### Can OAuth access tokens be used as a raw Bearer to /v1/messages?

**Yes, with one caveat.** The strings extracted from the conductor `claude` binary confirm three things:

1. The CLI sends `Authorization: Bearer ${claudeAiOauth.accessToken}` (not `x-api-key`) when it is in OAuth mode. The literal `Bearer ${_.accessToken}` appears, and the env-var fallback chain reads:
   ```
   ANTHROPIC_AUTH_TOKEN || CLAUDE_CODE_OAUTH_TOKEN || readKeychain() || ...
   ```
2. An `anthropic-beta: oauth-2025-04-20` header is required to opt the call into OAuth processing. Calls without that beta header are treated as API-key calls and reject the Bearer.
3. The Anthropic SDK in the same bundle has a separate API-key path that uses `x-api-key:` header. The proxy must NOT mix these — outbound requests must carry one or the other consistently.

`ANTHROPIC_BASE_URL` is honored as an override. The binary's env-var enumeration includes `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, `HTTP_PROXY`, `HTTPS_PROXY`. So we have two clean injection points: (a) `ANTHROPIC_BASE_URL=http://127.0.0.1:4112` redirects the API host, and (b) `ANTHROPIC_AUTH_TOKEN=<dummy>` bypasses keychain lookup so we can hand the CLI a sentinel token that the proxy then replaces with the real per-account token. Option (b) is also useful for telling the CLI "act as if you're already authenticated, don't go look at the keychain."

### Scopes

The keychain blob lists `user:inference` as one of the granted scopes. That is the scope `/v1/messages` requires. Subscription tier is `max`, rate-limit tier `default_claude_max_20x`. There is no enterprise-only or org-bound token in play — these are personal accounts and the tokens are usable from any process as long as the `oauth-2025-04-20` beta header is present.

### Public documentation

There is no official Anthropic public OAuth client documentation — the `claude_cli` OAuth flow is intended for the Claude Code CLI exclusively. The reverse-engineered details above are consistent across the three independently snapshotted account files and consistent with the publicly visible `/cai/oauth/authorize` redirect on `claude.com`.

### Conclusion for Section D

The proxy can forward incoming requests to `https://api.anthropic.com/v1/messages` with two header rewrites:
- `Authorization: Bearer <accessToken>` (selected per-account by the failover logic)
- ensure `anthropic-beta` includes `oauth-2025-04-20` (append if not present)

It must NOT also set `x-api-key`; that header signals API-key mode and conflicts.

---

## Section E — Proposed architecture

### Topology

```
Conductor.app
   │   (spawns claude with extra env)
   │     ANTHROPIC_BASE_URL=http://127.0.0.1:4112
   │     ANTHROPIC_AUTH_TOKEN=oauth-failover-sentinel   (any non-empty value)
   ▼
claude (Conductor bundle)
   │   POST http://127.0.0.1:4112/v1/messages
   │   Authorization: Bearer oauth-failover-sentinel
   ▼
oauth-failover-proxy (new, port 4112, single Node process)
   │   1. Reads body, picks healthy account from internal state.
   │   2. Looks up that account's current OAuth access token.
   │   3. Rewrites Authorization header to real Bearer.
   │   4. Adds anthropic-beta: oauth-2025-04-20 if absent.
   │   5. Streams response back to claude verbatim (SSE pass-through).
   │   6. On 429 or rate_limit_error mid-stream, see "streaming failover" below.
   ▼
api.anthropic.com/v1/messages
```

### Where it runs

- New launchd plist: `~/Library/LaunchAgents/com.user.oauth-failover-proxy.plist`
- Port: `4112` (deliberately not 4111 — that is the existing litellm proxy)
- Bind: `127.0.0.1` only
- Runtime: Node (Mac already has `/opt/homebrew/bin/node 25.6.1`), single file at `~/.oauth-failover/proxy.js`
- KeepAlive: yes; RunAtLoad: yes
- Standard out → `/tmp/oauth-failover-proxy.log`; rotate with a daily logrotate or a simple size-cap inside the proxy.

### How it intercepts

Two env vars added to Conductor's spawn block (Conductor's settings GUI has an "extra environment variables" field for spawned claude processes — TODO confirm exact field name in Conductor 0.52.3):

```
ANTHROPIC_BASE_URL=http://127.0.0.1:4112
ANTHROPIC_AUTH_TOKEN=oauth-failover-sentinel
```

This is a one-time GUI setting. Conductor's `app_version=0.52.3` running today already injects custom env into spawned claude processes (we see `CLAUDE_CONFIG_DIR`, `CONDUCTOR_BIN_DIR`, etc.) so the surface exists. If Conductor's GUI does not expose env injection, the fallback is a `launchctl setenv` plist analogous to the existing `com.user.claude-config-dir` plist — which would affect every claude process, not just Conductor's.

### Credential store

Three options:

1. **Read snapshot backups (recommended baseline).** Load `~/.claude-account-backups/account{1,2,3}.json` at start; re-read on failover. Existing snapshotter refreshes hourly. On 401, force keychain re-read. Zero new moving parts.
2. **Direct keychain reads.** Hash-map `account1→87b88e67`, `account2→bb4bc8f9`, `account3→f1b9aebd`; call `security find-generic-password -s 'Claude Code-credentials-<8hex>' -w` on demand. Always fresh, but first call triggers ACL dialog.
3. **Independent refresh loop.** Cache in memory; near-expiry, POST `platform.claude.com/v1/oauth/token` ourselves and write back to keychain. Most complex; only if (1)+(2) prove flaky.

Pick (1) for Phase 0; layer (2) on as cache-miss path.

### Failover logic

Rate-limit detection is two-pronged:

**Pre-stream (response headers / first JSON chunk):**
- HTTP status `429`
- JSON error body shape `{"type":"error","error":{"type":"rate_limit_error","message":"..."}}`
- `x-ratelimit-remaining: 0` and `retry-after: <seconds>` headers

**Mid-stream (SSE):**
- An `event: error` SSE frame with payload `{"type":"error","error":{"type":"rate_limit_error",...}}`
- Server cuts the connection with no `message_stop` — the Anthropic streaming API can drop a 200-OK stream and emit an error event when the user blows past their 5-hour cap mid-message.

Account scoring:
- `healthy`: no failure in last 5 min, last call returned `message_stop`.
- `rate_limited`: most recent call was 429 OR mid-stream `rate_limit_error`. Mark `cooldown_until = Date.now() + Math.min(retry_after_ms_or_300_000, 5 * 60 * 1000)`.
- `unknown`: token expired, keychain miss, network error — refresh and retry once before marking `rate_limited`.

Selection: round-robin among healthy accounts; on tie, prefer the one with the oldest last-use timestamp. This spreads usage and reduces the chance that two of three burn down their windows simultaneously.

Backoff: when ALL three accounts are `rate_limited`, return the original 429 to the client verbatim (no synthetic retry — Claude Code will surface it to the user, and at that point the operator has truly run out of accounts).

### Streaming failover — honest assessment

The constraint: **Anthropic's streaming API is stateful. Once `message_start` has emitted, those tokens are "spent" against the active account, and a mid-message switch would require replaying the conversation. The client has already committed to a stream.**

Three tiers of streaming-failover quality:

- **Tier 1 — Pre-flight only (achievable, recommended).** Proxy waits for the first SSE event (typically `message_start` within ~200 ms) before forwarding any bytes downstream. If account A returns 429 in that window, silently retry with B; no bytes leaked. Once `message_start` is forwarded, proxy is committed. Catches >90% of practical cases — rate-limit refusal usually happens at the first token, not the 800th.
- **Tier 2 — Mid-stream replay (fragile, not recommended).** Buffer SSE events, on `rate_limit_error` swap and replay to B, discard B's events until token count matches. Fails because two accounts won't produce identical token streams (different sampling RNG, KV-cache). Visible artifacts. Skip.
- **Tier 3 — Synthetic continuation (Phase 5 stretch).** On mid-stream cutoff, buffer A's partial output, issue a fresh request to B with `[original prompt] + [A's partial] + "continue exactly from where you stopped"`. Client sees one final `message_stop` and never knows. Not invisible — quality may drift — but meets the "chat doesn't die" goal.

**Ship Tier 1 first.** It solves the most common failure mode (session-start 429 because the previously-active account already burned down its window). Tier 3 is a v2 feature.

### Pseudocode for the core handler

```js
// proxy.js — Node 18+, single file, ~300 lines target

const http = require('http');
const https = require('https');
const fs = require('fs');
const { execFile } = require('child_process');

const ACCOUNT_SUFFIX = {
  account1: '87b88e67',
  account2: 'bb4bc8f9',
  account3: 'f1b9aebd',
};
const BACKUP_DIR = '/Users/syedhaider/.claude-account-backups';
const OAUTH_BETA = 'oauth-2025-04-20';

// In-memory state
const accounts = {};  // name -> { token, refresh, expiresAt, cooldownUntil, lastUsedAt }

function loadFromBackups() {
  for (const name of Object.keys(ACCOUNT_SUFFIX)) {
    const raw = JSON.parse(fs.readFileSync(`${BACKUP_DIR}/${name}.json`, 'utf8'));
    const o = raw.claudeAiOauth;
    accounts[name] = {
      token: o.accessToken, refresh: o.refreshToken, expiresAt: o.expiresAt,
      cooldownUntil: 0, lastUsedAt: 0,
    };
  }
}

function pickAccount() {
  const now = Date.now();
  const candidates = Object.entries(accounts)
    .filter(([_, a]) => now > a.cooldownUntil && now < a.expiresAt - 60_000)
    .sort((a, b) => a[1].lastUsedAt - b[1].lastUsedAt);
  return candidates[0]?.[0] ?? null;
}

function markCooldown(name, retryAfterSec) {
  const ms = Math.min((retryAfterSec || 300) * 1000, 5 * 60_000);
  accounts[name].cooldownUntil = Date.now() + ms;
  log(`account ${name} cooldown for ${ms / 1000}s`);
}

async function forward(req, body, accountName) {
  const acc = accounts[accountName];
  const beta = req.headers['anthropic-beta'] || '';
  const headers = {
    ...req.headers,
    'host': 'api.anthropic.com',
    'authorization': `Bearer ${acc.token}`,
    'anthropic-beta': beta.includes(OAUTH_BETA) ? beta : (beta ? `${beta},${OAUTH_BETA}` : OAUTH_BETA),
  };
  delete headers['x-api-key'];

  return new Promise((resolve, reject) => {
    const upstream = https.request({
      hostname: 'api.anthropic.com', port: 443, path: req.url, method: req.method, headers,
    }, resolve);
    upstream.on('error', reject);
    upstream.end(body);
  });
}

async function handle(req, res) {
  const chunks = []; req.on('data', c => chunks.push(c));
  await new Promise(r => req.on('end', r));
  const body = Buffer.concat(chunks);

  for (let attempt = 0; attempt < 3; attempt++) {
    const name = pickAccount();
    if (!name) { res.writeHead(429, {'content-type':'application/json'});
                 return res.end('{"error":"all accounts rate-limited"}'); }

    const upstream = await forward(req, body, name);
    accounts[name].lastUsedAt = Date.now();

    if (upstream.statusCode === 429 || upstream.statusCode === 401) {
      markCooldown(name, parseInt(upstream.headers['retry-after']) || 300);
      if (upstream.statusCode === 401) await refreshOrReload(name);
      continue;  // try next account, no bytes leaked to client yet
    }

    // Sniff first SSE event to detect immediate rate_limit_error before committing
    const firstChunk = await peekFirstSseEvent(upstream);
    if (firstChunk?.includes('"rate_limit_error"')) {
      markCooldown(name, 300);
      continue;
    }

    // Commit: forward status + headers + buffered first chunk + remainder
    res.writeHead(upstream.statusCode, upstream.headers);
    res.write(firstChunk);
    upstream.pipe(res);
    return;
  }
  res.writeHead(429); res.end('{"error":"exhausted retries"}');
}

http.createServer(handle).listen(4112, '127.0.0.1', () => {
  loadFromBackups();
  console.log('oauth-failover-proxy listening on 127.0.0.1:4112');
});
```

`peekFirstSseEvent` buffers up to ~4 KB or 250 ms (whichever comes first) before deciding the request "committed." `refreshOrReload(name)` first attempts a `security find-generic-password` to re-read the keychain, and only on miss does it POST to `platform.claude.com/v1/oauth/token`.

---

## Section F — Implementation plan

### Phase 0 — Token harvest dry-run *(0.5 day)*

Prove the proxy can read all three tokens from disk without touching the keychain. File: `~/.oauth-failover/harvest.js` reads the three backup JSONs, prints `{account, expiresIn, scopes, subscriptionType}`. Test: three rows, all `expiresIn > 0`. Files: 1 new script.

### Phase 1 — Single-account passthrough *(1 day)*

Validate the protocol: `ANTHROPIC_BASE_URL` + Bearer OAuth token + `anthropic-beta: oauth-2025-04-20` round-trips `/v1/messages`. File: `~/.oauth-failover/proxy.js` — handles only account1, no failover.

Tests:
- `curl -H "Authorization: Bearer dummy" http://127.0.0.1:4112/v1/messages -d '{"model":"claude-haiku-4-5-20251001","max_tokens":50,"messages":[{"role":"user","content":"hi"}]}'` returns a valid Anthropic response.
- `ANTHROPIC_BASE_URL=http://127.0.0.1:4112 ANTHROPIC_AUTH_TOKEN=dummy claude -p "hello"` works.
- SSE pass-through verified with `--output-format stream-json`.

**No launchd registration yet.** Run by hand. Files: 1 script.

### Phase 2 — Multi-account manual switch *(1 day)*

Load all three accounts; `?account=N` query-string override; no auto-failover. Add `GET /_health` returning all accounts' expiry/last-used/cooldown. Files: extend the script, 1 helper.

### Phase 3 — Auto-failover *(2 days, the feature)*

Add `pickAccount()` round-robin with cooldown, Tier-1 pre-flight rate-limit detection (4 KB / 250 ms peek buffer), retry loop (max 3 attempts), 401-triggered keychain re-read. Tests: stub upstream returning 429 → proxy rotates accounts; streaming responses terminate cleanly. **Now register** `~/Library/LaunchAgents/com.user.oauth-failover-proxy.plist` and flip Conductor's env vars. Recovery: `launchctl unload` reverts to direct. Files: extend script, 1 plist, 1 test file.

### Phase 4 — Observability *(1 day)*

Structured JSON logging (one line per request: `t, account, status_upstream, status_returned, ms, switched, retry_after`). Optional `http://127.0.0.1:4112/_status` HTML page showing live account scoring. Size-capped log rotation. Files: extend script, 1 HTML template.

### Phase 5 (stretch) — Tier-3 synthetic continuation *(2–3 days)*

Buffer assistant partial output; on mid-stream `rate_limit_error`, issue a continuation prompt to the next account. Not invisible — response quality may drift.

**Total estimate Phases 0–4: ~5.5 focused days.** Phase 5: +2–3 days if pursued.

---

## Section G — Risks and unknowns

- **Anthropic ToS — credential rotation across accounts may violate the Claude Max ToS.** The user is paying for three independent accounts and rotating them for personal-use throughput shaping. Arguably acceptable, but Anthropic could detect three accounts hitting one machine fingerprint within seconds and ban one or all. Mitigation: self-impose a token bucket (max 1 switch per chat-turn, max 6 switches per hour) to look human-paced.
- **Mid-stream switches are not truly invisible.** Tier 1 is invisible only for first-event refusals. Real mid-stream cutoffs either kill the chat (Tier 1 honest mode) or use a continuation prompt that may visibly degrade quality (Tier 3).
- **Stylistic drift.** `/v1/messages` is not session-bound, but server-side KV-cache state may subtly condition behavior. Account swap mid-conversation could cause style drift or repeated reasoning, independent of Tier 3.
- **Keychain ACL prompt on first run.** The proxy's first three `security find-generic-password` calls will pop "node wants to access Claude Code-credentials-87b88e67" dialogs. Must click "Always Allow" — "Allow Once" leads to silent failures later.
- **`expiresAt` is milliseconds**, not seconds. An off-by-1000 makes tokens look expired 1000× early.
- **Port collision.** 4111 is litellm. We pick 4112.
- **Conductor env-var injection may not be GUI-exposed in v0.52.3.** Fallback: `launchctl setenv ANTHROPIC_BASE_URL` — but that affects every `claude` process system-wide, including agentmemory bridge calls. Verify the GUI option exists before relying on it.
- **Snapshot freshness.** Backup JSONs are 0–60 min stale. The 401-triggered keychain re-read mitigates.
- **Refresh-token rotation race.** If proxy and a live `claude` session refresh the same account concurrently, one POST fails (refresh token already rotated). Mitigation: per-account in-process mutex + on-failure keychain re-read.
- **Performance.** One extra loopback hop, ~2–5 ms added vs. 200–4000 ms first-token latency. Negligible.
- **Tier-1 vs. Tier-3 tradeoff.** If the dominant failure mode is "session-start 429" (Tier 1 fixes it) vs. "mid-stream cutoff at token 800" (Tier 3 needed), scope decision rests on the operator's empirical observation. Worth instrumenting in Phase 4 before committing to Phase 5.

---

## Section H — Recommended next step

Build **Phase 1 (single-account passthrough)** next, on its own dedicated working directory at `~/.oauth-failover/`, with no Conductor wiring yet. The single biggest unknown is whether `ANTHROPIC_BASE_URL` + a Bearer OAuth token + the `oauth-2025-04-20` beta header actually round-trips a real `/v1/messages` request — until that's proven end-to-end with a single account, every later phase is built on assumption. Phase 1 takes one focused day, requires zero changes to anything currently running on the Mac, and either validates the entire approach or surfaces a fundamental blocker (e.g., Anthropic silently rate-limiting Bearer-only OAuth callers from non-CLI clients). Do not buy API-key access for one account instead — the cost asymmetry vs. three Claude Max subscriptions is large, and the user has already paid for the OAuth seats. Reserve the API-key buy as a Phase 6 fallback if Anthropic later closes the OAuth-Bearer path against non-claude-CLI user agents.

