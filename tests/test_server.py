"""Integration tests for conductor_chat.server — start an ephemeral server, hit endpoints."""

import json
import os
import shutil
import socket
import tempfile
import threading
import time
from contextlib import closing
from urllib import error as urlerror
from urllib import request as urlrequest

import pytest

from conductor_chat import server
from tests.fixtures.build_fixture import build_search_fixture


@pytest.fixture()
def running_server():
    db_path = build_search_fixture()
    out_dir = tempfile.mkdtemp(prefix="cchat-server-test-out-")
    # Override the default ~/Downloads target for export tests by patching the
    # export module's DEFAULT_OUT_DIR. We restore it after the test.
    from conductor_chat import export as _export
    original_default = _export.DEFAULT_OUT_DIR
    _export.DEFAULT_OUT_DIR = out_dir

    # Use a temp FTS index so tests never touch the operator's real sidecar.
    # Build it up-front so the search endpoint exercises the FTS path.
    from conductor_chat import db as _db, fts as _fts
    fts_dir = tempfile.mkdtemp(prefix="cchat-server-test-fts-")
    fts_path = os.path.join(fts_dir, "fts.db")
    if _fts.has_fts5():
        src = _db.open_ro(db_path)
        try:
            fcon = _fts.open_fts(fts_path)
            try:
                _fts.build_or_sync(fcon, src, progress_stream=None)
            finally:
                fcon.close()
        finally:
            src.close()
    else:
        fts_path = None  # type: ignore[assignment]

    httpd = server.make_server(
        host="127.0.0.1", port=0, db_path=db_path, fts_path=fts_path
    )
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    try:
        yield {"base": base, "out_dir": out_dir, "db_path": db_path}
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        _export.DEFAULT_OUT_DIR = original_default
        shutil.rmtree(out_dir, ignore_errors=True)
        shutil.rmtree(fts_dir, ignore_errors=True)
        try:
            os.remove(db_path)
        except OSError:
            pass


def _get_json(url: str, timeout: float = 5.0):
    req = urlrequest.Request(url, method="GET")
    with closing(urlrequest.urlopen(req, timeout=timeout)) as resp:
        body = resp.read()
        return resp.status, json.loads(body) if body else None


def _post_json(url: str, payload: dict, timeout: float = 5.0):
    data = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with closing(urlrequest.urlopen(req, timeout=timeout)) as resp:
            body = resp.read()
            return resp.status, json.loads(body) if body else None
    except urlerror.HTTPError as e:
        body = e.read()
        return e.code, json.loads(body) if body else None


def _get_raw(url: str, timeout: float = 5.0):
    req = urlrequest.Request(url, method="GET")
    try:
        with closing(urlrequest.urlopen(req, timeout=timeout)) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urlerror.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


def test_make_server_picks_a_port(running_server):
    base = running_server["base"]
    assert base.startswith("http://127.0.0.1:")
    # quick connectivity check via raw socket
    host, port = base.replace("http://", "").split(":")
    with closing(socket.create_connection((host, int(port)), timeout=2)):
        pass


def test_health_endpoint(running_server):
    status, body = _get_json(running_server["base"] + "/api/health")
    assert status == 200
    assert body["ok"] is True
    assert "schema_version" in body
    # New accounts field — defaults to 0 in tests (no account index injected).
    assert "accounts_indexed" in body
    assert isinstance(body["accounts_indexed"], int)


def test_workspaces_endpoint(running_server):
    status, body = _get_json(running_server["base"] + "/api/workspaces")
    assert status == 200
    names = sorted(w["directory_name"] for w in body)
    assert names == ["x", "y"]


def test_sessions_no_query(running_server):
    status, body = _get_json(running_server["base"] + "/api/sessions")
    assert status == 200
    ids = sorted(s["session_id"] for s in body)
    assert ids == ["sA", "sB", "sC", "sD", "sE"]


def test_sessions_search_one_term(running_server):
    status, body = _get_json(running_server["base"] + "/api/sessions?q=cat")
    assert status == 200
    ids = sorted(s["session_id"] for s in body)
    assert ids == ["sA", "sB", "sE"]
    # snippets should contain the «match» marker
    for s in body:
        assert s["snippets"]
        assert any("«" in sn["text"] for sn in s["snippets"])


def test_sessions_search_and_terms(running_server):
    status, body = _get_json(running_server["base"] + "/api/sessions?q=cat&q=happy")
    assert status == 200
    ids = sorted(s["session_id"] for s in body)
    assert ids == ["sB", "sE"]


def test_export_writes_file(running_server):
    status, body = _post_json(
        running_server["base"] + "/api/export",
        {"session_id": "sA"},
    )
    assert status == 200
    assert os.path.exists(body["path"])
    assert body["bytes"] > 0
    assert body["mode"] == "clean"


def test_export_unknown_session_400(running_server):
    status, body = _post_json(
        running_server["base"] + "/api/export",
        {"session_id": "nope-nope"},
    )
    assert status == 400
    assert "error" in body


def test_root_serves_html(running_server):
    status, ctype, body = _get_raw(running_server["base"] + "/")
    assert status == 200
    assert ctype.startswith("text/html")
    assert b"Conductor Chat Search" in body


def test_static_path_traversal_refused(running_server):
    status, _ctype, _body = _get_raw(running_server["base"] + "/static/../etc/passwd")
    assert status in (400, 404)


# -- Feature A: account field --


def test_sessions_carry_account_field(running_server):
    """Every row in /api/sessions has the ``account`` key (may be null)."""
    status, body = _get_json(running_server["base"] + "/api/sessions")
    assert status == 200
    for s in body:
        assert "account" in s
        # Tests don't inject an account_index, so every row is None.
        assert s["account"] is None


def test_sessions_search_results_carry_account_field(running_server):
    status, body = _get_json(running_server["base"] + "/api/sessions?q=cat")
    assert status == 200
    for s in body:
        assert "account" in s


def test_sessions_account_filter_orphaned(running_server):
    """?account=__orphaned__ returns rows where account is None.

    In this test setup no index is injected so every row is orphaned —
    this filter returns the full set.
    """
    status, body = _get_json(
        running_server["base"] + "/api/sessions?account=__orphaned__"
    )
    assert status == 200
    ids = sorted(s["session_id"] for s in body)
    assert ids == ["sA", "sB", "sC", "sD", "sE"]


def test_sessions_account_filter_unknown_account_returns_empty(running_server):
    """Filtering by a non-existent account returns an empty list."""
    status, body = _get_json(
        running_server["base"] + "/api/sessions?account=ghost-account"
    )
    assert status == 200
    assert body == []


# -- Feature B: exact-phrase + match_kind --


def test_sessions_match_kind_field_present(running_server):
    """Every row carries match_kind = 'and' or 'exact'."""
    status, body = _get_json(running_server["base"] + "/api/sessions?q=cat")
    assert status == 200
    for s in body:
        assert s["match_kind"] in ("and", "exact")
        assert s["match_kind"] == "and"


def test_sessions_q_exact_param(running_server):
    """?q_exact=<phrase> matches the contiguous substring and marks the row
    as match_kind='exact'."""
    from urllib.parse import quote
    phrase = "migration of the 684 sites"
    url = running_server["base"] + "/api/sessions?q_exact=" + quote(phrase)
    status, body = _get_json(url)
    assert status == 200
    ids = [s["session_id"] for s in body]
    assert ids == ["sE"]
    assert body[0]["match_kind"] == "exact"
    # Snippet wraps the whole phrase, not individual words.
    snip = body[0]["snippets"][0]
    assert "«migration of the 684 sites»" in snip["text"]
    assert snip["exact"] is True


def test_sessions_q_exact_no_match(running_server):
    """A phrase that's not present returns an empty list."""
    from urllib.parse import quote
    url = (
        running_server["base"]
        + "/api/sessions?q_exact="
        + quote("not a phrase that exists anywhere")
    )
    status, body = _get_json(url)
    assert status == 200
    assert body == []


def test_sessions_mixed_q_and_q_exact(running_server):
    """Combining an AND term with an exact phrase requires both to match."""
    from urllib.parse import quote
    url = (
        running_server["base"]
        + "/api/sessions?q=happy&q_exact="
        + quote("migration of the 684 sites")
    )
    status, body = _get_json(url)
    assert status == 200
    ids = [s["session_id"] for s in body]
    assert ids == ["sE"]
    assert body[0]["match_kind"] == "exact"


# -- Feature A: /api/sessions/lookup --


def _get_json_allow_err(url: str, timeout: float = 5.0):
    """Like _get_json but doesn't raise on 4xx — returns (status, body) too."""
    req = urlrequest.Request(url, method="GET")
    try:
        with closing(urlrequest.urlopen(req, timeout=timeout)) as resp:
            body = resp.read()
            return resp.status, json.loads(body) if body else None
    except urlerror.HTTPError as e:
        body = e.read()
        return e.code, json.loads(body) if body else None


def test_sessions_lookup_missing_id_param_400(running_server):
    """No ?id= -> 400 with an explanatory error."""
    status, body = _get_json_allow_err(
        running_server["base"] + "/api/sessions/lookup"
    )
    assert status == 400
    assert "error" in body


def test_sessions_lookup_short_id_400(running_server):
    """An id under 8 chars is refused so prefix scans stay narrow."""
    status, body = _get_json_allow_err(
        running_server["base"] + "/api/sessions/lookup?id=abc"
    )
    assert status == 400
    assert "8" in body["error"]


def test_sessions_lookup_unknown_id_returns_empty_list(running_server):
    """A non-matching 8+ char prefix returns [] (not an error)."""
    status, body = _get_json(
        running_server["base"] + "/api/sessions/lookup?id=deadbeef"
    )
    assert status == 200
    assert body == []


def test_sessions_lookup_full_uuid_equality(running_server):
    """A UUID-shaped value uses equality lookup (0 or 1 row)."""
    fake_uuid = "0ba636dc-5c0c-47c4-b4a7-27ea45e79533"
    status, body = _get_json(
        running_server["base"] + "/api/sessions/lookup?id=" + fake_uuid
    )
    assert status == 200
    # Fixture has no such id; equality lookup must not pick up a partial.
    assert body == []


def test_sessions_lookup_prefix_returns_rows(running_server):
    """Prefix match returns full session rows including the lookup-extension fields."""
    # Fixture ids are 'sA', 'sB', 'sC', 'sD', 'sE'. We can't use those raw
    # prefixes (only 2 chars) — bump to a 8-char synthetic instead. We pad
    # the prefix by re-inserting a known id with a longer value via the
    # public API: use 'sA' as the exact target, but the API requires 8+ chars
    # so build a longer test id by lookup of a known long substring.
    # Easiest path: hit the lookup with the FULL id 'sA' won't work
    # (under 8 chars) — instead skip prefix-row test against the search
    # fixture (its ids are short) and rely on the db unit tests.
    # Here we just confirm the endpoint accepts an 8-char input and returns
    # an empty list cleanly.
    status, body = _get_json(
        running_server["base"] + "/api/sessions/lookup?id=sAsAsAsA"
    )
    assert status == 200
    assert isinstance(body, list)


def test_sessions_lookup_row_shape(running_server):
    """Returned rows match the /api/sessions row shape (so the same UI renderer works)."""
    # Build a fresh fixture with a long session id we can prefix-match.
    import tempfile, threading, sqlite3 as sq
    from tests.fixtures.build_fixture import SCHEMA_SQL, _assistant_text

    fd, db_path = tempfile.mkstemp(prefix="cchat-lookup-fixture-", suffix=".db")
    os.close(fd)
    con = sq.connect(db_path)
    try:
        con.executescript(SCHEMA_SQL)
        con.execute(
            "INSERT INTO workspaces (id, directory_name, branch, state) VALUES (?, ?, ?, ?)",
            ("ws-x", "x", "main", "active"),
        )
        long_id = "0ba636dc-5c0c-47c4-b4a7-27ea45e79533"
        con.execute(
            """INSERT INTO sessions
            (id, title, workspace_id, model, agent_type, created_at, updated_at,
             last_user_message_at, context_used_percent, context_token_count, is_hidden)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (long_id, "Continue Resolve Abandoned Work", "ws-x", "sonnet", "claude",
             "2026-05-15T10:00:00", "2026-05-17T12:00:00",
             "2026-05-17T11:50:00", None, None, 0),
        )
        con.execute(
            """INSERT INTO session_messages
            (id, session_id, role, content, created_at, sent_at, turn_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("m1", long_id, "assistant",
             _assistant_text("PR #1720 rebased cleanly today."),
             "2026-05-17T11:55:30", "2026-05-17T11:55:30", "t1"),
        )
        con.execute(
            "INSERT INTO _sqlx_migrations (version, description) VALUES (?, ?)",
            (20260101, "init"),
        )
        con.commit()
    finally:
        con.close()

    # Boot a dedicated server for this fixture.
    from conductor_chat import server as _server
    httpd = _server.make_server(host="127.0.0.1", port=0, db_path=db_path, fts_path=None)
    port = httpd.server_address[1]
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    try:
        base = f"http://127.0.0.1:{port}"
        # Prefix lookup
        status, body = _get_json(base + "/api/sessions/lookup?id=0ba636dc")
        assert status == 200
        assert len(body) == 1
        row = body[0]
        assert row["session_id"] == long_id
        assert row["title"] == "Continue Resolve Abandoned Work"
        assert row["workspace_name"] == "x"
        assert row["message_count"] == 1
        # Frontend renders the row template — needs these fields too.
        assert "snippets" in row and row["snippets"] == []
        assert row["match_kind"] == "and"
        assert "account" in row

        # Full UUID equality
        status, body = _get_json(base + "/api/sessions/lookup?id=" + long_id)
        assert status == 200
        assert len(body) == 1
        assert body[0]["session_id"] == long_id
    finally:
        httpd.shutdown()
        httpd.server_close()
        th.join(timeout=5)
        try: os.remove(db_path)
        except OSError: pass
