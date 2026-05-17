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


def test_readonly_writes_blocked(fixture_db):
    con = db.open_ro(fixture_db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("DELETE FROM sessions WHERE id = 's1'")
    finally:
        con.close()
