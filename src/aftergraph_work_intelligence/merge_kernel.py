"""Shared merge kernel: one implementation behind REST and MCP.

ponytail: merge lives here once — the REST endpoint body used to be the
canonical spec with the MCP tool as a faithful port; the two had to be
kept in sync by hand. Ceiling: merge only (promote/review keep their own
shapes); transport mapping (HTTPException vs Mcp*) stays in the adapters.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from fastapi.encoders import jsonable_encoder

from .models import utc_now


class MergeNotFound(LookupError):
    """Source or target work item missing under the tenant."""


class MergeConflict(ValueError):
    """Idempotency key reused for a different merge, or item cancelled
    for another reason. Maps to 409 on REST, McpInvalid on MCP."""


class MergeInvalid(ValueError):
    """Caller input violates the merge contract. Maps to 400 on REST,
    McpInvalid on MCP."""


@dataclass
class MergeCredential:
    """Transport-supplied identity stamped into the merge evidence."""

    authenticated_credential: str
    auth_method: str
    trace_id: str


@dataclass
class MergeResult:
    work_item: dict[str, Any]
    evidence: dict[str, Any]


def run_merge(
    *,
    service: Any,
    engine: Any,
    store: Any,
    tenant_id: str,
    work_item_id: str,
    target_work_item_id: str,
    actor: str,
    reason: str = "",
    idempotency_key: str | None = None,
    credential: MergeCredential,
    fire: Callable[[str, Any], None] | None = None,
) -> MergeResult:
    """Audited-cancel `work_item_id` as merged into `target_work_item_id`.

    Same idempotency + replay semantics for every caller: a recorded key
    decides before any mutation, replaying the same merge is a 200-style
    replay, and a key used for a different merge (or a cancel for another
    reason) is a conflict. Raises MergeNotFound / MergeConflict /
    MergeInvalid; adapters map these to transport errors.
    """
    if work_item_id == target_work_item_id:
        raise MergeInvalid("cannot merge a work item into itself")
    if not actor or len(actor) > 512:
        raise MergeInvalid("actor must be 1..512 characters")
    if len(reason) > 2048:
        raise MergeInvalid("reason must be at most 2048 characters")
    if idempotency_key is not None and not (8 <= len(idempotency_key) <= 128):
        raise MergeInvalid("idempotency_key must be 8..128 characters")
    try:
        source = service.get_work_item_detail(work_item_id, tenant_id)
        service.get_work_item_detail(target_work_item_id, tenant_id)
    except KeyError as exc:
        raise MergeNotFound("work item not found") from exc
    reason = reason or f"merged into {target_work_item_id}"
    key = idempotency_key
    previous_state = source.work_item.status

    def evidence(item: Any, replay: bool) -> dict[str, Any]:
        return {
            "actor": actor,
            "authenticated_credential": credential.authenticated_credential,
            "tenant_id": tenant_id,
            "source_work_item_id": work_item_id,
            "target_work_item_id": target_work_item_id,
            "reason": reason,
            "previous_state": previous_state,
            "resulting_state": item["status"]
            if isinstance(item, dict)
            else item.status,
            "idempotency_key": key,
            "idempotent_replay": replay,
            "decided_at": utc_now().isoformat(),
            "auth_method": credential.auth_method,
            "trace_id": credential.trace_id,
        }

    def respond(item: Any, replay: bool) -> MergeResult:
        encoded = jsonable_encoder(asdict(item)) if not isinstance(item, dict) else item
        encoded["merged_into_work_item_id"] = target_work_item_id
        return MergeResult(work_item=encoded, evidence=evidence(encoded, replay))

    # Cross-restart idempotency: a recorded key decides before any mutation.
    if key:
        for prior in store.find_transition_by_idempotency_key(key):
            if prior.work_item_id != work_item_id or prior.reason != reason:
                raise MergeConflict(
                    "idempotency key already used for a different merge"
                )
    if previous_state == "CANCELLED":
        last = engine.last_transition(work_item_id)
        if last is not None and last.to_state == "CANCELLED" and last.reason == reason:
            return respond(source.work_item, replay=True)
        raise MergeConflict("work item already cancelled for another reason")
    try:
        item = engine.cancel(
            work_item_id, actor=actor, reason=reason, idempotency_key=key
        )
    except KeyError as exc:
        raise MergeNotFound("work item not found") from exc
    except ValueError as exc:
        raise MergeInvalid(str(exc)) from exc
    result = respond(item, replay=False)
    if fire is not None:
        fire("work_item.merged", jsonable_encoder(result_as_dict(result)))
    return result


def result_as_dict(result: MergeResult) -> dict[str, Any]:
    return {"work_item": result.work_item, "evidence": result.evidence}
