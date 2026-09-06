"""The stream and the scheduler are two deploys of ONE image. If each
reads its own build identity its own way they can disagree about which
commit they are, which is how a drift between them stays invisible.
Measured 2026-09-06: from build dbfe3320, /health said
`railway-git dbfe3320` while the scheduler's boot line said
`unknown dirty=True`. These pin them to one function.
"""

import astra.build_info as bi


def test_railway_commit_beats_the_local_marker(monkeypatch):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "a" * 40)
    b = bi.build_info()
    assert b["build_sha"] == "a" * 40
    assert b["dirty"] is False and b["build_source"] == "railway-git"


def test_unknown_is_the_honest_answer_off_railway(monkeypatch):
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    monkeypatch.setattr(bi, "build_info", bi.build_info)
    b = bi.build_info()
    # Either the checked-in sentinel (marker) or nothing at all; never a guess.
    assert b["build_source"] in ("marker", "none")
    assert b["build_sha"] == "unknown" or len(str(b["build_sha"])) == 40


def test_both_services_read_the_same_function(monkeypatch):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "b" * 40)
    from services.stream import main as stream_main

    assert stream_main._build_info()["build_sha"] == "b" * 40
    assert ("b" * 40) in bi.build_line("scheduler")


def test_the_boot_line_names_the_service_and_the_source(monkeypatch):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "c" * 40)
    line = bi.build_line("scheduler")
    assert line.startswith("[scheduler] build " + "c" * 40)
    assert "source=railway-git" in line
