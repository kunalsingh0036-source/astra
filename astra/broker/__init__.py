"""Astra capability broker — cloud-side pieces.

The broker itself is a compiled Swift binary on the Mac
(`/Users/kunalsingh/Claude Code/astra-broker`). What lives here is the
transport (the `intents` table and its store), `ace.py`, the
canonical-encoding oracle that proves the Swift implementation correct,
and — since A7 — the audit chain verifier.

The verifier is the one thing here that IS load-bearing, and it is
load-bearing precisely because it runs on the other side of the
boundary: `audit_chain.py` recomputes every hash and signature and
`audit_anchor.py` reads them out of the locked R2 bucket with a
READ-ONLY token the Mac has never held. A chain checked only by the
machine that wrote it is not evidence.

Design: `astra-body/docs/WORKSTREAM-A-DESIGN.md`.
"""
