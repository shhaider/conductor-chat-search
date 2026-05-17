# P06 — `src/conductor_chat/render.py`

## Goal

Pure function module that converts a Conductor `session_messages` row (with embedded JSON payload) into rendered markdown lines. No I/O. No DB access. Easy to unit test.

## Files to create

- `src/conductor_chat/__init__.py` (empty)
- `src/conductor_chat/render.py`
- `tests/test_render.py`

## API

```python
def render_block(content_item: dict, mode: str = "clean") -> list[str]:
    """Render one content-block dict to a list of markdown lines.

    content_item is one element of the message.content array, e.g.
      {"type": "text", "text": "..."}
      {"type": "thinking", "thinking": "..."}
      {"type": "tool_use", "name": "Read", "input": {...}}
      {"type": "tool_result", "content": "..." | [...]}
    mode: "clean" (default) | "raw"
      clean: tool inputs as single-line code spans; tool results truncated to 600 chars
      raw: tool inputs as fenced JSON blocks (up to 4000 chars); tool results untruncated
    """

def render_message(message_row: dict, mode: str = "clean") -> list[str]:
    """Render one session_messages row to markdown lines, starting with a `## role · ts` heading.

    message_row is a sqlite3.Row (or dict-like) with at least keys:
      - role (str)
      - content (str — JSON-encoded SDK message wrapper)
      - sent_at (str | None)
      - created_at (str)
    Returns the heading lines + content block lines + trailing blank line.
    Robust to malformed JSON (treat content as a single text block).
    """

def render_session_header(session_row: dict, workspace_row: dict | None, message_count: int) -> list[str]:
    """Render the frontmatter for an exported session."""
```

## Tests (`tests/test_render.py`)

Use `pytest` (Python's stdlib `unittest` is also fine — pick whichever is simpler with no deps; pytest will be available via the CI workflow).

Required test cases:

1. **text block, clean** — `{"type":"text","text":"hello"}` → lines ending with `"hello"`.
2. **thinking block, clean** — `{"type":"thinking","thinking":"two\nlines"}` → blockquote (`> two` / `> lines`).
3. **tool_use, clean** — `{"type":"tool_use","name":"Read","input":{"file_path":"/tmp/x"}}` → contains `**[tool: Read]**` and the file path as a code span.
4. **tool_use, raw** — same input → contains `**[tool: Read]**` and a fenced ```json block of the input.
5. **tool_result with string content, clean** — long string → truncated to 600 chars + `[truncated, …]` marker.
6. **tool_result with list-of-dicts content** — `[{"text": "a"}, {"text": "b"}]` → joined as `a\nb` inside fenced code.
7. **render_message integration** — given a row where content parses to `{"message":{"role":"assistant","content":[{"type":"text","text":"hi"}]}}`, output starts with `## assistant · <ts>` and contains `hi`.
8. **render_message malformed JSON** — content is `"not json"` → still produces a heading + treats as a single text block, no crash.

## Acceptance

- All 8 tests pass: `python3 -m pytest tests/test_render.py -v`.
- No network, no FS, no DB calls in this module.
- No third-party imports. `json`, `typing` only.
- Code is ≤ 150 lines.

## Constraint

Reuse the rendering logic style from `~/Downloads/conductor-export.sh` (Python heredoc inside that bash script). Do not duplicate decisions — match the existing format so the operator's eye doesn't have to recalibrate.
