"""Drafter eval harness — regression net for the content/reply drafters.

The Vellum lesson (deep-research, 2026-06-23): a prompt or code tweak must
not be able to SILENTLY degrade draft quality. So we keep a Test Suite that
asserts the properties drafts must hold — run it before/after any change.

Two layers:
  • Deterministic guard-regressions (no LLM, run always / in CI): the
    safety guards built this session — the LinkedIn internal-leak scanner,
    the outward-only briefing extraction — plus reusable text-property
    checks (no placeholders, no AI hedging, length, hook, hashtags).
  • Live quality evals (--live, costs Kimi tokens): run the REAL drafters
    against golden inputs, apply the same property checks, + an LLM-judge
    voice score. Run before deploys / on a weekly schedule.

Entry point: `python -m astra.evals.run` (add --live for the LLM layer).
"""
