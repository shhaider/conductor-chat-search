"""Two-pass term search over Conductor message text.

Pass 1: FTS5 MATCH (or SQL LIKE fallback) against an index of just the
``type=="text"`` fragments of session_messages. Returns candidate session_ids
cheaply.

Pass 2: Python parses each candidate message's JSON ``content`` and confirms
the term appears inside a ``type=="text"`` block — i.e. user prose or
assistant prose, not tool inputs, tool results, or thinking blocks. This is
kept as defense-in-depth: FTS5 tokenisation can disagree with substring match
at hyphens, dots, and other punctuation, and we want the on-screen snippet
to actually contain the term.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from typing import Any

from . import db, fts

SNIPPET_RADIUS = 80


def search(
    con: sqlite3.Connection,
    terms: list[str],
    workspace_id: str | None = None,
    limit: int = 500,
    fts_con: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """AND-search across user+assistant text content.

    Returns a list of session dicts (same fields as ``db.list_sessions``) with an
    extra ``snippets`` key: a list of {term, text, message_id, role, sent_at}
    items — one snippet per matched term per session. The matched substring in
    ``text`` is wrapped in ``«...»`` for the UI to highlight.

    Semantics:
      - Empty terms list -> identical to ``db.list_sessions`` (no filtering).
      - All terms must match somewhere in the session (AND).
      - Term match is case-insensitive substring (LIKE fallback) or FTS5
        tokenised phrase (when ``fts_con`` is provided).
      - Match must be inside a content item with ``type=="text"``; tool inputs,
        tool results, and thinking blocks do NOT count.
      - If workspace_id is set, restrict to that workspace.
      - ``fts_con``: an open connection to the sidecar FTS index. When provided,
        pass-1 uses FTS5 MATCH instead of LIKE. None means LIKE fallback.
    """
    normalised_terms = [t for t in (s.strip() for s in terms) if t]

    if not normalised_terms:
        sessions = db.list_sessions(con, limit=limit)
        if workspace_id:
            sessions = [s for s in sessions if s["workspace_id"] == workspace_id]
        for s in sessions:
            s["snippets"] = []
        return sessions

    # Pass 1: candidate session_ids that contain each term.
    candidate_sets: list[set[str]] = []
    for term in normalised_terms:
        candidate_sets.append(
            _candidates_for_term(con, term, workspace_id, fts_con=fts_con)
        )

    candidate_ids: set[str] = (
        set.intersection(*candidate_sets) if candidate_sets else set()
    )
    if not candidate_ids:
        return []

    # Pass 2: for each candidate session, fetch messages and confirm each term
    # appears in at least one text block. Build snippets while we're at it.
    all_sessions = db.list_sessions(con, limit=max(limit, len(candidate_ids)))
    session_index = {s["session_id"]: s for s in all_sessions}

    results: list[dict[str, Any]] = []
    for sid in candidate_ids:
        if sid not in session_index:
            continue
        if workspace_id and session_index[sid]["workspace_id"] != workspace_id:
            continue
        messages = db.get_session_messages(con, sid)
        confirmed_snippets: list[dict[str, Any]] = []
        all_terms_confirmed = True
        for term in normalised_terms:
            term_snippet: dict[str, Any] | None = None
            for msg in messages:
                ok, snippet = _confirm_term_in_text_blocks(msg, term)
                if ok:
                    term_snippet = snippet
                    break
            if term_snippet is None:
                all_terms_confirmed = False
                break
            confirmed_snippets.append(term_snippet)
        if all_terms_confirmed:
            row = dict(session_index[sid])
            row["snippets"] = confirmed_snippets
            results.append(row)

    # Preserve list_sessions ordering (updated_at DESC).
    sid_to_order = {s["session_id"]: i for i, s in enumerate(all_sessions)}
    results.sort(key=lambda r: sid_to_order.get(r["session_id"], 1_000_000))
    return results[:limit]


# -- internal helpers --


def _candidates_for_term(
    con: sqlite3.Connection,
    term: str,
    workspace_id: str | None,
    fts_con: sqlite3.Connection | None,
) -> set[str]:
    """Return session_ids whose any message contains the term, via FTS5 if
    available, else SQL LIKE."""
    if fts_con is not None:
        return _candidates_via_fts(con, fts_con, term, workspace_id)
    return _candidates_via_like(con, term, workspace_id)


def _candidates_via_fts(
    con: sqlite3.Connection,
    fts_con: sqlite3.Connection,
    term: str,
    workspace_id: str | None,
) -> set[str]:
    """Pass-1 via FTS5 MATCH. Returns session_ids whose any message_id is in
    the FTS hit set AND (optionally) belongs to the given workspace."""
    hit_message_ids = fts.search_term(fts_con, term)
    if not hit_message_ids:
        return set()
    # Resolve message_id -> session_id from conductor.db. Chunked IN to stay
    # well under SQLite's ~999-param default ceiling.
    session_ids: set[str] = set()
    ids = list(hit_message_ids)
    CHUNK = 800
    if workspace_id:
        sql = (
            "SELECT DISTINCT m.session_id "
            "FROM session_messages m "
            "JOIN sessions s ON s.id = m.session_id "
            "WHERE s.is_hidden = 0 "
            "  AND s.workspace_id = ? "
            "  AND m.id IN ({placeholders})"
        )
    else:
        sql = (
            "SELECT DISTINCT m.session_id "
            "FROM session_messages m "
            "JOIN sessions s ON s.id = m.session_id "
            "WHERE s.is_hidden = 0 "
            "  AND m.id IN ({placeholders})"
        )
    for i in range(0, len(ids), CHUNK):
        chunk = ids[i:i + CHUNK]
        placeholders = ",".join("?" * len(chunk))
        params: list[Any] = []
        if workspace_id:
            params.append(workspace_id)
        params.extend(chunk)
        rows = con.execute(
            sql.format(placeholders=placeholders), params
        ).fetchall()
        session_ids.update(r[0] for r in rows)
    return session_ids


def _candidates_via_like(
    con: sqlite3.Connection, term: str, workspace_id: str | None
) -> set[str]:
    """SQL LIKE pre-filter. Returns session_ids whose any message's raw content contains term.

    Case-insensitive via SQLite's NOCASE collation on the LIKE comparison.
    Slow on large corpora (full table scan, no index use) — kept as fallback
    when FTS5 isn't available.
    """
    pattern = f"%{_escape_like(term)}%"
    if workspace_id:
        rows = con.execute(
            """
            SELECT DISTINCT m.session_id
            FROM session_messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE s.is_hidden = 0
              AND s.workspace_id = ?
              AND m.content LIKE ? ESCAPE '\\' COLLATE NOCASE
            """,
            (workspace_id, pattern),
        ).fetchall()
    else:
        rows = con.execute(
            """
            SELECT DISTINCT m.session_id
            FROM session_messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE s.is_hidden = 0
              AND m.content LIKE ? ESCAPE '\\' COLLATE NOCASE
            """,
            (pattern,),
        ).fetchall()
    return {r[0] for r in rows}


def _confirm_term_in_text_blocks(
    message_row: dict[str, Any], term: str
) -> tuple[bool, dict[str, Any] | None]:
    """Parse content JSON, walk content[i] looking for type==text containing term.

    Returns (True, snippet_dict) on first match, otherwise (False, None).
    """
    raw = message_row.get("content") or ""
    try:
        payload = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return (False, None)
    if not isinstance(payload, dict):
        return (False, None)
    message_obj = payload.get("message") or {}
    if not isinstance(message_obj, dict):
        return (False, None)
    role = message_obj.get("role") or message_row.get("role") or "?"
    content = message_obj.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return (False, None)

    needle = term.lower()
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "text":
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        if needle in text.lower():
            return (True, _make_snippet(message_row, role, text, term))
    return (False, None)


def _make_snippet(
    message_row: dict[str, Any], role: str, text: str, term: str
) -> dict[str, Any]:
    """Build the snippet dict with surrounding context, wrapping the match in «...»."""
    lower = text.lower()
    needle = term.lower()
    pos = lower.find(needle)
    if pos < 0:
        snippet_text = text[: SNIPPET_RADIUS * 2]
    else:
        start = max(0, pos - SNIPPET_RADIUS)
        end = min(len(text), pos + len(term) + SNIPPET_RADIUS)
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(text) else ""
        matched = text[pos:pos + len(term)]
        snippet_text = (
            prefix
            + text[start:pos]
            + "«" + matched + "»"
            + text[pos + len(term):end]
            + suffix
        )
    return {
        "term": term,
        "text": snippet_text,
        "message_id": message_row.get("id"),
        "role": role,
        "sent_at": message_row.get("sent_at") or message_row.get("created_at"),
    }


def _escape_like(term: str) -> str:
    """Escape SQL LIKE wildcards in a user-supplied term."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
