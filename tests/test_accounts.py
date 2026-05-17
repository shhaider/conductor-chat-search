"""Unit tests for conductor_chat.accounts — multi-account JSONL discovery."""

from __future__ import annotations

import os

import pytest

from conductor_chat import accounts


def _touch(path: str, content: str = "") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


@pytest.fixture()
def fake_home(tmp_path):
    """Build a fake $HOME with several Claude-shaped paths.

    Layout mimics the real operator's environment:
      ~/.claude/projects/<encoded>/aaa.jsonl, bbb.jsonl   (default account)
      ~/.claude-account1/projects/<encoded>/ccc.jsonl     (account1)
      ~/.claude-account-backups/refresh.sh                (no projects/ → skip)
      ~/.claude-account4/settings.json                    (no projects/ → skip)
      ~/.claude.json                                      (plain file → skip)
      ~/.claude-peers.db                                  (plain file → skip)
    """
    home = tmp_path
    # default account
    _touch(str(home / ".claude" / "projects" / "encoded-ws-A" / "aaa.jsonl"))
    _touch(str(home / ".claude" / "projects" / "encoded-ws-A" / "bbb.jsonl"))
    _touch(str(home / ".claude" / "projects" / "encoded-ws-B" / "shared.jsonl"))
    # account1
    _touch(str(home / ".claude-account1" / "projects" / "encoded-ws-A" / "ccc.jsonl"))
    # account-backups: no projects/
    _touch(str(home / ".claude-account-backups" / "refresh.sh"), "#!/bin/sh\n")
    # account4: no projects/
    _touch(str(home / ".claude-account4" / "settings.json"), "{}")
    # plain files at the home root
    _touch(str(home / ".claude.json"), "{}")
    _touch(str(home / ".claude-peers.db"), "data")
    return str(home)


def test_discover_account_dirs_skips_nondirs_and_no_projects(fake_home):
    dirs = accounts.discover_account_dirs(fake_home)
    bases = sorted(os.path.basename(d) for d in dirs)
    assert bases == [".claude", ".claude-account1"]


def test_discover_account_dirs_empty_home(tmp_path):
    """No .claude* paths -> empty list, no error."""
    assert accounts.discover_account_dirs(str(tmp_path)) == []


def test_account_name_default():
    assert accounts.account_name("/Users/me/.claude") == "default"


def test_account_name_numbered():
    assert accounts.account_name("/Users/me/.claude-account1") == "account1"
    assert accounts.account_name("/Users/me/.claude-account2") == "account2"


def test_account_name_named():
    assert accounts.account_name("/Users/me/.claude-account-backups") == "account-backups"


def test_account_name_trailing_slash():
    assert accounts.account_name("/Users/me/.claude/") == "default"


def test_build_index_maps_session_to_account(fake_home):
    index = accounts.build_index(fake_home)
    assert index["aaa"] == "default"
    assert index["bbb"] == "default"
    assert index["shared"] == "default"
    assert index["ccc"] == "account1"
    # No phantoms.
    assert len(index) == 4


def test_build_index_alphabetical_wins_on_duplicate(tmp_path):
    """If the same session id appears in multiple accounts, the alphabetically
    first account dir wins (deterministic precedence)."""
    home = tmp_path
    _touch(str(home / ".claude" / "projects" / "ws" / "dup.jsonl"))
    _touch(str(home / ".claude-account1" / "projects" / "ws" / "dup.jsonl"))
    index = accounts.build_index(str(home))
    # ".claude" sorts before ".claude-account1"
    assert index["dup"] == "default"


def test_account_for_known_and_unknown():
    index = {"sess-123": "account2"}
    assert accounts.account_for(index, "sess-123") == "account2"
    assert accounts.account_for(index, "nonexistent") is None
    assert accounts.account_for(None, "anything") is None
    assert accounts.account_for({}, "anything") is None


def test_decorate_rows():
    index = {"s1": "default", "s2": "account1"}
    rows = [
        {"session_id": "s1", "title": "first"},
        {"session_id": "s2", "title": "second"},
        {"session_id": "s3", "title": "orphan"},
    ]
    out = accounts.decorate_rows(rows, index)
    assert out[0]["account"] == "default"
    assert out[1]["account"] == "account1"
    assert out[2]["account"] is None


def test_decorate_rows_no_index():
    rows = [{"session_id": "s1"}]
    out = accounts.decorate_rows(rows, None)
    assert out[0]["account"] is None


def test_filter_by_account_specific():
    rows = [
        {"session_id": "s1", "account": "default"},
        {"session_id": "s2", "account": "account1"},
        {"session_id": "s3", "account": None},
    ]
    out = accounts.filter_by_account(rows, "default")
    assert [r["session_id"] for r in out] == ["s1"]


def test_filter_by_account_orphaned():
    rows = [
        {"session_id": "s1", "account": "default"},
        {"session_id": "s2", "account": None},
    ]
    out = accounts.filter_by_account(rows, accounts.ORPHANED_SENTINEL)
    assert [r["session_id"] for r in out] == ["s2"]


def test_filter_by_account_empty_passthrough():
    rows = [{"session_id": "s1", "account": "default"}]
    assert accounts.filter_by_account(rows, None) is rows
    assert accounts.filter_by_account(rows, "") is rows


def test_build_index_real_home_returns_dict():
    """Smoke test against the actual operator home dir. Just confirms shape;
    contents depend on the running environment."""
    out = accounts.build_index()
    assert isinstance(out, dict)
    for k, v in out.items():
        assert isinstance(k, str)
        assert isinstance(v, str)
