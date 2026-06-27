"""CI gate: the drafter safety guards must stay green.

Wraps astra.evals.run.run_deterministic so a prompt/code change that
weakens the LinkedIn internal-leak scanner, the outward-only briefing
extraction, or the placeholder/AI-tell checks fails the test suite —
the Vellum "regression-before-deploy" net.
"""

from astra.evals.run import run_deterministic


def test_drafter_guard_regressions():
    rows = run_deterministic()
    assert rows, "no eval checks ran"
    failures = [f"{name}: {detail}" for name, ok, detail in rows if not ok]
    assert not failures, "drafter guard regressions:\n" + "\n".join(failures)
