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

Terms come in two flavours:

  - **AND terms** (``exact=False``) — current default behaviour. Each term
    is one word/token; multiple terms are AND-joined.
  - **Exact-phrase terms** (``exact=True``) — the value is a full phrase,
    matched as a contiguous substring. The snippet wraps the WHOLE phrase
    in ``«…»`` (a side-effect of ``_make_snippet`` using ``len(term)``).

Each result row carries ``match_kind``: ``"exact"`` if at least one term in
the query was an exact-phrase match, else ``"and"``. Exact rows are sorted
above AND rows in the returned list.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from typing import Any

from . import db, fts

SNIPPET_RADIUS = 80


def _normalise_terms(terms: list[Any]) -> list[dict[str, Any]]:
    """Coerce mixed ``list[str] | list[dict]`` into the canonical structured form.

    A bare string is treated as an AND term (``exact=False``). A dict is
    expected to carry ``value`` (str) and ``exact`` (bool). Empty / whitespace
    values are stripped.
    """
    out: list[dict[str, Any]] = []
    for t in terms:
        if isinstance(t, str):
            value = t.strip()
            if not value:
                continue
            out.append({"value": value, "exact": False})
        elif isinstance(t, dict):
            value = str(t.get("value") or "").strip()
            if not value:
                continue
            out.append({"value": value, "exact": bool(t.get("exact", False))})
        # silently ignore unknown shapes
    return out


def search(
    con: sqlite3.Connection,
    terms: list[Any],
    workspace_id: str | None = None,
    limit: int = 500,
    fts_con: sqlite3.Connection | None = None,
    account_index: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """AND-search across user+assistant text content.

    Returns a list of session dicts (same fields as ``db.list_sessions``) with
    extra ``snippets``, ``match_kind``, and ``account`` keys.

    ``snippets`` is a list of {term, text, message_id, role, sent_at, exact}
    items — one per term per session. The matched substring in ``text`` is
    wrapped in ``«...»`` for the UI to highlight.

    Semantics:
      - Empty terms list -> identical to ``db.list_sessions`` (no filtering).
      - All terms must match somewhere in the session (AND).
      - Term match is case-insensitive substring (LIKE fallback) or FTS5
        phrase (when ``fts_con`` is provided). Multi-word exact-phrase terms
        rely on FTS5's native phrase-query syntax.
      - Match must be inside a content item with ``type=="text"``; tool inputs,
        tool results, and thinking blocks do NOT count.
      - If workspace_id is set, restrict to that workspace.
      - ``fts_con``: an open connection to the sidecar FTS index. When provided,
        pass-1 uses FTS5 MATCH instead of LIKE. None means LIKE fallback.
      - ``account_index``: dict mapping ``session_id -> account_name``. When
        provided, each returned row's ``account`` field is populated.

    Result ordering:
      1. ``match_kind == "exact"`` rows first, then ``"and"`` rows.
      2. Within each group, ``updated_at DESC`` (list_sessions order).
    """
    structured = _normalise_terms(terms)

    if not structured:
        sessions = db.list_sessions(con, limit=limit, account_index=account_index)
        if workspace_id:
            sessions = [s for s in sessions if s["workspace_id"] == workspace_id]
        for s in sessions:
            s["snippets"] = []
            s["match_kind"] = "and"
        return sessions

    # Pass 1: for each term, find the candidate session_ids that contain it.
    # When using FTS we also collect the hit message_ids so pass 2 can fetch
    # only those messages instead of every message in every candidate session.
    candidate_sets: list[set[str]] = []
    # Per-term mapping: session_id -> set of message_ids that FTS5 says hit
    # this term inside that session. None means "FTS not used; pass 2 must
    # scan all messages in the session" (LIKE fallback path).
    per_term_msg_hits: list[dict[str, set[str]] | None] = []

    for term in structured:
        sids, hit_map = _candidates_for_term(
            con, term["value"], workspace_id, fts_con=fts_con
        )
        candidate_sets.append(sids)
        per_term_msg_hits.append(hit_map)

    candidate_ids: set[str] = (
        set.intersection(*candidate_sets) if candidate_sets else set()
    )
    if not candidate_ids:
        return []

    # Pass 2: for each candidate session, fetch the relevant messages and
    # confirm each term appears in at least one text block. Build snippets
    # while we're at it. When FTS provided hit message_ids we fetch only
    # those (typically a handful); otherwise we fall back to fetching all
    # messages in the session (LIKE path).
    # When the user has typed a query, surface hidden sessions too — those
    # are exactly the chats they've lost track of and are likely hunting for.
    # The UI badges hidden rows so the user knows.
    all_sessions = db.list_sessions(
        con,
        limit=max(limit, len(candidate_ids)),
        account_index=account_index,
        include_hidden=True,
    )
    session_index = {s["session_id"]: s for s in all_sessions}

    has_any_exact = any(t["exact"] for t in structured)

    results: list[dict[str, Any]] = []
    for sid in candidate_ids:
        if sid not in session_index:
            continue
        if workspace_id and session_index[sid]["workspace_id"] != workspace_id:
            continue

        # Collect the message_ids we need to fetch for this session: the union
        # of FTS hits across all terms. If any term had no FTS map (LIKE
        # fallback), we fetch all messages in the session.
        msg_ids_needed: set[str] | None = set()
        for hit_map in per_term_msg_hits:
            if hit_map is None:
                msg_ids_needed = None
                break
            msg_ids_needed.update(hit_map.get(sid, set()))

        if msg_ids_needed is None:
            messages = db.get_session_messages(con, sid)
        else:
            messages = db.get_messages_by_ids(con, sid, list(msg_ids_needed))

        confirmed_snippets: list[dict[str, Any]] = []
        # Per-message confirmation map: msg_id -> set(term_value) so we can
        # detect "all words appear inside the SAME message" co-occurrence.
        per_msg_term_hits: dict[str, set[str]] = {}
        all_terms_confirmed = True
        for term in structured:
            term_snippet: dict[str, Any] | None = None
            for msg in messages:
                ok, snippet = _confirm_term_in_text_blocks(msg, term["value"])
                if ok:
                    snippet["exact"] = term["exact"]
                    if term_snippet is None:
                        term_snippet = snippet
                    mid = msg.get("id") or ""
                    if mid:
                        per_msg_term_hits.setdefault(mid, set()).add(term["value"])
            if term_snippet is None:
                all_terms_confirmed = False
                break
            confirmed_snippets.append(term_snippet)
        if all_terms_confirmed:
            row = dict(session_index[sid])
            row["snippets"] = confirmed_snippets
            row["match_kind"] = "exact" if has_any_exact else "and"
            # Co-occurrence flag: every term landed in at least one common
            # message. Used to rank above scattered-across-messages hits.
            term_values_needed = {t["value"] for t in structured}
            row["all_in_one_message"] = any(
                term_values_needed.issubset(hits)
                for hits in per_msg_term_hits.values()
            )
            results.append(row)

    # Preserve list_sessions ordering (updated_at DESC) within each group;
    # ordering keys (lower sorts first):
    #   1. match_kind == "exact" (phrase hit) — top
    #   2. AND-of-words with every term in a single message — next
    #   3. AND-of-words scattered across messages — last
    # Within each group: updated_at DESC.
    sid_to_order = {s["session_id"]: i for i, s in enumerate(all_sessions)}
    results.sort(
        key=lambda r: (
            0 if r["match_kind"] == "exact"
            else (1 if r.get("all_in_one_message") else 2),
            sid_to_order.get(r["session_id"], 1_000_000),
        )
    )
    return results[:limit]


# -- internal helpers --


def _candidates_for_term(
    con: sqlite3.Connection,
    term: str,
    workspace_id: str | None,
    fts_con: sqlite3.Connection | None,
) -> tuple[set[str], dict[str, set[str]] | None]:
    """Return ``(session_ids, msg_hit_map)`` for one term.

    - ``session_ids``: set of session_ids whose any message contains the term.
    - ``msg_hit_map``: when FTS is used, ``{session_id -> {message_id, ...}}``
      so pass 2 can fetch only the messages that actually matched. ``None``
      means the caller fell back to LIKE and pass 2 must scan every message
      in each candidate session.
    """
    if fts_con is not None:
        return _candidates_via_fts(con, fts_con, term, workspace_id)
    return (_candidates_via_like(con, term, workspace_id), None)


def _candidates_via_fts(
    con: sqlite3.Connection,
    fts_con: sqlite3.Connection,
    term: str,
    workspace_id: str | None,
) -> tuple[set[str], dict[str, set[str]]]:
    """Pass-1 via FTS5 MATCH. Returns ``(session_ids, msg_hit_map)`` where
    ``msg_hit_map[session_id]`` is the set of message_ids that matched.

    The workspace filter is applied at the SQL join layer so we never include
    sessions outside the active workspace.
    """
    hit_message_ids = fts.search_term(fts_con, term)
    if not hit_message_ids:
        return (set(), {})
    # Resolve message_id -> session_id from conductor.db. Chunked IN to stay
    # well under SQLite's ~999-param default ceiling.
    session_ids: set[str] = set()
    msg_hit_map: dict[str, set[str]] = {}
    ids = list(hit_message_ids)
    CHUNK = 800
    # Search includes hidden sessions on purpose — when the user has typed
    # a query, hidden chats are exactly what they're hunting for. The UI
    # badges hidden rows so the user knows.
    if workspace_id:
        sql = (
            "SELECT m.session_id, m.id "
            "FROM session_messages m "
            "JOIN sessions s ON s.id = m.session_id "
            "WHERE s.workspace_id = ? "
            "  AND m.id IN ({placeholders})"
        )
    else:
        sql = (
            "SELECT m.session_id, m.id "
            "FROM session_messages m "
            "JOIN sessions s ON s.id = m.session_id "
            "WHERE m.id IN ({placeholders})"
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
        for row in rows:
            sid = row[0]
            mid = row[1]
            session_ids.add(sid)
            msg_hit_map.setdefault(sid, set()).add(mid)
    return (session_ids, msg_hit_map)


def _candidates_via_like(
    con: sqlite3.Connection, term: str, workspace_id: str | None
) -> set[str]:
    """SQL LIKE pre-filter. Returns session_ids whose any message's raw content contains term.

    Case-insensitive via SQLite's NOCASE collation on the LIKE comparison.
    Slow on large corpora (full table scan, no index use) — kept as fallback
    when FTS5 isn't available.
    """
    pattern = f"%{_escape_like(term)}%"
    # Search includes hidden sessions on purpose (see _candidates_via_fts).
    if workspace_id:
        rows = con.execute(
            """
            SELECT DISTINCT m.session_id
            FROM session_messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE s.workspace_id = ?
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
            WHERE m.content LIKE ? ESCAPE '\\' COLLATE NOCASE
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
