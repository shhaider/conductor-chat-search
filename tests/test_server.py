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
