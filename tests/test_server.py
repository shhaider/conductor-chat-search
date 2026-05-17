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

    httpd = server.make_server(host="127.0.0.1", port=0, db_path=db_path)
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


def test_workspaces_endpoint(running_server):
    status, body = _get_json(running_server["base"] + "/api/workspaces")
    assert status == 200
    names = sorted(w["directory_name"] for w in body)
    assert names == ["x", "y"]


def test_sessions_no_query(running_server):
    status, body = _get_json(running_server["base"] + "/api/sessions")
    assert status == 200
    ids = sorted(s["session_id"] for s in body)
    assert ids == ["sA", "sB", "sC", "sD"]


def test_sessions_search_one_term(running_server):
    status, body = _get_json(running_server["base"] + "/api/sessions?q=cat")
    assert status == 200
    ids = sorted(s["session_id"] for s in body)
    assert ids == ["sA", "sB"]
    # snippets should contain the «match» marker
    for s in body:
        assert s["snippets"]
        assert any("«" in sn["text"] for sn in s["snippets"])


def test_sessions_search_and_terms(running_server):
    status, body = _get_json(running_server["base"] + "/api/sessions?q=cat&q=happy")
    assert status == 200
    ids = [s["session_id"] for s in body]
    assert ids == ["sB"]


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
