"""Unit tests for conductor_chat.db — read-only connectivity + schema-shape queries."""

import os
import sqlite3
import tempfile

import pytest

from conductor_chat import db
from tests.fixtures.build_fixture import build_default_fixture


@pytest.fixture()
def fixture_db():
    path = build_default_fixture()
    yield path
    try:
        os.remove(path)
    except OSError:
        pass


def test_open_ro_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        db.open_ro("/nonexistent/path/to/conductor.db")


def test_open_ro_connects(fixture_db):
    con = db.open_ro(fixture_db)
    try:
        # Trivial sanity query
        row = con.execute("SELECT 1").fetchone()
        assert row[0] == 1
    finally:
        con.close()


def test_list_sessions_returns_visible_only_sorted(fixture_db):
    con = db.open_ro(fixture_db)
    try:
        sessions = db.list_sessions(con)
        ids = [s["session_id"] for s in sessions]
        assert ids == ["s1", "s2"]  # s3 hidden; s1 has later updated_at than s2
    finally:
        con.close()


def test_list_sessions_join_and_count(fixture_db):
    con = db.open_ro(fixture_db)
    try:
        sessions = db.list_sessions(con)
        by_id = {s["session_id"]: s for s in sessions}
        assert by_id["s1"]["workspace_name"] == "alpha"
        assert by_id["s2"]["workspace_name"] == "beta"
        assert by_id["s1"]["message_count"] == 2
        assert by_id["s2"]["message_count"] == 2
    finally:
        con.close()


def test_list_workspaces(fixture_db):
    con = db.open_ro(fixture_db)
    try:
        ws = db.list_workspaces(con)
        names = sorted(w["directory_name"] for w in ws)
        assert names == ["alpha", "beta"]
    finally:
        con.close()


def test_get_session_known_and_unknown(fixture_db):
    con = db.open_ro(fixture_db)
    try:
        s = db.get_session(con, "s1")
        assert s is not None
        assert s["title"] == "First session"
        assert db.get_session(con, "nope") is None
    finally:
        con.close()


def test_get_session_messages_ordered(fixture_db):
    con = db.open_ro(fixture_db)
    try:
        msgs = db.get_session_messages(con, "s1")
        ids = [m["id"] for m in msgs]
        assert ids == ["m1", "m2"]
    finally:
        con.close()


def test_get_schema_version(fixture_db):
    con = db.open_ro(fixture_db)
    try:
        ver = db.get_schema_version(con)
        assert ver is not None
        assert "20260415" in ver
    finally:
        con.close()


def test_list_sessions_account_field_default_null(fixture_db):
    """Without an account index, every row's ``account`` field is None."""
    con = db.open_ro(fixture_db)
    try:
        sessions = db.list_sessions(con)
        assert all("account" in s for s in sessions)
        assert all(s["account"] is None for s in sessions)
    finally:
        con.close()


def test_list_sessions_account_field_populated(fixture_db):
    """With an account index, matching session ids carry the account name."""
    con = db.open_ro(fixture_db)
    try:
        index = {"s1": "account2", "s3": "default"}  # s2 absent → orphaned
        sessions = db.list_sessions(con, account_index=index)
        by_id = {s["session_id"]: s for s in sessions}
        assert by_id["s1"]["account"] == "account2"
        assert by_id["s2"]["account"] is None  # orphaned
    finally:
        con.close()


def test_readonly_writes_blocked(fixture_db):
    con = db.open_ro(fixture_db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("DELETE FROM sessions WHERE id = 's1'")
    finally:
        con.close()


# -- lookup_sessions_by_id (Feature A) --


def test_lookup_by_id_empty_value(fixture_db):
    con = db.open_ro(fixture_db)
    try:
        assert db.lookup_sessions_by_id(con, "") == []
    finally:
        con.close()


def test_lookup_by_id_prefix_match(fixture_db):
    """A prefix returns every session whose id starts with it (incl. hidden)."""
    con = db.open_ro(fixture_db)
    try:
        # Fixture ids are 's1', 's2', 's3'. Use 's' prefix as the lookup —
        # the function does prefix matching for any non-UUID-shaped value.
        # Hidden sessions ARE returned by id lookup (the user explicitly
        # asked for that ID; the row carries is_hidden so callers can show
        # the state).
        rows = db.lookup_sessions_by_id(con, "s")
        ids = sorted(r["session_id"] for r in rows)
        assert ids == ["s1", "s2", "s3"]
    finally:
        con.close()


def test_lookup_by_id_no_match(fixture_db):
    con = db.open_ro(fixture_db)
    try:
        assert db.lookup_sessions_by_id(con, "deadbeef-no-such-id") == []
    finally:
        con.close()


def test_lookup_by_id_exact_match_carries_row_shape(fixture_db):
    """Returned rows carry the standard list_sessions shape: title, workspace_name, message_count, account."""
    con = db.open_ro(fixture_db)
    try:
        rows = db.lookup_sessions_by_id(con, "s1")
        assert len(rows) == 1
        r = rows[0]
        for key in (
            "session_id", "title", "workspace_id", "workspace_name",
            "model", "agent_type", "created_at", "updated_at",
            "message_count", "account",
        ):
            assert key in r, f"missing key {key!r}"
        assert r["title"] == "First session"
        assert r["workspace_name"] == "alpha"
        assert r["message_count"] == 2
        assert r["account"] is None  # no index passed
    finally:
        con.close()


def test_lookup_by_id_returns_hidden_sessions_too(fixture_db):
    """Hidden sessions are still reachable by direct ID lookup.

    Rationale: a hidden chat in Conductor is one the user dismissed/archived
    in the UI. If the operator pasted a specific ID they explicitly want
    THAT chat — even if the hidden flag is set. The row carries
    ``is_hidden`` so the caller can surface the state.
    """
    con = db.open_ro(fixture_db)
    try:
        # s3 is hidden in the default fixture.
        rows = db.lookup_sessions_by_id(con, "s3")
        assert len(rows) == 1
        assert rows[0]["session_id"] == "s3"
        assert rows[0]["is_hidden"] == 1
    finally:
        con.close()


def test_lookup_by_id_uuid_shape_uses_equality(fixture_db):
    """A UUID-shaped value (36 chars, 4 hyphens) returns 0 or 1 row, never partial."""
    con = db.open_ro(fixture_db)
    try:
        # Synthetic UUID — no row matches.
        rows = db.lookup_sessions_by_id(con, "deadbeef-1234-5678-9abc-def012345678")
        assert rows == []
    finally:
        con.close()


def test_lookup_by_id_escapes_wildcards(fixture_db):
    """User-supplied % or _ chars are escaped so they don't become globs."""
    con = db.open_ro(fixture_db)
    try:
        # '%' would otherwise match everything; we expect zero matches.
        rows = db.lookup_sessions_by_id(con, "%")
        assert rows == []
        rows = db.lookup_sessions_by_id(con, "_1")
        assert rows == []  # literal underscore — fixture ids have none
    finally:
        con.close()


def test_lookup_by_id_carries_account_when_index_provided(fixture_db):
    con = db.open_ro(fixture_db)
    try:
        rows = db.lookup_sessions_by_id(
            con, "s1", account_index={"s1": "account2"}
        )
        assert len(rows) == 1
        assert rows[0]["account"] == "account2"
    finally:
        con.close()
