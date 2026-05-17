"""Static inspection of the served index.html.

We don't have a browser test runner — and the UX feedback layer is JS+CSS
only — so these tests assert that the expected class names, ids, status
copy, and structural pieces are present in the HTML the server actually
serves. If any of these strings get accidentally removed in a future edit,
the UX feedback layer would silently break; these tests catch that.
"""

from __future__ import annotations

import http.client
import threading
from pathlib import Path

from conductor_chat import server
from tests.fixtures import build_fixture


REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_PATH = REPO_ROOT / "src" / "conductor_chat" / "static" / "index.html"


def _read_index() -> str:
    return INDEX_PATH.read_text(encoding="utf-8")


# -------- offline (read the file directly) --------


def test_index_contains_search_status_div():
    html = _read_index()
    assert 'id="search-status"' in html
    assert 'class="search-status hidden"' in html


def test_index_contains_spinner_styles():
    html = _read_index()
    # CSS class + keyframes for the in-progress spinner.
    assert ".spinner" in html
    assert "@keyframes spin" in html


def test_index_contains_input_searching_class():
    html = _read_index()
    # Class applied to the search input while a request is in flight.
    assert 'input[type="search"].searching' in html
    assert "@keyframes search-pulse" in html


def test_index_contains_perf_banner_copy():
    html = _read_index()
    # The exact heads-up copy that explains why searches are slow (issue #2).
    assert "Heads up: searches currently take 10-40s" in html
    assert "FTS5 fix tracked at #2" in html
    assert 'id="perf-banner"' in html
    assert 'id="perf-banner-dismiss"' in html


def test_index_contains_localstorage_dismiss_key():
    html = _read_index()
    # The dismiss-state key must match what the spec calls out.
    assert "cchat-perf-banner-dismissed" in html


def test_index_contains_abort_controller():
    html = _read_index()
    # Abort-old-on-new behaviour — required so slow searches don't pile up.
    assert "AbortController" in html
    assert "inflightController" in html


def test_index_contains_setsearchingui_helper():
    html = _read_index()
    # The function that toggles workspace/export disabled state during search.
    assert "function setSearchingUi" in html
    assert "elWs.disabled" in html


def test_index_contains_found_in_seconds_copy():
    html = _read_index()
    # The "Found N matches for <term> in 12.3s" success message.
    assert "Found " in html
    assert "matches" in html or "match" in html


def test_index_contains_searching_for_copy():
    html = _read_index()
    # The "Searching for <term>..." in-progress message.
    assert "Searching for " in html


def test_index_contains_search_failed_copy():
    html = _read_index()
    # The error path on the new status line.
    assert "Search failed: " in html


# -------- live (boot the server and curl /) --------


def test_server_serves_new_ux_strings(tmp_path):
    """End-to-end: boot the server and confirm the served HTML carries the
    new strings. Catches the case where a build step or a stale cached copy
    silently strips the new UX additions."""
    db_path = build_fixture.build_default_fixture(str(tmp_path / "fixture.db"))
    httpd = server.make_server(host="127.0.0.1", port=0, db_path=db_path)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        con = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        con.request("GET", "/")
        resp = con.getresponse()
        body = resp.read().decode("utf-8")
        assert resp.status == 200
        # Same critical strings as the offline tests — but proves the server actually serves them.
        for needle in [
            'id="search-status"',
            ".spinner",
            "@keyframes search-pulse",
            "Heads up: searches currently take 10-40s",
            "FTS5 fix tracked at #2",
            "cchat-perf-banner-dismissed",
            "AbortController",
        ]:
            assert needle in body, f"missing UX string in served HTML: {needle!r}"
    finally:
        httpd.shutdown()
        httpd.server_close()
