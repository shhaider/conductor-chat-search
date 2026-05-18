# Live Validation Report — id-lookup + search-toggle + click-to-copy

**Merge commit:** `da679e6` on `main` (squash of PRs #7, #8, #9).
**PRs:**
- https://github.com/shhaider/conductor-chat-search/pull/7 — feat: chat-ID lookup + persisted exact-phrase toggle + click-to-copy IDs
- https://github.com/shhaider/conductor-chat-search/pull/8 — fix(lookup): include hidden chats in ID lookup
- https://github.com/shhaider/conductor-chat-search/pull/9 — fix(search): include hidden chats in query results

**Run date:** 2026-05-18
**Runtime environment:** macOS, Python 3.14, PYTHONPATH=src, port 17891
**Real data:** `~/Library/Application Support/com.conductor.app/conductor.db` — 3000+ sessions, ~647k+ FTS rows.
**Server process:** restarted post-merge against the freshly pulled `main` checkout.

## Server bring-up

```bash
$ kill "$(cat /tmp/cchat-server.pid)"
$ PYTHONPATH=src nohup python3 -m conductor_chat.server --port 17891 --no-open > /tmp/cchat-server.log 2>&1 &
$ tail -3 /tmp/cchat-server.log
Account index built: 3011 session(s) across 4 Claude account dir(s)
Conductor Chat Search listening at http://127.0.0.1:17891/
Ctrl-C to stop.
```

## Check 1 — UI: ID input field appears in the HTML

```
$ curl -s -o /tmp/cchat-index.html -w "HTTP=%{http_code} bytes=%{size_download} time=%{time_total}s" http://127.0.0.1:17891/
HTTP=200 bytes=27258 time=0.003819s
$ grep -c 'id="chat-id"' /tmp/cchat-index.html
1
$ grep -c 'Chat ID:' /tmp/cchat-index.html
1
```

**PASS** — 200 in 4ms, 1 input with `id="chat-id"`, label "Chat ID:" present.

## Check 2 — UI: exact-phrase toggle + persistence

```
$ grep -c 'id="exact-toggle"' /tmp/cchat-index.html
1
$ grep -c 'Exact phrase' /tmp/cchat-index.html
2
$ grep -c 'cchat-exact-toggle' /tmp/cchat-index.html
1
$ grep -c 'Type the exact phrase you remember' /tmp/cchat-index.html
1
$ grep -c 'Type words; space = AND' /tmp/cchat-index.html
2
```

**PASS** — toggle present, label "Exact phrase" rendered, localStorage key
`cchat-exact-toggle` referenced, both placeholder strings emitted as JS
constants (one ON variant, two OFF references including default attr).

## Check 3 — API: 8-char prefix lookup

```
$ curl -s -o /tmp/cchat-c3.json -w "HTTP=%{http_code} bytes=%{size_download} time=%{time_total}s" \
    "http://127.0.0.1:17891/api/sessions/lookup?id=0ba636dc"
HTTP=200 bytes=518 time=0.014646s
$ jq . /tmp/cchat-c3.json
[
  {
    "session_id": "0ba636dc-5c0c-47c4-b4a7-27ea45e79533",
    "title": "Continue Resolve Abandoned Work",
    "workspace_id": "d4832e8f-c93a-4be8-8a8e-6dff0c12d85c",
    "workspace_name": "oslo",
    "model": "sonnet",
    "agent_type": "claude",
    "context_token_count": 136138,
    "is_hidden": 1,
    "message_count": 5333,
    "account": "account3",
    "snippets": [],
    "match_kind": "and"
  }
]
```

**PASS** — 200 in 15ms, exactly 1 match, the chat the user lost track of
(`Continue Resolve Abandoned Work` in workspace `oslo`). The chat has
`is_hidden=1` — this was the silent reason the user couldn't find it via
the search box (PRs #8 and #9 fixed both lookup and search to surface
hidden chats).

## Check 4 — API: full UUID lookup

```
$ curl -s -o /tmp/cchat-c4.json -w "HTTP=%{http_code} bytes=%{size_download} time=%{time_total}s" \
    "http://127.0.0.1:17891/api/sessions/lookup?id=0ba636dc-5c0c-47c4-b4a7-27ea45e79533"
HTTP=200 bytes=518 time=0.003319s
matches: 1
  session_id=0ba636dc-5c0c-47c4-b4a7-27ea45e79533
  title="Continue Resolve Abandoned Work"  workspace=oslo
```

**PASS** — 200 in 3ms, equality lookup returns the same single chat.

## Check 5 — API: no-match returns 0 results

```
$ curl -s -o /tmp/cchat-c5.json -w "HTTP=%{http_code} bytes=%{size_download} time=%{time_total}s" \
    "http://127.0.0.1:17891/api/sessions/lookup?id=deadbeef"
HTTP=200 bytes=2 time=0.005463s
$ cat /tmp/cchat-c5.json
[]
```

**PASS** — 200 in 5ms, empty list (no chat starts with `deadbeef`).

## Check 6 — API: exact-phrase search finds the target chat

```
$ curl -s --max-time 60 -o /tmp/cchat-c6.json -w "HTTP=%{http_code} bytes=%{size_download} time=%{time_total}s" \
    "http://127.0.0.1:17891/api/sessions?q_exact=PR%20%231720%20rebased%20cleanly"
HTTP=200 bytes=847 time=0.835395s
matches: 1
  session_id=0ba636dc-5c0c-47c4-b4a7-27ea45e79533
  title="Continue Resolve Abandoned Work"  workspace=oslo  match_kind=exact  is_hidden=1
  snippet="«PR #1720 rebased cleanly» onto `38e5252ad9`, pushed as `0d94b42ca2`, `@mergifyio queue` sent. Should pick…"
```

**PASS** — 200 in 835ms, exactly 1 match, the oslo `Continue Resolve
Abandoned Work` chat. `match_kind: "exact"` confirms the snippet wraps
the WHOLE phrase (`«PR #1720 rebased cleanly»`) — not the individual
words. Search now surfaces hidden chats (fixed in PR #9).

## Summary

| # | Check | Status | Latency | Notes |
|---|---|---|---|---|
| 1 | `GET /` serves new ID input field | PASS | 4ms | 27258 bytes |
| 2 | Toggle + persistence appears in HTML | PASS | (same fetch) | localStorage key emitted, both placeholders present |
| 3 | `GET /api/sessions/lookup?id=0ba636dc` | PASS | 15ms | 1 match: oslo "Continue Resolve Abandoned Work" |
| 4 | `GET /api/sessions/lookup?id=<full UUID>` | PASS | 3ms | Same chat |
| 5 | `GET /api/sessions/lookup?id=deadbeef` | PASS | 5ms | Empty list (`[]`) |
| 6 | `GET /api/sessions?q_exact=PR%20%231720%20rebased%20cleanly` | PASS | 835ms | 1 match, `match_kind=exact`, snippet wraps full phrase |

**All 6 live checks PASS.**

## Findings during validation (resolved before final report)

The user's target chat had `is_hidden=1` in Conductor's DB. Two follow-up
PRs (#8 lookup, #9 search) widened the visibility to include hidden chats
whenever the user is actively querying (paste an ID, type a phrase). The
default empty-state listing still hides them. Each result row now carries
the `is_hidden` flag and the UI renders a `(hidden)` badge so the user
knows the state.

## Cleanup

- `/tmp/cchat-c3.json`, `c4.json`, `c5.json`, `c6.json`, `/tmp/cchat-index.html` — transient curl artifacts, safe to leave or delete.
- No persistent state changes to `conductor.db` (read-only).
- Server left running on PID `$(cat /tmp/cchat-server.pid)` per the run protocol.
