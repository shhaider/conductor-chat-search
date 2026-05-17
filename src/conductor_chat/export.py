"""Export glue: render a session via P06 and write it to ~/Downloads as markdown."""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import db, render

DEFAULT_OUT_DIR = os.path.expanduser("~/Downloads")
MAX_SUFFIX_TRIES = 1000


def export_session(
    con: sqlite3.Connection,
    session_id: str,
    mode: str = "clean",
    out_dir: str | None = None,
) -> dict[str, Any]:
    """Render the session as markdown and write to disk.

    Returns {"path": absolute_path, "bytes": int, "messages": int, "mode": mode}.
    Filename: ``conductor-chat-<session_id>-<YYYYMMDD-HHMMSS>.md``; on collision
    appends ``-001``, ``-002``, ...

    Raises ValueError if session_id is unknown. Raises OSError on filesystem failure.
    """
    session = db.get_session(con, session_id)
    if session is None:
        raise ValueError(f"unknown session_id: {session_id}")

    workspace = None
    ws_id = session.get("workspace_id")
    if ws_id:
        for w in db.list_workspaces(con):
            if w["id"] == ws_id:
                workspace = w
                break

    messages = db.get_session_messages(con, session_id)

    lines: list[str] = render.render_session_header(session, workspace, len(messages), mode)
    for m in messages:
        lines.extend(render.render_message(m, mode))
    body = "\n".join(lines)

    out_root = Path(out_dir) if out_dir else Path(DEFAULT_OUT_DIR)
    out_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
    base = f"conductor-chat-{session_id}-{stamp}.md"
    target = out_root / base

    if target.exists():
        for n in range(1, MAX_SUFFIX_TRIES + 1):
            candidate = out_root / f"conductor-chat-{session_id}-{stamp}-{n:03d}.md"
            if not candidate.exists():
                target = candidate
                break
        else:
            raise OSError(
                f"could not find a free filename in {out_root} after {MAX_SUFFIX_TRIES} tries"
            )

    target.write_text(body, encoding="utf-8")
    size = target.stat().st_size

    return {
        "path": str(target.resolve()),
        "bytes": size,
        "messages": len(messages),
        "mode": mode,
        # informational: when the file was written, mostly for tests
        "written_at": time.time(),
    }
