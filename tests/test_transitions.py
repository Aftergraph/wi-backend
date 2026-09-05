"""TDD tests for V2 review/approval flow.

Work-items move through a strict state machine:

    OPEN -> APPROVED  (reviewer approves)
    OPEN -> REJECTED  (reviewer rejects; no publish, no promotion)
    OPEN -> SNOOZED   (reviewer snoozes until a timestamp; auto-resumes)
    OPEN -> CANCELLED (operator cancels; same as REJECTED for downstream)
    APPROVED -> PROMOTED_TO_WORKS (only via explicit promote call;
                                    requires allow_works policy + APPROVED status)
    APPROVED -> PUBLISHED (after a successful publisher call)

Every state transition writes a ``Transition`` row in ``intake_transitions``
with (id, work_item_id, from_state, to_state, actor, at, reason). The audit
chain is durable and append-only.

Review actions require an ``actor`` (human or system). The engine rejects a
review action without an actor — except for ``cancelled`` which is permitted
without an actor (operator override path).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aftergraph_work_intelligence.models import ObservationInput
from aftergraph_work_intelligence.policy import PolicyStore, TenantPolicy
from aftergraph_work_intelligence.service import WorkIntelligenceService
from aftergraph_work_intelligence.store import SQLiteStore
from aftergraph_work_intelligence.transitions import TransitionEngine


def _make_service(tmp_path, policies: dict[str, TenantPolicy] | None = None) -> WorkIntelligenceService:
    store = SQLiteStore(tmp_path / "wi.db")
    policy_store = PolicyStore()
    for tid, policy in (policies or {}).items():
        policy_store.put(tid, policy)
    return WorkIntelligenceService(store, policy_store=policy_store)


def _engine(svc: WorkIntelligenceService) -> TransitionEngine:
    return TransitionEngine(svc.store, policy_store=svc.policy_store)


def _create_item(svc, tmp_path):
    result = svc.ingest(ObservationInput(
        tenant_id="renos",
        source="conversation",
        text="Vi skal sende kunden en bekræftelse før mandag",
    ))
    assert result.work_item is not None
    return result.work_item.id


def test_new_work_item_starts_in_open_state(tmp_path):
    svc = _make_service(tmp_path)
    wid = _create_item(svc, tmp_path)
    detail = svc.get_work_item_detail(wid, "renos")
    assert detail.work_item.status == "OPEN"


def test_review_approve_moves_open_to_approved(tmp_path):
    svc = _make_service(tmp_path)
    wid = _create_item(svc, tmp_path)
    engine = _engine(svc)
    item = engine.approve(wid, actor="jonas", reason="confirmed")
    assert item.status == "APPROVED"
    assert engine.last_transition(wid).actor == "jonas"
    assert engine.last_transition(wid).from_state == "OPEN"
    assert engine.last_transition(wid).to_state == "APPROVED"


def test_review_reject_moves_open_to_rejected(tmp_path):
    svc = _make_service(tmp_path)
    wid = _create_item(svc, tmp_path)
    engine = _engine(svc)
    item = engine.reject(wid, actor="jonas", reason="not a real task")
    assert item.status == "REJECTED"


def test_review_requires_actor(tmp_path):
    svc = _make_service(tmp_path)
    wid = _create_item(svc, tmp_path)
    engine = _engine(svc)
    with pytest.raises(ValueError, match="actor"):
        engine.approve(wid, actor="", reason="x")


def test_snooze_blocks_resolution_until_resume(tmp_path):
    svc = _make_service(tmp_path)
    wid = _create_item(svc, tmp_path)
    engine = _engine(svc)
    resume_at = datetime.now(UTC) + timedelta(hours=2)
    item = engine.snooze(wid, actor="jonas", resume_at=resume_at)
    assert item.status == "SNOOZED"
    # A new actionable observation should NOT resolve into the snoozed item.
    # And V1-style "open work items" list excludes SNOOZED.
    open_items = svc.store.list_open_work_items("renos")
    assert all(it.id != wid for it in open_items)


def test_works_promotion_requires_approved_status(tmp_path):
    svc = _make_service(tmp_path, {"renos": TenantPolicy(allowed_sources={"conversation"}, allow_works=True)})
    wid = _create_item(svc, tmp_path)
    engine = _engine(svc)
    with pytest.raises(PermissionError, match="APPROVED"):
        engine.promote_to_works(wid, actor="jonas")


def test_works_promotion_requires_policy_allow(tmp_path):
    svc = _make_service(tmp_path, {"renos": TenantPolicy(allowed_sources={"conversation"}, allow_works=False)})
    wid = _create_item(svc, tmp_path)
    engine = _engine(svc)
    engine.approve(wid, actor="jonas")
    with pytest.raises(PermissionError, match="allow_works"):
        engine.promote_to_works(wid, actor="jonas")


def test_works_promotion_happy_path(tmp_path):
    svc = _make_service(tmp_path, {"renos": TenantPolicy(allowed_sources={"conversation"}, allow_works=True)})
    wid = _create_item(svc, tmp_path)
    engine = _engine(svc)
    engine.approve(wid, actor="jonas")
    item = engine.promote_to_works(wid, actor="jonas")
    assert item.status == "PROMOTED_TO_WORKS"


def test_promotion_records_transition_audit(tmp_path):
    svc = _make_service(tmp_path, {"renos": TenantPolicy(allowed_sources={"conversation"}, allow_works=True)})
    wid = _create_item(svc, tmp_path)
    engine = _engine(svc)
    engine.approve(wid, actor="jonas")
    engine.promote_to_works(wid, actor="jonas")
    chain = engine.transitions_for(wid)
    states = [(t.from_state, t.to_state) for t in chain]
    assert ("OPEN", "APPROVED") in states
    assert ("APPROVED", "PROMOTED_TO_WORKS") in states


def test_cancelled_state_is_terminal(tmp_path):
    svc = _make_service(tmp_path)
    wid = _create_item(svc, tmp_path)
    engine = _engine(svc)
    item = engine.cancel(wid, actor="", reason="noise")
    assert item.status == "CANCELLED"
    # No further transitions allowed from CANCELLED.
    with pytest.raises(ValueError):
        engine.approve(wid, actor="jonas")


def test_unknown_work_item_raises(tmp_path):
    svc = _make_service(tmp_path)
    engine = _engine(svc)
    with pytest.raises(KeyError):
        engine.approve("wi_does_not_exist", actor="jonas")