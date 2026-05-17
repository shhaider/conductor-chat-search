# P01 — `src/conductor_chat/db.py` + list_sessions

## Goal

Read-only access layer for `conductor.db`. One small module providing a connection factory and three queries.

## Files

- `src/conductor_chat/db.py`
- `tests/test_db.py`
- `tests/fixtures/build_fixture.py` (helper that builds a tiny in-memory DB matching Conductor's schema for testing)

## API

```python
DEFAULT_DB_PATH = os.path.expanduser(
    "~/Library/Application Support/com.conductor.app/conductor.db"
)

def open_ro(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open the DB read-only via the URI mode. Sets row_factory = sqlite3.Row."""

def list_sessions(con: sqlite3.Connection, limit: int = 500) -> list[dict]:
    """Return non-hidden sessions joined with workspace name and message count.

    Each row dict has keys:
      session_id, title, workspace_id, workspace_name, model, agent_type,
      created_at, updated_at, last_user_message_at, context_used_percent,
      context_token_count, message_count
    Ordered by updated_at DESC.
    """

def list_workspaces(con: sqlite3.Connection) -> list[dict]:
    """Return all workspaces with keys: id, directory_name, branch, state."""

def get_session(con: sqlite3.Connection, session_id: str) -> dict | None:
    """Return one session row dict or None."""

def get_session_messages(con: sqlite3.Connection, session_id: str) -> list[dict]:
    """Return all messages for a session ordered by sent_at ASC, rowid ASC.
    Each dict has at least: id, session_id, role, content, sent_at, created_at, turn_id."""

def get_schema_version(con: sqlite3.Connection) -> str | None:
    """Latest version_id from _sqlx_migrations, or None if missing."""
```

## Tests

Use the fixture-builder to create a temp DB with:
- 2 workspaces (alpha, beta)
- 3 sessions (one hidden, two visible)
- 4 messages across the two visible sessions

Assert:

1. `open_ro` on a missing file raises a clear error.
2. `open_ro` on the real fixture connects successfully.
3. `list_sessions` returns exactly the 2 visible sessions ordered by updated_at DESC.
4. `list_sessions` rows include the joined workspace_name and message_count.
5. `list_workspaces` returns 2 rows.
6. `get_session` for a known id returns a dict; for unknown returns None.
7. `get_session_messages` returns rows ordered by sent_at ASC.
8. `get_schema_version` returns a non-None string when `_sqlx_migrations` has rows.
9. Attempting a write through the connection raises `sqlite3.OperationalError` (verifies read-only mode).

## Acceptance

- 9 tests pass: `python3 -m pytest tests/test_db.py -v`.
- No third-party deps. Only `sqlite3`, `os`, `pathlib`.
- ≤ 100 lines.
