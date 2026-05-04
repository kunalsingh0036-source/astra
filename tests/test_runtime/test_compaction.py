"""
Tests for _compact_messages — the context-window manager.

Triggered by a real production hit: a session crossed 213k tokens
(over Claude's 200k limit) because tool_result content from
local_read / local_glob / web fetches kept accumulating. The
compactor's job is to keep sessions usable across many turns by
truncating oversized blocks and elidating older messages.
"""

from __future__ import annotations

from astra.runtime.agent_loop import (
    _compact_messages,
    _estimate_tokens_for_block,
    _estimate_tokens_for_message,
    _truncate_tool_result_content,
    _TOOL_RESULT_CAP_CHARS,
)


# ── Token estimation ──────────────────────────────────────


def test_estimate_string_block() -> None:
    """4 chars ≈ 1 token (rough English BPE approximation)."""
    assert _estimate_tokens_for_block("hello world") == 11 // 4


def test_estimate_text_block() -> None:
    block = {"type": "text", "text": "x" * 400}
    assert _estimate_tokens_for_block(block) == 100  # 400 / 4


def test_estimate_tool_use_block() -> None:
    block = {
        "type": "tool_use",
        "id": "tu_1",
        "name": "local_read",
        "input": {"path": "/foo/bar.txt", "offset": 1, "limit": 100},
    }
    n = _estimate_tokens_for_block(block)
    # name + serialized input
    assert n > 0
    assert n < 100  # small block — well under


def test_estimate_tool_result_string_content() -> None:
    block = {"type": "tool_result", "tool_use_id": "tu_1", "content": "x" * 4000}
    assert _estimate_tokens_for_block(block) == 1000  # 4000 / 4


def test_estimate_tool_result_list_content() -> None:
    block = {
        "type": "tool_result",
        "tool_use_id": "tu_1",
        "content": [
            {"type": "text", "text": "first"},
            {"type": "text", "text": "second"},
        ],
    }
    n = _estimate_tokens_for_block(block)
    assert n > 0


def test_estimate_message_with_role_overhead() -> None:
    msg = {"role": "user", "content": "hello"}
    # 5 chars ≈ 1 token + 4 role overhead = ~5
    assert _estimate_tokens_for_message(msg) == 1 + 4


# ── Tool-result truncation ───────────────────────────────


def test_truncate_short_string_passthrough() -> None:
    out, did = _truncate_tool_result_content("short")
    assert out == "short"
    assert did is False


def test_truncate_long_string_caps_with_marker() -> None:
    long = "x" * (_TOOL_RESULT_CAP_CHARS + 1000)
    out, did = _truncate_tool_result_content(long)
    assert did is True
    assert out.startswith("x" * 100)  # head preserved
    assert "[tool_result truncated" in out
    assert str(_TOOL_RESULT_CAP_CHARS + 1000) in out  # original size cited


def test_truncate_list_content() -> None:
    content = [
        {"type": "text", "text": "x" * (_TOOL_RESULT_CAP_CHARS + 500)},
        {"type": "text", "text": "short"},
    ]
    out, did = _truncate_tool_result_content(content)
    assert did is True
    assert isinstance(out, list)
    # First block truncated, second untouched
    assert "[tool_result truncated" in out[0]["text"]
    assert out[1]["text"] == "short"


# ── Compaction — happy paths ─────────────────────────────


def test_compact_under_limit_passes_through() -> None:
    """When already under budget, no compaction happens."""
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    out, before, after = _compact_messages(messages, max_tokens=10_000)
    assert out == messages
    assert before == after
    assert before > 0


def test_compact_truncates_huge_tool_result_first() -> None:
    """Pass 1 should trim oversized tool_result content before resorting
    to dropping messages — preserving turn structure when possible."""
    huge = "y" * 50_000  # ~12.5k tokens — way over a tight budget
    messages = [
        {"role": "user", "content": "find the readme"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": "local_read",
                    "input": {"path": "/foo/README.md"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_1",
                    "content": huge,
                },
            ],
        },
        {"role": "assistant", "content": "got it, summary…"},
    ]
    out, before, after = _compact_messages(messages, max_tokens=2_000)
    assert after < before
    # Same number of messages — pass 1 was enough
    assert len(out) == len(messages)
    # tool_result content is now truncated
    tool_result_msg = out[2]
    content = tool_result_msg["content"][0]
    assert "[tool_result truncated" in content["content"]


def test_compact_drops_middle_when_pass1_insufficient() -> None:
    """When even after truncation we're over budget (lots of small
    messages that each stay), pass 2 drops the middle and inserts a
    synthetic gap marker."""
    # 30 small messages alternating roles → over budget at very low cap
    msgs = []
    for i in range(30):
        role = "user" if i % 2 == 0 else "assistant"
        msgs.append({"role": role, "content": f"message {i} " * 50})

    out, before, after = _compact_messages(msgs, max_tokens=500)
    # Should have shrunk
    assert len(out) < len(msgs)
    # Gap marker present
    gap = next(
        (m for m in out
         if isinstance(m.get("content"), list)
         and isinstance(m["content"][0], dict)
         and "elided" in str(m["content"][0].get("text", ""))),
        None,
    )
    assert gap is not None, "compaction should insert a gap marker"


def test_compact_preserves_first_message_anchor() -> None:
    """The first user message stays — it anchors the session."""
    msgs = []
    msgs.append({"role": "user", "content": "ORIGINAL ANCHOR PROMPT"})
    for i in range(20):
        role = "assistant" if i % 2 == 0 else "user"
        msgs.append({"role": role, "content": f"filler turn {i} " * 100})

    out, _, _ = _compact_messages(msgs, max_tokens=500)
    # First message must be the anchor
    assert out[0]["content"] == "ORIGINAL ANCHOR PROMPT"


def test_compact_preserves_recent_tail() -> None:
    """The most recent messages (where the in-flight prompt lives)
    must be preserved — that's where the next API call's logic
    operates."""
    msgs = [{"role": "user", "content": "anchor"}]
    for i in range(30):
        role = "assistant" if i % 2 == 0 else "user"
        msgs.append({"role": role, "content": f"old {i} " * 80})
    # The in-flight prompt — must survive compaction
    msgs.append({"role": "user", "content": "FINAL IN-FLIGHT PROMPT"})

    out, _, _ = _compact_messages(msgs, max_tokens=500)
    # Last message preserved
    assert out[-1]["content"] == "FINAL IN-FLIGHT PROMPT"


def test_compact_short_chains_returned_as_is_when_possible() -> None:
    """If the chain is already short (≤10 messages), don't drop anything
    even if technically over budget — better to fail loudly at the API
    than silently lose context that's already small."""
    msgs = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
    ]
    out, _, _ = _compact_messages(msgs, max_tokens=1)
    # Compaction shouldn't aggressively drop short chains
    assert len(out) == len(msgs)
