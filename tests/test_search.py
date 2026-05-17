"""Unit tests for conductor_chat.search — two-pass filter, AND semantics, text-only matching."""

import os

import pytest

from conductor_chat import db, search
from tests.fixtures.build_fixture import build_search_fixture


@pytest.fixture()
def search_db():
    path = build_search_fixture()
    yield path
    try:
        os.remove(path)
    except OSError:
        pass


def _ids(results):
    return sorted(r["session_id"] for r in results)


def test_empty_terms_returns_all(search_db):
    con = db.open_ro(search_db)
    try:
        results = search.search(con, [])
        assert _ids(results) == ["sA", "sB", "sC", "sD"]
    finally:
        con.close()


def test_cat_returns_a_and_b_not_c(search_db):
    """sA has 'cat' in an assistant text block; sB in two text blocks; sC only in a tool_result."""
    con = db.open_ro(search_db)
    try:
        results = search.search(con, ["cat"])
        assert _ids(results) == ["sA", "sB"]
    finally:
        con.close()


def test_and_semantics_cat_and_happy(search_db):
    """Only sB has both 'cat' and 'happy' in text blocks."""
    con = db.open_ro(search_db)
    try:
        results = search.search(con, ["cat", "happy"])
        assert _ids(results) == ["sB"]
    finally:
        con.close()


def test_mat_returns_only_a(search_db):
    con = db.open_ro(search_db)
    try:
        results = search.search(con, ["mat"])
        assert _ids(results) == ["sA"]
    finally:
        con.close()


def test_no_match_returns_empty(search_db):
    con = db.open_ro(search_db)
    try:
        assert search.search(con, ["fish"]) == []
    finally:
        con.close()


def test_snippet_wraps_match(search_db):
    con = db.open_ro(search_db)
    try:
        results = search.search(con, ["cat"])
        snippets_by_session = {r["session_id"]: r["snippets"] for r in results}
        # sA's first text match is "the cat sat on the mat"
        a_snip = snippets_by_session["sA"][0]
        assert "«cat»" in a_snip["text"]
        assert a_snip["term"] == "cat"
        assert a_snip["role"] in ("assistant", "user")
    finally:
        con.close()


def test_case_insensitive(search_db):
    con = db.open_ro(search_db)
    try:
        results = search.search(con, ["CAT"])
        assert _ids(results) == ["sA", "sB"]
    finally:
        con.close()


def test_workspace_filter(search_db):
    """sA, sB, sD are in ws-x; sC is in ws-y. Searching for 'cat' in ws-x returns A and B only."""
    con = db.open_ro(search_db)
    try:
        results = search.search(con, ["cat"], workspace_id="ws-x")
        assert _ids(results) == ["sA", "sB"]
        # sanity: no results in ws-y for cat (sC's cat is in a tool_result, not text)
        results_y = search.search(con, ["cat"], workspace_id="ws-y")
        assert _ids(results_y) == []
    finally:
        con.close()


def test_thinking_block_not_searched(search_db):
    """sD has 'interesting cat fact' in a thinking block; 'interesting' must not match it."""
    con = db.open_ro(search_db)
    try:
        results = search.search(con, ["interesting"])
        assert _ids(results) == []
    finally:
        con.close()
