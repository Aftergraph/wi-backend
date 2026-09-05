"""V2 tenant policy store.

A ``TenantPolicy`` is per-tenant configuration enforced by the engine BEFORE
work-items are created or promoted. Policies are evaluated at four points:

1.  **Ingest**: source allowlist, auto-create on/off, priority cap.
2.  **Resolution**: per-tenant dedupe threshold (stricter = fewer merges).
3.  **Quota**: max work-items per tenant.
4.  **WORKS promotion**: explicit ``allow_works=True`` AND work-item in APPROVED
    state is required before the engine submits to works-execution.

Policies are in-memory for V2 (held by the running service). Persistent
loading from a file/DB is a follow-up — the contract is the policy object, not
its location.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

_PRIORITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


@dataclass(slots=True)
class TenantPolicy:
    """Per-tenant configuration."""

    #: Set of source names allowed for this tenant (lowercased). Empty = no
    #: restriction (V1 behaviour).
    allowed_sources: set[str] = field(default_factory=set)

    #: When False, actionable observations are still persisted but NO work-item
    #: is created (the engine returns ``action="observed"``). This lets a tenant
    #: run in observation-only mode during evaluation.
    auto_create_work_items: bool = True

    #: Maximum number of OPEN work-items the tenant may hold. 0 = unlimited.
    max_work_items: int = 0

    #: Hard ceiling on work-item priority. Any higher is demoted to this value.
    #: One of "low"|"medium"|"high"|"critical".
    max_priority: str = "critical"

    #: Per-tenant dedupe threshold (Jaccard). Higher = stricter = fewer merges.
    #: Defaults to the V1 default (0.72) at the engine layer.
    dedupe_threshold: float = 0.72

    #: Whether this tenant's work-items can be PROMOTED to works-execution. V2
    #: defaults to False — promotion is always opt-in.
    allow_works: bool = False

    #: Set of destination names (lowercased) the tenant may publish to. None =
    #: all configured destinations allowed (operator's responsibility to keep
    #: this sane). An empty set means NO destinations allowed.
    allowed_destinations: set[str] | None = None

    #: Whether approved-by-human is required before promotion. Even if
    #: ``allow_works=True``, the work-item must have been reviewed.
    require_approval_for_promotion: bool = True

    def cap_priority(self, priority: str) -> str:
        cap = _PRIORITY_RANK.get(self.max_priority, 3)
        cur = _PRIORITY_RANK.get(priority, 1)
        if cur > cap:
            return self.max_priority
        return priority

    def allows_source(self, source: str) -> bool:
        if not self.allowed_sources:
            return True
        return source.casefold() in {s.casefold() for s in self.allowed_sources}

    def allows_destination(self, destination: str) -> bool:
        if self.allowed_destinations is None:
            return True
        return destination.casefold() in {d.casefold() for d in self.allowed_destinations}


@dataclass(slots=True)
class PolicyDecision:
    """Result of a policy evaluation."""

    allowed: bool
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


class PolicyStore:
    """In-memory per-tenant policy store."""

    def __init__(self) -> None:
        self._policies: dict[str, TenantPolicy] = {}

    def get(self, tenant_id: str) -> TenantPolicy:
        """Return the policy for tenant_id, or V1-equivalent defaults."""
        return self._policies.get(tenant_id, TenantPolicy())

    def put(self, tenant_id: str, policy: TenantPolicy) -> None:
        self._policies[tenant_id] = policy

    def clear(self) -> None:
        self._policies.clear()

    def evaluate_works_promotion(self, tenant_id: str, work_item_id: str) -> PolicyDecision:
        policy = self.get(tenant_id)
        if not policy.allow_works:
            return PolicyDecision(
                allowed=False,
                reason="tenant policy does not allow works promotion",
            )
        if policy.require_approval_for_promotion:
            # Service is responsible for status check; we expose a hint here.
            return PolicyDecision(
                allowed=True,
                reason="allow_works=true; subject to approval requirement",
                detail={"work_item_id": work_item_id},
            )
        return PolicyDecision(allowed=True, reason="allow_works=true")


def merge_policy(base: TenantPolicy, override: TenantPolicy) -> TenantPolicy:
    """Compose two policies; ``override`` wins on most fields.

    ``allowed_sources`` is intersected if both are non-empty (refusal-by-default).
    Quotas take the min if both > 0.
    """
    if not base.allowed_sources:
        sources = override.allowed_sources
    elif not override.allowed_sources:
        sources = base.allowed_sources
    else:
        sources = {s.casefold() for s in base.allowed_sources} & {
            s.casefold() for s in override.allowed_sources
        }
    max_wi = min(base.max_work_items, override.max_work_items) if (
        base.max_work_items and override.max_work_items
    ) else (base.max_work_items or override.max_work_items)
    cap = base.max_priority if _PRIORITY_RANK[base.max_priority] < _PRIORITY_RANK[override.max_priority] else override.max_priority
    return replace(
        base,
        allowed_sources=sources,
        max_work_items=max_wi,
        max_priority=cap,
        auto_create_work_items=base.auto_create_work_items and override.auto_create_work_items,
        allow_works=base.allow_works and override.allow_works,
        require_approval_for_promotion=base.require_approval_for_promotion or override.require_approval_for_promotion,
        dedupe_threshold=max(base.dedupe_threshold, override.dedupe_threshold),
    )


__all__ = ["PolicyDecision", "PolicyStore", "TenantPolicy", "merge_policy"]