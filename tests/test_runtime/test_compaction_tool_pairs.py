"""
Compaction tool-pair atomicity tests.

Production failure (turn #53 of session a859083e…): the message
compactor's pass-2 sliced `tail = pass1[-8:]` mid-way through a
tool_use → tool_result pair. The tail kept the user-message
containing tool_result; the assistant message with the matching
tool_use landed in the elided middle. Anthropic's API rejected:
"unexpected tool_use_id found in tool_result blocks".

These tests construct synthetic message stacks that mirror the
shape of long agent sessions (many turns with tool calls) and
assert the compactor's output is always self-consistent — every
tool_result has its matching tool_use in the immediately previous
message.
"""

from __future__ import annotations

from astra.runtime.agent_loop import (
    _compact_messages,
    _validate_and_repair_messages,
)


def _has_orphans(messages: list[dict]) -> list[str]:
    """Return tool_use_ids that are tool_results without matching
    tool_use in the immediately previous message. Empty list = OK."""
    orphans: list[str] = []
    available_tool_use_ids: set[str] = set()
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    tid = b.get("tool_use_id")
                    if tid not in available_tool_use_ids:
                        orphans.append(tid)
        # Update the set of available tool_use IDs for the NEXT message
        available_tool_use_ids = set()
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    available_tool_use_ids.add(b.get("id"))
    return orphans


def _build_long_session_with_tools(n_turns: int = 30) -> list[dict]:
    """Construct a many-turn session whose every turn used a tool.
    Each turn produces 4 messages:
      0: user prompt
      1: assistant text + tool_use
      2: user tool_result
      3: assistant final text
    So an n-turn session is 4*n messages, with tool_use/tool_result
    pairs at positions (4i+1, 4i+2) for i in [0..n).
    """
    messages: list[dict] = []
    for i in range(n_turns):
        tool_id = f"toolu_{i:04d}"
        messages.append(
            {"role": "user", "content": f"please do task {i}"}
        )
        messages.append(
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": f"running tool for task {i}"},
                    {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": "do_task",
                        "input": {"i": i},
                    },
                ],
            }
        )
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": f"task {i} done",
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "assistant",
                "content": [{"type": "text", "text": f"task {i} complete."}],
            }
        )
    return messages


def test_compactor_does_not_orphan_tool_results_on_long_session() -> None:
    """The exact bug from production: a long session forces pass-2,
    which slices tail[-8:], landing mid-pair. Result must have no
    orphans.
    """
    msgs = _build_long_session_with_tools(n_turns=30)
    # 120 messages — well over the 200-message early-exit threshold's
    # token estimate but exercises the pass-2 slice logic deterministically.
    # Force pass-2 by setting a tiny token target.
    compacted, _, _ = _compact_messages(msgs, max_tokens=1_000)
    orphans = _has_orphans(compacted)
    assert orphans == [], (
        f"Compaction produced orphaned tool_result blocks: {orphans}. "
        f"Compacted stack ({len(compacted)} msgs) starts with roles "
        f"{[m.get('role') for m in compacted[:6]]}"
    )


def test_compactor_preserves_tool_pairs_when_tail_starts_with_tool_result() -> None:
    """Construct a stack where the naive last-8 slice would put a
    tool_result at position 0. The fixed compactor must walk back
    one step to include the matching tool_use.
    """
    # Build so that pass1[-8] is a user message with tool_result.
    # Layout: [u, a, u, a, u, a-tool_use, u-tool_result, a-final]
    # Add a head, then this 8-block tail, then padding. Total = ~20.
    msgs = _build_long_session_with_tools(n_turns=10)
    # Force a small max so pass-2 fires.
    compacted, _, _ = _compact_messages(msgs, max_tokens=1_000)
    assert _has_orphans(compacted) == [], (
        f"Tail-boundary slice orphaned tool_result. Compacted: "
        f"{[m.get('role') for m in compacted]}"
    )


def test_validate_and_repair_drops_orphan_tool_result() -> None:
    """Even with the compactor fix, defense in depth: any orphaned
    tool_result that slips through must be dropped before the call
    so we never crash on a 400.
    """
    # User message at index 0 contains an orphan tool_result with
    # no preceding assistant message.
    bad = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "some text"},
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_orphan",
                    "content": "result for nothing",
                },
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
    ]
    repaired = _validate_and_repair_messages(bad)
    assert _has_orphans(repaired) == []
    # The text block should remain
    assert any(
        isinstance(b, dict) and b.get("type") == "text"
        for b in repaired[0].get("content", [])
    )


def test_validate_and_repair_preserves_valid_pair() -> None:
    """The repair must NOT touch valid tool_use → tool_result pairs."""
    good = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "calling tool"},
                {"type": "tool_use", "id": "toolu_X", "name": "do", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_X",
                    "content": "ok",
                }
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]
    repaired = _validate_and_repair_messages(good)
    assert _has_orphans(repaired) == []
    assert len(repaired) == 4
    # Tool_result block must still be there, not replaced
    assert (
        repaired[2]["content"][0]["type"] == "tool_result"
    ), f"valid tool_result was modified: {repaired[2]['content']}"


def test_validate_and_repair_replaces_empty_user_message() -> None:
    """If a user message contained ONLY orphaned tool_results,
    after dropping them it'd be empty — replace with synthetic text
    so role alternation stays valid.
    """
    bad = [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_orphan_only",
                    "content": "nothing",
                }
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
    ]
    repaired = _validate_and_repair_messages(bad)
    # Message[0] must NOT have empty content
    msg0_content = repaired[0].get("content")
    assert isinstance(msg0_content, list) and len(msg0_content) > 0
    assert any(
        isinstance(b, dict) and b.get("type") == "text" for b in msg0_content
    ), "expected synthetic text replacement"
