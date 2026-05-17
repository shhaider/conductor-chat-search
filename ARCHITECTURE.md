# Architecture

## One-paragraph design

A Python 3 stdlib HTTP server (`http.server.ThreadingHTTPServer`) listens on `127.0.0.1:<port>` and serves three API routes (`list`, `search`, `export`) plus one static HTML page. The HTML page is a single self-contained file: vanilla JS, no build, no framework. The server reads `~/Library/Application Support/com.conductor.app/conductor.db` in read-only URI mode (`file:…?mode=ro`). Search is a two-pass filter: a SQL `LIKE` pre-pass narrows candidates by raw `content` substring, then a Python pass parses each candidate row's JSON and confirms the term appears inside a `type==text` block (excluding tool calls/thinking/tool results per spec). Export reuses a shared `render` module that converts a session's message rows into a markdown document under `~/Downloads/`. Lifecycle: `run.sh` picks a port, spawns the server, opens the browser, traps SIGINT to clean up.

## Module map

```
src/conductor_chat/
├── __init__.py          empty package marker
├── db.py                read-only sqlite connection + queries (list_sessions)
├── search.py            two-pass term filter (SQL LIKE → Python JSON confirm)
├── render.py            pure function: message JSON → markdown lines
├── export.py            wraps render + writes file to ~/Downloads
├── server.py            HTTP server, routes /api/sessions, /api/search, /api/export, /
└── static/
    └── index.html       single-file UI
```

Each module is ≤ ~200 LOC. Each has a clear single responsibility. Future contributors (human or AI) can read `db.py` in isolation and understand it.

## Data flow

```
Browser ───GET /───▶ server.py ───serves───▶ static/index.html
   │                     │
   ├─GET /api/sessions──▶│  db.list_sessions()
   │  (no q)             │
   │                     ▼
   │                  sqlite3 ──read──▶  conductor.db (mode=ro)
   │                     │
   │                  JSON
   │◀────JSON────────────│
   │
   ├─GET /api/search?q=…&q=…─▶ server ─▶ search.search(terms)
   │                                       │
   │                                  db.candidates_by_like(terms)  (SQL LIKE)
   │                                       │
   │                                  search.confirm_text_matches(rows, terms)  (Python)
   │                                       │
   │◀───────────JSON (sessions + snippets)─┘
   │
   └─POST /api/export {session_id}─▶ server ─▶ export.export_session()
                                                   │
                                              render.render_session()
                                                   │
                                              write ~/Downloads/...md
                                                   │
                                       JSON {path, bytes}
                                                   │
        ◀──────────────────────────────────────────┘
```

## Threading and concurrency

- One process. `ThreadingHTTPServer` handles concurrent requests (search-while-list-loads).
- SQLite connections are per-request — opened cheaply via the read-only URI, closed at end of handler. SQLite-3 with WAL handles many concurrent readers cleanly.
- No global state besides the cached DB path.

## Port selection

- Try fixed port 17891. If `EADDRINUSE`, fall back to OS-assigned (`socket.bind(('127.0.0.1', 0))`).
- Print the URL prominently to stderr regardless, so user can copy-paste if browser auto-open fails.

## Versioned schema awareness

On startup, query `SELECT version FROM _sqlx_migrations ORDER BY version DESC LIMIT 1`. Compare against a known-good version baked into code. If newer, print a warning to stderr; do NOT block. (The schema is stable for `sessions`, `session_messages`, `workspaces` — the columns we read have been there for many versions per the migration history.)

## Search correctness

Term semantics:
- Multiple terms in the search box (separated by whitespace) = AND.
- Quoted phrases = exact substring match (case-insensitive).
- Term is matched against ONLY `content[i].text` for `content[i].type in ("text",)` — both user prose and assistant prose. Tool inputs/results and thinking blocks are NOT searched (operator decision in Step -1).

Two-pass implementation:
1. SQL: `SELECT DISTINCT session_id FROM session_messages WHERE content LIKE ?` for each term, then intersect session_ids across terms.
2. Python: for each candidate session, fetch its messages, parse content JSON, verify the term appears in at least one `type==text` block. Drop sessions where no candidate message text-matches.

This avoids false positives from terms that match inside tool JSON (like a filename in a Read tool input) while keeping the SQL fast (no JSON parsing in SQL).

Snippet generation: for the FIRST text-block match in a confirmed session, return 80 chars before and 80 chars after the match, with the match itself wrapped in `«match»` markers for the UI to highlight.

## Export format

Same as `~/Downloads/conductor-export.sh` clean mode (already operator-validated):
- Frontmatter block with session id, title, workspace, model, timestamps, context-used, message count.
- One `## <role> · <timestamp>` heading per message.
- Text blocks rendered as plain markdown.
- `[thinking]` blocks rendered as blockquotes.
- `[tool: <name>]` headings with the relevant input field (command / file_path / etc.) as a single-line code span.
- `[tool result]` blocks rendered as fenced code (truncated to 600 chars unless `raw` mode).

For v1: only `clean` mode. `raw` is a follow-up.

## Frontend (single-page) layout

```
┌─────────────────────────────────────────────────────────┐
│ Conductor Chat Search                          ⟳ Refresh│
├─────────────────────────────────────────────────────────┤
│ [ search box: type words; space = AND ]   [Clear]       │
│ Workspace filter: [ ▾ All workspaces ]                  │
├─────────────────────────────────────────────────────────┤
│ ▶ 47fe347d  florence  Execute plan  sonnet   2h ago  127│
│    «…the assistant said «merge» the worktree…»          │
│    [Export ↓]                                            │
│                                                         │
│ ▶ 744a99c1  cairo     Continue Analysis  sonnet 3h  43  │
│    «…running «merge» on the audit results…»             │
│    [Export ↓]                                            │
│                                                         │
│ ...                                                     │
└─────────────────────────────────────────────────────────┘

Status: Last export → ~/Downloads/conductor-chat-…md
```

Behavior:
- Debounced search (200ms idle after last keystroke).
- Workspace filter loaded from `/api/workspaces` once at startup.
- Clicking a row reveals: full snippets for each matched term, the matched-message timestamp, an Export button.
- Export button: POSTs, shows success path inline, copies path to clipboard.
- No client-side state library, no router. ~150 lines of vanilla JS.

## Error handling

| Condition | Server response | UI behavior |
|---|---|---|
| conductor.db missing | startup error → exit 1 with clear message | (server doesn't start) |
| DB schema unknown | startup warning; proceed | UI shows banner: "Untested schema version" |
| Search returns 0 | empty list with "No matches" hint | "Try fewer terms" message |
| Export to ~/Downloads fails | 500 with error string | toast: "Export failed: <reason>" |
| Concurrent export of same session | second wins (different timestamp suffix) | both files written |
| Browser closed but server still running | server keeps running; Ctrl-C in terminal kills it | n/a |

## Decisions rejected

- **Building an FTS5 virtual table.** Premature optimization for 329k rows on M-series. Revisit if A7 (<2s) misses.
- **Datasette / sqlite-utils.** Brings pip dependencies; violates "stdlib only" constraint.
- **WebSocket for live updates.** No real-time requirement; refresh button is enough.
- **Pagination.** 216 sessions is small enough to render in one DOM list. Revisit at >1000.
- **Server-side persistence of search history.** YAGNI; let the browser do that.
