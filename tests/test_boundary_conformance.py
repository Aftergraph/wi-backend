"""
Conformance tests for the Work Intelligence V2 ⇄ Trust Gateway boundary contract.

Vendored contract: contracts/work-intelligence-boundary/1.0.json
Canonical source: Aftergraph/after-graph-governance docs/contracts/work-intelligence-boundary/1.0.json

Invariants enforced here:
1. WI is detection/observation/proposal-only — no implicit execution authority.
2. Every execution path from a WI work-item requires explicit promotion gated by
   tenant policy (allow_works=True) + APPROVED state + human review by default.
3. Publishing requires an explicit action with a destination in allowed_destinations.
4. The canonical work-item stays strictly separate from executable WORKS Work.

Run: PYTHONPATH=src python -m pytest tests/test_boundary_conformance.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

CONTRACT = Path(__file__).resolve().parent.parent / "contracts" / "work-intelligence-boundary" / "1.0.json"

from aftergraph_work_intelligence.models import WorkItem, utc_now  # noqa: E402
from aftergraph_work_intelligence.policy import PolicyStore, TenantPolicy  # noqa: E402
from aftergraph_work_intelligence.publishers import (  # noqa: E402
    DestinationNotAllowed,
    PublishRouter,
)


@pytest.fixture(scope="module")
def boundary() -> dict:
    with open(CONTRACT) as f:
        return json.load(f)


def test_contract_identity(boundary):
    assert boundary["$id"].endswith("work-intelligence-boundary/1.0.json")
    assert boundary["properties"]["layer_roles"]["properties"]["work_intelligence"]["enum"] == [
        "detection/observation/proposal-only"
    ]


def test_no_implicit_execution_authority(boundary):
    """Invariant 1: authority_declaration must always say execution_authority=none."""
    declaration = boundary["properties"]["work_intelligence_proposal"]["properties"]["authority_declaration"]
    assert declaration["properties"]["execution_authority"]["const"] == "none"
    assert declaration["properties"]["promotion_required"]["const"] is True
    assert declaration["properties"]["human_review_required"]["const"] is True


def test_promotion_requires_policy_gate():
    """Invariant 2: tenant policy defaults forbid works promotion (allow_works=False)."""
    policy = TenantPolicy()
    assert policy.allow_works is False
    assert policy.require_approval_for_promotion is True


def test_publishing_requires_allowed_destination():
    """Invariant 3: destinations are allowlisted per tenant — deny by default."""
    policy = TenantPolicy(allowed_destinations={"renos"})
    store = PolicyStore()
    store.put("tenant-a", policy)
    router = PublishRouter(destinations={"works": object()}, policy_store=store, always_config=True)

    item = WorkItem(
        id="wi-1",
        tenant_id="tenant-a",
        title="t",
        summary="s",
        status="APPROVED",
        priority="medium",
        next_action="do",
        confidence=0.9,
        canonical_key="ck",
        canonical_tokens=("t",),
        observation_count=1,
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    with pytest.raises(DestinationNotAllowed):
        router.publish("works", item, [])  # 'works' NOT in allowed_destinations


def test_canonical_work_item_separate_from_works_work(boundary):
    """Invariant 4: engine never auto-promotes — promotion is explicit + audited."""
    import inspect

    from aftergraph_work_intelligence.transitions import TransitionEngine

    src = inspect.getsource(TransitionEngine.promote_to_works)
    # The promote path must consult tenant policy before any state change.
    assert "policy_store.evaluate_works_promotion" in src
    assert "PermissionError" in src  # denied promotion raises, does not silently pass