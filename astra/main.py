"""
Astra entry point.

Boots the Astra agent via the Claude Agent SDK.
Supports two modes:
- Interactive: Chat with Astra in the terminal
- Single query: Pass a prompt as a command-line argument
"""

import asyncio
import sys

from dotenv import load_dotenv

load_dotenv()


async def run_interactive():
    """Run Astra in interactive chat mode."""
    from claude_agent_sdk import query, ClaudeSDKClient
    from astra.core.agent import create_astra_options

    options = create_astra_options()

    print("Astra v0.1.0 — Foundation")
    print("Type your message (or 'exit' to quit)")
    print("-" * 50)

    async with ClaudeSDKClient(options=options) as client:
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

            await client.query(user_input)
            async for message in client.receive_response():
                if hasattr(message, "content"):
                    for block in message.content:
                        if hasattr(block, "text"):
                            print(f"\nAstra: {block.text}")
                elif hasattr(message, "result"):
                    pass  # ResultMessage — session complete


async def run_single(prompt: str):
    """Run a single query and exit."""
    from claude_agent_sdk import query
    from astra.core.agent import create_astra_options

    options = create_astra_options()

    async for message in query(prompt=prompt, options=options):
        if hasattr(message, "content"):
            for block in message.content:
                if hasattr(block, "text"):
                    print(block.text)


def main():
    """Entry point for the `astra` CLI command."""
    if len(sys.argv) > 1:
        prompt = " ".join(sys.argv[1:])
        asyncio.run(run_single(prompt))
    else:
        asyncio.run(run_interactive())


if __name__ == "__main__":
    main()
