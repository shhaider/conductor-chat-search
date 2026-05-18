"""Read-only SQLite access layer for the Conductor chat database.

Single responsibility: open the DB in read-only mode and run a small set of
parameterised queries against the sessions / session_messages / workspaces tables.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH: str = os.path.expanduser(
    "~/Library/Application Support/com.conductor.app/conductor.db"
)


def open_ro(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open the DB read-only via SQLite's URI mode.

    Sets ``row_factory = sqlite3.Row`` so callers can index by column name.
    Raises FileNotFoundError if the path does not exist.
    """
    if not Path(db_path).exists():
        raise FileNotFoundError(f"conductor.db not found at: {db_path}")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def list_sessions(
    con: sqlite3.Connection,
    limit: int = 500,
    account_index: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return non-hidden sessions ordered by updated_at DESC.

    Each dict carries: session_id, title, workspace_id, workspace_name, model,
    agent_type, created_at, updated_at, last_user_message_at,
    context_used_percent, context_token_count, message_count, account.

    ``account`` is the Claude Code account directory whose ``projects/`` tree
    holds the resumable JSONL for this session id, or None if no account
    holds resume state (an "orphaned" session — chat content is intact but
    Claude Code cannot resume it). When ``account_index`` is not supplied,
    the field is set to None on every row.
    """
    rows = con.execute(
        """
        SELECT s.id AS session_id,
               s.title AS title,
               s.workspace_id AS workspace_id,
               w.directory_name AS workspace_name,
               s.model AS model,
               s.agent_type AS agent_type,
               s.created_at AS created_at,
               s.updated_at AS updated_at,
               s.last_user_message_at AS last_user_message_at,
               s.context_used_percent AS context_used_percent,
               s.context_token_count AS context_token_count,
               (SELECT COUNT(*) FROM session_messages m WHERE m.session_id = s.id)
                   AS message_count
        FROM sessions s
        LEFT JOIN workspaces w ON w.id = s.workspace_id
        WHERE s.is_hidden = 0
        ORDER BY s.updated_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    result = [dict(r) for r in rows]
    # Decorate with the account field (None if no index provided).
    for r in result:
        sid = r.get("session_id")
        r["account"] = (
            account_index.get(sid) if (account_index and sid is not None) else None
        )
    return result


def list_workspaces(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return all workspaces ordered by directory_name."""
    rows = con.execute(
        """
        SELECT id, directory_name, branch, state
        FROM workspaces
        ORDER BY directory_name ASC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_session(con: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    """Return one session row dict (all columns) or None if not found."""
    row = con.execute(
        "SELECT * FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    return dict(row) if row else None


def lookup_sessions_by_id(
    con: sqlite3.Connection,
    id_value: str,
    limit: int = 50,
    account_index: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return non-hidden sessions whose id matches ``id_value``.

    Resolution rules:
      - Exact UUID-shaped match (length 36 with hyphens) -> equality lookup,
        returns 0 or 1 row.
      - Otherwise -> prefix lookup ``id LIKE '<id_value>%'`` so that the
        operator can paste any 8+ char prefix and still land on the chat.

    ID lookup INCLUDES hidden sessions on purpose. The general listing and
    search paths skip ``is_hidden=1`` (because hidden chats clutter the
    default view), but if the user has pasted a specific ID they explicitly
    want THAT chat — even if Conductor has it flagged hidden. Each row
    carries an ``is_hidden`` field so the UI can surface the state if it
    wants to.

    Each dict carries the same shape as ``list_sessions`` rows:
    session_id, title, workspace_id, workspace_name, model, agent_type,
    created_at, updated_at, last_user_message_at, context_used_percent,
    context_token_count, message_count, account — plus ``is_hidden``.

    The caller is responsible for length validation (>=8 chars). An empty
    ``id_value`` returns an empty list without touching the DB.
    """
    if not id_value:
        return []
    # Exact UUID form: 36 chars + hyphens at fixed positions.
    is_uuid_shape = (
        len(id_value) == 36
        and id_value[8] == "-"
        and id_value[13] == "-"
        and id_value[18] == "-"
        and id_value[23] == "-"
    )
    if is_uuid_shape:
        where_clause = "s.id = ?"
        params: tuple[Any, ...] = (id_value,)
    else:
        # Prefix LIKE — escape any user-supplied wildcards so '%foo' can't
        # become a glob match.
        escaped = id_value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where_clause = "s.id LIKE ? ESCAPE '\\'"
        params = (escaped + "%",)

    rows = con.execute(
        f"""
        SELECT s.id AS session_id,
               s.title AS title,
               s.workspace_id AS workspace_id,
               w.directory_name AS workspace_name,
               s.model AS model,
               s.agent_type AS agent_type,
               s.created_at AS created_at,
               s.updated_at AS updated_at,
               s.last_user_message_at AS last_user_message_at,
               s.context_used_percent AS context_used_percent,
               s.context_token_count AS context_token_count,
               s.is_hidden AS is_hidden,
               (SELECT COUNT(*) FROM session_messages m WHERE m.session_id = s.id)
                   AS message_count
        FROM sessions s
        LEFT JOIN workspaces w ON w.id = s.workspace_id
        WHERE {where_clause}
        ORDER BY s.updated_at DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    result = [dict(r) for r in rows]
    for r in result:
        sid = r.get("session_id")
        r["account"] = (
            account_index.get(sid) if (account_index and sid is not None) else None
        )
    return result


def get_session_messages(con: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
    """Return all messages for a session ordered by sent_at ASC, rowid ASC."""
    rows = con.execute(
        """
        SELECT *
        FROM session_messages
        WHERE session_id = ?
        ORDER BY sent_at ASC, rowid ASC
        """,
        (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_messages_by_ids(
    con: sqlite3.Connection, session_id: str, message_ids: list[str]
) -> list[dict[str, Any]]:
    """Return the subset of messages in ``session_id`` whose id is in
    ``message_ids``, ordered by sent_at ASC, rowid ASC.

    Used by the FTS path to fetch ONLY the messages that the FTS index
    flagged as containing a term — avoiding a full per-session message
    read in pass-2 confirmation.

    Chunked IN clauses keep us under SQLite's ~999-param default ceiling.
    Empty ``message_ids`` returns an empty list without touching the DB.
    """
    if not message_ids:
        return []
    CHUNK = 800
    out: list[dict[str, Any]] = []
    for i in range(0, len(message_ids), CHUNK):
        chunk = message_ids[i:i + CHUNK]
        placeholders = ",".join("?" * len(chunk))
        sql = (
            "SELECT * FROM session_messages "
            f"WHERE session_id = ? AND id IN ({placeholders}) "
            "ORDER BY sent_at ASC, rowid ASC"
        )
        rows = con.execute(sql, [session_id, *chunk]).fetchall()
        out.extend(dict(r) for r in rows)
    return out


def get_schema_version(con: sqlite3.Connection) -> str | None:
    """Return the latest version_id from _sqlx_migrations, or None if the table is absent."""
    try:
        row = con.execute(
            "SELECT version FROM _sqlx_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return str(row[0]) if row else None
