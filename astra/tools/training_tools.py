"""
Training tools — the conversational surface for the cloud training counters.

Kunal reports training over WhatsApp/chat in natural language; the agent
translates that into a log_training call. This replaces the Mac-only Apple
Note as the way the 6 debt counters get updated, so training context stays
live regardless of the laptop (the macOS fault-line unlock).

Debt-counter semantics (from kunal_compass): a counter is sessions OWED.
  - did/completed a session  → debt DOWN 1  (done=...)
  - missed/skipped a session → debt UP 1     (missed=...)
  - explicit correction      → absolute set  (set_values="skill=175")
Every call echoes the full counter state so Kunal can correct in one line.
"""

from __future__ import annotations

from astra.notes.missed_sessions import TYPES
from astra.runtime.sdk_compat import create_sdk_mcp_server, tool

_ALIASES = {"breathing": "breathe", "breath": "breathe", "move": "movement",
            "workouts": "workout", "skills": "skill"}


def _norm_types(raw: str) -> tuple[list[str], list[str]]:
    """Parse a comma/space list of type names → (valid_canonical, unknown)."""
    valid: list[str] = []
    unknown: list[str] = []
    for tok in (raw or "").replace(",", " ").split():
        t = _ALIASES.get(tok.lower().strip(), tok.lower().strip())
        if t in TYPES:
            if t not in valid:
                valid.append(t)
        elif tok.strip():
            unknown.append(tok.strip())
    return valid, unknown


def _fmt_counters(c) -> str:
    return ", ".join(f"{t} {getattr(c, t)}" for t in TYPES if getattr(c, t) is not None)


@tool(
    "log_training",
    "Update Kunal's training debt counters (stretch/meditate/breathe/"
    "movement/skill/workout) when he reports a session over chat/WhatsApp. "
    "Debt = sessions OWED: a session DONE lowers it by 1, a session MISSED "
    "raises it by 1. Pass `done` and/or `missed` as comma-separated type "
    "names (e.g. done='stretch, skill', missed='workout'). For an explicit "
    "correction pass `set_values` as comma-separated type=number "
    "(e.g. 'skill=175, workout=180'). `note` is optional free text. This is "
    "the cloud source of truth — it works without the Mac. Always read back "
    "the resulting counters to Kunal.",
    {"done": str, "missed": str, "set_values": str, "note": str},
)
async def log_training_tool(args: dict) -> dict:
    from astra.notes.missed_sessions import snapshot_today
    from astra.notes.training_state import apply_update

    done, unk1 = _norm_types(args.get("done") or "")
    missed, unk2 = _norm_types(args.get("missed") or "")
    note = (args.get("note") or "").strip()

    set_map: dict[str, int] = {}
    bad_sets: list[str] = []
    for pair in (args.get("set_values") or "").replace(";", ",").split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" in pair:
            k, _, v = pair.partition("=")
            kt = _ALIASES.get(k.lower().strip(), k.lower().strip())
            try:
                if kt in TYPES:
                    set_map[kt] = int(v.strip())
                else:
                    bad_sets.append(pair)
            except ValueError:
                bad_sets.append(pair)
        else:
            bad_sets.append(pair)

    if not (done or missed or set_map):
        return _err(
            "Nothing to log — tell me which sessions you DID, MISSED, or the "
            "exact counts to SET (e.g. done='stretch, skill', missed='workout')."
        )

    delta_map: dict[str, int] = {}
    for t in done:
        delta_map[t] = delta_map.get(t, 0) - 1
    for t in missed:
        delta_map[t] = delta_map.get(t, 0) + 1

    try:
        new, no_baseline = await apply_update(
            set_map=set_map, delta_map=delta_map, via="whatsapp", note=note
        )
        # Refresh today's snapshot now so trend() + "Kunal Now" reflect this
        # immediately instead of waiting for the 21:30 job.
        await snapshot_today(force=True)
    except Exception as e:
        return _err(f"couldn't update training counters: {e}")

    bits = []
    if done:
        bits.append("did " + ", ".join(t for t in done if t not in no_baseline))
    if missed:
        bits.append("missed " + ", ".join(t for t in missed if t not in no_baseline))
    if set_map:
        bits.append("set " + ", ".join(f"{k}={v}" for k, v in set_map.items()))
    bits = [b for b in bits if not b.endswith(" ")]  # drop empties
    changed = "; ".join(bits) or "no change"

    notes = []
    if no_baseline:
        # We refuse to invent a baseline — ask Kunal for the real count.
        notes.append(
            "I don't have a baseline yet for: " + ", ".join(no_baseline) +
            ". Tell me the current count to start it, e.g. "
            f"\"set {no_baseline[0]}=178\"."
        )
    skipped = unk1 + unk2 + bad_sets
    if skipped:
        notes.append("ignored unrecognized: " + ", ".join(skipped))
    warn = ("\n" + "\n".join(notes)) if notes else ""
    return _ok(f"Logged ({changed}).\nCurrent debt — {_fmt_counters(new)}.{warn}")


@tool(
    "training_status",
    "Show Kunal's current training debt counters and the week-over-week "
    "trend (which gaps are closing vs growing). Use when he asks how his "
    "training is going. Reads the cloud source of truth (Mac not required).",
    {},
)
async def training_status_tool(args: dict) -> dict:
    from astra.notes.missed_sessions import current_counters, trend
    from astra.notes.training_state import cloud_meta

    c = await current_counters()
    if c is None:
        return _ok(
            "No training counters yet. Tell me your current numbers to seed "
            "them, e.g. \"set stretch=311, meditate=317, breathe=205, "
            "movement=190, skill=178, workout=178\"."
        )

    lines = [f"Training debt — {_fmt_counters(c)}."]
    try:
        tr = await trend(14)
        direction = tr.get("direction") or {}
        wow = tr.get("wow_delta") or {}
        movers = []
        for t in TYPES:
            d = wow.get(t)
            if d:
                arrow = "↓" if d < 0 else "↑"
                movers.append(f"{t} {arrow}{abs(d)}/wk")
        if movers:
            lines.append("Week trend: " + ", ".join(movers))
        elif direction:
            lines.append("Week trend: flat.")
    except Exception:
        pass

    try:
        meta = await cloud_meta()
        if meta.get("updated_at"):
            from datetime import timedelta, timezone

            ts = meta["updated_at"]
            if hasattr(ts, "astimezone"):
                ts = ts.astimezone(timezone(timedelta(hours=5, minutes=30)))
            lines.append(
                f"Last updated: {ts:%a %d %b %H:%M} IST via {meta.get('updated_via') or '—'}"
            )
    except Exception:
        pass

    return _ok("\n".join(lines))


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def create_training_mcp_server():
    return create_sdk_mcp_server(
        name="astra-training",
        version="0.1.0",
        tools=[log_training_tool, training_status_tool],
    )
