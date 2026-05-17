# Implementation Plan — Task Graph

## Repo layout (target)

```
conductor-chat-search/
├── README.md                  — user-facing intro + quickstart
├── SPEC.md                    — what we're building, acceptance criteria
├── RESEARCH.md                — Conductor schema, search strategy, prior art
├── PLAN.md                    — this file
├── STATE.md                   — file-based state machine, updated each phase
├── ARCHITECTURE.md            — Phase 4 output
├── AMENDMENTS.md              — Phase 5.9 amendments log
├── prompts/
│   ├── P01_server_list_sessions.md
│   ├── P02_server_search.md
│   ├── P03_server_export.md
│   ├── P04_frontend.md
│   ├── P05_launch_script.md
│   ├── P06_shared_render_module.md
│   └── P07_verification.md
├── src/
│   └── conductor_chat/
│       ├── __init__.py
│       ├── server.py          — http.server with API routes
│       ├── db.py              — read-only SQLite connection + queries
│       ├── search.py          — LIKE pre-filter + Python text-block confirm
│       ├── render.py          — JSON message → clean markdown (shared w/ CLI)
│       └── static/
│           └── index.html     — single-page UI (HTML + inline CSS + inline vanilla JS)
├── tests/
│   ├── test_db.py             — read-only connectivity, schema sanity
│   ├── test_search.py         — match correctness, AND-narrowing
│   ├── test_render.py         — message JSON → markdown shape
│   └── test_server.py         — endpoint roundtrip on a tiny fixture DB
├── run.sh                     — launcher (picks port, opens browser, runs server)
└── .github/workflows/ci.yml   — pytest on push/PR
```

## Implementation nodes (Phase 5)

Numbered for clear PROMPT FILE mapping. Each node has its own prompt at `prompts/P{NN}_*.md` and produces a verifiable artifact.

### P06 — `src/conductor_chat/render.py` (FIRST — shared module)

Why first: both the GUI export and the existing CLI use the same rendering. Build the shared module first; the CLI gets refactored to call into it as a follow-up (not in v1 scope). For v1 we only depend on it from the new server.

Inputs: a row dict from `session_messages`.
Output: list of markdown lines.

Pure function, no I/O. Easy to unit test.

### P01 — `src/conductor_chat/db.py` + server.list_sessions

Read-only sqlite3 connection factory. `list_sessions()` returns rows from `sessions` LEFT JOIN `workspaces` ordered by `updated_at` DESC, with message count joined in.

API: `GET /api/sessions` → JSON.

### P02 — `src/conductor_chat/search.py` + server.search

Search function: given N terms, run `LIKE '%term%'` against `session_messages.content` to find candidate session_ids; then parse each candidate's content JSON in Python and confirm the term appears in a `type==text` block (filters out tool-call hits). Return session list with a snippet per match.

API: `GET /api/sessions?q=<term>&q=<term>` (multiple `q` params = AND).

### P03 — server.export

POST `/api/export` body `{session_id}`. Calls `render.export_session(session_id, mode='clean')` which writes `~/Downloads/conductor-chat-<id>-<timestamp>.md` and returns the path.

### P04 — `src/conductor_chat/static/index.html`

Single HTML file. No build step. Layout:
- Top: search input (one box, but typing space-separated terms is interpreted as AND).
- Below: result list — each row shows title, workspace, last-updated, message count, snippet (if from search), Export button.
- Footer: small "Refresh" button (re-fetches list); status message area (shows last export path).

Vanilla JS only. fetch() for API calls. Debounced search-on-type (200ms idle).

### P05 — `run.sh`

Bash script: cd to repo root, pick a port (try 17891 then random), start `python3 -m conductor_chat.server <port>` in background, `open http://127.0.0.1:<port>/`, trap SIGINT to clean up.

### P07 — Tests + verification

`tests/` directory with pytest-compatible tests against a fixture DB (create a tiny in-memory or temp-file SQLite with hand-crafted rows). End-to-end test: start server on random port, hit `/api/sessions`, hit search, hit export, verify markdown shape.

**Live user journey** (gate test): launch the real tool against operator's actual conductor.db (read-only), confirm the most recent 5 sessions appear in the UI, confirm a search for a known phrase ("metabuilder") returns at least one hit, confirm export of one chat produces a non-empty markdown in `~/Downloads/`.

## Dependency graph

```
P06 (render) ──┬──> P03 (export)
               └──> (also used by future CLI refactor)
P01 (db + list) ──> P04 (frontend) ──> P05 (launch)
P02 (search) ──┘
P03 (export) ──┘
P07 (verification) depends on P01, P02, P03, P04
```

**Parallelizable groups:**
- Wave 1 (parallel): P06, P01.
- Wave 2 (parallel): P02 (needs P01), P03 (needs P06 + P01).
- Wave 3: P04 (needs P01, P02, P03).
- Wave 4: P05.
- Wave 5: P07.

## Integration classification (preview for Step 5.8)

Every module added must be WIRED (called by something that runs) or marked ISLAND with explicit justification.

| Module | Wired by | Status |
|---|---|---|
| `db.py` | `server.py` routes | WIRED |
| `render.py` | `server.py` export route + future CLI | WIRED |
| `search.py` | `server.py` search route | WIRED |
| `server.py` | `run.sh` | WIRED |
| `static/index.html` | served by `server.py` `GET /` | WIRED |
| `run.sh` | user runs it | WIRED (operator entry point) |

No ISLANDs expected in v1.

## Commit cadence

- Commit after Wave 1 lands (P06 + P01 + their tests).
- Commit after Wave 2 (P02 + P03 + tests).
- Commit after Wave 3 (P04 + tests).
- Commit after Wave 4 (P05).
- Commit after Wave 5 (P07 + verification report).

Each commit message: `feat(<area>): <node-id> <one-line>`. Conventional commits.

## Release flow

1. After all phases pass (verification, review, audit clean): tag `v0.1.0`.
2. Open a PR against `main` from `feature/v0.1.0`. PR body lists acceptance criteria with checkboxes.
3. CI runs (`.github/workflows/ci.yml` — pytest).
4. Operator reviews + merges.
5. Done.

(For brand-new repo: first push initializes `main` with planning artifacts only. Implementation lands on `feature/v0.1.0` and PRs into `main`. That gives reviewable diffs from day 1.)

## Open questions for operator review (before Phase 4)

1. Repo name confirmed as `conductor-chat-search`? (Easy to change before push.)
2. License: MIT? Apache-2.0? Unlicense? Default to MIT unless specified.
3. Should `run.sh` add itself to PATH (one-time-install step) or stay path-relative?
4. Public or private repo on GitHub?
