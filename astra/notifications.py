"""
Native macOS notifications.

Primary channel for Astra's proactive pings. The caller passes a title,
body, and optional URL. We fire a `display notification` via osascript;
the URL is copied to the clipboard so Kunal can cmd-V → open it in the
browser without reading it off a notification bubble.

Why not a custom click-action? macOS `display notification` (the only
path without shipping a helper app) cannot route a click to a URL.
`terminal-notifier` can but isn't installed. Clipboard + "Tap for link"
is the friction-free compromise until we build a sidebar pin.

All calls are fire-and-forget — if the notification fails, we log and
return False but never raise. The scheduler depends on this.
"""

from __future__ import annotations

import logging
import shlex
import subprocess

logger = logging.getLogger(__name__)


def _osa(script: str, timeout: int = 5) -> None:
    subprocess.run(
        ["/usr/bin/osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _copy_to_clipboard(text: str) -> None:
    """pbcopy (trivial, sync). Fails silently if pbcopy missing."""
    try:
        proc = subprocess.run(
            ["/usr/bin/pbcopy"],
            input=text,
            text=True,
            capture_output=True,
            timeout=3,
        )
        if proc.returncode != 0:
            logger.warning("[notify] pbcopy rc=%s", proc.returncode)
    except Exception as e:
        logger.warning("[notify] pbcopy error: %s", e)


def _escape_apple_str(s: str) -> str:
    """Escape a string for AppleScript double-quoted form."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def notify(
    *,
    title: str,
    body: str,
    subtitle: str | None = None,
    url: str | None = None,
    sound: str | None = "Ping",
    copy_url_to_clipboard: bool = True,
    tag: str | None = None,
    also_push: bool = True,
) -> bool:
    """Notify Kunal. Fans out to:

      1. Native macOS notification (this machine's Mac)
      2. Web Push (every subscribed browser/PWA — iPhone, other laptops)

    - `title`  : bold first line (≤30 chars is ideal)
    - `subtitle`: optional second line
    - `body`   : third line / main text
    - `url`    : opens in browser when the push notification is tapped.
                 Also copied to macOS clipboard for cmd+V.
    - `tag`    : collapses repeat notifications with the same tag in
                 the browser tray. Defaults to the title slug.
    - `also_push`: when False, skip Web Push and ONLY ping the Mac.
                 Useful for notifications that are only relevant at
                 Kunal's desk (rare).
    """
    mac_ok = False
    try:
        parts = [f'"{_escape_apple_str(body)}" with title "{_escape_apple_str(title)}"']
        if subtitle:
            parts.append(f'subtitle "{_escape_apple_str(subtitle)}"')
        if sound:
            parts.append(f'sound name "{_escape_apple_str(sound)}"')
        script = "display notification " + " ".join(parts)
        _osa(script, timeout=4)
        if url and copy_url_to_clipboard:
            _copy_to_clipboard(url)
        mac_ok = True
        logger.info("[notify] macos sent: %s — %s", title, body[:80])
    except Exception as e:
        logger.warning("[notify] macos failed: %s", e)

    # Fan out to every subscribed browser/PWA. Fully async under the
    # hood; we fire-and-forget here so macOS notify stays sync.
    if also_push:
        try:
            import asyncio as _asyncio

            push_body = body if not subtitle else f"{subtitle} · {body}"
            tag_final = tag or _slug(title)

            async def _push_now() -> None:
                from astra.push import broadcast

                await broadcast(
                    title=title,
                    body=push_body,
                    url=url or "/",
                    tag=tag_final,
                )

            try:
                loop = _asyncio.get_running_loop()
                loop.create_task(_push_now())
            except RuntimeError:
                # No running loop (called from a sync context) — run it
                # inline in a fresh loop. Costs ~500ms for the HTTP POST
                # but is safe.
                _asyncio.run(_push_now())
        except Exception as e:
            logger.warning("[notify] push fan-out failed: %s", e)

    return mac_ok


def _slug(s: str) -> str:
    """Convert a title to a short notification tag."""
    import re as _re

    slug = _re.sub(r"[^a-zA-Z0-9]+", "-", s.lower()).strip("-")
    return f"astra-{slug[:40]}" if slug else "astra"


def open_url(url: str) -> bool:
    """Open a URL in the default browser (non-blocking).

    Used by jobs that want to actively pop a page open (rarely — most
    flows should just notify and let Kunal choose to open). The ping
    stays gentle; URL-open is only for "this is the one thing you
    need to act on right now".
    """
    try:
        subprocess.Popen(
            ["/usr/bin/open", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as e:
        logger.warning("[notify] open failed: %s", e)
        return False
