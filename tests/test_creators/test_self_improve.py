"""
Tests for astra/creators/self_improve.py — Layer 4 proactive self-improvement.

Tests cover:
- observe() with validation, dedup
- list_observations() with filters
- propose_fix() generates structured proposals via LLM (mocked)
- apply_fix() dispatches to kit-editor / code-editor functions
- dismiss() updates status correctly
- The auto-detection hooks log observations from generate_json + critique

The DB layer is mocked at the session level so tests don't need a
real Postgres. We use a dict-backed in-memory store instead.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.test_creators.conftest import TEST_KIT_SLUG


# ── In-memory DB substitute ─────────────────────────────────────────


class FakeRow:
    """Mimic SQLAlchemy Row object enough for our queries."""
    def __init__(self, values):
        self._values = values

    def __getitem__(self, idx):
        return self._values[idx]

    def first(self):
        return self


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None

    def one(self):
        return self._rows[0]

    def scalar(self):
        if self._rows:
            return self._rows[0][0]
        return None


@pytest.fixture
def fake_db(monkeypatch):
    """Replace astra.db.engine.async_session() with an in-memory store.

    Yields the dict so tests can inspect state."""
    store: dict[int, dict[str, Any]] = {}
    counter = {"next_id": 1}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def execute(self, stmt, params=None):
            params = params or {}
            sql = str(stmt).strip()

            # Dedup check (SELECT id FROM self_improvements WHERE ...)
            if "SELECT id FROM self_improvements" in sql:
                bs = params.get("bs")
                aid = params.get("aid")
                obs = params.get("obs")
                src = params.get("src")
                for row in store.values():
                    if (row["source"] == src
                            and (row.get("business_slug") or "") == (bs or "")
                            and (row.get("artifact_id") or 0) == (aid or 0)
                            and row["observation"] == obs
                            and row["status"] in ("observed", "proposed", "approved")):
                        return FakeResult([(row["id"],)])
                return FakeResult([])

            # Insert
            if "INSERT INTO self_improvements" in sql:
                from datetime import datetime, timezone
                aid = counter["next_id"]
                counter["next_id"] += 1
                store[aid] = {
                    "id": aid,
                    "source": params["src"],
                    "business_slug": params.get("bs"),
                    "artifact_id": params.get("aid"),
                    "observation": params["obs"],
                    "severity": params.get("sev", "medium"),
                    "status": "observed",
                    "proposed_action": None,
                    "proposed_tool_calls": None,
                    "applied_commit": None,
                    "dismissed_reason": None,
                    "observed_at": datetime.now(timezone.utc),
                    "resolved_at": None,
                }
                return FakeResult([(aid,)])

            # Listing query (SELECT id, source, ... FROM self_improvements ...)
            if "SELECT id, source," in sql:
                rows = list(store.values())
                # Apply WHERE filter — naive but enough for tests
                if "status IN ('observed'" in sql:
                    rows = [r for r in rows
                             if r["status"] in ("observed", "proposed", "approved")]
                if "status = :st" in sql:
                    rows = [r for r in rows if r["status"] == params.get("st")]
                if "business_slug = :bs" in sql:
                    rows = [r for r in rows if r["business_slug"] == params.get("bs")]
                if "severity = :sev" in sql:
                    rows = [r for r in rows if r["severity"] == params.get("sev")]
                rows.sort(key=lambda r: r["observed_at"], reverse=True)
                rows = rows[: params.get("lim", 50)]
                return FakeResult([
                    (
                        r["id"], r["source"], r["business_slug"],
                        r["artifact_id"], r["observation"], r["severity"],
                        r["status"], r["proposed_action"],
                        r["proposed_tool_calls"], r["applied_commit"],
                        r["dismissed_reason"], r["observed_at"], r["resolved_at"],
                    )
                    for r in rows
                ])

            # UPDATE for proposal
            if "UPDATE self_improvements" in sql and "proposed_action" in sql:
                aid = params["id"]
                if aid in store:
                    store[aid]["proposed_action"] = params["pa"]
                    store[aid]["proposed_tool_calls"] = json.loads(params["tc"])
                    store[aid]["status"] = "proposed"
                return FakeResult([])

            # UPDATE for applied
            if "UPDATE self_improvements" in sql and "applied_commit" in sql:
                from datetime import datetime, timezone
                aid = params["id"]
                if aid in store:
                    store[aid]["status"] = "applied"
                    store[aid]["applied_commit"] = params.get("ch")
                    store[aid]["resolved_at"] = datetime.now(timezone.utc)
                return FakeResult([])

            # UPDATE for dismissed
            if "UPDATE self_improvements" in sql and "dismissed_reason" in sql:
                from datetime import datetime, timezone
                aid = params["id"]
                if aid in store:
                    store[aid]["status"] = "dismissed"
                    store[aid]["dismissed_reason"] = params["reason"]
                    store[aid]["resolved_at"] = datetime.now(timezone.utc)
                return FakeResult([])

            return FakeResult([])

        async def commit(self):
            pass

    def fake_session_factory():
        return FakeSession()

    monkeypatch.setattr(
        "astra.creators.self_improve.async_session",
        fake_session_factory,
    )
    return store


# ── observe() ───────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestObserve:
    async def test_creates_new_observation(self, fake_db):
        from astra.creators.self_improve import observe

        result = await observe(
            source="manual",
            observation="Test observation",
            business_slug="testco",
            severity="medium",
        )
        assert result["status"] == "observed"
        assert result["deduped"] is False
        assert result["id"] >= 1
        assert len(fake_db) == 1

    async def test_dedup_skips_duplicate(self, fake_db):
        from astra.creators.self_improve import observe

        first = await observe(
            source="manual", observation="dup test",
            business_slug="testco",
        )
        second = await observe(
            source="manual", observation="dup test",
            business_slug="testco",
        )
        assert second["deduped"] is True
        assert second["id"] == first["id"]
        # Still only one row in store
        assert len(fake_db) == 1

    async def test_invalid_source_raises(self, fake_db):
        from astra.creators.self_improve import observe

        with pytest.raises(ValueError, match="source"):
            await observe(source="not-a-real-source", observation="x")

    async def test_invalid_severity_raises(self, fake_db):
        from astra.creators.self_improve import observe

        with pytest.raises(ValueError, match="severity"):
            await observe(
                source="manual", observation="x", severity="critical",
            )

    async def test_empty_observation_raises(self, fake_db):
        from astra.creators.self_improve import observe

        with pytest.raises(ValueError, match="empty"):
            await observe(source="manual", observation="   ")


# ── list_observations() ─────────────────────────────────────────────


@pytest.mark.asyncio
class TestListObservations:
    async def test_default_returns_active_only(self, fake_db):
        from astra.creators.self_improve import observe, list_observations

        await observe(source="manual", observation="active 1")
        await observe(source="manual", observation="active 2")
        # Manually mark one as dismissed
        list(fake_db.values())[0]["status"] = "dismissed"

        rows = await list_observations()
        # Only the active one
        assert len(rows) == 1
        assert rows[0]["observation"] == "active 2"

    async def test_filter_by_business(self, fake_db):
        from astra.creators.self_improve import observe, list_observations

        await observe(source="manual", observation="for testco", business_slug="testco")
        await observe(source="manual", observation="for other", business_slug="other")

        rows = await list_observations(business_slug="testco")
        assert len(rows) == 1
        assert rows[0]["business_slug"] == "testco"

    async def test_filter_by_severity(self, fake_db):
        from astra.creators.self_improve import observe, list_observations

        await observe(source="manual", observation="A", severity="high")
        await observe(source="manual", observation="B", severity="low")

        rows = await list_observations(severity="high")
        assert len(rows) == 1
        assert rows[0]["severity"] == "high"


# ── propose_fix() ───────────────────────────────────────────────────


VALID_PROPOSAL = json.dumps({
    "diagnosis": "The kit's voice rules don't ban this phrase.",
    "proposed_action": "Add 'world-class' to the forbidden list for testco.",
    "tool_calls": [
        {
            "tool": "add_forbidden_phrase",
            "args": {
                "business": "testco", "phrase": "world-class",
                "rationale": "empty boast",
            },
            "rationale": "Hard ban via the kit-edit tool.",
        }
    ],
    "estimated_impact": "medium",
    "risk_notes": "",
})


@pytest.mark.asyncio
class TestProposeFix:
    async def test_generates_proposal_for_observation(
        self, fake_db, test_kits_dir, mock_anthropic,
    ):
        from astra.creators.self_improve import observe, propose_fix
        mock_anthropic(VALID_PROPOSAL)

        obs = await observe(
            source="forbidden_phrase_persisted",
            observation="'world-class' kept landing despite the regen loop",
            business_slug="testco",
        )
        result = await propose_fix(obs["id"])
        assert result["status"] == "proposed"
        assert "world-class" in result["proposed_action"]
        assert len(result["proposed_tool_calls"]) == 1

    async def test_unknown_observation_raises(self, fake_db):
        from astra.creators.self_improve import propose_fix

        with pytest.raises(FileNotFoundError):
            await propose_fix(99999)

    async def test_already_applied_refuses(
        self, fake_db, test_kits_dir, mock_anthropic,
    ):
        from astra.creators.self_improve import observe, propose_fix
        mock_anthropic(VALID_PROPOSAL)

        obs = await observe(source="manual", observation="x")
        # Manually mark as applied
        fake_db[obs["id"]]["status"] = "applied"

        with pytest.raises(ValueError, match="status="):
            await propose_fix(obs["id"])


# ── dismiss() ───────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestDismiss:
    async def test_marks_dismissed_with_reason(self, fake_db):
        from astra.creators.self_improve import observe, dismiss

        obs = await observe(source="manual", observation="x")
        result = await dismiss(obs["id"], reason="not actionable")
        assert result["status"] == "dismissed"
        assert fake_db[obs["id"]]["status"] == "dismissed"
        assert fake_db[obs["id"]]["dismissed_reason"] == "not actionable"

    async def test_empty_reason_raises(self, fake_db):
        from astra.creators.self_improve import observe, dismiss

        obs = await observe(source="manual", observation="x")
        with pytest.raises(ValueError, match="reason"):
            await dismiss(obs["id"], reason="")

    async def test_already_resolved_refuses(self, fake_db):
        from astra.creators.self_improve import observe, dismiss

        obs = await observe(source="manual", observation="x")
        fake_db[obs["id"]]["status"] = "applied"
        with pytest.raises(ValueError, match="already"):
            await dismiss(obs["id"], reason="x")


# ── auto-detection hooks ────────────────────────────────────────────


@pytest.mark.asyncio
class TestAutoDetectionHooks:
    async def test_persistent_forbidden_logs(self, fake_db):
        from astra.creators.self_improve import auto_observe_persistent_forbidden

        await auto_observe_persistent_forbidden(
            forbidden_hits=["world-class", "synergy"],
            business_slug="testco",
            artifact_id=42,
        )
        # An observation was logged
        assert len(fake_db) == 1
        row = list(fake_db.values())[0]
        assert row["source"] == "forbidden_phrase_persisted"
        assert "world-class" in row["observation"]

    async def test_low_critique_logs(self, fake_db):
        from astra.creators.self_improve import auto_observe_low_critique

        await auto_observe_low_critique(
            critique_artifact_id=10,
            parent_artifact_id=5,
            business_slug="testco",
            overall_score=45,
        )
        assert len(fake_db) == 1
        row = list(fake_db.values())[0]
        assert row["source"] == "low_critique_score"
        assert row["severity"] == "medium"  # 45 ≥ 40 threshold for 'high'

    async def test_low_critique_above_threshold_no_log(self, fake_db):
        from astra.creators.self_improve import auto_observe_low_critique

        await auto_observe_low_critique(
            critique_artifact_id=10,
            parent_artifact_id=5,
            business_slug="testco",
            overall_score=80,  # above default threshold of 60
        )
        # No observation logged
        assert len(fake_db) == 0

    async def test_very_low_critique_marked_high_severity(self, fake_db):
        from astra.creators.self_improve import auto_observe_low_critique

        await auto_observe_low_critique(
            critique_artifact_id=10,
            parent_artifact_id=5,
            business_slug="testco",
            overall_score=30,  # below 40 → severity=high
        )
        row = list(fake_db.values())[0]
        assert row["severity"] == "high"

    async def test_empty_forbidden_hits_no_log(self, fake_db):
        """Hook should be a no-op when there are no hits — the
        regeneration loop succeeded, no observation needed."""
        from astra.creators.self_improve import auto_observe_persistent_forbidden

        await auto_observe_persistent_forbidden(forbidden_hits=[])
        assert len(fake_db) == 0
