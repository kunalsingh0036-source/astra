"""Golden fixtures for the drafter regression evals.

These encode the failure modes we've actually hit this session, so they
can never silently come back:
  • LEAK_BAIT_BRIEFING — a research briefing whose internal roadmap
    sections (Build/Subtract/Urgent/Action) MUST be stripped by
    _extract_outward and MUST be caught by _leaks_internal if they ever
    reach a post (the "22 jobs, zero turns, 84 tasks" leak).
  • META_REVIEW_BRIEFING — internal-only; the postability gate should
    decline it (no outward angle).
  • KNOWN_LEAK_POST / CLEAN_POST — the exact leaked draft vs. its clean
    rewrite, to pin the leak scanner.
  • EMAIL_* — placeholder/hedging samples for the email-reply properties.
"""

LEAK_BAIT_BRIEFING = """# AI agents in production

**Gist.** Most agent deployments fail on reliability, not capability.

## Findings
- **Coding + support are the only production-grade verticals** _(high)_ — outputs are verifiable.
  _sources: example.com_

## Signals
- _this week_ — Klarna walked back its autonomous SDR → HITL is winning.

## Build
- **[p3] ship the WhatsApp draft loop** (big lift) — close the inbox loop
  _unblocks: measured value_

## Subtract
- **kill the autonomous-SDR experiment** _(high)_ — churned 60%

## Urgent
- **by 2026-06-25** — rotate the chat-shared API keys (security)

## Action items
- [p3] Wire draft-sent metric _(Kunal)_ · due 2026-06-22

## Sources
- [Klarna walkback](https://example.com)
"""

# Tokens that must NOT survive _extract_outward (they're internal roadmap):
LEAK_BAIT_FORBIDDEN_AFTER_STRIP = (
    "WhatsApp draft loop",
    "autonomous-SDR experiment",
    "rotate the chat-shared",
    "Wire draft-sent metric",
    "## Build",
    "## Subtract",
    "## Urgent",
)
# Sections that MUST survive:
LEAK_BAIT_REQUIRED_AFTER_STRIP = ("Gist", "Findings", "Signals", "Klarna", "Coding + support")

META_REVIEW_BRIEFING = """# Saturday meta-review

**Gist.** Internal self-audit of Astra this week.

## Build
- ship X
## Subtract
- cut Y
## Urgent
- fix Z
"""  # no Findings/Signals → _extract_outward yields ~nothing → not_postable

# The actual leak from prod (2026-06-23) and its clean rewrite:
KNOWN_LEAK_POST = (
    "OpenAI shipped multi-day workflow persistence. Meanwhile I'm looking at "
    "our own stack: 22 scheduler jobs, 5 episodic memories, zero agent turns "
    "in 7 days. Training data is flat week-over-week. 84 overdue tasks."
)
CLEAN_POST = (
    "OpenAI shipped multi-day workflow persistence. Anthropic's MCP spec now "
    "streams server-to-client. The infrastructure layer is being solved in "
    "public, for free. When persistence becomes a commodity, differentiation "
    "moves up the stack — to context curation, not orchestration plumbing."
)

# Email-reply property samples.
EMAIL_WITH_PLACEHOLDER = "Hi [Name],\n\nThanks for reaching out. I'll send the deck by [date].\n\n— Kunal"
EMAIL_CLEAN = (
    "Thanks for setting this up, Esha. I'll join on Teams.\n\n"
    "Quick check — what's the agenda so I come prepared?\n\nBest, Kunal"
)
EMAIL_WITH_HEDGE = (
    "Dear Sir/Ma'am,\n\nI hope this email finds you well. I am thrilled to "
    "announce that I will revert shortly.\n\nRegards, Kunal"
)
