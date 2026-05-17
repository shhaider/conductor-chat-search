"""Unit tests for conductor_chat.fts — sidecar FTS5 index over text fragments.

Covers:
  - Text-fragment extraction from the JSON content blob (only type=="text").
  - Initial backfill against a fresh sidecar.
  - Incremental sync: add new source rows, re-sync, confirm they're findable.
  - Schema-version drift: stored value is updated, warning emitted.
  - Fallback path when FTS5 is unavailable (skipped on environments that have it).
  - End-to-end search results are the same as the LIKE backend on the search fixture.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile

import pytest

from conductor_chat import db, fts, search
from tests.fixtures.build_fixture import (
    SCHEMA_SQL,
    build_search_fixture,
    _assistant_text,
    _user_text,
    _assistant_blocks,
    _user_blocks,
)


# -- extract_text_fragments --


def test_extract_text_fragments_assistant_text():
    raw = _assistant_text("the cat sat on the mat")
    fragments = fts.extract_text_fragments(raw)
    assert fragments == ["the cat sat on the mat"]


def test_extract_text_fragments_user_text():
    raw = _user_text("hello world")
    fragments = fts.extract_text_fragments(raw)
    assert fragments == ["hello world"]


def test_extract_text_fragments_skips_tool_use():
    raw = _assistant_blocks([
        {"type": "tool_use", "name": "Read", "input": {"file_path": "cat.py"}}
    ])
    assert fts.extract_text_fragments(raw) == []


def test_extract_text_fragments_skips_tool_result():
    raw = _user_blocks([
        {"tool_use_id": "x", "type": "tool_result",
         "content": "found cat in output", "is_error": False}
    ])
    assert fts.extract_text_fragments(raw) == []


def test_extract_text_fragments_skips_thinking():
    raw = _assistant_blocks([
        {"type": "thinking", "thinking": "interesting cat fact"}
    ])
    assert fts.extract_text_fragments(raw) == []


def test_extract_text_fragments_mixed_blocks():
    raw = _assistant_blocks([
        {"type": "text", "text": "step 1"},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "x.py"}},
        {"type": "text", "text": "step 2"},
    ])
    assert fts.extract_text_fragments(raw) == ["step 1", "step 2"]


def test_extract_text_fragments_malformed_json_returns_empty():
    assert fts.extract_text_fragments("not json {") == []
    assert fts.extract_text_fragments(None) == []
    assert fts.extract_text_fragments("") == []
    assert fts.extract_text_fragments('"a string"') == []


def test_extract_text_fragments_content_as_string():
    """Some payloads have message.content as a bare string instead of a list."""
    raw = json.dumps({
        "type": "user",
        "message": {"role": "user", "content": "bare string content"},
    })
    assert fts.extract_text_fragments(raw) == ["bare string content"]


# -- has_fts5 --


def test_has_fts5_returns_bool():
    # Just confirm it returns a bool without raising. On CPython on macOS
    # FTS5 is bundled, so this should be True in practice.
    assert isinstance(fts.has_fts5(), bool)


# -- build_or_sync --


def _open_temp_fts(tmp_path):
    path = str(tmp_path / "sidecar.db")
    return fts.open_fts(path), path


def test_backfill_indexes_only_text_fragments(tmp_path):
    if not fts.has_fts5():
        pytest.skip("FTS5 not available in this Python's SQLite")
    src_path = build_search_fixture(str(tmp_path / "src.db"))
    src = db.open_ro(src_path)
    fcon, _fpath = _open_temp_fts(tmp_path)
    try:
        stats = fts.build_or_sync(fcon, src, progress_stream=None)
        # The search fixture has: mA1 (text), mA2 (tool_use), mB1 (text), mB2
        # (text), mC1 (tool_result), mD1 (thinking), mE1 (text) → 4 indexed,
        # 3 skipped.
        assert stats["is_initial_build"] is True
        assert stats["indexed"] == 4
        assert stats["skipped"] == 3
        # Now MATCH "cat" — should hit mA1, mB2, mE1, but NOT mC1 or mD1.
        hits = fts.search_term(fcon, "cat")
        assert hits == {"mA1", "mB2", "mE1"}
    finally:
        fcon.close()
        src.close()


def test_incremental_sync_picks_up_new_rows(tmp_path):
    if not fts.has_fts5():
        pytest.skip("FTS5 not available in this Python's SQLite")
    src_path = build_search_fixture(str(tmp_path / "src.db"))
    fpath = str(tmp_path / "sidecar.db")

    # Initial build
    src = db.open_ro(src_path)
    fcon = fts.open_fts(fpath)
    try:
        fts.build_or_sync(fcon, src, progress_stream=None)
    finally:
        fcon.close()
        src.close()

    # Append a new row to the source DB (need a writeable connection).
    write_con = sqlite3.connect(src_path)
    try:
        write_con.execute(
            """INSERT INTO session_messages
            (id, session_id, role, content, created_at, sent_at, turn_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "mNEW", "sA", "user",
                _user_text("a brand new aardvark sighting"),
                "2026-05-17T20:00:00", "2026-05-17T20:00:00", "tNEW",
            ),
        )
        write_con.commit()
    finally:
        write_con.close()

    # Re-open read-only and sync.
    src = db.open_ro(src_path)
    fcon = fts.open_fts(fpath)
    try:
        # Before sync: aardvark not indexed.
        assert fts.search_term(fcon, "aardvark") == set()
        stats = fts.build_or_sync(fcon, src, progress_stream=None)
        assert stats["is_initial_build"] is False
        assert stats["indexed"] == 1
        # After sync: aardvark is now findable, pointing at the new message.
        assert fts.search_term(fcon, "aardvark") == {"mNEW"}
        # And re-syncing again is a no-op.
        stats2 = fts.build_or_sync(fcon, src, progress_stream=None)
        assert stats2["indexed"] == 0
        assert stats2["skipped"] == 0
    finally:
        fcon.close()
        src.close()


def test_schema_version_drift_warns_but_stores_new_version(tmp_path):
    if not fts.has_fts5():
        pytest.skip("FTS5 not available in this Python's SQLite")
    fpath = str(tmp_path / "sidecar.db")
    fcon = fts.open_fts(fpath)
    try:
        # First call: no prior version stored → no warning, version recorded.
        buf = io.StringIO()
        fts.check_schema_version(fcon, "20260101", progress_stream=buf)
        assert "WARN" not in buf.getvalue()
        # Second call: same version → still no warning.
        buf = io.StringIO()
        fts.check_schema_version(fcon, "20260101", progress_stream=buf)
        assert "WARN" not in buf.getvalue()
        # Third call: version changed → warning, new version stored.
        buf = io.StringIO()
        fts.check_schema_version(fcon, "20260999", progress_stream=buf)
        out = buf.getvalue()
        assert "WARN" in out
        assert "schema_version changed" in out
        # Fourth call with new version → no warning (idempotent).
        buf = io.StringIO()
        fts.check_schema_version(fcon, "20260999", progress_stream=buf)
        assert "WARN" not in buf.getvalue()
    finally:
        fcon.close()


def test_progress_messages_emitted(tmp_path):
    """If we lower PROGRESS_EVERY, the backfill should write progress lines."""
    if not fts.has_fts5():
        pytest.skip("FTS5 not available in this Python's SQLite")
    src_path = build_search_fixture(str(tmp_path / "src.db"))
    fpath = str(tmp_path / "sidecar.db")
    src = db.open_ro(src_path)
    fcon = fts.open_fts(fpath)
    buf = io.StringIO()
    # Temporarily lower the threshold so even our 6-row fixture triggers it.
    original = fts.PROGRESS_EVERY
    fts.PROGRESS_EVERY = 2
    try:
        fts.build_or_sync(fcon, src, progress_stream=buf)
    finally:
        fts.PROGRESS_EVERY = original
        fcon.close()
        src.close()
    out = buf.getvalue()
    assert "FTS backfill" in out
    # Initial build summary should also appear.
    assert "complete" in out


# -- search.search with FTS connection --


def test_search_with_fts_matches_like_results(tmp_path):
    """Functional equivalence: with the FTS backend, the same fixture returns
    the same session_ids as the LIKE backend."""
    if not fts.has_fts5():
        pytest.skip("FTS5 not available in this Python's SQLite")
    src_path = build_search_fixture(str(tmp_path / "src.db"))
    fpath = str(tmp_path / "sidecar.db")

    src = db.open_ro(src_path)
    fcon = fts.open_fts(fpath)
    try:
        fts.build_or_sync(fcon, src, progress_stream=None)

        # LIKE path
        like_results = search.search(src, ["cat"])
        # FTS path
        fts_results = search.search(src, ["cat"], fts_con=fcon)

        like_ids = sorted(r["session_id"] for r in like_results)
        fts_ids = sorted(r["session_id"] for r in fts_results)
        assert like_ids == fts_ids == ["sA", "sB", "sE"]

        # AND semantics: "cat" + "happy" → sB and sE.
        and_results = search.search(src, ["cat", "happy"], fts_con=fcon)
        assert sorted(r["session_id"] for r in and_results) == ["sB", "sE"]

        # Workspace filter
        wsx = search.search(src, ["cat"], workspace_id="ws-x", fts_con=fcon)
        assert sorted(r["session_id"] for r in wsx) == ["sA", "sB", "sE"]
        wsy = search.search(src, ["cat"], workspace_id="ws-y", fts_con=fcon)
        assert wsy == []

        # Thinking-block term is still excluded by the pass-2 confirm step,
        # even if FTS5 somehow happened to index it (it doesn't, since we
        # only feed text fragments into the FTS index).
        assert search.search(src, ["interesting"], fts_con=fcon) == []

        # Snippets still wrap matches with «...»
        cat_results = search.search(src, ["cat"], fts_con=fcon)
        for r in cat_results:
            assert r["snippets"]
            assert any("«" in sn["text"] for sn in r["snippets"])
    finally:
        fcon.close()
        src.close()


def test_search_empty_terms_unchanged_with_fts(tmp_path):
    """Empty-terms list is a list_sessions passthrough — FTS shouldn't change that."""
    if not fts.has_fts5():
        pytest.skip("FTS5 not available in this Python's SQLite")
    src_path = build_search_fixture(str(tmp_path / "src.db"))
    fpath = str(tmp_path / "sidecar.db")
    src = db.open_ro(src_path)
    fcon = fts.open_fts(fpath)
    try:
        fts.build_or_sync(fcon, src, progress_stream=None)
        out = search.search(src, [], fts_con=fcon)
        ids = sorted(r["session_id"] for r in out)
        assert ids == ["sA", "sB", "sC", "sD", "sE"]
    finally:
        fcon.close()
        src.close()


# -- LIKE fallback path (search with fts_con=None) --


def test_search_with_fts_con_none_uses_like(tmp_path):
    """Passing fts_con=None must give correct LIKE-pathway behaviour."""
    src_path = build_search_fixture(str(tmp_path / "src.db"))
    src = db.open_ro(src_path)
    try:
        results = search.search(src, ["cat"], fts_con=None)
        assert sorted(r["session_id"] for r in results) == ["sA", "sB", "sE"]
    finally:
        src.close()


# -- Phrase escaping (avoid FTS5 operator injection) --


def test_search_term_handles_special_chars(tmp_path):
    """Terms with FTS5 operator chars (-, OR, etc.) should not break the query.

    We don't assert hits; just that no OperationalError is raised. With FTS5's
    unicode61 tokenizer, hyphen is a token separator anyway, so 'foo-bar' is
    indexed as two tokens. Quoting in _fts5_phrase prevents operator parsing.
    """
    if not fts.has_fts5():
        pytest.skip("FTS5 not available in this Python's SQLite")
    fpath = str(tmp_path / "sidecar.db")
    fcon = fts.open_fts(fpath)
    try:
        # Insert a row with a phrase that includes operator-looking text
        fcon.execute(
            "INSERT INTO fts_messages(text, message_id) VALUES (?, ?)",
            ("the foo bar OR not", "mZ"),
        )
        fcon.commit()
        # None of these should raise.
        fts.search_term(fcon, "foo-bar")
        fts.search_term(fcon, "OR")
        fts.search_term(fcon, '"weird"')
    finally:
        fcon.close()


# -- Fallback when FTS5 unavailable --


def test_has_fts5_fallback_path_is_exercisable(monkeypatch):
    """Simulate FTS5 being unavailable; confirm has_fts5() can return False.

    We can't actually unload FTS5 from the bundled SQLite, but we can monkey-
    patch the probe to verify the fallback wiring stays sane.
    """
    monkeypatch.setattr(fts, "has_fts5", lambda: False)
    # When callers use this signal to set fts_con=None, search.search must
    # still work end-to-end via LIKE.
    src_path = build_search_fixture()
    try:
        src = db.open_ro(src_path)
        try:
            results = search.search(src, ["cat"], fts_con=None)
            assert sorted(r["session_id"] for r in results) == ["sA", "sB", "sE"]
        finally:
            src.close()
    finally:
        try:
            os.remove(src_path)
        except OSError:
            pass


# -- _fts5_phrase --


def test_fts5_phrase_escapes_quotes():
    assert fts._fts5_phrase('he said "hi"') == '"he said ""hi"""'


def test_fts5_phrase_plain_term():
    assert fts._fts5_phrase("metabuilder") == '"metabuilder"'
