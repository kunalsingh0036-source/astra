"""
Astra local bridge daemon.

Runs on Kunal's Mac. Long-polls Railway for pending tool calls, executes
them locally against the filesystem / shell, and posts results back. The
single trust boundary is path/command allowlists checked against the
token issued at setup time.

Usage:
    python -m astra.bridge_daemon \\
        --token $ASTRA_BRIDGE_TOKEN \\
        --server https://stream.thearrogantclub.com

The daemon prints what it does so the user can see the bridge
working in real time. Handles SIGINT cleanly. If the network drops,
it reconnects with exponential backoff up to a 30s cap.

Tool implementations live in this same file so the daemon is a
single self-contained script — no Astra imports needed on the Mac.
Mac-side dependencies: just httpx and stdlib.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fnmatch
import glob
import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover
    print(
        "missing httpx — install with: pip install httpx",
        file=sys.stderr,
    )
    sys.exit(2)


logger = logging.getLogger("astra-bridge")


# ── Helpers ────────────────────────────────────────────────


def _is_path_allowed(path: str, allowed_roots: list[str]) -> bool:
    """Resolve `path` to its real absolute location; refuse if it's
    not under any of the allowed roots. Matches the server-side check."""
    if not path:
        return False
    try:
        resolved = Path(path).resolve()
    except Exception:
        return False
    abs_path = str(resolved)
    for root in allowed_roots or []:
        try:
            root_resolved = str(Path(root).resolve())
        except Exception:
            continue
        if abs_path == root_resolved:
            return True
        if abs_path.startswith(root_resolved.rstrip("/") + "/"):
            return True
    return False


# ── Tool implementations ───────────────────────────────────


def _local_read(args: dict[str, Any], roots: list[str]) -> str:
    path = args.get("path", "")
    if not _is_path_allowed(path, roots):
        raise PermissionError(f"path not in allowlist: {path}")
    offset = max(1, int(args.get("offset") or 1))
    limit = max(1, min(2000, int(args.get("limit") or 200)))
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"file not found: {path}")
    if not p.is_file():
        raise IsADirectoryError(f"not a file: {path}")
    with p.open("r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    chunk = lines[offset - 1 : offset - 1 + limit]
    out = "".join(chunk)
    return (
        f"# {path} ({len(lines)} lines, showing {offset}–{offset + len(chunk) - 1})\n"
        + out
    )


def _local_write(args: dict[str, Any], roots: list[str]) -> str:
    path = args.get("path", "")
    if not _is_path_allowed(path, roots):
        raise PermissionError(f"path not in allowlist: {path}")
    content = args.get("content", "")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {path}"


def _local_edit(args: dict[str, Any], roots: list[str]) -> str:
    path = args.get("path", "")
    if not _is_path_allowed(path, roots):
        raise PermissionError(f"path not in allowlist: {path}")
    old_string = args.get("old_string", "")
    new_string = args.get("new_string", "")
    if not old_string:
        raise ValueError("old_string is empty")
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    occurrences = text.count(old_string)
    if occurrences == 0:
        raise ValueError(f"old_string not found in {path}")
    if occurrences > 1:
        raise ValueError(
            f"old_string matches {occurrences} times in {path} — "
            "include more surrounding context to make it unique"
        )
    new_text = text.replace(old_string, new_string, 1)
    p.write_text(new_text, encoding="utf-8")
    return f"edited {path}: {len(old_string)} → {len(new_string)} chars"


def _local_bash(
    args: dict[str, Any], roots: list[str], allowed_patterns: list[str] | None
) -> str:
    command = args.get("command", "").strip()
    if not command:
        raise ValueError("command is empty")
    cwd = args.get("cwd") or ""
    if cwd and not _is_path_allowed(cwd, roots):
        raise PermissionError(f"cwd not in allowlist: {cwd}")

    if allowed_patterns:
        if not any(re.search(p, command) for p in allowed_patterns):
            raise PermissionError(
                f"command does not match any allowed pattern; "
                f"allowed: {allowed_patterns}"
            )

    timeout = max(1, min(120, int(args.get("timeout_sec") or 30)))
    proc = subprocess.run(
        command,
        shell=True,
        cwd=cwd or None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    out = (
        f"$ {command}\n"
        f"[exit={proc.returncode}]\n"
        f"--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}"
    )
    return out


def _local_glob(args: dict[str, Any], roots: list[str]) -> str:
    pattern = args.get("pattern", "")
    if not pattern:
        raise ValueError("pattern is empty")
    matches = glob.glob(pattern, recursive=True)
    # Filter to allowlisted paths
    matches = [m for m in matches if _is_path_allowed(m, roots)]
    matches = matches[:500]
    return f"# {len(matches)} match(es) for {pattern}\n" + "\n".join(matches)


def _local_grep(args: dict[str, Any], roots: list[str]) -> str:
    pattern = args.get("pattern", "")
    path = args.get("path", "")
    include_glob = args.get("include")
    if not _is_path_allowed(path, roots):
        raise PermissionError(f"path not in allowlist: {path}")
    regex = re.compile(pattern)
    hits: list[str] = []
    root = Path(path)
    paths = list(root.rglob("*")) if root.is_dir() else [root]
    for p in paths:
        if not p.is_file():
            continue
        if include_glob and not fnmatch.fnmatch(p.name, include_glob):
            continue
        try:
            with p.open("r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    if regex.search(line):
                        hits.append(f"{p}:{i}: {line.rstrip()[:200]}")
                        if len(hits) >= 200:
                            break
        except Exception:
            continue
        if len(hits) >= 200:
            break
    return f"# {len(hits)} match(es) for /{pattern}/ in {path}\n" + "\n".join(hits)


# ── Dispatch table ────────────────────────────────────────


_DISPATCH = {
    "local_read": lambda args, roots, bash_pat: _local_read(args, roots),
    "local_write": lambda args, roots, bash_pat: _local_write(args, roots),
    "local_edit": lambda args, roots, bash_pat: _local_edit(args, roots),
    "local_bash": lambda args, roots, bash_pat: _local_bash(args, roots, bash_pat),
    "local_glob": lambda args, roots, bash_pat: _local_glob(args, roots),
    "local_grep": lambda args, roots, bash_pat: _local_grep(args, roots),
}


# ── Daemon loop ───────────────────────────────────────────


async def run_daemon(
    *,
    token: str,
    server_url: str,
    allowed_roots: list[str] | None = None,
    allowed_bash_patterns: list[str] | None = None,
) -> None:
    """Long-poll the server, execute calls, post results. Runs forever.

    `allowed_roots` and `allowed_bash_patterns` are local fallbacks if
    we ever need to harden beyond what the token says — for now we
    trust the server's view (tokens carry the policy).
    """
    poll_url = server_url.rstrip("/") + "/bridge/poll"
    result_url = server_url.rstrip("/") + "/bridge/result"
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=35.0) as client:
        backoff = 1.0
        while True:
            try:
                r = await client.get(poll_url, headers=headers)
                if r.status_code == 401:
                    logger.error(
                        "[daemon] token rejected (401). exiting."
                    )
                    return
                r.raise_for_status()
                payload = r.json()
                call = payload.get("call")
                if call is None:
                    backoff = 1.0  # reset
                    continue

                logger.info(
                    "[daemon] received call id=%s tool=%s",
                    call.get("id"),
                    call.get("tool"),
                )
                tool = call.get("tool", "")
                args = call.get("args") or {}
                handler = _DISPATCH.get(tool)
                if not handler:
                    await _post_result(
                        client,
                        result_url,
                        headers,
                        call_id=int(call["id"]),
                        ok=False,
                        error=f"unknown tool: {tool!r}",
                    )
                    continue

                try:
                    # Use the daemon's own allowed_roots if provided,
                    # otherwise default to ['/'] (token-based check
                    # doesn't see roots — only the server queue does).
                    # In practice the server already filtered by
                    # is_path_allowed before queueing.
                    result_text = handler(
                        args,
                        allowed_roots or ["/"],
                        allowed_bash_patterns,
                    )
                    await _post_result(
                        client,
                        result_url,
                        headers,
                        call_id=int(call["id"]),
                        ok=True,
                        result=result_text,
                    )
                except Exception as e:
                    logger.exception("[daemon] tool %s raised", tool)
                    await _post_result(
                        client,
                        result_url,
                        headers,
                        call_id=int(call["id"]),
                        ok=False,
                        error=f"{type(e).__name__}: {e}",
                    )
            except (httpx.RequestError, httpx.HTTPStatusError) as e:
                logger.warning(
                    "[daemon] poll error (%s) — backing off %.1fs",
                    type(e).__name__,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            except Exception:
                logger.exception("[daemon] unexpected error in poll loop")
                await asyncio.sleep(2.0)


async def _post_result(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    *,
    call_id: int,
    ok: bool,
    result: str = "",
    error: str | None = None,
) -> None:
    try:
        await client.post(
            url,
            headers=headers,
            json={
                "call_id": call_id,
                "ok": ok,
                "result": result,
                "error_message": error,
            },
        )
    except Exception:
        logger.exception("[daemon] failed to post result for call %s", call_id)


# ── CLI ───────────────────────────────────────────────────


def main() -> None:
    p = argparse.ArgumentParser(description="Astra local bridge daemon")
    p.add_argument(
        "--token",
        default=os.environ.get("ASTRA_BRIDGE_TOKEN", ""),
        help="Bridge token (env: ASTRA_BRIDGE_TOKEN)",
    )
    p.add_argument(
        "--server",
        default=os.environ.get("ASTRA_BRIDGE_SERVER", "https://stream.thearrogantclub.com"),
        help="Server base URL",
    )
    p.add_argument(
        "--root",
        action="append",
        default=[],
        help=(
            "Optional local allowlist root (can be repeated). If set, "
            "the daemon refuses paths outside these even if the token's "
            "server-side policy is more permissive. Belt + suspenders."
        ),
    )
    p.add_argument(
        "--allow-bash",
        action="append",
        default=[],
        help="Local bash regex allowlist (repeatable). Empty = no extra restriction.",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = p.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    if not args.token:
        print(
            "ASTRA_BRIDGE_TOKEN not set — pass --token or export the env var",
            file=sys.stderr,
        )
        sys.exit(2)

    print(f"astra bridge daemon starting → {args.server}", file=sys.stderr)
    if args.root:
        print(f"local roots: {args.root}", file=sys.stderr)

    loop = asyncio.new_event_loop()

    def _stop(*_a):
        for task in asyncio.all_tasks(loop):
            task.cancel()

    with contextlib.suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGINT, _stop)
        loop.add_signal_handler(signal.SIGTERM, _stop)

    try:
        loop.run_until_complete(
            run_daemon(
                token=args.token,
                server_url=args.server,
                allowed_roots=args.root or None,
                allowed_bash_patterns=args.allow_bash or None,
            )
        )
    except (asyncio.CancelledError, KeyboardInterrupt):
        print("astra bridge daemon shutting down", file=sys.stderr)
    finally:
        loop.close()


if __name__ == "__main__":
    main()
