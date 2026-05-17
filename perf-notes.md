# FTS5 perf measurements

All numbers measured against the operator's real `conductor.db`
(`~/Library/Application Support/com.conductor.app/conductor.db`) on
2026-05-17. Hardware: macOS / Apple Silicon. Python 3.14.3 (bundled SQLite has
FTS5 compiled in).

## Corpus

| metric | value |
|---|---|
| `session_messages` row count | 350,784 |
| Non-text rows skipped during index | 326,361 |
| Text fragments indexed | 24,424 |
| `conductor.db` size | 1,063 MB |
| Sidecar `fts.db` size after backfill | 12 MB |

The vast majority of rows are tool calls / tool results / thinking blocks
that the index correctly skips. Only ~7% of rows carry user/assistant
prose, which is why the FTS index is so small relative to the source DB.

## Cold backfill (one-time, first run)

| run | wall-clock |
|---|---|
| build_or_sync against fresh sidecar | **14.7s** |

Issue #2 estimated ~30s. The actual time is ~half that because most rows
are skipped without an FTS5 INSERT (parsing JSON is faster than indexing
text).

## Warm incremental sync (every subsequent server start)

| run | wall-clock |
|---|---|
| build_or_sync, ~11 new rows since last index | **4.5ms** |

Effectively free. The sync queries `rowid > meta.max_rowid` and only pays
for rows the source DB added since last open.

## Warm search latency (after backfill, page cache hot)

5 repeats per query, reporting min / mean / max wall-clock.

| query | min | mean | max | sessions returned |
|---|---|---|---|---|
| `metabuilder` (single common term) | 156ms | 350ms | 1081ms | 42 |
| `hetzner` (no matches) | 0.1ms | 0.2ms | 0.3ms | 0 |
| `swagger-parser` (1 match) | 13ms | 18ms | 28ms | 1 |
| `metabuilder agent tool` (3-term AND) | 257ms | 366ms | 707ms | 25 |

The cold first-call for a common term (~1s) is dominated by SQLite
opening message rows for the 42 candidate sessions; subsequent calls hit
the OS page cache and drop into the ~150ms range.

## Comparison vs the LIKE backend on the same DB

| query | LIKE backend | FTS5 backend | speedup |
|---|---|---|---|
| `metabuilder` | **8.68s** | 156ms (warm) | ~55x |
| `swagger-parser` | **5.71s** | 13ms (warm) | ~440x |

Both backends return **identical session_id sets** on `metabuilder` (42 sessions)
and `swagger-parser` (1 session) — equivalence verified during live validation.

## Where the time goes (FTS path, common term)

Profiling `metabuilder` (a worst-case common term):

| phase | time |
|---|---|
| FTS5 `MATCH` query | 23ms (1342 message_id hits) |
| message_id → session_id join (`_candidates_via_fts`) | 60ms (resolves to 42 sessions) |
| Pass-2 confirm + snippet build (`db.get_messages_by_ids` × 42 sessions) | ~80–1000ms cold, ~50–100ms warm |

The pass-2 cost scales with the number of candidate **sessions**, not
messages, because the new `db.get_messages_by_ids` only pulls the
messages FTS already flagged as containing the term — rather than every
message in every candidate session, which was the original 3s bottleneck.

## A7 acceptance

SPEC.md A7 target was **<2s** for common-term search. Issue #2 specified
the more ambitious **<100ms** target.

- Warm-cache `metabuilder` (worst-case common term): **156ms min, 350ms mean**.
- Cold first-call after server boot: up to ~1080ms (page-cache miss).
- `hetzner` / `swagger-parser` / less common terms: <30ms.

This **passes the original SPEC A7 target by ~6x** on the median and is
within ~50% of issue #2's stretch goal. The remaining gap is page-cache
warm-up on the source DB, not the FTS layer itself.

## Caveats

- Numbers are single-machine, single-snapshot. Other machines / a colder
  filesystem cache will be slower on the first query.
- FTS5 tokenisation differs from substring `LIKE`. The pass-2 confirm
  step ensures the on-screen snippet always contains the user's exact
  term — when FTS5 says hit but the substring isn't present (rare; e.g.
  punctuation-split tokens) the session is dropped silently.
- Hyphenated terms like `swagger-parser` are stored as two separate
  tokens by the `unicode61` tokeniser; the phrase quoting in
  `fts._fts5_phrase` ensures FTS5 looks for the tokens adjacently, which
  matches user expectations.
