"""V2 state-transition engine for canonical work-items.

Implements the strict state machine:

    OPEN          --approve-->   APPROVED
    OPEN          --reject-->    REJECTED
    OPEN          --snooze-->    SNOOZED   (with resume_at)
    OPEN          --cancel-->    CANCELLED (terminal)
    APPROVED      --publish-->   PUBLISHED
    APPROVED      --promote-->   PROMOTED_TO_WORKS

Every transition is persisted in ``intake_transitions`` and the work-item's
``status`` column is updated in the same DB transaction. The audit chain is
the source of truth for "what happened to this work-item?" — even if the
status column is later changed, transitions tell the whole story.

Promotion to WORKS is a **separate, gated** operation:
- The tenant's policy must have ``allow_works=True``;
- The work-item must be in ``APPROVED`` status.

This keeps the canonical work-item strictly separate from executable WORKS
``Work`` objects: the engine never auto-promotes; promotion is explicit and
audited.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .policy import PolicyStore
from .store import SQLiteStore

_TERMINAL_STATES = {"CANCELLED", "REJECTED"}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass(slots=True)
class Transition:
    id: str
    work_item_id: str
    from_state: str
    to_state: str
    actor: str
    reason: str
    at: datetime
    resume_at: datetime | None = None
    idempotency_key: str | None = None


class TransitionEngine:
    """Apply audited state transitions to work-items."""

    def __init__(self, store: SQLiteStore, policy_store: PolicyStore | None = None) -> None:
        self.store = store
        self.policy_store = policy_store or PolicyStore()

    # ---------------- high-level actions ----------------

    def approve(self, work_item_id: str, *, actor: str, reason: str = "") -> Any:
        return self._apply(work_item_id, to_state="APPROVED", actor=actor, reason=reason)

    def reject(self, work_item_id: str, *, actor: str, reason: str = "") -> Any:
        return self._apply(work_item_id, to_state="REJECTED", actor=actor, reason=reason)

    def cancel(self, work_item_id: str, *, actor: str = "", reason: str = "", idempotency_key: str | None = None) -> Any:
        # Operator path — actor may be empty (system-cancel).
        return self._apply(work_item_id, to_state="CANCELLED", actor=actor or "system", reason=reason, idempotency_key=idempotency_key)

    def snooze(self, work_item_id: str, *, actor: str, resume_at: datetime, reason: str = "") -> Any:
        if not actor:
            raise ValueError("actor is required for snooze")
        return self._apply(
            work_item_id,
            to_state="SNOOZED",
            actor=actor,
            reason=reason,
            resume_at=resume_at,
        )

    def publish(self, work_item_id: str, *, actor: str, reason: str = "") -> Any:
        return self._apply(work_item_id, to_state="PUBLISHED", actor=actor, reason=reason)

    def promote_to_works(self, work_item_id: str, *, actor: str, reason: str = "") -> Any:
        if not actor:
            raise ValueError("actor is required for promotion")
        item = self.store.get_work_item(work_item_id)
        if item is None:
            raise KeyError(work_item_id)
        if item.tenant_id is None:
            raise ValueError("work item has no tenant_id")
        # Policy gate
        decision = self.policy_store.evaluate_works_promotion(item.tenant_id, work_item_id)
        if not decision.allowed:
            raise PermissionError(
                f"tenant policy forbids works promotion ({decision.reason}); "
                f"set policy.allow_works=True to enable"
            )
        if item.status != "APPROVED":
            raise PermissionError(
                f"works promotion requires status APPROVED, got {item.status}"
            )
        return self._apply(work_item_id, to_state="PROMOTED_TO_WORKS", actor=actor, reason=reason)

    # ---------------- internals ----------------

    def _apply(
        self,
        work_item_id: str,
        *,
        to_state: str,
        actor: str,
        reason: str = "",
        resume_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        if not actor:
            raise ValueError("actor is required")
        item = self.store.get_work_item(work_item_id)
        if item is None:
            raise KeyError(work_item_id)
        if item.status in _TERMINAL_STATES:
            raise ValueError(
                f"work item is in terminal state {item.status}; no further transitions allowed"
            )
        # Allowed transitions:
        # OPEN -> APPROVED | REJECTED | SNOOZED | CANCELLED
        # APPROVED -> PUBLISHED | PROMOTED_TO_WORKS
        # SNOOZED -> OPEN (auto-resume; not implemented here, see clock-driven helper)
        allowed = _allowed_transitions(item.status)
        if to_state not in allowed:
            raise ValueError(
                f"cannot transition from {item.status} to {to_state}"
            )
        at = _utc_now()
        transition = Transition(
            id=f"tr_{uuid.uuid4().hex}",
            work_item_id=work_item_id,
            from_state=item.status,
            to_state=to_state,
            actor=actor,
            reason=reason or "",
            at=at,
            resume_at=resume_at,
            idempotency_key=idempotency_key,
        )
        self.store.write_transition(transition, new_status=to_state, updated_at=at)
        updated = self.store.get_work_item(work_item_id)
        assert updated is not None
        return updated

    # ---------------- read helpers ----------------

    def transitions_for(self, work_item_id: str) -> list[Transition]:
        return self.store.list_transitions(work_item_id)

    def last_transition(self, work_item_id: str) -> Transition | None:
        rows = self.store.list_transitions(work_item_id)
        return rows[-1] if rows else None


def _allowed_transitions(current: str) -> set[str]:
    if current == "OPEN":
        return {"APPROVED", "REJECTED", "SNOOZED", "CANCELLED"}
    if current == "APPROVED":
        return {"PUBLISHED", "PROMOTED_TO_WORKS", "CANCELLED"}
    if current == "SNOOZED":
        return {"OPEN", "CANCELLED"}
    return set()


__all__ = ["Transition", "TransitionEngine"]