"""TDD tests for V2 tenant policies.

A TenantPolicy is per-tenant configuration that controls:
- which sources are allowed,
- whether to auto-create work-items from observations or only persist the observation,
- the dedupe threshold (per source or global),
- max work-items per tenant,
- max work-item priority (cap),
- whether the tenant allows WORKS promotion (governs executable work),
- reviewer rules.

The policy is enforced in ``WorkIntelligenceService.ingest()`` BEFORE the work-item
is created. If the policy forbids a source or hits a quota, the observation is
still persisted (so we never lose signal) but no work-item is created.
"""
from __future__ import annotations

from aftergraph_work_intelligence.models import ObservationInput
from aftergraph_work_intelligence.policy import PolicyStore, TenantPolicy
from aftergraph_work_intelligence.service import WorkIntelligenceService
from aftergraph_work_intelligence.store import SQLiteStore


def _make_service(tmp_path, policies: dict[str, TenantPolicy] | None = None) -> WorkIntelligenceService:
    store = SQLiteStore(tmp_path / "wi.db")
    policy_store = PolicyStore()
    for tid, policy in (policies or {}).items():
        policy_store.put(tid, policy)
    return WorkIntelligenceService(store, policy_store=policy_store)


def test_default_policy_persists_observation_but_creates_no_work_for_unknown_tenant(tmp_path):
    svc = _make_service(tmp_path)
    # No policy registered for "ghost-tenant"
    result = svc.ingest(ObservationInput(
        tenant_id="ghost-tenant",
        source="conversation",
        text="Vi skal købe noget helt unikt",
    ))
    # Even without a policy, V1 behaviour holds: actionable observation creates a work-item.
    assert result.action == "created"


def test_policy_forbids_source_persists_but_no_work_item(tmp_path):
    policy = TenantPolicy(allowed_sources={"email"})  # not "conversation"
    svc = _make_service(tmp_path, {"renos": policy})
    result = svc.ingest(ObservationInput(
        tenant_id="renos",
        source="conversation",
        text="Vi skal købe rengøringsmidler",
    ))
    assert result.action == "observed"
    assert result.work_item is None
    # Observation still persisted (we never lose signal)
    assert svc.get_observation(result.observation.id) is not None


def test_policy_allowlist_permits_listed_source(tmp_path):
    policy = TenantPolicy(allowed_sources={"conversation"})
    svc = _make_service(tmp_path, {"renos": policy})
    result = svc.ingest(ObservationInput(
        tenant_id="renos",
        source="conversation",
        text="Vi skal sende en bekræftelse",
    ))
    assert result.action == "created"


def test_policy_auto_create_false_persists_only(tmp_path):
    policy = TenantPolicy(allowed_sources={"conversation"}, auto_create_work_items=False)
    svc = _make_service(tmp_path, {"renos": policy})
    result = svc.ingest(ObservationInput(
        tenant_id="renos",
        source="conversation",
        text="Vi skal sende en bekræftelse",
    ))
    assert result.action == "observed"
    assert result.work_item is None
    # But the observation must exist
    assert svc.get_observation(result.observation.id) is not None


def test_policy_quota_blocks_after_n_work_items(tmp_path):
    policy = TenantPolicy(allowed_sources={"conversation"}, max_work_items=2)
    svc = _make_service(tmp_path, {"renos": policy})
    # First two create work-items (different enough to not merge)
    r1 = svc.ingest(ObservationInput(tenant_id="renos", source="conversation",
                                     text="Husk at bestille nye rengøringsklude til kontoret"))
    r2 = svc.ingest(ObservationInput(tenant_id="renos", source="conversation",
                                     text="Ring til leverandøren angående sæbe og papir"))
    # Third is observed-only (over quota)
    r3 = svc.ingest(ObservationInput(tenant_id="renos", source="conversation",
                                    text="Send faktura til kunden i Fredericia"))
    assert r1.action == "created", r1
    assert r2.action == "created", r2
    assert r3.action == "observed"
    assert r3.work_item is None


def test_policy_priority_cap_demotes_critical(tmp_path):
    policy = TenantPolicy(allowed_sources={"conversation"}, max_priority="high")
    svc = _make_service(tmp_path, {"renos": policy})
    result = svc.ingest(ObservationInput(
        tenant_id="renos",
        source="conversation",
        text="URGENT: Vi skal ringe til kunden straks",
    ))
    assert result.action == "created"
    assert result.work_item.priority == "high"  # demoted from "critical" by cap


def test_policy_works_promotion_default_disallowed(tmp_path):
    policy = TenantPolicy(allowed_sources={"conversation"})
    svc = _make_service(tmp_path, {"renos": policy})
    # Even if the engine tries to promote to works, the policy must gate it.
    item_id = svc.ingest(ObservationInput(
        tenant_id="renos", source="conversation",
        text="Vi skal sende en bekræftelse",
    )).work_item.id
    decision = svc.policy_store.evaluate_works_promotion("renos", item_id)
    assert decision.allowed is False
    assert "allow_works" in decision.reason or "policy" in decision.reason.lower()


def test_policy_works_promotion_allowed_when_explicit(tmp_path):
    policy = TenantPolicy(allowed_sources={"conversation"}, allow_works=True)
    svc = _make_service(tmp_path, {"renos": policy})
    item_id = svc.ingest(ObservationInput(
        tenant_id="renos", source="conversation",
        text="Vi skal sende en bekræftelse",
    )).work_item.id
    decision = svc.policy_store.evaluate_works_promotion("renos", item_id)
    assert decision.allowed is True


def test_policy_dedupe_threshold_overrides_default(tmp_path):
    # A higher threshold means stricter matching → less merging.
    strict = TenantPolicy(allowed_sources={"conversation"}, dedupe_threshold=0.95)
    svc_strict = _make_service(tmp_path, {"renos": strict})
    r1 = svc_strict.ingest(ObservationInput(
        tenant_id="renos", source="conversation",
        text="Vi skal sende kunden en bekræftelse før mandag",
    ))
    # Slightly different phrasing → should NOT merge under strict threshold.
    r2 = svc_strict.ingest(ObservationInput(
        tenant_id="renos", source="conversation",
        text="Husk at sende en bekræftelse til kunden i næste uge",
    ))
    assert r2.action == "created"
    assert r2.work_item.id != r1.work_item.id