"""Sidecar FTS5 index over Conductor message text.

The production search bottleneck is ``LIKE '%term%'`` over 329k JSON-encoded
``session_messages`` rows — full table scan, no index use, 10-40s per query.
This module builds a SQLite FTS5 virtual table indexing JUST the ``type=="text"``
fragments of user+assistant content blocks, keyed by ``session_messages.id``.

Hard constraints:
  - The Conductor DB is read-only. The FTS index lives in a separate sidecar
    SQLite at ``~/Library/Application Support/conductor-chat-search/fts.db``.
    Nothing in this module ever opens conductor.db for write.
  - One-time backfill on first run (~30s on the operator's 329k-row corpus).
  - Incremental sync on each subsequent open: pull rows from conductor.db
    where ``rowid > MAX(indexed_rowid)`` and add them.
  - If SQLite was built without FTS5, the module exposes ``has_fts5()`` so
    callers can fall back to LIKE.

Sidecar schema:
  - ``fts_messages`` (FTS5 virtual): columns ``text``, ``message_id`` UNINDEXED.
  - ``fts_meta`` (KV): ``key`` PK, ``value``. Stores ``schema_version`` of the
    source conductor.db at last index, and ``max_rowid`` of session_messages
    successfully indexed so far.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable

DEFAULT_FTS_DIR: str = os.path.expanduser(
    "~/Library/Application Support/conductor-chat-search"
)
DEFAULT_FTS_PATH: str = os.path.join(DEFAULT_FTS_DIR, "fts.db")

PROGRESS_EVERY = 10_000


def has_fts5() -> bool:
    """Probe whether this Python's bundled SQLite supports FTS5.

    The only reliable check is to try creating a virtual table and see whether
    SQLite accepts it. The ephemeral connection is discarded.
    """
    try:
        probe = sqlite3.connect(":memory:")
        try:
            probe.execute("CREATE VIRTUAL TABLE fts5_probe USING fts5(x)")
            return True
        finally:
            probe.close()
    except sqlite3.OperationalError:
        return False
    except Exception:
        return False


def open_fts(fts_path: str = DEFAULT_FTS_PATH) -> sqlite3.Connection:
    """Open (and create if needed) the sidecar FTS database.

    Ensures the parent dir exists. Initialises the schema if the DB is empty.
    Returns a sqlite3 Connection. The caller closes it.
    """
    parent = os.path.dirname(fts_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(fts_path)
    con.row_factory = sqlite3.Row
    _init_schema(con)
    return con


def _init_schema(con: sqlite3.Connection) -> None:
    """Idempotent: create the FTS5 virtual table + meta table if absent."""
    con.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS fts_messages USING fts5(
            text,
            message_id UNINDEXED,
            tokenize = 'unicode61 remove_diacritics 2'
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS fts_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    con.commit()


def _meta_get(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM fts_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _meta_set(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO fts_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def extract_text_fragments(content_json: str | None) -> list[str]:
    """Parse one ``session_messages.content`` JSON blob; return the list of
    ``type=="text"`` fragments (user prose + assistant prose).

    Mirrors the rules in ``search._confirm_term_in_text_blocks``:
      - content may be a dict with ``message.content`` as list or string.
      - tool_use / tool_result / thinking blocks are NOT included.
      - malformed JSON / unexpected shapes yield an empty list (silently).
    """
    if not content_json:
        return []
    try:
        payload = json.loads(content_json)
    except (ValueError, TypeError):
        return []
    if not isinstance(payload, dict):
        return []
    message_obj = payload.get("message") or {}
    if not isinstance(message_obj, dict):
        return []
    content = message_obj.get("content")
    if isinstance(content, str):
        return [content] if content else []
    if not isinstance(content, list):
        return []
    fragments: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "text":
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            fragments.append(text)
    return fragments


def _max_indexed_rowid(con: sqlite3.Connection) -> int:
    """Return the highest source-DB rowid we've already indexed (0 if none)."""
    raw = _meta_get(con, "max_rowid")
    if raw is None:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def build_or_sync(
    fts_con: sqlite3.Connection,
    src_con: sqlite3.Connection,
    progress_stream=sys.stderr,
) -> dict[str, Any]:
    """Bring the FTS index up to date with conductor.db.

    First call (empty index) is the full backfill. Subsequent calls only pull
    rows where ``session_messages.rowid > meta.max_rowid``.

    Prints progress to ``progress_stream`` every PROGRESS_EVERY source rows.
    Returns a stats dict: {"indexed": int, "skipped": int, "is_initial_build": bool}.
    """
    last_indexed = _max_indexed_rowid(fts_con)
    is_initial = last_indexed == 0

    cursor = src_con.execute(
        """
        SELECT rowid, id, content
        FROM session_messages
        WHERE rowid > ?
        ORDER BY rowid ASC
        """,
        (last_indexed,),
    )

    indexed_count = 0
    skipped_count = 0
    last_rowid_seen = last_indexed

    batch: list[tuple[str, str]] = []
    BATCH_SIZE = 1000

    fts_con.execute("BEGIN")
    try:
        for row in cursor:
            rowid = row["rowid"]
            mid = row["id"]
            content = row["content"]
            fragments = extract_text_fragments(content)
            if not fragments:
                skipped_count += 1
            else:
                # Join fragments with a newline. FTS5 tokenises across these.
                joined = "\n".join(fragments)
                batch.append((joined, mid))
                indexed_count += 1

            last_rowid_seen = rowid

            if (indexed_count + skipped_count) % PROGRESS_EVERY == 0 and progress_stream:
                progress_stream.write(
                    f"FTS backfill: processed {indexed_count + skipped_count} rows "
                    f"({indexed_count} indexed, {skipped_count} skipped)\n"
                )
                progress_stream.flush()

            if len(batch) >= BATCH_SIZE:
                fts_con.executemany(
                    "INSERT INTO fts_messages(text, message_id) VALUES (?, ?)",
                    batch,
                )
                batch.clear()

        if batch:
            fts_con.executemany(
                "INSERT INTO fts_messages(text, message_id) VALUES (?, ?)",
                batch,
            )
            batch.clear()

        if last_rowid_seen > last_indexed:
            _meta_set(fts_con, "max_rowid", str(last_rowid_seen))
        fts_con.commit()
    except Exception:
        fts_con.rollback()
        raise

    if is_initial and progress_stream:
        progress_stream.write(
            f"FTS backfill complete: {indexed_count} text messages indexed, "
            f"{skipped_count} non-text rows skipped.\n"
        )
        progress_stream.flush()

    return {
        "indexed": indexed_count,
        "skipped": skipped_count,
        "is_initial_build": is_initial,
    }


def check_schema_version(
    fts_con: sqlite3.Connection,
    current_version: str | None,
    progress_stream=sys.stderr,
) -> None:
    """Warn (but do not rebuild) if conductor.db's schema_version has changed
    since the last FTS index update.

    The operator decides whether a rebuild is needed; we just surface the drift.
    Stores the version after warning so the warning fires once per migration.
    """
    if current_version is None:
        return
    last = _meta_get(fts_con, "schema_version")
    if last is not None and last != current_version:
        if progress_stream:
            progress_stream.write(
                f"WARN: conductor.db schema_version changed: {last} -> {current_version}. "
                "The FTS index may be stale. To rebuild, delete "
                f"{DEFAULT_FTS_PATH} and restart.\n"
            )
            progress_stream.flush()
    if last != current_version:
        _meta_set(fts_con, "schema_version", current_version)
        fts_con.commit()


def search_term(
    fts_con: sqlite3.Connection,
    term: str,
    limit: int = 100_000,
) -> set[str]:
    """Run an FTS5 MATCH for ``term`` and return the set of message_ids that hit.

    The caller is responsible for joining message_ids back to sessions and
    confirming the term is in a ``type=="text"`` block (defense-in-depth —
    FTS5 tokenisation can have edge cases at hyphens, dots, etc.).

    ``term`` is treated as a phrase ('"term"') so FTS5's per-token AND
    semantics don't fire for multi-word inputs (the caller passes one term
    at a time).
    """
    phrase = _fts5_phrase(term)
    cursor = fts_con.execute(
        "SELECT message_id FROM fts_messages WHERE fts_messages MATCH ? LIMIT ?",
        (phrase, limit),
    )
    return {r["message_id"] for r in cursor}


def _fts5_phrase(term: str) -> str:
    """Wrap a term in double quotes and escape any embedded double quotes.

    FTS5's phrase syntax doubles up embedded quotes (a la SQL string literals).
    This neutralises operator characters like ``-``, ``OR``, etc.
    """
    escaped = term.replace('"', '""')
    return f'"{escaped}"'
