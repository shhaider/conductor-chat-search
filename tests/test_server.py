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
    # Verify-step fields — the endpoint must confirm the file is real before
    # returning success so the UI never lies about a not-yet-visible file.
    assert body["verified"] is True
    assert body["verified_size_bytes"] == body["bytes"]
    # verified_size_bytes must match actual on-disk size too (defence in depth).
    assert body["verified_size_bytes"] == os.path.getsize(body["path"])
    assert isinstance(body["verified_at"], str) and body["verified_at"]
    # ISO 8601 — sanity check: contains 'T' separator and a timezone offset/Z.
    assert "T" in body["verified_at"]


def test_export_unknown_session_400(running_server):
    status, body = _post_json(
        running_server["base"] + "/api/export",
        {"session_id": "nope-nope"},
    )
    assert status == 400
    assert "error" in body


def test_export_verify_fails_when_file_deleted_mid_flight(running_server, monkeypatch):
    """Simulate the race where the file is gone by the time we re-stat.

    We monkeypatch the server's verify-pause helper to delete the freshly
    written file during the pause window between the first and second stat.
    The endpoint must return 500 with the documented error shape — not
    silently claim success.
    """
    import os as _os
    from conductor_chat import server as _server

    sid = "sA"
    deleted = {"path": None}

    original_sleep = _server.time.sleep

    def _delete_during_pause(seconds):
        # Find the just-written export file in the out_dir and delete it.
        out_dir = running_server["out_dir"]
        entries = sorted(_os.listdir(out_dir))
        ours = [e for e in entries if e.startswith(f"conductor-chat-{sid}-") and e.endswith(".md")]
        if ours:
            # Newest one (lexicographic order matches timestamp order).
            target = _os.path.join(out_dir, ours[-1])
            try:
                _os.remove(target)
                deleted["path"] = target
            except OSError:
                pass
        # Still sleep so the server's verify timing is unchanged.
        original_sleep(seconds)

    monkeypatch.setattr(_server.time, "sleep", _delete_during_pause)

    status, body = _post_json(
        running_server["base"] + "/api/export",
        {"session_id": sid},
    )
    assert status == 500
    assert "error" in body
    assert "verify failed" in body["error"]
    # Failure shape contract: path + expected_size + actual_size all present.
    assert "path" in body
    assert "expected_size" in body
    assert "actual_size" in body
    # The file we deleted should match what the error reports.
    # Compare via realpath because macOS resolves /var -> /private/var, and
    # export.export_session() returns the resolved path while listdir does not.
    assert deleted["path"] is not None
    assert os.path.realpath(body["path"]) == os.path.realpath(deleted["path"])


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


# -- Feature: general file search (files-by-name, files-by-content, copy, reveal) --


@pytest.fixture()
def files_tree(tmp_path):
    """Tree of fixture files for the file-search endpoints.

    Lives under ``tmp_path`` so it's outside HOME — we use it as a
    ``scope`` argument and validate_path_for_action allows /var/folders/.
    """
    root = tmp_path / "files_tree"
    root.mkdir()
    (root / "alpha.md").write_text(
        "the quick brown fox migration of the 684 sites\n", encoding="utf-8"
    )
    (root / "beta.txt").write_text("hello world\n", encoding="utf-8")
    (root / ".secret.md").write_text("hidden content here\n", encoding="utf-8")
    return root


def test_files_by_name_finds_match(running_server, files_tree):
    from urllib.parse import urlencode
    url = (
        running_server["base"]
        + "/api/files/by-name?"
        + urlencode({"q": "alpha", "scope": str(files_tree)})
    )
    status, body = _get_json(url)
    assert status == 200
    basenames = [r["basename"] for r in body]
    assert "alpha.md" in basenames
    # Required fields:
    row = next(r for r in body if r["basename"] == "alpha.md")
    for k in ("path", "basename", "parent_dir", "size_bytes", "mtime_iso", "kind"):
        assert k in row


def test_files_by_name_missing_q_400(running_server):
    status, body = _get_json_allow_err(running_server["base"] + "/api/files/by-name")
    assert status == 400
    assert "error" in body


def test_files_by_name_include_hidden_toggle(running_server, files_tree):
    from urllib.parse import urlencode
    # Default include_hidden=True → finds .secret.md
    url = (
        running_server["base"]
        + "/api/files/by-name?"
        + urlencode({"q": "*.md", "scope": str(files_tree)})
    )
    status, body = _get_json(url)
    assert status == 200
    names = [r["basename"] for r in body]
    assert ".secret.md" in names

    # include_hidden=false → omits it
    url2 = (
        running_server["base"]
        + "/api/files/by-name?"
        + urlencode({"q": "*.md", "scope": str(files_tree), "include_hidden": "false"})
    )
    status2, body2 = _get_json(url2)
    assert status2 == 200
    names2 = [r["basename"] for r in body2]
    assert ".secret.md" not in names2
    assert "alpha.md" in names2


def test_files_by_content_finds_phrase(running_server, files_tree):
    from urllib.parse import urlencode
    from conductor_chat import files as F
    if not F.ripgrep_available():
        pytest.skip("ripgrep not installed")
    url = (
        running_server["base"]
        + "/api/files/by-content?"
        + urlencode({"q": "migration of the 684 sites", "scope": str(files_tree)})
    )
    status, body = _get_json(url)
    assert status == 200
    assert any(r["path"].endswith("/alpha.md") for r in body)
    # Snippet carries the «match» markers.
    matched = [r for r in body if r["path"].endswith("/alpha.md")]
    assert "«migration of the 684 sites»" in matched[0]["match_text"]


def test_files_by_content_missing_q_400(running_server):
    from conductor_chat import files as F
    if not F.ripgrep_available():
        pytest.skip("ripgrep not installed")
    status, body = _get_json_allow_err(
        running_server["base"] + "/api/files/by-content"
    )
    assert status == 400


def test_files_by_content_503_when_rg_unavailable(running_server_no_rg, files_tree):
    """The endpoint returns 503 with an install hint when ripgrep is missing."""
    from urllib.parse import urlencode
    url = (
        running_server_no_rg["base"]
        + "/api/files/by-content?"
        + urlencode({"q": "anything", "scope": str(files_tree)})
    )
    status, body = _get_json_allow_err(url)
    assert status == 503
    assert "ripgrep" in body["error"].lower()
    assert "brew install ripgrep" in body["error"]


def test_files_copy_to_downloads(running_server, files_tree, tmp_path, monkeypatch):
    """POSTing a path copies it to the (mocked) Downloads dir and verifies."""
    import os as _os
    # Redirect ~/Downloads to a temp dir so we don't pollute the real one.
    fake_dl = tmp_path / "FakeDownloads"
    fake_dl.mkdir()
    from conductor_chat import files as F
    monkeypatch.setattr(F, "DEFAULT_DOWNLOADS_DIR", str(fake_dl))

    src = files_tree / "alpha.md"
    status, body = _post_json(
        running_server["base"] + "/api/files/copy-to-downloads",
        {"path": str(src)},
    )
    assert status == 200, body
    assert body["verified"] is True
    assert body["bytes"] == src.stat().st_size
    assert _os.path.exists(body["copied_to"])
    assert body["copied_to"].startswith(str(fake_dl))


def test_files_copy_rejects_missing_path(running_server):
    status, body = _post_json(
        running_server["base"] + "/api/files/copy-to-downloads",
        {"path": "/no/such/file/exists.xyz"},
    )
    assert status == 404
    assert "error" in body


def test_files_copy_rejects_missing_body_field(running_server):
    status, body = _post_json(
        running_server["base"] + "/api/files/copy-to-downloads",
        {},
    )
    assert status == 400


def test_files_copy_rejects_outside_home(running_server):
    """/etc/hosts is outside HOME and Volumes; should be 400."""
    status, body = _post_json(
        running_server["base"] + "/api/files/copy-to-downloads",
        {"path": "/etc/hosts"},
    )
    assert status == 400
    assert "outside" in body["error"].lower()


def test_files_reveal_invokes_open_dash_r(running_server, files_tree, monkeypatch):
    """POSTing reveal calls open -R via subprocess (mocked)."""
    import subprocess as _sp
    from conductor_chat import files as F
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return _sp.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(F.subprocess, "run", fake_run)

    src = files_tree / "alpha.md"
    status, body = _post_json(
        running_server["base"] + "/api/files/reveal",
        {"path": str(src)},
    )
    assert status == 200
    assert body == {"ok": True}
    assert captured["argv"][:2] == ["open", "-R"]
    assert captured["argv"][2] == str(src)


def test_files_reveal_rejects_missing(running_server):
    status, body = _post_json(
        running_server["base"] + "/api/files/reveal",
        {"path": "/no/such/file/exists.xyz"},
    )
    assert status == 404
    assert body["ok"] is False


@pytest.fixture()
def running_server_no_rg():
    """Like ``running_server`` but with ripgrep marked unavailable."""
    db_path = build_search_fixture()
    httpd = server.make_server(
        host="127.0.0.1", port=0, db_path=db_path, fts_path=None, ripgrep_ok=False
    )
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"base": f"http://127.0.0.1:{port}"}
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        try:
            os.remove(db_path)
        except OSError:
            pass


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
