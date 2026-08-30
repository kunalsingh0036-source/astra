"""Astra capability broker — cloud-side pieces.

The broker itself is a compiled Swift binary on the Mac
(`/Users/kunalsingh/Claude Code/astra-broker`); nothing in this package
is in the trust path. What lives here is the transport (the `intents`
table and its store) and `ace.py`, the canonical-encoding oracle that
proves the Swift implementation correct.

Design: `astra-body/docs/WORKSTREAM-A-DESIGN.md`.
"""
