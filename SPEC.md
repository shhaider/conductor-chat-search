# conductor-chat-search — Product / Design Spec

## Step 0 — Right-thing audit (one sentence each)

- **Are we solving the right problem?** Yes — the bottleneck for fast rate-limit handoff between Anthropic accounts is *discovering which Conductor chat to migrate*, not exporting it.
- **Is there a more meta solution?** Yes, in theory: fix Conductor's account-switching and lossy-resume bugs upstream. But that requires Charlie Holtz's team; we have no control over their release cadence. A local-search tool is the highest-leverage thing we can build ourselves.
- **What's the opportunity cost?** ~4–8 hours of focused work. Worth it if account-rotation happens ≥1×/day, which it currently does.

## Goals

### Broader
Make rate-limited account rotation in Conductor.app fast and lossless. Today the painful step is finding the right past chat among 216 sessions with auto-assigned titles like "Untitled" / "Execute plan" / "Continue Analysis" — none of which are uniquely identifying.

### Specific (this tool)
A locally-served web GUI that:
1. Lists all Conductor chat sessions, sortable by recency, filterable by workspace.
2. Lets the user search across **message text** (user prompts + assistant prose, excluding tool-call JSON) for substrings or phrases, with results ranked by recency.
3. Displays match context — which chats contain the search term, with a snippet of where it matched.
4. Allows the user to **refine** with additional search terms (AND-style filtering) until only one or a few chats remain.
5. On chat selection, exports the chat as a clean markdown file to `~/Downloads/conductor-chat-<id>-<timestamp>.md` — same format as the existing CLI tool's `clean` mode.

## Scope

### In scope
- Read-only access to `~/Library/Application Support/com.conductor.app/conductor.db`.
- Local Python stdlib HTTP server (no pip deps), serves on `127.0.0.1:<dynamic-port>`.
- Single HTML page with vanilla JS (no framework, no build step).
- Search across `session_messages.content` for type-`text` segments (user + assistant text).
- Markdown export reusing the existing extraction logic from `~/Downloads/conductor-export.sh`.
- One-shot lifecycle: launch script opens the browser, server stops on Ctrl-C.

### Out of scope (v1)
- Multi-user / network exposure (loopback only).
- Searching tool inputs / outputs / thinking blocks (just message text per operator decision).
- Editing or deleting chats.
- Auto-detect-and-handoff on rate limit (would require Conductor integration).
- Persistent background service (we chose one-shot launch).
- Native macOS app shell.
- Authentication (loopback-only, so single-user).

### Non-goals
- Re-implementing Conductor's full chat UI.
- Replacing the existing CLI `conductor-export.sh` (that stays — it's the engine the GUI calls).

## Acceptance criteria

A1. Launching `./run.sh` (from the repo root) starts the server and opens `http://127.0.0.1:<port>/` in the default browser within 3 seconds.

A2. The page shows a list of all non-hidden chat sessions, default-sorted by `updated_at` descending. Each row shows: title, workspace name, model, last-updated timestamp, message count.

A3. A search box at the top performs a substring match against the `text` content of `session_messages` rows. Hitting Enter (or auto-debounced after typing pauses) filters the list to sessions where at least one message contains the term, AND shows a short snippet per result with the matched text highlighted.

A4. The search supports additive terms — if the user types a second term while a first is active, the result list narrows to chats matching BOTH terms (AND logic). A clear button removes all terms.

A5. Each result row has an "Export" button. Clicking it writes a markdown file to `~/Downloads/conductor-chat-<session-id>-<timestamp>.md` matching the format of the existing CLI tool's clean mode. The UI confirms with the file path.

A6. The server stops cleanly when the user closes the launch terminal or presses Ctrl-C.

A7. Search across all 329k+ message rows for a common term returns within 2 seconds on the user's M-series Mac (no full-table scan in a tight loop; use SQLite's built-in indexes + LIKE, or FTS5 if necessary).

A8. No data is mutated in `conductor.db` — opened with `?mode=ro` URI.

A9. No third-party Python dependencies (`pip install` not required). Standard library only.

A10. No native compile step. Pure scripts.

## Out-of-band constraints

- Mac-only (paths assume `~/Library/Application Support/com.conductor.app/`).
- Conductor.app may be holding the DB open with WAL. Read-only access via the SQLite URI is the safe pattern.
- The DB is ~924 MB; loading everything in memory is wasteful. Stream / index-driven queries only.

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Conductor schema changes in a future version | Medium | Version-check `_sqlx_migrations.last()` on startup; if unknown, warn but proceed. |
| WAL contention while Conductor is running | Low | Use `mode=ro` URI; SQLite handles concurrent readers cleanly. |
| Browser auto-open fails on some Macs | Low | Print the URL prominently to the terminal so user can paste it. |
| FTS5 not available in system SQLite | Low | Fall back to `LIKE '%term%'`; document expected perf. |
| Tool conflicts with the existing CLI `conductor-export.sh` | Low | GUI calls a shared Python module; no path conflicts. |

## Open questions (resolved during Step -1)

- Repo: new standalone GitHub repo (`shhaider/conductor-chat-search`).
- Tech: local web (Python stdlib + vanilla JS).
- Search scope: user+assistant prose only, not tool calls.
- Lifecycle: one-shot launch.
