"""Drafter eval runner.

    python -m astra.evals.run           # deterministic guard-regressions (no LLM)
    python -m astra.evals.run --live    # + run the REAL drafters + LLM-judge

Deterministic suite exits non-zero on any failure (CI gate). The live
suite needs the astra DB (research_briefings) + the Kimi key, so it runs
in-cluster (or anywhere ASTRA_DB_URL/ANTHROPIC_* are set); locally it
skips gracefully.
"""

from __future__ import annotations

import sys

from astra.evals import checks, golden


def run_deterministic() -> list[tuple[str, bool, str]]:
    """The regression net — pure functions, no LLM. Pins the safety guards
    built this session so a future edit can't silently weaken them."""
    from astra.creators.draft_linkedin_post import _extract_outward

    out: list[tuple[str, bool, str]] = []

    def ck(name: str, passed: bool, detail: str = "") -> None:
        out.append((name, passed, detail))

    # 1. Outward-only extraction must strip the internal roadmap...
    stripped = _extract_outward(golden.LEAK_BAIT_BRIEFING)
    leaked = [t for t in golden.LEAK_BAIT_FORBIDDEN_AFTER_STRIP if t in stripped]
    ck("extract_outward strips internal sections", not leaked,
       f"leaked {leaked}" if leaked else "Build/Subtract/Urgent/Action gone")
    # ...and keep the outward insight.
    missing = [t for t in golden.LEAK_BAIT_REQUIRED_AFTER_STRIP if t not in stripped]
    ck("extract_outward keeps outward insight", not missing,
       f"missing {missing}" if missing else "Gist/Findings/Signals kept")

    # 2. The internal-leak scanner: flags the real leak, passes the rewrite.
    flagged, _ = checks.no_internal_leak(golden.KNOWN_LEAK_POST)
    ck("leak scanner flags the known prod leak", flagged is False,
       "flagged" if flagged is False else "MISSED the leak — guard rotted")
    clean_ok, d = checks.no_internal_leak(golden.CLEAN_POST)
    ck("leak scanner passes a clean market take", clean_ok is True, d)

    # 3. Meta-review briefing has no outward angle → gate would decline.
    mr = _extract_outward(golden.META_REVIEW_BRIEFING)
    ck("meta-review yields no postable outward content", len(mr.strip()) < 80,
       f"{len(mr.strip())} outward chars")

    # 4. Property checks self-validate (so the checks themselves can't rot).
    p_bad, _ = checks.no_placeholder(golden.EMAIL_WITH_PLACEHOLDER)
    ck("placeholder check catches [Name]/[date]", p_bad is False)
    p_ok, _ = checks.no_placeholder(golden.EMAIL_CLEAN)
    ck("placeholder check passes a clean email", p_ok is True)
    h_bad, _ = checks.no_ai_tells(golden.EMAIL_WITH_HEDGE)
    ck("AI-tells check catches hedging/boilerplate", h_bad is False)
    h_ok, _ = checks.no_ai_tells(golden.EMAIL_CLEAN)
    ck("AI-tells check passes a clean email", h_ok is True)

    return out


async def run_live() -> list[tuple[str, bool, str]]:
    """Run the REAL LinkedIn drafter against the latest briefing and assert
    the output holds its properties. Best-effort: skips if DB/Kimi absent."""
    out: list[tuple[str, bool, str]] = []
    try:
        from astra.creators.draft_linkedin_post import (
            _get_latest_ready_briefing,
            draft_linkedin_post,
            get_artifact,
        )
    except Exception as e:  # pragma: no cover
        return [("live: import", False, f"skipped — {e}")]

    briefing = await _get_latest_ready_briefing()
    if not briefing:
        return [("live: latest briefing", False, "skipped — no ready briefing reachable (run in-cluster)")]

    res = await draft_linkedin_post(briefing_id=briefing["id"], force=True)
    if res.get("status") != "staged":
        out.append((f"live draft (briefing {briefing['id']})", res.get("status") == "not_postable",
                    f"status={res.get('status')} ({res.get('reason','')})"))
        return out
    art = await get_artifact(res["artifact_id"])
    c = (art or {}).get("content") or {}
    body = c.get("body", "")
    full = body + "\n" + " ".join(c.get("hashtags") or [])
    leak_ok, ld = checks.no_internal_leak(full)
    out.append(("live: no internal leak", leak_ok, ld))
    out.append(("live: no placeholders", *checks.no_placeholder(full)))
    out.append(("live: no AI tells", *checks.no_ai_tells(body)))
    out.append(("live: length", *checks.within_chars(body, 300, 2600)))
    out.append(("live: hashtags", *checks.has_hashtags(c.get("hashtags"))))
    # clean up the eval-generated draft
    try:
        from astra.creators.store import set_artifact_status
        await set_artifact_status(res["artifact_id"], status="rejected")
    except Exception:
        pass
    return out


def _print(title: str, rows: list[tuple[str, bool, str]]) -> int:
    print(f"\n=== {title} ===")
    fails = 0
    for name, ok, detail in rows:
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            fails += 1
    print(f"  {len(rows)-fails}/{len(rows)} passed")
    return fails


def main() -> int:
    live = "--live" in sys.argv
    det = run_deterministic()
    fails = _print("Deterministic guard-regressions", det)
    if live:
        import asyncio

        liverows = asyncio.run(run_live())
        _print("Live drafter evals", liverows)  # informational; doesn't gate
    print()
    if fails:
        print(f"FAIL — {fails} deterministic regression(s). Do not ship.")
        return 1
    print("PASS — all guard-regressions green.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
