# Verification — v0.1.0

- **Commit:** `6e86220` (Wave 4 head) — CI workflow lands at the next commit.
- **CI run:** see GitHub Actions for `feature/v0.1.0` (`.github/workflows/ci.yml`)
- **Date:** 2026-05-17
- **Local Python:** 3.14.3 / pytest 9.0.2 (macOS arm64)

## Unit tests (local, full suite green)

| File | Tests | Result |
|---|---|---|
| tests/test_render.py | 8 | pass |
| tests/test_db.py | 9 | pass |
| tests/test_search.py | 9 | pass |
| tests/test_export.py | 6 | pass |
| tests/test_server.py | 10 | pass |

**Total: 42 tests, 42 passed, 0 failed.** Wall-clock under 8s.

Reproduce locally:

```bash
cd /Users/syedhaider/Projects/conductor-chat-search
python3 -m pytest tests/ -v
```

CI runs the same command on `macos-latest` with Python 3.12.

## Runtime-deps audit

Grep over `src/conductor_chat/*.py` for any non-stdlib imports returns empty.
Modules import only: `argparse`, `datetime`, `http`, `json`, `logging`, `os`,
`pathlib`, `re`, `sqlite3`, `sys`, `time`, `typing`, `urllib`, `__future__`,
plus intra-package imports. Pytest is dev-only (CI installs via pip).

## Acceptance criteria check (SPEC.md A1–A10)

- [ ] **A1** Launching `./run.sh` opens the browser within 3s — *deferred to live validation*. `bash -n run.sh` passes; the URL-print/wait loop is wired with a 3s ceiling.
- [x] **A2** List endpoint returns sessions ordered by `updated_at` DESC, with workspace + message_count joined — covered by `test_db::test_list_sessions_returns_visible_only_sorted`, `test_db::test_list_sessions_join_and_count`, `test_server::test_sessions_no_query`.
- [x] **A3** Search filters via text-block match — covered by `test_search::test_cat_returns_a_and_b_not_c`, `test_search::test_mat_returns_only_a`, `test_search::test_thinking_block_not_searched`, `test_server::test_sessions_search_one_term`.
- [x] **A4** AND semantics across terms — covered by `test_search::test_and_semantics_cat_and_happy`, `test_server::test_sessions_search_and_terms`.
- [x] **A5** Export writes to `~/Downloads/`, format matches CLI — covered by `test_export::test_export_writes_file_with_session_id`, `test_export::test_export_uses_out_dir`, `test_export::test_export_mode_raw_larger_with_tool_calls`. Server-side end-to-end: `test_server::test_export_writes_file`.
- [ ] **A6** Ctrl-C stops server cleanly — *deferred to live validation*. `server.main` catches `KeyboardInterrupt` and calls `server_close`; `run.sh` traps `INT/TERM` to clean up the background PID.
- [ ] **A7** <2s search on real 329k-row DB — *deferred to live validation* (informational, not a CI gate). The two-pass design avoids JSON parsing inside SQL and only parses confirmed candidates in Python.
- [x] **A8** Read-only DB access — covered by `test_db::test_readonly_writes_blocked` (asserts `sqlite3.OperationalError` on attempted DELETE).
- [x] **A9** No pip deps in runtime code — verified by `grep` over `src/` (above). `pytest` is dev-only.
- [x] **A10** No native compile — pure Python + a bash launcher; verified by absence of any build step.

## Live-validation-blocked items

A1, A6, A7 require running the binary against the operator's real `~/Library/Application Support/com.conductor.app/conductor.db`. They are reported here as **implementation-complete, awaiting live validation** per the operator's definition of done.

## What live validation should check

1. `./run.sh` — server starts, browser opens to `http://127.0.0.1:17891/` (or the kernel-assigned fallback) within 3s.
2. The session list renders with at least the 5 most-recent chats and shows workspace/model/age/msg-count per row.
3. Search for `metabuilder` returns a non-empty result list with at least one snippet wrapped in `<mark>`.
4. Add a second term (`merge`) — result list narrows.
5. Click `Export` on a row — a markdown file appears in `~/Downloads/conductor-chat-<id>-<timestamp>.md`, opens cleanly in a markdown viewer.
6. Ctrl-C in the launching terminal — server stops, PID is gone.
7. Time a single search for a common term — should return well under 2s on M-series.

If any of those fail, file an issue with the failing step, the relevant stderr from the launcher, and the schema version reported by `/api/health`.
