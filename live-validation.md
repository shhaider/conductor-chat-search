# Live Validation Report — v0.1.0

**Commit:** `60d4402` (top of `feature/v0.1.0`)
**PR:** https://github.com/shhaider/conductor-chat-search/pull/1
**Run date:** 2026-05-17
**Runtime environment:** macOS, M-series, Python 3.14
**Real data:** `~/Library/Application Support/com.conductor.app/conductor.db` — 49 workspaces, 79 visible sessions, ~329k messages

## Definition of done — per operator rules

> Completion requires post-merge validation of the actual running artifact in the target environment.
> Validate through realistic end-to-end user journeys, inspect outputs for semantic/qualitative correctness,
> verify internal state and downstream side effects, test happy-path + edge/failure cases, collect evidence,
> clean up test artifacts, produce a validation report tied to the deployed commit/build.

## Journeys executed

| # | Journey | Pass | Evidence |
|---|---|---|---|
| 1 | Server starts against real conductor.db on auto port | ✅ | `URL: http://127.0.0.1:61583` printed within 1.5s |
| 2 | `/api/health` returns OK + schema_version | ✅ | `{ok: true, schema_version: "103", schema_warning: "Untested..."}` — soft-warn working |
| 3 | `/api/workspaces` returns real workspaces | ✅ | 49 workspaces returned; matches DB count |
| 4 | `/api/sessions` (no filter) returns recent sessions | ✅ | 79 sessions, ordered by updated_at DESC; titles + workspace_name joined |
| 5 | Search for "metabuilder" returns matches with snippets | ✅ (functionally) ❌ (perf) | 40 matches, snippets correctly highlight `«metabuilder»`. Took **59.6s** — spec is <2s (A7 FAIL). |
| 6 | AND search "metabuilder" + "telegram" narrows | ✅ | 0 matches — semantically correct (no Conductor chat discusses both) |
| 7 | Empty result on bogus term | ✅ | `xyzzy_zzz_no_chance_match` → 0 matches |
| 8 | Path traversal blocked | ✅ | `/static/../etc/passwd` → HTTP 404 `{error: not_found}` |
| 9 | Unknown session_id rejected | ✅ | POST `/api/export` with bogus id → HTTP 400 `{error: "unknown session_id: ..."}` |
| 10 | Rare-term search performance | ⚠️ partial | `q=hetzner` → 0 matches in 6.9s. Still slow but matches expected; A7 still misses target |
| 11 | AND with two rare terms | ⚠️ partial | `q=hetzner&q=redis` → 0 matches in 15.9s |
| 12 | Workspace filter | ✅ | `workspace=<caracas-id>` → 3 sessions (caracas-only); matches manual count |
| 13 | End-to-end export to ~/Downloads | ✅ | Real session (2254 messages) → 547KB markdown at `/Users/syedhaider/Downloads/conductor-chat-...md`. File contains 2274 `## ` headings (one per message + header), header has correct session_id/title/workspace/model/timestamps |

**Score: 11 pass, 0 functional fail, 1 perf fail (A7), 2 edge cases with degraded perf but correct results.**

## What works (operator can use today)

- Listing all sessions with workspace + msg count ordered by recency.
- Filtering by workspace (e.g. just see "caracas" chats).
- Searching for rare/specific terms (under 7s when matches are rare).
- AND-narrowing across multiple terms (semantics correct).
- Path traversal + unknown-id error handling.
- Exporting any selected chat to `~/Downloads/` as a complete markdown transcript that matches the existing CLI's clean-mode format (operator already eyeballed and accepts that format).

## What's broken / off-spec

**A7 performance — FAIL.**

The two-pass strategy (SQL `LIKE '%term%' → Python JSON-parse confirm) is too slow against 329k JSON-encoded rows where `content` is often multi-KB. Common terms ("metabuilder") take ~60s. The acceptance criterion was <2s.

Behavior is *correct* — just slow. For a "find the chat I want among 79 sessions" rescue use case, 60s per search is workable but painful. Will block "scan-as-you-type" debounced UX from feeling responsive.

**Root cause:** SQL `LIKE` with leading wildcard skips all indexes; SQLite must do a full table scan of 329k rows AND read the full `content` column (avg multi-KB) for each. Python pass then JSON-parses every candidate. Total work ~= 2-3 GB scanned linearly.

**Fix path:** Replace `LIKE` pre-filter with a SQLite FTS5 virtual table. Index just the user+assistant `text` fields (not raw JSON). Initial index build is one-time ~30s; subsequent queries should be ≤100ms. The architecture already anticipated this — RESEARCH.md flagged FTS5 as the upgrade and ARCHITECTURE.md noted "revisit if A7 misses".

## Recommended next sprint

1. **`feat: FTS5 search index`** — separate PR. Add a `db.fts.py` module that builds and maintains an FTS5 table from `session_messages`. Replace `_candidates_for_term` to query FTS5 instead of LIKE. Update `db.py` to auto-rebuild the index on startup if it's missing or out-of-date (cheap: `INSERT INTO fts SELECT … WHERE rowid > <last_indexed_rowid>`). The Python-side `_confirm_term_in_text_blocks` stays — FTS5 just narrows candidates faster.

## Acceptance criteria — final status

| ID | Criterion | Status |
|---|---|---|
| A1 | run.sh opens browser within 3s | ✅ (live: 1.5s to URL printed) |
| A2 | list endpoint, ordered by updated_at | ✅ |
| A3 | search filters by text-block match | ✅ |
| A4 | AND across terms | ✅ |
| A5 | export to ~/Downloads, format matches CLI | ✅ |
| A6 | Ctrl-C stops server cleanly | ✅ (verified by manual kill + ps check) |
| A7 | search <2s on 329k rows | ❌ — common term 60s, rare term 7s |
| A8 | read-only DB | ✅ (unit test + live re-verified) |
| A9 | no pip deps in runtime | ✅ |
| A10 | no native build | ✅ |

9/10 criteria pass. A7 misses by ~30×.

## Verdict

**Implementation-complete and functional, with one known performance regression (A7).** The tool is *usable* — every other journey works correctly and the exported markdown is high-fidelity. A7 is a real gap and the FTS5 follow-up is the right fix.

**This is NOT "done"** in the operator's definition-of-done sense: A7 is a measurable failure. The honest framing is:
> Implementation complete; live-validated as functionally correct on all journeys; one acceptance criterion (A7 perf) measurably fails and requires a follow-up FTS5 sprint.

The operator can decide whether to:
- (a) merge as-is and use the tool with the slow search, since it's still better than the broken Conductor UI workflow,
- (b) hold the merge and do the FTS5 follow-up first,
- (c) merge + immediately open the FTS5 issue.

## Cleanup

- Test export artifact at `/Users/syedhaider/Downloads/conductor-chat-0ba636dc-...md` was deleted after journey 13.
- `/tmp/cchat-stdout.log` removed.
- No persistent state changes to `conductor.db` (read-only).
