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


# -------- Feature A: account column --------


def test_index_contains_account_dropdown():
    html = _read_index()
    assert 'id="account"' in html
    assert "All accounts" in html
    assert "__orphaned__" in html


def test_index_contains_account_badge_css():
    html = _read_index()
    assert ".acct" in html
    assert ".acct.orphaned" in html


def test_index_contains_orphaned_copy():
    html = _read_index()
    # Visual marker text for sessions Claude Code cannot resume.
    assert "(orphaned)" in html


def test_index_contains_accounts_indexed_meta():
    html = _read_index()
    # The /api/health response's accounts_indexed field is surfaced in the meta line.
    assert "accounts_indexed" in html


# -------- Feature B: exact-phrase toggle + match_kind badges --------


def test_index_contains_exact_toggle():
    html = _read_index()
    assert 'id="exact-toggle"' in html
    assert "Exact phrase" in html


def test_index_contains_match_kind_badge_css():
    html = _read_index()
    assert "badge-mk" in html
    assert ".badge-mk.exact" in html
    assert ".badge-mk.and" in html


def test_index_contains_exact_row_accent():
    html = _read_index()
    # Visual cue for exact-phrase rows (left border accent).
    assert ".row.exact" in html


def test_index_emits_q_exact_param():
    html = _read_index()
    # The JS sends q_exact for phrase terms, q for AND terms.
    assert "q_exact" in html


def test_index_contains_match_kind_field_reference():
    html = _read_index()
    # render() inspects s.match_kind to set the badge class.
    assert "match_kind" in html


# -------- Feature A (id-lookup) + Feature B (toggle persistence)
#          + Feature C (clickable session ID) --------


def test_index_contains_chat_id_input():
    html = _read_index()
    assert 'id="chat-id"' in html
    # Label copy the user sees.
    assert "Chat ID:" in html
    assert 'id="chat-id-clear"' in html


def test_index_calls_lookup_endpoint():
    html = _read_index()
    # The JS must hit /api/sessions/lookup with the entered ID.
    assert "/api/sessions/lookup" in html
    assert "loadById" in html


def test_index_contains_no_chat_with_that_id_copy():
    html = _read_index()
    # The required user-facing empty-state copy.
    assert "No chat with that ID." in html


def test_index_persists_exact_toggle_to_localstorage():
    html = _read_index()
    # The toggle's persistence key + the apply/init function names.
    assert "cchat-exact-toggle" in html
    assert "initExactToggle" in html
    assert "applyExactToggle" in html


def test_index_placeholder_strings_depend_on_toggle():
    html = _read_index()
    # Both placeholder variants must appear in the JS as constants.
    assert "Type the exact phrase you remember" in html
    assert "Type words; space = AND" in html


def test_index_session_id_is_clickable_copyable():
    html = _read_index()
    # The .sid chip carries .copyable and a click-to-copy handler.
    assert "sid copyable" in html
    assert ".sid.copyable" in html
    assert "navigator.clipboard.writeText" in html


# -------- Export verify flow (this branch) --------


def test_index_export_button_states_present():
    """All four button-state labels for the verified export flow must be in the JS source."""
    html = _read_index()
    assert "Exporting…" in html
    assert "Verifying…" in html
    assert "✓ Exported" in html
    assert "✗ Export failed" in html


def test_index_export_button_uses_verified_response():
    """The frontend gates the 'Exported' state on the server's verified=true field."""
    html = _read_index()
    # Reads the verified flag from the JSON response.
    assert "r.verified" in html
    # Surfaces the verified size to the user.
    assert "verified_size_bytes" in html


def test_index_export_button_has_inline_spinner_css():
    """Pure-CSS spinner sits inside the button while the export is in flight."""
    html = _read_index()
    assert ".btn-spinner" in html
    assert ".export-btn" in html


def test_index_export_button_shows_path_below():
    """On verified success the rendered path appears beneath the button."""
    html = _read_index()
    assert ".export-path" in html
    assert "export-path" in html  # also referenced by class= in JS


# -------- Tab strip + multi-tab UI (files-by-name, files-by-content) --------


def test_index_contains_tab_strip():
    html = _read_index()
    # Three tab buttons.
    assert 'id="tab-chats"' in html
    assert 'id="tab-filename"' in html
    assert 'id="tab-content"' in html
    # Tab labels visible to operator.
    assert "Conductor chats" in html
    assert "Files by name" in html
    assert "Files by content" in html


def test_index_tab_panels_present():
    html = _read_index()
    assert 'data-panel="chats"' in html
    assert 'data-panel="filename"' in html
    assert 'data-panel="content"' in html
    # Chats panel is the default active one.
    assert 'class="tab-panel active" data-panel="chats"' in html


def test_index_persists_active_tab():
    html = _read_index()
    assert "cchat-active-tab" in html
    assert "initActiveTab" in html
    assert "activateTab" in html


def test_index_contains_files_by_name_controls():
    html = _read_index()
    assert 'id="fn-q"' in html
    assert 'id="fn-scope"' in html
    assert 'id="fn-hidden"' in html
    assert 'id="file-name-results"' in html


def test_index_contains_files_by_content_controls():
    html = _read_index()
    assert 'id="fc-q"' in html
    assert 'id="fc-scope"' in html
    assert 'id="fc-hidden"' in html
    assert 'id="file-content-results"' in html


def test_index_hits_files_endpoints():
    html = _read_index()
    assert "/api/files/by-name" in html
    assert "/api/files/by-content" in html
    assert "/api/files/copy-to-downloads" in html
    assert "/api/files/reveal" in html


def test_index_contains_reveal_and_copy_buttons():
    html = _read_index()
    # Action buttons that show on every file row.
    assert "Reveal in Finder" in html
    assert "Copy to Downloads" in html
    # CSS classes used to delegate clicks.
    assert "reveal-btn" in html
    assert "copy-btn" in html


def test_index_file_rows_render_file_metadata():
    html = _read_index()
    # Per-row helpers used in the file-row renderers.
    assert "renderFileNameRows" in html
    assert "renderFileContentRows" in html
    assert "fmtBytes" in html


def test_index_include_hidden_defaults_on():
    html = _read_index()
    # Both file tabs ship with the include-hidden checkbox checked.
    # The 'checked' attribute appears on both fn-hidden and fc-hidden.
    # (We check both inputs are marked checked.)
    assert 'id="fn-hidden" type="checkbox" checked' in html
    assert 'id="fc-hidden" type="checkbox" checked' in html


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
