"""The escalation ladder decides whether Kunal gets pinged. Pin it."""

from datetime import datetime, timedelta, timezone

from astra.objectives.decide import Action, decide, next_check, summarise

NOW = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)


def _obj(**kw):
    base = {
        "id": 1, "title": "term sheet from Samarth", "state": "active",
        "attempts": 0, "max_attempts": 4, "escalate_after": 2,
        "cadence_days": 3, "next_check_at": NOW - timedelta(minutes=1),
        "deadline_at": None, "last_action_at": None,
    }
    base.update(kw)
    return base


def test_reply_closes_it_immediately():
    d = decide(_obj(attempts=2), now=NOW, satisfied_at=NOW, satisfied_detail="replied")
    assert d.action is Action.CLOSE_DONE


def test_success_beats_a_blown_deadline():
    """If they finally replied, that is the outcome — do not escalate."""
    o = _obj(deadline_at=NOW - timedelta(days=1))
    d = decide(o, now=NOW, satisfied_at=NOW)
    assert d.action is Action.CLOSE_DONE


def test_waits_until_next_check():
    d = decide(_obj(next_check_at=NOW + timedelta(days=1)), now=NOW)
    assert d.action is Action.WAIT


def test_first_nudge_is_gentle_then_firm():
    assert decide(_obj(attempts=0), now=NOW).action is Action.DRAFT
    assert decide(_obj(attempts=1), now=NOW).action is Action.DRAFT
    assert decide(_obj(attempts=2), now=NOW).action is Action.DRAFT_FIRM


def test_runs_out_of_attempts_and_stops():
    """A loop that never stops is a nag, and a nag gets muted."""
    d = decide(_obj(attempts=4), now=NOW)
    assert d.action is Action.ABANDON


def test_blown_deadline_escalates_to_a_human():
    d = decide(_obj(deadline_at=NOW - timedelta(hours=1)), now=NOW)
    assert d.action is Action.ESCALATE


def test_paused_objective_does_nothing():
    assert decide(_obj(state="paused"), now=NOW).action is Action.WAIT


def test_stale_evidence_does_not_close_it():
    """A reply that predates our last nudge is not a reply TO it."""
    o = _obj(attempts=1, last_action_at=NOW - timedelta(hours=1))
    d = decide(o, now=NOW, satisfied_at=NOW - timedelta(days=2))
    assert d.action is not Action.CLOSE_DONE


def test_backoff_lengthens_with_attempts():
    a = next_check(_obj(attempts=0), now=NOW)
    b = next_check(_obj(attempts=3), now=NOW)
    assert b > a


def test_summary_is_one_batched_message():
    objs = [(_obj(id=i, title=f"t{i}"), decide(_obj(attempts=0), now=NOW)) for i in (1, 2, 3)]
    out = summarise(objs)
    assert out.count("Drafted a nudge:") == 1
    assert "t1" in out and "t3" in out


def test_quiet_tick_produces_no_message():
    objs = [(_obj(), decide(_obj(next_check_at=NOW + timedelta(days=2)), now=NOW))]
    assert summarise(objs) == ""
