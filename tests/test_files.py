"""Tests for conductor_chat.files — name+content search, copy, reveal.

These tests build a small fixture tree under ``tmp_path`` with a handful of
files (some hidden, some not) and run the real ``find`` / ``rg`` against
it. We narrow the search ``scope`` to the fixture root so the tests are
fast and don't depend on what's on the developer's filesystem.

``reveal_in_finder`` is mocked out (we don't want every test run to pop
Finder windows).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from conductor_chat import files as F


# ---------- fixture tree ----------


@pytest.fixture()
def tree(tmp_path):
    """Build a fixture tree under ``tmp_path`` and return its root.

    ::

        root/
          visible.md           — "the quick brown fox migration"
          alpha.txt            — "hello world\\nthe quick brown fox\\n"
          .hidden_file.md      — "secret stash content"
          notes/
            inner.md           — "deeply nested marker"
          .hidden_dir/
            inside_hidden.txt  — "hidden parent marker"
    """
    root = tmp_path / "tree"
    root.mkdir()
    (root / "visible.md").write_text("the quick brown fox migration\n", encoding="utf-8")
    (root / "alpha.txt").write_text("hello world\nthe quick brown fox\n", encoding="utf-8")
    (root / ".hidden_file.md").write_text("secret stash content\n", encoding="utf-8")
    (root / "notes").mkdir()
    (root / "notes" / "inner.md").write_text("deeply nested marker\n", encoding="utf-8")
    (root / ".hidden_dir").mkdir()
    (root / ".hidden_dir" / "inside_hidden.txt").write_text(
        "hidden parent marker\n", encoding="utf-8"
    )
    return root


# ---------- find_by_name ----------


def test_find_by_name_finds_visible_file(tree):
    rows = F.find_by_name("visible", scope=str(tree))
    paths = [r["path"] for r in rows]
    assert any(p.endswith("/visible.md") for p in paths)


def test_find_by_name_uses_substring_when_no_glob(tree):
    """A bare word should match anywhere in the basename."""
    rows = F.find_by_name("alpha", scope=str(tree))
    names = [r["basename"] for r in rows]
    assert "alpha.txt" in names


def test_find_by_name_glob_pattern(tree):
    rows = F.find_by_name("*.md", scope=str(tree))
    names = sorted(r["basename"] for r in rows)
    # visible.md, inner.md, and .hidden_file.md if include_hidden=True (default)
    assert "visible.md" in names
    assert "inner.md" in names
    assert ".hidden_file.md" in names


def test_find_by_name_excludes_hidden_when_toggled_off(tree):
    rows = F.find_by_name("*.md", scope=str(tree), include_hidden=False)
    names = [r["basename"] for r in rows]
    assert ".hidden_file.md" not in names
    assert "visible.md" in names


def test_find_by_name_returns_expected_fields(tree):
    rows = F.find_by_name("visible", scope=str(tree))
    assert rows
    row = rows[0]
    for k in ("path", "basename", "parent_dir", "size_bytes", "mtime_iso", "kind"):
        assert k in row
    assert row["kind"] == "file"
    assert row["size_bytes"] > 0
    assert row["mtime_iso"].count("-") >= 2  # ISO 8601


def test_find_by_name_directory_kind(tree):
    rows = F.find_by_name("notes", scope=str(tree))
    kinds = {r["basename"]: r["kind"] for r in rows}
    assert kinds.get("notes") == "dir"


def test_find_by_name_respects_max_results(tree):
    # Create many files so the cap matters.
    bulk = tree / "bulk"
    bulk.mkdir()
    for i in range(20):
        (bulk / f"f{i:02d}.tmp").write_text("x", encoding="utf-8")
    rows = F.find_by_name("*.tmp", scope=str(tree), max_results=5)
    assert len(rows) == 5


def test_find_by_name_empty_pattern_returns_empty():
    assert F.find_by_name("", scope="/tmp") == []
    assert F.find_by_name("   ", scope="/tmp") == []


# ---------- find_by_content ----------


@pytest.mark.skipif(not F.ripgrep_available(), reason="ripgrep not installed")
def test_find_by_content_finds_phrase(tree):
    rows = F.find_by_content("brown fox migration", scope=str(tree))
    paths = [r["path"] for r in rows]
    assert any(p.endswith("/visible.md") for p in paths)


@pytest.mark.skipif(not F.ripgrep_available(), reason="ripgrep not installed")
def test_find_by_content_snippet_has_markers(tree):
    rows = F.find_by_content("brown fox", scope=str(tree))
    assert rows
    # At least one snippet must carry the «match» marker.
    assert any("«brown fox»" in r["match_text"] for r in rows)


@pytest.mark.skipif(not F.ripgrep_available(), reason="ripgrep not installed")
def test_find_by_content_returns_line_number(tree):
    rows = F.find_by_content("hello world", scope=str(tree))
    matched = [r for r in rows if r["path"].endswith("/alpha.txt")]
    assert matched
    assert matched[0]["line_number"] == 1


@pytest.mark.skipif(not F.ripgrep_available(), reason="ripgrep not installed")
def test_find_by_content_searches_hidden_by_default(tree):
    rows = F.find_by_content("secret stash", scope=str(tree))
    paths = [r["path"] for r in rows]
    assert any(p.endswith("/.hidden_file.md") for p in paths)


@pytest.mark.skipif(not F.ripgrep_available(), reason="ripgrep not installed")
def test_find_by_content_skips_hidden_when_off(tree):
    rows = F.find_by_content("secret stash", scope=str(tree), include_hidden=False)
    paths = [r["path"] for r in rows]
    assert not any(p.endswith("/.hidden_file.md") for p in paths)


@pytest.mark.skipif(not F.ripgrep_available(), reason="ripgrep not installed")
def test_find_by_content_empty_query_returns_empty():
    assert F.find_by_content("", scope="/tmp") == []


@pytest.mark.skipif(not F.ripgrep_available(), reason="ripgrep not installed")
def test_find_by_content_fixed_strings(tree):
    """``.`` is a regex metachar; --fixed-strings means we treat it literally.

    No file has a literal ``b.own`` substring, so this should return [].
    """
    rows = F.find_by_content("b.own fox", scope=str(tree))
    assert rows == []


# ---------- _build_snippet (pure unit) ----------


def test_build_snippet_marks_single_match():
    text = "the quick brown fox jumps over the lazy dog"
    spans = [{"start": 10, "end": 19, "match": {"text": "brown fox"}}]
    out = F._build_snippet(text, spans)
    assert "«brown fox»" in out


def test_build_snippet_handles_no_spans():
    out = F._build_snippet("hello", [])
    assert out == "hello"


def test_build_snippet_trims_long_line():
    text = "x" * 500 + "MATCH" + "y" * 500
    spans = [{"start": 500, "end": 505, "match": {"text": "MATCH"}}]
    out = F._build_snippet(text, spans)
    assert "«MATCH»" in out
    # Trimmed on at least one side.
    assert "…" in out
    # Should be much shorter than the original line.
    assert len(out) < 300


# ---------- copy_to_downloads ----------


def test_copy_to_downloads_writes_and_verifies(tmp_path):
    src = tmp_path / "input.txt"
    src.write_text("hello world\n", encoding="utf-8")
    out_dir = tmp_path / "Downloads"
    result = F.copy_to_downloads(str(src), out_dir=str(out_dir))
    assert result["verified"] is True
    assert Path(result["copied_to"]).exists()
    assert result["bytes"] == src.stat().st_size


def test_copy_to_downloads_collision_adds_suffix(tmp_path):
    src = tmp_path / "input.txt"
    src.write_text("v1\n", encoding="utf-8")
    out_dir = tmp_path / "Downloads"
    r1 = F.copy_to_downloads(str(src), out_dir=str(out_dir))
    r2 = F.copy_to_downloads(str(src), out_dir=str(out_dir))
    assert r1["copied_to"] != r2["copied_to"]
    assert Path(r2["copied_to"]).name.endswith("-001.txt")


def test_copy_to_downloads_rejects_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        F.copy_to_downloads(str(tmp_path / "nope.txt"), out_dir=str(tmp_path / "out"))


def test_copy_to_downloads_rejects_directory(tmp_path):
    d = tmp_path / "sub"
    d.mkdir()
    with pytest.raises(IsADirectoryError):
        F.copy_to_downloads(str(d), out_dir=str(tmp_path / "out"))


def test_copy_to_downloads_rejects_huge_file(tmp_path):
    src = tmp_path / "big.bin"
    src.write_bytes(b"x" * 200)
    with pytest.raises(ValueError):
        F.copy_to_downloads(str(src), out_dir=str(tmp_path / "out"), max_bytes=100)


# ---------- reveal_in_finder (mocked subprocess) ----------


def test_reveal_in_finder_invokes_open_dash_r(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    with mock.patch("conductor_chat.files.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=["open", "-R", str(target)], returncode=0, stdout="", stderr=""
        )
        result = F.reveal_in_finder(str(target))
    assert result == {"ok": True}
    run.assert_called_once()
    args, _ = run.call_args
    # First positional arg is the argv list.
    assert args[0] == ["open", "-R", str(target)]


def test_reveal_in_finder_rejects_missing_path(tmp_path):
    result = F.reveal_in_finder(str(tmp_path / "nope.txt"))
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_reveal_in_finder_surfaces_open_failure(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    with mock.patch("conductor_chat.files.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=["open", "-R", str(target)],
            returncode=1,
            stdout="",
            stderr="something went wrong",
        )
        result = F.reveal_in_finder(str(target))
    assert result["ok"] is False
    assert "something went wrong" in result["error"]


# ---------- validate_path_for_action ----------


def test_validate_path_accepts_home_subpath(tmp_path):
    # Need a real path inside HOME; create one.
    home = os.path.expanduser("~")
    test_path = os.path.join(home, ".cchat-test-marker")
    Path(test_path).write_text("x", encoding="utf-8")
    try:
        ok, err = F.validate_path_for_action(test_path)
        assert ok is True, err
    finally:
        try:
            os.remove(test_path)
        except OSError:
            pass


def test_validate_path_accepts_tmp(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("x", encoding="utf-8")
    ok, err = F.validate_path_for_action(str(target))
    assert ok is True, err


def test_validate_path_rejects_missing():
    ok, err = F.validate_path_for_action("/nonexistent/path/here.xyz")
    assert ok is False
    assert "not found" in err


def test_validate_path_rejects_outside_home():
    # /etc/hosts always exists on macOS and is outside HOME.
    ok, err = F.validate_path_for_action("/etc/hosts")
    assert ok is False
    assert "outside" in err.lower()


# ---------- ripgrep_available ----------


def test_ripgrep_available_returns_bool():
    """Sanity check: function returns a bool without raising."""
    assert isinstance(F.ripgrep_available(), bool)
