# P02 — `src/conductor_chat/search.py`

## Goal

Two-pass search: SQL `LIKE` for candidate filtering, then Python JSON-parse confirmation that the match falls inside a `type==text` content block (not inside a tool call or thinking block).

## Files

- `src/conductor_chat/search.py`
- `tests/test_search.py`

## API

```python
def search(
    con: sqlite3.Connection,
    terms: list[str],
    workspace_id: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """AND-search across user+assistant text content.

    Returns list of session dicts (same fields as db.list_sessions) PLUS:
      - snippets: list of {term: str, text: str, message_id: str, role: str, sent_at: str}
        — one snippet per term that matched within the session, 80 chars
          before/after, with the matched substring wrapped in «...».

    Semantics:
      - Empty terms list → equivalent to list_sessions (no filtering).
      - All terms must match somewhere in the session (AND).
      - Term match is case-insensitive substring.
      - Match must be inside a content item with type=="text".
        Tool inputs, tool results, thinking blocks do NOT count.
      - If workspace_id is set, restrict to that workspace.
    """
```

## Internal helpers (not exported)

```python
def _candidates_for_term(con, term: str, workspace_id: str | None) -> set[str]:
    """SQL LIKE pre-filter. Returns session_ids whose any message's raw content contains term."""

def _confirm_term_in_text_blocks(message_row: dict, term: str) -> tuple[bool, dict | None]:
    """Parse content JSON, walk content[i] looking for type==text where text contains term.
    Returns (True, snippet_dict) or (False, None)."""

def _make_snippet(message_row: dict, role: str, text: str, term: str) -> dict:
    """Build the snippet dict with surrounding context."""
```

## Tests

Fixture: a DB with one workspace, three sessions:
- Session A: 4 messages, one assistant text block "the cat sat on the mat", one tool_use with input file_path="cat.py"
- Session B: 2 messages, both text blocks ("a dog ran", "the cat is happy")
- Session C: 1 message, only a tool_result containing "cat" — should NOT match "cat" search.

Cases:

1. `search(con, [])` returns 3 sessions (all of them — empty terms = no filter).
2. `search(con, ["cat"])` returns A and B (NOT C — C's "cat" is inside a tool_result).
3. `search(con, ["cat", "happy"])` returns only B (AND semantics).
4. `search(con, ["mat"])` returns only A.
5. `search(con, ["fish"])` returns empty list.
6. Snippet for A's "cat" search has text containing `«cat»` and includes some context on either side.
7. Case-insensitive: `search(con, ["CAT"])` returns A and B.
8. Workspace filter: if A and B are in workspace_x and C in workspace_y, `search(con, ["cat"], workspace_id="workspace_x")` returns A and B only.
9. Term that matches inside a thinking block does NOT count (add a session D with `{"type":"thinking","thinking":"interesting cat fact"}` — search for "interesting" returns empty).

## Acceptance

- 9 tests pass.
- `search` returns under 1 second on a 329k-message DB for a common term (informational, not a CI gate — note in test docstring).
- No third-party deps. `json`, `re` for boundary escaping if needed.
- ≤ 150 lines.

## Performance note

If the SQL `LIKE` pre-filter is too slow on the real DB in operator testing, add a SQLite index on `session_messages.content` is NOT useful (LIKE with leading wildcard skips indexes). Real solution would be FTS5 — track as a follow-up issue, do not implement now.
