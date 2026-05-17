# Research Notes

## Conductor SQLite schema (verified 2026-05-17)

DB path: `~/Library/Application Support/com.conductor.app/conductor.db` (~924 MB).
Engine: SQLite 3 with WAL journal. Conductor often has the DB open while running — use `mode=ro` URI for reads.

### Tables we care about

**`sessions`** (216 rows on operator's Mac)
- `id` (TEXT, primary key — UUID)
- `title` (TEXT, default "Untitled")
- `workspace_id` (TEXT) — joins to `workspaces.id`
- `model` (TEXT) — e.g. "sonnet"
- `agent_type` (TEXT) — e.g. "claude"
- `created_at`, `updated_at`, `last_user_message_at` (TEXT, ISO timestamps)
- `is_hidden` (INTEGER, default 0) — filter for `= 0`
- `context_used_percent` (FLOAT, nullable)
- `context_token_count` (INTEGER, nullable)
- Index: `idx_sessions_workspace_id`

**`session_messages`** (329,038 rows)
- `id` (TEXT, PK — UUID)
- `session_id` (TEXT) — joins to `sessions.id`
- `role` (TEXT) — "user" / "assistant" / etc.
- `content` (TEXT) — **JSON-encoded** SDK message wrapper
- `created_at`, `sent_at` (TEXT)
- `turn_id` (TEXT)
- Index: `idx_session_messages_sent_at (session_id, sent_at)`, `idx_session_messages_turn_id`

**`workspaces`** (49 rows)
- `id` (TEXT, PK)
- `directory_name` (TEXT) — the human-recognizable workspace label
- `branch` (TEXT)
- `repository_id` (TEXT)
- `state` (TEXT, "active" / "archived" / etc.)

### Message content structure

`session_messages.content` is JSON. Example shapes:

```json
{"type":"user","message":{"role":"user","content":[{"type":"text","text":"…"}]}}
```

```json
{"type":"assistant","message":{"role":"assistant","content":[
  {"type":"text","text":"prose"},
  {"type":"thinking","thinking":"…"},
  {"type":"tool_use","name":"Read","input":{"file_path":"…"}}
]}}
```

```json
{"type":"user","message":{"role":"user","content":[
  {"tool_use_id":"toolu_…","type":"tool_result","content":"…","is_error":false}
]}}
```

For **search of "message text" only** (operator decision):
- Include `content[i].type == "text"` (both user and assistant)
- Exclude `tool_use`, `tool_result`, `thinking` from the search index

### Search strategy options

1. **`LIKE '%term%'` over `content`** — works without indexes, slow for big result sets, but for our 329k rows on M-series Mac probably under 1s. **Simplest. Probably good enough.**
2. **SQLite FTS5 virtual table** — fast full-text search, but requires building & maintaining a virtual table (extra setup, sync logic). Overkill for 329k rows on a personal tool.
3. **In-memory grep** — load message text into a Python list and scan. Wasteful for cold start; bad fit.

**Decision: start with option 1 (LIKE).** If A7 (<2s) isn't met in practice, upgrade to FTS5 later.

To extract just the text content from the JSON `content` for searching, we need either:
- A SQL `json_extract` over each row (SQLite has `json_extract()`), or
- Naive substring matching on the raw JSON column (matches text inside `"text":"…"` but ALSO matches inside tool inputs — operator said to exclude tool calls).

**Decision:** filter in Python after the SQL query. SQL `LIKE` over raw `content` is a first-pass filter that's overly permissive; Python parses each candidate's JSON and confirms the term appears in a `text` block. Trades cycles for simplicity. With LIKE narrowing to 99% of rows out, the Python pass is fast.

## Browser auto-open on macOS

`open http://127.0.0.1:<port>/` opens the default browser. Backup if `open` fails: print the URL to stderr so user can copy-paste.

## Python stdlib HTTP server

`http.server.ThreadingHTTPServer` (since 3.7) handles concurrent requests on the same port. Each handler is fine for our load (one user, one browser, ~10 in-flight requests max during typing).

Pick a port:
- Try a fixed port (e.g. 17891) first — if EADDRINUSE, fall back to `socket.bind(('127.0.0.1', 0))` to let kernel assign.

Routes:
- `GET /` → serve `index.html`
- `GET /static/<file>` → serve files from `static/`
- `GET /api/sessions?q=<term>&q2=<term>&workspace=<id>` → JSON list of sessions matching ALL terms
- `POST /api/export` → body `{session_id}`, writes markdown to `~/Downloads/`, returns `{path}`

## Reuse from existing CLI tool

`~/Downloads/conductor-export.sh` (built earlier today) contains the inline Python that converts a `session_messages` row's JSON content into rendered markdown. **Lift that into a shared module** in this repo (`src/conductor_chat/render.py`) so both the GUI's export endpoint and the existing CLI can call it. This satisfies rule #3 (organize code for AI agent reuse).

## Prior art / ecosystem

- **`datasette`** is a popular tool for serving SQLite over HTTP. Powerful but pulls dozens of pip deps and is overkill for our use.
- **Cursor**, **Conductor**, **Aider** all use SQLite for chat — none ship an open-source export tool.
- **`sqlite-utils`** is a CLI for ad-hoc SQLite queries. Useful for debugging but not as a GUI engine.
- **No existing tool** found that does Conductor-specific chat search/export. Our niche is real.

## Ecosystem watch (release notes since Conductor 0.52.3)

Operator has Conductor 0.52.3 (May 13 release). Charlie Holtz / team release roughly monthly. Schema is stable across recent versions per the migration table (`_sqlx_migrations` has 103 entries, last applied 2026-04-something). If a new migration ships, we'll detect by `_sqlx_migrations`'s top version_id changing and warn the user to re-test.

## Risks (mirrored to SPEC.md)

- Schema drift on Conductor update — version check on startup.
- WAL contention — `mode=ro` handles it.
- Browser auto-open failures — print URL.
- LIKE perf — fall back to FTS5 if needed.
