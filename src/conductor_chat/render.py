"""Pure rendering: Conductor session_messages rows -> markdown lines.

No I/O. No DB. No network. Matches the rendering style of ~/Downloads/conductor-export.sh
so exports produced here look identical to what the operator has already eyeballed.
"""

from __future__ import annotations

import json
from typing import Any

TRUNCATE_CLEAN = 600
TRUNCATE_RAW_TOOL_INPUT = 4000


def render_block(content_item: Any, mode: str = "clean") -> list[str]:
    """Render one content-block dict to a list of markdown lines.

    Block types handled:
      - text         -> raw text body (rstripped)
      - thinking     -> ``**[thinking]**`` + lines prefixed with ``> ``
      - tool_use     -> ``**[tool: <name>]**`` + hint (clean) or fenced JSON (raw)
      - tool_result  -> ``**[tool result]**`` + fenced code (clean truncates to 600)
      - other / non-dict -> single-line backtick fallback
    """
    if not isinstance(content_item, dict):
        return [f"`{str(content_item)[:300]}`"]

    t = content_item.get("type")

    if t == "text":
        return [str(content_item.get("text", "")).rstrip()]

    if t == "thinking":
        body = str(content_item.get("thinking", "")).rstrip()
        quoted = "\n".join("> " + line for line in body.splitlines() if line.strip())
        return ["**[thinking]**", quoted]

    if t == "tool_use":
        name = content_item.get("name", "?")
        inp = content_item.get("input", {})
        out_lines = [f"**[tool: {name}]**"]
        if mode == "raw":
            out_lines.append("```json")
            out_lines.append(json.dumps(inp, indent=2)[:TRUNCATE_RAW_TOOL_INPUT])
            out_lines.append("```")
        else:
            hint: Any = ""
            if isinstance(inp, dict):
                hint = (
                    inp.get("command")
                    or inp.get("file_path")
                    or inp.get("path")
                    or inp.get("prompt")
                    or ""
                )
            if hint:
                hint_str = hint if isinstance(hint, str) else json.dumps(hint)
                out_lines.append(f"`{hint_str[:200]}`")
        return out_lines

    if t == "tool_result":
        body: Any = content_item.get("content", "")
        if isinstance(body, list):
            parts: list[str] = []
            for b in body:
                if isinstance(b, dict):
                    parts.append(str(b.get("text", "")))
                else:
                    parts.append(str(b))
            body = "\n".join(parts)
        body = str(body).rstrip()
        if mode != "raw" and len(body) > TRUNCATE_CLEAN:
            body = body[:TRUNCATE_CLEAN] + f"\n…[truncated, {len(body) - TRUNCATE_CLEAN} more chars]"
        return ["**[tool result]**", "```", body, "```"]

    return [f"`<unhandled type={t}>`"]


def render_message(message_row: Any, mode: str = "clean") -> list[str]:
    """Render one session_messages row to markdown lines.

    The row must be dict-like with at least: role, content (JSON string),
    sent_at (str|None), created_at (str). Robust to malformed JSON — treats the
    raw content as a single text block in that case.
    """
    raw_content = _row_get(message_row, "content") or ""
    fallback_role = _row_get(message_row, "role") or "?"
    ts = _row_get(message_row, "sent_at") or _row_get(message_row, "created_at") or ""

    try:
        payload = json.loads(raw_content) if raw_content else {}
        if not isinstance(payload, dict):
            payload = {"_raw": payload}
    except (ValueError, TypeError):
        payload = {"_raw": raw_content}

    message_obj = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    role = message_obj.get("role") or fallback_role
    content = message_obj.get("content")

    if content is None and "_raw" in payload:
        content = [{"type": "text", "text": str(payload["_raw"])}]
    elif isinstance(content, str):
        content = [{"type": "text", "text": content}]
    elif not isinstance(content, list):
        content = []

    lines: list[str] = [f"## {role}  ·  {ts}", ""]
    for c in content:
        lines.extend(render_block(c, mode))
    lines.append("")
    return lines


def render_session_header(
    session_row: Any,
    workspace_row: Any | None,
    message_count: int,
    mode: str = "clean",
) -> list[str]:
    """Render the frontmatter block for an exported session."""
    sid = _row_get(session_row, "id") or _row_get(session_row, "session_id") or "?"
    title = _row_get(session_row, "title") or "(no title)"
    model = _row_get(session_row, "model") or "?"
    created = _row_get(session_row, "created_at") or ""
    updated = _row_get(session_row, "updated_at") or ""
    pct = _row_get(session_row, "context_used_percent")
    tok = _row_get(session_row, "context_token_count")
    pct_str = f"{pct:.1f}%" if isinstance(pct, (int, float)) else "n/a"
    tok_str = str(tok) if tok is not None else "n/a"
    ws_name = "?"
    if workspace_row is not None:
        ws_name = _row_get(workspace_row, "directory_name") or "?"

    return [
        "# Conductor chat export",
        "",
        f"- **Session id:** `{sid}`",
        f"- **Title:** {title}",
        f"- **Workspace:** {ws_name}",
        f"- **Model:** {model}",
        f"- **Created:** {created}",
        f"- **Updated:** {updated}",
        f"- **Context used:** {pct_str} ({tok_str} tokens)",
        f"- **Total messages:** {message_count}",
        f"- **Export mode:** {mode}",
        "",
        "---",
        "",
    ]


def _row_get(row: Any, key: str) -> Any:
    """Get a value from a dict-like row or sqlite3.Row, returning None if missing."""
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, IndexError):
        return None
    except Exception:
        return None
