"""Build a tiny SQLite DB matching the Conductor schema for use in unit tests.

Only the columns and tables the production code reads are materialised. The
schema below intentionally omits the dozens of other Conductor tables we never
touch.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from typing import Any

SCHEMA_SQL = """
CREATE TABLE workspaces (
    id TEXT PRIMARY KEY,
    directory_name TEXT,
    branch TEXT,
    repository_id TEXT,
    state TEXT
);

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    workspace_id TEXT,
    model TEXT,
    agent_type TEXT,
    created_at TEXT,
    updated_at TEXT,
    last_user_message_at TEXT,
    context_used_percent REAL,
    context_token_count INTEGER,
    is_hidden INTEGER DEFAULT 0
);

CREATE TABLE session_messages (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    role TEXT,
    content TEXT,
    created_at TEXT,
    sent_at TEXT,
    turn_id TEXT
);

CREATE INDEX idx_sessions_workspace_id ON sessions(workspace_id);
CREATE INDEX idx_session_messages_sent_at ON session_messages(session_id, sent_at);

CREATE TABLE _sqlx_migrations (
    version INTEGER PRIMARY KEY,
    description TEXT
);
"""


def build_default_fixture(path: str | None = None) -> str:
    """Build the default fixture used by test_db.

    Schema:
      - 2 workspaces (alpha, beta)
      - 3 sessions: s1 (visible, ws alpha), s2 (visible, ws beta), s3 (hidden, ws alpha)
      - 4 messages: 2 in s1, 2 in s2

    Returns the path to the created DB file.
    """
    if path is None:
        fd, path = tempfile.mkstemp(prefix="cchat-fixture-", suffix=".db")
        os.close(fd)

    con = sqlite3.connect(path)
    try:
        con.executescript(SCHEMA_SQL)
        con.executemany(
            "INSERT INTO workspaces (id, directory_name, branch, state) VALUES (?, ?, ?, ?)",
            [
                ("ws-alpha", "alpha", "main", "active"),
                ("ws-beta", "beta", "dev", "active"),
            ],
        )
        con.executemany(
            """INSERT INTO sessions
            (id, title, workspace_id, model, agent_type, created_at, updated_at,
             last_user_message_at, context_used_percent, context_token_count, is_hidden)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                ("s1", "First session", "ws-alpha", "sonnet", "claude",
                 "2026-05-15T10:00:00", "2026-05-17T12:00:00",
                 "2026-05-17T11:50:00", 23.5, 12345, 0),
                ("s2", "Second session", "ws-beta", "sonnet", "claude",
                 "2026-05-16T09:00:00", "2026-05-16T18:30:00",
                 "2026-05-16T18:25:00", 12.0, 6000, 0),
                ("s3", "Hidden one", "ws-alpha", "sonnet", "claude",
                 "2026-05-10T09:00:00", "2026-05-10T10:00:00",
                 "2026-05-10T09:55:00", None, None, 1),
            ],
        )
        msgs = [
            ("m1", "s1", "user", _user_text("hello there"),
             "2026-05-17T11:55:00", "2026-05-17T11:55:00", "t1"),
            ("m2", "s1", "assistant", _assistant_text("hi! how can I help"),
             "2026-05-17T11:55:30", "2026-05-17T11:55:30", "t1"),
            ("m3", "s2", "user", _user_text("dog stuff"),
             "2026-05-16T18:00:00", "2026-05-16T18:00:00", "t2"),
            ("m4", "s2", "assistant", _assistant_text("the dog is fine"),
             "2026-05-16T18:00:30", "2026-05-16T18:00:30", "t2"),
        ]
        con.executemany(
            """INSERT INTO session_messages
            (id, session_id, role, content, created_at, sent_at, turn_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            msgs,
        )
        con.execute(
            "INSERT INTO _sqlx_migrations (version, description) VALUES (?, ?)",
            (20260101, "init"),
        )
        con.execute(
            "INSERT INTO _sqlx_migrations (version, description) VALUES (?, ?)",
            (20260415, "later"),
        )
        con.commit()
    finally:
        con.close()
    return path


def build_search_fixture(path: str | None = None) -> str:
    """Build a fixture tailored to test_search.

    Sessions:
      - sA (ws-x): two messages — one assistant text "the cat sat on the mat",
        one assistant tool_use with file_path="cat.py" (no text match for "cat").
      - sB (ws-x): two assistant texts — "a dog ran", "the cat is happy".
      - sC (ws-y): one user message whose only content is a tool_result containing "cat".
      - sD (ws-x): one assistant thinking block "interesting cat fact" (no text match).
    """
    if path is None:
        fd, path = tempfile.mkstemp(prefix="cchat-search-fixture-", suffix=".db")
        os.close(fd)

    con = sqlite3.connect(path)
    try:
        con.executescript(SCHEMA_SQL)
        con.executemany(
            "INSERT INTO workspaces (id, directory_name, branch, state) VALUES (?, ?, ?, ?)",
            [
                ("ws-x", "x", "main", "active"),
                ("ws-y", "y", "main", "active"),
            ],
        )
        con.executemany(
            """INSERT INTO sessions
            (id, title, workspace_id, model, agent_type, created_at, updated_at,
             last_user_message_at, context_used_percent, context_token_count, is_hidden)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                ("sA", "A", "ws-x", "sonnet", "claude",
                 "2026-05-15T10:00:00", "2026-05-17T12:00:00",
                 "2026-05-17T11:50:00", None, None, 0),
                ("sB", "B", "ws-x", "sonnet", "claude",
                 "2026-05-14T10:00:00", "2026-05-16T18:30:00",
                 "2026-05-16T18:25:00", None, None, 0),
                ("sC", "C", "ws-y", "sonnet", "claude",
                 "2026-05-13T10:00:00", "2026-05-15T18:30:00",
                 "2026-05-15T18:25:00", None, None, 0),
                ("sD", "D", "ws-x", "sonnet", "claude",
                 "2026-05-12T10:00:00", "2026-05-14T18:30:00",
                 "2026-05-14T18:25:00", None, None, 0),
            ],
        )

        msgs = [
            # sA: assistant text with "cat", then a tool_use whose input has "cat.py"
            ("mA1", "sA", "assistant",
             _assistant_text("the cat sat on the mat"),
             "2026-05-17T11:55:30", "2026-05-17T11:55:30", "tA"),
            ("mA2", "sA", "assistant",
             _assistant_blocks([
                 {"type": "tool_use", "name": "Read", "input": {"file_path": "cat.py"}}
             ]),
             "2026-05-17T11:55:40", "2026-05-17T11:55:40", "tA"),
            # sB: two assistant texts
            ("mB1", "sB", "assistant", _assistant_text("a dog ran"),
             "2026-05-16T18:00:30", "2026-05-16T18:00:30", "tB"),
            ("mB2", "sB", "assistant", _assistant_text("the cat is happy"),
             "2026-05-16T18:01:00", "2026-05-16T18:01:00", "tB"),
            # sC: user message whose only content is a tool_result containing "cat"
            ("mC1", "sC", "user",
             _user_blocks([
                 {"tool_use_id": "toolu_x", "type": "tool_result",
                  "content": "found cat in output", "is_error": False}
             ]),
             "2026-05-15T18:00:00", "2026-05-15T18:00:00", "tC"),
            # sD: assistant thinking block (no text match)
            ("mD1", "sD", "assistant",
             _assistant_blocks([
                 {"type": "thinking", "thinking": "interesting cat fact"}
             ]),
             "2026-05-14T18:00:00", "2026-05-14T18:00:00", "tD"),
        ]
        con.executemany(
            """INSERT INTO session_messages
            (id, session_id, role, content, created_at, sent_at, turn_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            msgs,
        )
        con.execute(
            "INSERT INTO _sqlx_migrations (version, description) VALUES (?, ?)",
            (20260101, "init"),
        )
        con.commit()
    finally:
        con.close()
    return path


# -- payload helpers --


def _user_text(text: str) -> str:
    return json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    })


def _assistant_text(text: str) -> str:
    return json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    })


def _user_blocks(blocks: list[dict[str, Any]]) -> str:
    return json.dumps({
        "type": "user",
        "message": {"role": "user", "content": blocks},
    })


def _assistant_blocks(blocks: list[dict[str, Any]]) -> str:
    return json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": blocks},
    })
