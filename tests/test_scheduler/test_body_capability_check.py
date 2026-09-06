"""The capability check must alert on a LOST capability and on nothing else.

Full Disk Access is keyed to the executor's exact bytes and the
executor is signed ad hoc, so every rebuild voids the grant. macOS does
not announce a requirement that stopped matching; it denies, and EACCES
upstream is indistinguishable from a file that is not there. Without
this job the body loses half its senses silently.

The three ways this job could be wrong are all worse than not having
it, so each has a test: paging about a closed laptop (the cry-wolf
class this project has already paid for twice), paging about a grant
nobody ever made, and drawing a conclusion when the control probe says
the whole matrix is meaningless.
"""

import pytest

from astra.broker import client
from astra.scheduler import jobs


class _Live:
    poll_age_sec = 3.0
    body_id = 4
    label = "kunal-mbp"


def _probe_result(text: str, status: str = "succeeded"):
    return client.IntentResult(
        intent_id=1, status=status, verb="body.probe",
        result_bytes=text.encode())


def _stub(monkeypatch, *, live, answers, was, pushed):
    async def _sole():
        return live

    async def _last(*, within_days=7):
        return was

    async def _run(verb, args, **kw):
        return _probe_result(answers[args["target"]])

    async def _broadcast(**kw):
        pushed.append(kw)
        return None

    from astra.broker import store
    monkeypatch.setattr(store, "sole_live_body", _sole, raising=False)
    monkeypatch.setattr(store, "last_successful_probes", _last, raising=False)
    monkeypatch.setattr(client, "run_intent", _run)
    import astra.push.sender as sender
    monkeypatch.setattr(sender, "broadcast", _broadcast)


ALL_YES = {t: "opened: yes  target=%s" % t
           for t in ("documents", "messages", "safari", "mail")}


def test_a_closed_laptop_is_never_paged_about(monkeypatch):
    pushed: list = []
    _stub(monkeypatch, live=None, answers=ALL_YES, was={}, pushed=pushed)
    out = _run_job()
    assert out["status"] == "skipped"
    assert "closed laptop" in out["reason"]
    assert pushed == [], "a closed laptop must never push"


def test_a_grant_never_made_is_setup_not_a_regression(monkeypatch):
    pushed: list = []
    answers = dict(ALL_YES)
    for t in ("messages", "safari", "mail"):
        answers[t] = "opened: no   target=%s  errno=1 (Operation not permitted)" % t
    _stub(monkeypatch, live=_Live(), answers=answers, was={}, pushed=pushed)
    out = _run_job()
    assert out["status"] == "skipped"
    assert set(out["pending"]) == {"messages", "safari", "mail"}
    assert pushed == [], "a capability nobody granted must not page anyone"


def test_a_lost_capability_pages_once_with_the_remedy(monkeypatch):
    pushed: list = []
    answers = dict(ALL_YES)
    answers["messages"] = "opened: no   target=messages  errno=1 (Operation not permitted)"
    _stub(monkeypatch, live=_Live(), answers=answers,
          was={"messages": True, "safari": True, "mail": True}, pushed=pushed)
    out = _run_job()
    assert out["status"] == "failed"
    assert out["lost"] == ["messages"]
    assert len(pushed) == 1
    text = (pushed[0]["title"] + " " + pushed[0]["body"]).lower()
    assert "full disk access" in text
    assert "rebuil" in text, "the push must name the cause, not just the symptom"


def test_eacces_is_reported_as_the_acl_not_the_grant(monkeypatch):
    """EACCES means the refusal happened in the filesystem, before TCC
    was consulted: uid 451 cannot traverse there. Sending Kunal to the
    Full Disk Access pane for that costs an afternoon, so this case must
    name the ACL and must not push."""
    pushed: list = []
    answers = dict(ALL_YES)
    answers["documents"] = "opened: no   target=documents  errno=13 (Permission denied)"
    answers["messages"] = "opened: no   target=messages  errno=13 (Permission denied)"
    _stub(monkeypatch, live=_Live(), answers=answers,
          was={"messages": True}, pushed=pushed)
    out = _run_job()
    assert out["status"] == "failed"
    low = out["reason"].lower()
    assert "eacces" in low and "acl" in low and "not the grant" in low
    assert "grant-protected-roots" in out["reason"], "name the script that fixes it"
    assert pushed == [], "an ACL problem is not a lost grant; do not push about TCC"


def test_every_probe_eperm_is_one_lost_grant_not_four_lost_senses(monkeypatch):
    """The case measured in production on 2026-09-06: a rebuild voided
    the grant, so EVERY probe — including the one on ~/Documents, which
    macOS also protects — returned EPERM. The old control logic called
    that 'cannot conclude' and stayed silent about a grant that was
    plainly gone."""
    pushed: list = []
    answers = {t: f"opened: no   target={t}  errno=1 (Operation not permitted)"
               for t in ("documents", "messages", "safari", "mail")}
    _stub(monkeypatch, live=_Live(), answers=answers,
          was={"messages": True, "safari": True, "mail": True}, pushed=pushed)
    out = _run_job()
    assert out["status"] == "failed"
    assert out["lost"] == ["mail", "messages", "safari"]
    assert len(pushed) == 1, "one push, naming the whole grant — not one per store"
    text = (pushed[0]["title"] + " " + pushed[0]["body"]).lower()
    assert "full disk access" in text and "rebuil" in text
    assert "kickstart" in out["reason"], "the remedy must include the restart"


def test_eperm_everywhere_before_anything_ever_worked_is_setup_not_a_regression(monkeypatch):
    """Same signal, no history: nobody has granted it yet. Reported,
    never pushed — that is the cry-wolf class."""
    pushed: list = []
    answers = {t: f"opened: no   target={t}  errno=1 (Operation not permitted)"
               for t in ("documents", "messages", "safari", "mail")}
    _stub(monkeypatch, live=_Live(), answers=answers, was={}, pushed=pushed)
    out = _run_job()
    assert pushed == []
    assert out["status"] == "skipped"


def test_everything_in_force_is_quiet(monkeypatch):
    pushed: list = []
    _stub(monkeypatch, live=_Live(), answers=ALL_YES,
          was={"messages": True, "safari": True, "mail": True}, pushed=pushed)
    out = _run_job()
    assert out["status"] == "success" and pushed == []


def _run_job():
    import asyncio
    return asyncio.run(jobs.body_capability_check())
