"""
Astra CLI entry point.

Boots Astra via the LEAN RUNTIME (astra.runtime.agent_loop) — direct
anthropic.AsyncAnthropic, no Claude Agent SDK, no bundled CLI subprocess.
Phase 6 of the runtime migration removed the SDK-based entry that
used to live here.

Two modes:
  - Interactive: chat with Astra in the terminal
  - Single query: pass a prompt as a command-line argument

This is for local development convenience; production traffic flows
through services/stream's HTTP endpoint, not this entry.
"""

from __future__ import annotations

import asyncio
import json
import sys

from dotenv import load_dotenv

load_dotenv()


async def _run_one_turn(prompt: str, *, session_id: str | None = None) -> str | None:
    """Stream one Astra turn to stdout. Returns the canonical session_id
    so an interactive loop can keep the conversation continuous."""
    # Lazy imports — registers all 107 tools as a side effect.
    import astra.runtime.tools  # noqa: F401
    from astra.runtime.agent_loop import run_lean_turn
    from astra.core.system_prompt import get_system_prompt

    canonical_sid = session_id

    async for frame in run_lean_turn(
        prompt,
        session_id=session_id,
        system_prompt=get_system_prompt(),
        load_history=True,
    ):
        # Each frame is "event: <name>\ndata: <json>\n\n". Parse + render.
        text = frame.decode("utf-8")
        for line in text.split("\n"):
            if line.startswith("event: "):
                event_name = line[len("event: "):]
            elif line.startswith("data: "):
                try:
                    data = json.loads(line[len("data: "):])
                except json.JSONDecodeError:
                    continue
                if event_name == "session":
                    canonical_sid = data.get("session_id") or canonical_sid
                elif event_name == "text_delta":
                    sys.stdout.write(data.get("content", ""))
                    sys.stdout.flush()
                elif event_name == "tool_call":
                    sys.stderr.write(
                        f"\n[tool] {data.get('name', '')}…\n"
                    )
                elif event_name == "tool_result":
                    if data.get("is_error"):
                        sys.stderr.write(
                            f"[tool error] {data.get('preview', '')}\n"
                        )
                elif event_name == "error":
                    sys.stderr.write(f"\n[error] {data.get('message', '')}\n")
                elif event_name == "done":
                    sys.stdout.write("\n")

    return canonical_sid


async def run_interactive() -> None:
    """Run Astra in interactive chat mode in the terminal."""
    print("Astra v0.2 — lean runtime")
    print("Type your message (or 'exit' to quit)")
    print("-" * 50)

    session_id: str | None = None
    while True:
        try:
            user_input = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAstra shutting down.")
            break
        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "q"):
            print("Astra shutting down.")
            break

        sys.stdout.write("\nAstra: ")
        session_id = await _run_one_turn(user_input, session_id=session_id)


async def run_single(prompt: str) -> None:
    """Run a single query and exit."""
    await _run_one_turn(prompt)


def main() -> None:
    """Entry point for the `astra` CLI command."""
    if len(sys.argv) > 1:
        prompt = " ".join(sys.argv[1:])
        asyncio.run(run_single(prompt))
    else:
        asyncio.run(run_interactive())


if __name__ == "__main__":
    main()
