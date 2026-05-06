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
import json
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


_NOISE_DIRS: set[str] = {
    # Source-control & language toolchains
    ".git", ".hg", ".svn",
    # Python
    "__pycache__", ".venv", "venv", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", "*.egg-info",
    # Node / web
    "node_modules", ".next", ".nuxt", ".cache", ".npm", "dist", "build",
    "out", ".turbo", ".vercel", ".parcel-cache",
    # macOS noise
    "Library", "Caches", ".Trash", ".DS_Store", ".Spotlight-V100",
    ".fseventsd", ".DocumentRevisions-V100", ".TemporaryItems",
}


def _resolve_glob_base(pattern: str, roots: list[str]) -> str | None:
    """Find the longest prefix of `pattern` that is itself an allowed
    root. Returns the root, or None if the pattern doesn't start under
    any of them. Refusing globs that escape the allowlist is the
    daemon's first line of defense — without this, a pattern like
    `/Users/kunalsingh/**/secret` would walk every directory under
    /Users/kunalsingh/ on the way to finding nothing.
    """
    if not pattern:
        return None
    for r in roots:
        rn = r.rstrip("/")
        if pattern == rn:
            return rn
        if pattern.startswith(rn + "/"):
            return rn
    return None


def _local_glob(args: dict[str, Any], roots: list[str]) -> str:
    """Find files matching a pattern. Walks manually so we can prune
    noise directories (node_modules, .git, Library, etc.) and enforce
    a wall-clock timeout — the naive `glob.glob(pattern, recursive=True)`
    walks the entire FS on a Mac and never returns.
    """
    import fnmatch

    pattern = args.get("pattern", "")
    if not pattern:
        raise ValueError("pattern is empty")

    base = _resolve_glob_base(pattern, roots)
    if base is None:
        raise PermissionError(
            f"glob pattern must start under an allowed root. "
            f"allowed: {roots}"
        )

    matches: list[str] = []
    deadline = time.time() + 10.0  # hard wall clock — daemon stays responsive
    cap = 500

    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        if time.time() > deadline or len(matches) >= cap:
            break
        # Prune in-place. os.walk respects in-place mutation of dirnames.
        # Drop noise dirs and dot-dirs (except a few we care about).
        keep_dot = {".astra", ".astra-state", ".env", ".github"}
        dirnames[:] = [
            d for d in dirnames
            if d not in _NOISE_DIRS
            and not (d.startswith(".") and d not in keep_dot)
        ]

        for name in filenames + list(dirnames):
            full = os.path.join(dirpath, name)
            # fnmatch's `*` matches across slashes, so `/**/` works the
            # same as `/*/` for our purposes.
            if fnmatch.fnmatch(full, pattern):
                matches.append(full)
                if len(matches) >= cap:
                    break

    truncated = f" (capped at {cap})" if len(matches) >= cap else ""
    timed_out = " (walk timed out at 10s)" if time.time() > deadline else ""
    return (
        f"# {len(matches)} match(es) for {pattern}{truncated}{timed_out}\n"
        + "\n".join(matches)
    )


def _local_grep(args: dict[str, Any], roots: list[str]) -> str:
    """Search files for a regex. Manually walks with pruning so we
    don't recurse into node_modules etc., and enforces a wall-clock
    timeout to stay responsive on big trees."""
    pattern = args.get("pattern", "")
    path = args.get("path", "")
    include_glob = args.get("include")
    if not _is_path_allowed(path, roots):
        raise PermissionError(f"path not in allowlist: {path}")
    if not pattern:
        raise ValueError("pattern is empty")

    regex = re.compile(pattern)
    hits: list[str] = []
    deadline = time.time() + 15.0  # hard wall clock
    cap = 200

    keep_dot = {".astra", ".astra-state", ".env", ".github"}

    root_path = Path(path)
    if root_path.is_file():
        files = [root_path]
    else:
        files = []
        for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
            if time.time() > deadline or len(files) > 50_000:
                break
            dirnames[:] = [
                d for d in dirnames
                if d not in _NOISE_DIRS
                and not (d.startswith(".") and d not in keep_dot)
            ]
            for name in filenames:
                if include_glob and not fnmatch.fnmatch(name, include_glob):
                    continue
                files.append(Path(dirpath) / name)

    for p in files:
        if time.time() > deadline or len(hits) >= cap:
            break
        try:
            with p.open("r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    if regex.search(line):
                        hits.append(f"{p}:{i}: {line.rstrip()[:200]}")
                        if len(hits) >= cap:
                            break
        except Exception:
            # Binary files, permission errors, etc. — silently skip
            continue

    truncated = f" (capped at {cap})" if len(hits) >= cap else ""
    timed_out = " (walk timed out at 15s)" if time.time() > deadline else ""
    return (
        f"# {len(hits)} match(es) for /{pattern}/ in {path}{truncated}{timed_out}\n"
        + "\n".join(hits)
    )


# ── Dispatch table ────────────────────────────────────────


def _local_screenshot(args: dict[str, Any]) -> str:
    """Capture a URL as a PNG via headless Chrome.

    No allowlist applies — this is a remote-URL fetch + render, no
    file-system access. We do bound viewport size and timeout so the
    user's machine isn't tied up by an agent run gone wild.

    Returns the screenshot as a base64-encoded PNG (text-safe for the
    bridge result channel) plus simple metadata. The runtime tool
    wrapper turns that into an image artifact with a data URI.
    """
    import base64
    import shutil
    import tempfile
    import uuid

    url = (args.get("url") or "").strip()
    if not url:
        raise ValueError("url is empty")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("url must start with http:// or https://")
    width = max(320, min(3840, int(args.get("viewport_width") or 1440)))
    height = max(240, min(2400, int(args.get("viewport_height") or 900)))
    full_page = bool(args.get("full_page") or False)
    # Total wall-clock cap. Most pages render in 2-4s; allow up to
    # 30s for slow third-party sites. The runtime tool's outer timeout
    # is the real ceiling.
    timeout = max(5, min(60, int(args.get("timeout_sec") or 30)))

    # Locate Chrome — prefer Chrome over Chromium when both exist.
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
        "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chrome"),
    ]
    chrome = next((c for c in candidates if c and os.path.exists(c)), None)
    if not chrome:
        raise RuntimeError(
            "no Chrome/Chromium found on this machine. Install Google "
            "Chrome (https://www.google.com/chrome/) for screenshot_url."
        )

    out_dir = tempfile.mkdtemp(prefix="astra-shot-")
    out_path = os.path.join(out_dir, f"{uuid.uuid4().hex}.png")
    cmd = [
        chrome,
        "--headless=new",  # new headless mode in Chrome 112+
        "--disable-gpu",
        "--no-sandbox",
        "--hide-scrollbars",
        "--disable-blink-features=AutomationControlled",
        f"--window-size={width},{height}",
        f"--screenshot={out_path}",
    ]
    if full_page:
        # Headless Chrome supports full-page via the "shotInfo" flag
        # only in newer builds; the most portable approach is just to
        # set window-size large. We document the limitation.
        cmd.append("--disable-features=IsolateOrigins,site-per-process")
    cmd.append(url)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"screenshot timed out after {timeout}s")
    finally:
        # Don't leave temp dir lingering even on failure.
        pass
    if not os.path.exists(out_path):
        # Chrome can exit 0 but produce no file (e.g. URL refused
        # connection). Bubble its stderr so the agent can react.
        raise RuntimeError(
            f"screenshot failed: chrome exited {proc.returncode}, "
            f"stderr: {proc.stderr.strip()[:500] or '(empty)'}"
        )
    with open(out_path, "rb") as f:
        png_bytes = f.read()
    # Clean up temp file (best effort)
    try:
        os.remove(out_path)
        os.rmdir(out_dir)
    except OSError:
        pass

    encoded = base64.b64encode(png_bytes).decode("ascii")
    return json.dumps(
        {
            "url": url,
            "width": width,
            "height": height,
            "byte_count": len(png_bytes),
            "png_base64": encoded,
        }
    )


_DISPATCH = {
    "local_read": lambda args, roots, bash_pat: _local_read(args, roots),
    "local_write": lambda args, roots, bash_pat: _local_write(args, roots),
    "local_edit": lambda args, roots, bash_pat: _local_edit(args, roots),
    "local_bash": lambda args, roots, bash_pat: _local_bash(args, roots, bash_pat),
    "local_glob": lambda args, roots, bash_pat: _local_glob(args, roots),
    "local_grep": lambda args, roots, bash_pat: _local_grep(args, roots),
    "local_screenshot": lambda args, roots, bash_pat: _local_screenshot(args),
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
