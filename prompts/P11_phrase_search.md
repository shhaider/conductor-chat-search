# P11 — Exact-phrase search (Feature B, backend)

## Goal

Add an exact-phrase search mode where the WHOLE phrase is one match, snippets wrap the entire phrase in `«…»` (not each word), and exact-phrase hits rank ABOVE word-AND hits.

## Files

- `src/conductor_chat/search.py` — accept structured terms (str + exact flag); thread flag through to FTS and snippets; add `match_kind` to result rows.
- `src/conductor_chat/server.py` — parse `?q=<term>` (AND) and `?q_exact=<phrase>` (exact). Ensure ordering: exact rows first, then AND rows.
- `tests/test_search.py` — add cases for exact mode, mixed, and ranking.
- `tests/test_server.py` — add API contract for `q_exact` and the `match_kind` field.

## Protocol

The HTTP API distinguishes two query-string keys:

- `q=<value>` — one AND term (current behaviour; whitespace inside the value still treated as part of the term itself, but the FRONTEND parses input and sends each word as its own `q`).
- `q_exact=<value>` — one exact-phrase term. Treated as a single contiguous substring.

A query can mix both. Example: `?q=cat&q_exact=migration%20of%20the%20684%20sites`

A session matches when ALL terms (across both kinds) confirm in text blocks. Ranking applies AFTER that filter (see below).

## Internal representation

`search.search(con, terms, ...)` accepts a `list[dict]` instead of `list[str]`:

```python
Term = dict[str, Any]   # {"value": str, "exact": bool}

def search(
    con: sqlite3.Connection,
    terms: list[Term] | list[str],   # backward-compat: bare strings = AND terms
    workspace_id: str | None = None,
    limit: int = 500,
    fts_con: sqlite3.Connection | None = None,
    account_index: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
```

If a bare `str` is passed (existing tests + callers), normalise to `{"value": s, "exact": False}`.

Each returned row gets a new field:
- `match_kind`: `"exact"` if at least one `exact` term confirmed; otherwise `"and"`.

Result ordering:
1. `match_kind == "exact"` first, then `"and"`.
2. Within each group, preserve the existing `updated_at DESC` order.

## FTS pass-1

For exact-phrase terms, the FTS5 MATCH must use a true phrase query. The existing `_fts5_phrase` already does this (wraps in double quotes) — so the same code path works. The semantic difference is purely in how Python interprets the term in pass-2:

- `exact=False`: current behaviour — single token / single word; `_confirm_term_in_text_blocks` already does substring match.
- `exact=True`: same substring match, but with the full multi-word phrase as the needle. Already works because `_confirm_term_in_text_blocks` uses `needle = term.lower()` and `needle in text.lower()`. No code change needed in `_confirm_term_in_text_blocks` itself; it ALREADY honours the full term as a substring.

The snippet construction `_make_snippet` likewise wraps `text[pos:pos+len(term)]` in `«...»` already — for a multi-word phrase, that becomes the FULL phrase wrapped, not individual words.

So most of the heavy lifting is just routing the `exact` flag through and labelling each result.

## Server changes

`_handle_sessions`:

```python
def _handle_sessions(self, qs):
    and_terms = [t for t in qs.get("q", []) if t]
    exact_terms = [t for t in qs.get("q_exact", []) if t]
    terms = (
        [{"value": t, "exact": False} for t in and_terms]
        + [{"value": t, "exact": True} for t in exact_terms]
    )
    # ... existing wiring, pass terms instead of and_terms
```

Account filter (?account=) is applied to the list AFTER ordering.

## Tests

`tests/test_search.py` additions:

- `test_exact_phrase_single_term`: build a fixture with one session whose assistant text contains "migration of the 684 sites". A search with `[{"value": "migration of the 684 sites", "exact": True}]` matches that session. Snippet contains `«migration of the 684 sites»` as ONE wrap (not per-word).
- `test_exact_phrase_no_match_for_word_only`: searching for the exact phrase but with one word missing should NOT match.
- `test_exact_ranks_above_and`: create one session that matches exact and one that only matches AND words. Order in result: exact first.
- `test_match_kind_field_present`: every returned row has `match_kind in {"exact", "and"}`.
- `test_legacy_str_terms_still_work`: passing `["cat"]` (bare strings) must keep current behaviour. All rows have `match_kind == "and"`.

`tests/test_server.py` additions:

- `/api/sessions?q_exact=cat` → returns matching rows with `match_kind == "exact"`.
- Mixed: `/api/sessions?q=cat&q_exact=happy` → rows must satisfy BOTH; ordering correct.

## Constraints

- Backward-compatible: existing tests passing `list[str]` must continue to work.
- The Python `_confirm_term_in_text_blocks` ALREADY handles full substring matching — do not over-engineer.
- FTS5 pass-1 already wraps single terms in double quotes for phrase queries.

