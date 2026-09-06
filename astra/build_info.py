"""Which commit is running. ONE implementation, used by every service.

The stream reports this on /health and the scheduler logs it at boot.
They are two deploys of one image and can drift from each other
invisibly, so a second copy of this logic is a way for them to disagree
about what they are: on 2026-09-06 the stream said `railway-git
dbfe3320` while the scheduler said `unknown dirty=True`, from the same
build, because each read a different source.

Order matters. RAILWAY_GIT_COMMIT_SHA is injected by whatever actually
built these bytes, so it beats astra/_build.py, which is written by
whoever ran a script. Never guess: "unknown" is the true answer for a
dev run, and the reason this is worth reading at all.
"""

from __future__ import annotations

import os

__all__ = ["build_info", "build_line"]


def build_info() -> dict[str, object]:
    sha = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "").strip()
    if sha:
        return {
            "build_sha": sha,
            "dirty": False,
            "built_at_utc": os.environ.get(
                "RAILWAY_DEPLOYMENT_CREATED_AT", "unknown"),
            "build_source": "railway-git",
        }
    try:
        from astra import _build  # type: ignore[import-not-found]
    except Exception:
        return {"build_sha": "unknown", "dirty": "unknown",
                "built_at_utc": "unknown", "build_source": "none"}
    return {
        "build_sha": getattr(_build, "build_sha", "unknown"),
        "dirty": getattr(_build, "dirty", "unknown"),
        "built_at_utc": getattr(_build, "built_at_utc", "unknown"),
        "build_source": "marker",
    }


def build_line(service: str) -> str:
    """The one-line form for a service with no HTTP surface."""
    b = build_info()
    return (f"[{service}] build {b['build_sha']} dirty={b['dirty']} "
            f"built_at={b['built_at_utc']} source={b['build_source']}")
