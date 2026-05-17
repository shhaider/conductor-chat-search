# P03 — `src/conductor_chat/export.py`

## Goal

Glue: take a session_id, render via P06, write to `~/Downloads/conductor-chat-<id>-<timestamp>.md`. Return the path. Safe across concurrent calls (timestamps include seconds + a tiebreaker if needed).

## Files

- `src/conductor_chat/export.py`
- `tests/test_export.py`

## API

```python
def export_session(
    con: sqlite3.Connection,
    session_id: str,
    mode: str = "clean",
    out_dir: str | None = None,
) -> dict:
    """Render the session as markdown and write to disk.

    Returns {"path": absolute_path, "bytes": int, "messages": int, "mode": mode}.
    out_dir defaults to ~/Downloads.
    Raises ValueError if session_id is unknown.
    Raises OSError on filesystem failure.
    """
```

## Implementation

1. Fetch session via `db.get_session(con, session_id)`. If None → raise ValueError.
2. Fetch workspace via `db.list_workspaces`; pick the one matching `session.workspace_id`.
3. Fetch messages via `db.get_session_messages(con, session_id)`.
4. Build output via `render.render_session_header(session, workspace, len(messages))` followed by `render.render_message(m, mode)` for each m. Join with newlines.
5. Filename: `conductor-chat-<session_id>-<YYYYMMDD-HHMMSS>.md`. If a file with that exact name already exists (unlikely but possible at the same second), append `-001`, `-002`, …
6. Write with `pathlib.Path(...).write_text(content, encoding="utf-8")`.
7. Return the result dict.

## Tests

1. Export a known session from the fixture DB → file exists, contains the session id in body, message count matches.
2. Export with `out_dir=tempfile.mkdtemp()` → uses that dir, not ~/Downloads.
3. Export unknown session_id → ValueError.
4. Filename collision: pre-create the expected filename → second write uses the `-001` suffix.
5. `mode="raw"` produces a larger output than `mode="clean"` for a session containing tool calls (sanity check the mode threading).
6. Cleanup: delete temp files in test teardown (rule 5).

## Acceptance

- 6 tests pass.
- Output file is parseable as markdown.
- ≤ 80 lines.
- No third-party deps.
