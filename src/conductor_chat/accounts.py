"""Resolve which Claude Code account directory holds each session's resumable JSONL.

Claude Code stores per-account, per-session resume state at:

    ~/.claude-account<N>/projects/<encoded-workspace-path>/<claude_session_id>.jsonl

The default account uses ``~/.claude/`` (no suffix). The operator may have several
account directories. When Conductor switches active accounts, sessions whose
resumable state lives under a different account cannot be resumed by Claude
Code ("No conversation found with session ID: <id>"). The chat content itself
stays intact in ``conductor.db``.

This module:

  1. Discovers all ``~/.claude*`` directories that look like Claude Code config
     dirs (heuristic: have a ``projects/`` subdir).
  2. Walks every such dir ONCE at server startup, building an in-memory index
     ``{session_id: account_name}``.
  3. Provides a small lookup API the rest of the codebase uses to decorate
     session rows.

Notes:

  - We scan once at startup. The operator restarts the server when they switch
    accounts (the prompt asked for refresh-on-startup-only).
  - When the same session id appears in multiple accounts (unlikely but
    possible), the FIRST encountered (sorted by account dir path) wins.
  - Paths/files that are not directories (`.claude.json`, `.claude-peers.db`)
    or directories missing the `projects/` subdir (`.claude-account-backups`,
    `.claude-account4`) are skipped.
"""

from __future__ import annotations

import glob
import os
from typing import Iterable

DEFAULT_HOME: str = os.path.expanduser("~")

# Sentinel passed via the HTTP API to filter rows whose account is None.
ORPHANED_SENTINEL: str = "__orphaned__"


def discover_account_dirs(home: str = DEFAULT_HOME) -> list[str]:
    """Return absolute paths of every ``~/.claude*`` dir that has a ``projects/`` subdir.

    Sorted alphabetically for deterministic ordering. Directories without a
    ``projects/`` subdir (e.g. ``~/.claude-account-backups``) are skipped.
    Plain files matching the glob (e.g. ``~/.claude.json``) are skipped.
    """
    candidates = sorted(glob.glob(os.path.join(home, ".claude*")))
    out: list[str] = []
    for c in candidates:
        if not os.path.isdir(c):
            continue
        if not os.path.isdir(os.path.join(c, "projects")):
            continue
        out.append(c)
    return out


def account_name(account_dir: str) -> str:
    """Map an account directory path to a short display name.

    Examples:
        ``~/.claude`` -> ``"default"``
        ``~/.claude-account1`` -> ``"account1"``
        ``~/.claude-account-backups`` -> ``"account-backups"``

    The display name is what the UI shows in the row badge.
    """
    base = os.path.basename(account_dir.rstrip("/"))
    if base == ".claude":
        return "default"
    if base.startswith(".claude-"):
        return base[len(".claude-"):]
    if base.startswith(".claude"):
        return base[len(".claude"):]
    return base


def _iter_session_ids(account_dir: str) -> Iterable[str]:
    """Yield session ids (jsonl filename stems) for one account directory.

    Uses ``os.scandir`` recursively (one level deep into ``projects/<workspace>/``)
    rather than ``Path.glob`` for speed on large trees.
    """
    projects = os.path.join(account_dir, "projects")
    try:
        ws_entries = list(os.scandir(projects))
    except OSError:
        return
    for ws_entry in ws_entries:
        if not ws_entry.is_dir(follow_symlinks=False):
            continue
        try:
            session_entries = os.scandir(ws_entry.path)
        except OSError:
            continue
        with session_entries as it:
            for entry in it:
                if not entry.is_file(follow_symlinks=False):
                    continue
                name = entry.name
                if not name.endswith(".jsonl"):
                    continue
                yield name[: -len(".jsonl")]


def build_index(home: str = DEFAULT_HOME) -> dict[str, str]:
    """Scan all account dirs once; return ``{session_id: account_name}``.

    If a session id shows up in more than one account, the alphabetically first
    account (per ``discover_account_dirs`` ordering) wins. Subsequent
    duplicates are silently ignored — logging every dup would be noisy.
    """
    index: dict[str, str] = {}
    for adir in discover_account_dirs(home):
        name = account_name(adir)
        for sid in _iter_session_ids(adir):
            if sid in index:
                continue
            index[sid] = name
    return index


def account_for(index: dict[str, str] | None, session_id: str) -> str | None:
    """O(1) lookup. Returns the account name or None (orphaned / no index)."""
    if not index:
        return None
    return index.get(session_id)


def decorate_rows(
    rows: list[dict],
    index: dict[str, str] | None,
) -> list[dict]:
    """In-place mutation: set ``row['account']`` on each row using ``index``.

    Returns ``rows`` for chaining convenience.
    """
    for r in rows:
        sid = r.get("session_id") or r.get("id")
        r["account"] = account_for(index, sid) if sid else None
    return rows


def filter_by_account(
    rows: list[dict], account_filter: str | None
) -> list[dict]:
    """Apply the ``?account=`` filter to a list of decorated rows.

    - ``account_filter`` None or empty -> no filter.
    - ``ORPHANED_SENTINEL`` -> only rows whose account is None.
    - Any other string -> only rows whose account equals that string.
    """
    if not account_filter:
        return rows
    if account_filter == ORPHANED_SENTINEL:
        return [r for r in rows if r.get("account") is None]
    return [r for r in rows if r.get("account") == account_filter]
