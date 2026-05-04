"""
Issue a new bridge token.

Run on Railway (via `railway run`) or locally with DATABASE_URL set:

    railway run --service scheduler python scripts/issue_bridge_token.py \\
        --label "kunal-mbp" \\
        --root "/Users/kunalsingh/Claude Code" \\
        --root "/Users/kunalsingh/Documents/Astra"

Outputs the plaintext token ONCE — copy it into the Mac daemon's
ASTRA_BRIDGE_TOKEN env var. Once printed, only the SHA-256 hash
remains in the DB; the plaintext is unrecoverable.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from dotenv import load_dotenv

load_dotenv()


async def _main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--label", required=True, help="Human-readable name (e.g. 'kunal-mbp')")
    p.add_argument(
        "--root",
        action="append",
        required=True,
        help="Allowed filesystem root (absolute path). Repeatable.",
    )
    p.add_argument(
        "--allow-bash",
        action="append",
        default=[],
        help="Optional bash pattern allowlist (regex). Repeatable. "
        "Empty = autonomy tier is the only gate (DESTRUCTIVE → asks).",
    )
    args = p.parse_args()

    from astra.runtime.bridge import issue_bridge_token

    plaintext, token_id = await issue_bridge_token(
        label=args.label,
        allowed_paths=args.root,
        allowed_bash_patterns=args.allow_bash or None,
    )

    print()
    print("─" * 60)
    print(f"BRIDGE TOKEN MINTED — id={token_id}, label={args.label!r}")
    print("─" * 60)
    print()
    print("Plaintext token (copy NOW — won't be shown again):")
    print()
    print(f"    {plaintext}")
    print()
    print("Allowed roots:")
    for r in args.root:
        print(f"  - {r}")
    if args.allow_bash:
        print()
        print("Allowed bash patterns:")
        for pat in args.allow_bash:
            print(f"  - {pat}")
    print()
    print("Daemon command (run on Mac):")
    print()
    print(
        f"    ASTRA_BRIDGE_TOKEN='{plaintext}' \\\n"
        f"        python -m astra.bridge_daemon"
    )
    print()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
