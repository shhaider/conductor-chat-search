"""Unit tests for conductor_chat.render — pure function, no I/O."""

import json

from conductor_chat import render


def test_text_block_clean():
    out = render.render_block({"type": "text", "text": "hello"})
    assert out == ["hello"]


def test_thinking_block_clean():
    out = render.render_block({"type": "thinking", "thinking": "two\nlines"})
    assert out[0] == "**[thinking]**"
    assert "> two" in out[1]
    assert "> lines" in out[1]


def test_tool_use_clean():
    out = render.render_block(
        {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/x"}}
    )
    assert "**[tool: Read]**" in out
    assert any("`/tmp/x`" in line for line in out)


def test_tool_use_raw():
    out = render.render_block(
        {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/x"}},
        mode="raw",
    )
    assert "**[tool: Read]**" in out
    joined = "\n".join(out)
    assert "```json" in joined
    assert "/tmp/x" in joined


def test_tool_result_string_clean_truncates():
    long_text = "x" * 1000
    out = render.render_block({"type": "tool_result", "content": long_text})
    joined = "\n".join(out)
    assert "**[tool result]**" in out
    assert "```" in joined
    assert "[truncated," in joined
    # 600 char body + truncation marker; should not contain full 1000 x's
    assert "x" * 1000 not in joined


def test_tool_result_list_of_dicts():
    out = render.render_block(
        {"type": "tool_result", "content": [{"text": "a"}, {"text": "b"}]}
    )
    joined = "\n".join(out)
    assert "a\nb" in joined
    assert "**[tool result]**" in out


def test_render_message_integration():
    payload = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
    }
    row = {"role": "assistant", "content": json.dumps(payload), "sent_at": "2026-05-17T12:00:00"}
    lines = render.render_message(row)
    assert lines[0].startswith("## assistant")
    assert "2026-05-17T12:00:00" in lines[0]
    assert "hi" in lines


def test_render_message_malformed_json():
    row = {"role": "user", "content": "not json", "sent_at": "2026-05-17T12:00:00"}
    lines = render.render_message(row)
    assert lines[0].startswith("## user")
    # malformed json falls back to a text block containing the raw string
    assert any("not json" in line for line in lines)
