"""MCP server over Work Intelligence (approach A: mounted on the secure app).

Boundaries — mirrors ``api.auth`` / ``api.require_admin``, never weaker:
- Bearer [REDACTED] token: every explicit tenant, admin tools allowed.
- ``ak_*`` key bound to a tenant: that tenant only. Unbound legacy keys keep
  working with an explicit tenant, matching historical REST behavior.
- Admin tools (policy writes, keys, limits, cache): Bearer [REDACTED] only.
- Mutating data tools and all admin tools write audit rows (actor
  ``mcp:{identity}``). The REST layer is unchanged.
- Excluded on purpose: webhook management + stats (HMAC secret surface),
  migrations/run (infra), request logs + cleanup (PII/secrets + destructive),
  metrics/monitoring/response-times/cache-stats (ops scrapes; version, usage
  and readiness cover health).

Transport auth is enforced twice: the production middleware 401s
unauthenticated ``/mcp`` traffic first, and every tool re-resolves the caller
from request headers (fail-closed when headers are absent, e.g. stdio).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any, cast

from fastapi.encoders import jsonable_encoder
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError

from .api import (
    AutonomyEvaluateRequest,
    ObservationRequest,
    _derive_source,
    _dt,
    _fire_webhooks,
    _persist_autonomy_decision,
)
from .autonomy import AutonomyEvaluationInput, Capability, evaluate_autonomy
from .evidence import build_evidence
from .models import ObservationInput, Publication, utc_now


class McpForbidden(MCPError):
    """Caller lacks permission (auth failure, tenant mismatch, non-admin)."""

    def __init__(self, message: str = "forbidden") -> None:
        super().__init__(-32001, message)


class McpNotFound(MCPError):
    """Referenced object does not exist (fail-closed 404 shape)."""

    def __init__(self, message: str = "not found") -> None:
        super().__init__(-32002, message)


class McpInvalid(MCPError):
    """Invalid tool input (mirrors REST 400)."""

    def __init__(self, message: str = "invalid input") -> None:
        super().__init__(-32602, message)


class McpUpstream(MCPError):
    """Configured downstream/publisher unavailable (mirrors REST 502/503)."""

    def __init__(self, message: str = "upstream unavailable") -> None:
        super().__init__(-32003, message)


@dataclass
class McpCaller:
    method: str  # "bearer" | "api_key"
    identity: str  # "bearer" | "api_key:{key_id}"
    bound_tenant: str | None
    is_admin: bool


@dataclass
class McpRuntime:
    store: Any
    service: Any
    policy_store: Any
    transitions: Any
    publisher: Any
    audit: Any
    rate_limiter: Any
    task_queue: Any
    cache: Any
    evidence_secret: Any
    master_token: str | None
    state: Any


def make_runtime(app: Any) -> McpRuntime:
    """Collect the MCP runtime from an assembled application."""
    state = app.state
    return McpRuntime(
        state=state,
        store=state.store,
        service=state.service,
        policy_store=state.policy_store,
        transitions=state.transitions,
        publisher=getattr(state, "publisher", None),
        audit=getattr(state, "audit_log", None),
        rate_limiter=getattr(state, "rate_limiter", None),
        task_queue=getattr(state, "task_queue", None),
        cache=getattr(state, "cache", None),
        evidence_secret=getattr(state, "evidence_secret", None),
        master_token=getattr(state, "mcp_master_token", None),
    )


def _headers_lower(headers: Mapping[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    try:
        items = headers.items()  # type: ignore[union-attr]
    except AttributeError:
        return {}
    return {str(k).lower(): str(v) for k, v in items}


def resolve_caller(
    headers: Mapping[str, str] | None, store: Any, master_token: str | None
) -> McpCaller:
    """Resolve the MCP caller from request headers. Fail-closed."""
    flat = _headers_lower(headers)
    authorization = flat.get("authorization", "")
    if (
        master_token
        and authorization
        and hmac.compare_digest(authorization, f"Bearer {master_token}")
    ):
        return McpCaller(
            method="bearer", identity="bearer", bound_tenant=None, is_admin=True
        )
    candidate = flat.get("x-api-key", "")
    if candidate and candidate.startswith("ak_") and len(candidate) >= 16:
        binding = store.get_api_key_binding(candidate[:12])
        if binding is not None and binding.get("active"):
            expected = str(binding.get("key_hash") or "")
            if hmac.compare_digest(
                expected, hashlib.sha256(candidate.encode()).hexdigest()
            ):
                store.validate_api_key(candidate[:12])
                return McpCaller(
                    method="api_key",
                    identity=f"api_key:{binding.get('id')}",
                    bound_tenant=binding.get("tenant_id"),
                    is_admin=False,
                )
    raise McpForbidden("invalid or missing credentials")


def require_tenant(caller: McpCaller, tenant_id: Any) -> str:
    """Validate an explicit tenant and enforce the caller's binding."""
    tenant = tenant_id.strip() if isinstance(tenant_id, str) else ""
    if not tenant or len(tenant) > 128:
        raise McpForbidden("invalid tenant_id")
    if caller.bound_tenant is not None and caller.bound_tenant != tenant:
        raise McpForbidden("tenant not permitted for this credential")
    return tenant


def require_admin(caller: McpCaller) -> None:
    """Mirror ``api.require_admin``: Bearer [REDACTED] only."""
    if not caller.is_admin:
        raise McpForbidden("admin credential required")


def _audit(
    runtime: McpRuntime,
    caller: McpCaller,
    event: str,
    target: str,
    details: dict[str, Any] | None = None,
) -> None:
    if runtime.audit is None:
        return
    runtime.audit.record(
        event, actor=f"mcp:{caller.identity}", target=target, details=details or {}
    )


def _ctx_headers(ctx: Context | None) -> dict[str, str]:
    if ctx is None:
        return {}
    try:
        return _headers_lower(ctx.headers)
    except (ValueError, AttributeError, TypeError):
        return {}


def _caller(runtime: McpRuntime, ctx: Context | None) -> McpCaller:
    return resolve_caller(_ctx_headers(ctx), runtime.store, runtime.master_token)


def _not_found(work_item_id: str) -> McpNotFound:
    return McpNotFound("work item not found")


# ---------------------------------------------------------------------------
# Data plane (tenant-gated; mirrors the /v1 REST shapes)
# ---------------------------------------------------------------------------


def serve_search(
    runtime: McpRuntime,
    caller: McpCaller,
    q: str,
    tenant_id: str,
    limit: int = 50,
) -> dict[str, Any]:
    tenant = require_tenant(caller, tenant_id)
    if not q or not q.strip() or len(q) > 256:
        raise McpInvalid("q must be 1..256 characters")
    if not 1 <= limit <= 500:
        raise McpInvalid("limit must be 1..500")
    items = runtime.service.list_work_items(tenant, limit=1000)
    needle = q.lower()
    results = [
        item
        for item in items
        if needle in (item.title or "").lower()
        or needle in (item.summary or "").lower()
    ][:limit]
    return {
        "query": q,
        "tenant_id": tenant,
        "results": jsonable_encoder([asdict(item) for item in results]),
        "count": len(results),
    }


def serve_list_work_items(
    runtime: McpRuntime,
    caller: McpCaller,
    tenant_id: str,
    status: str | None = None,
    priority: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    tenant = require_tenant(caller, tenant_id)
    if not 1 <= limit <= 1000:
        raise McpInvalid("limit must be 1..1000")
    items = runtime.service.list_work_items(tenant, limit=limit)
    out: list[dict[str, Any]] = []
    for item in items:
        if status is not None and item.status != status:
            continue
        if priority is not None and item.priority != priority:
            continue
        encoded = jsonable_encoder(asdict(item))
        encoded["source"] = _derive_source(runtime.store, item.id)
        out.append(encoded)
    return {"tenant_id": tenant, "count": len(out), "work_items": out}


def serve_get_work_item(
    runtime: McpRuntime, caller: McpCaller, work_item_id: str, tenant_id: str
) -> dict[str, Any]:
    tenant = require_tenant(caller, tenant_id)
    try:
        detail = runtime.service.get_work_item_detail(work_item_id, tenant)
    except KeyError as exc:
        raise _not_found(work_item_id) from exc
    return jsonable_encoder(asdict(detail))


def serve_create_observation(
    runtime: McpRuntime,
    caller: McpCaller,
    tenant_id: str,
    source: str,
    text: str,
    external_id: str | None = None,
    actor: str | None = None,
    occurred_at: Any = None,
    metadata: dict[str, Any] | None = None,
    title_hint: str | None = None,
    owner_hint: str | None = None,
    due_hint: str | None = None,
    priority_hint: str | None = None,
) -> dict[str, Any]:
    tenant = require_tenant(caller, tenant_id)
    try:
        payload = ObservationRequest(
            tenant_id=tenant,
            source=source,
            text=text,
            external_id=external_id,
            actor=actor if actor is not None else caller.identity,
            occurred_at=occurred_at,
            metadata=metadata or {},
            title_hint=title_hint,
            owner_hint=owner_hint,
            due_hint=due_hint,
            priority_hint=priority_hint,
        )
    except Exception as exc:
        raise McpInvalid(str(exc)) from exc
    try:
        result = runtime.service.ingest(ObservationInput(**payload.model_dump()))
    except ValueError as exc:
        raise McpInvalid(str(exc)) from exc
    encoded = jsonable_encoder(asdict(result))
    _fire_webhooks(runtime.state, "observation.ingested", encoded)
    _audit(
        runtime,
        caller,
        "mcp.observation.ingested",
        f"tenant:{tenant}",
        {"action": result.action, "source": source},
    )
    return encoded


def serve_review_work_item(
    runtime: McpRuntime,
    caller: McpCaller,
    work_item_id: str,
    tenant_id: str,
    action: str,
    actor: str,
    reason: str = "",
    resume_at: Any = None,
) -> dict[str, Any]:
    tenant = require_tenant(caller, tenant_id)
    if action not in {"approve", "reject", "snooze", "cancel", "resume"}:
        raise McpInvalid("action must be approve|reject|snooze|cancel|resume")
    if not actor or len(actor) > 512:
        raise McpInvalid("actor must be 1..512 characters")
    if len(reason) > 2048:
        raise McpInvalid("reason must be at most 2048 characters")
    try:
        runtime.service.get_work_item_detail(work_item_id, tenant)
        engine = runtime.transitions
        if action == "approve":
            item = engine.approve(work_item_id, actor=actor, reason=reason)
        elif action == "reject":
            item = engine.reject(work_item_id, actor=actor, reason=reason)
        elif action == "snooze":
            if resume_at is None:
                raise McpInvalid("resume_at is required for snooze")
            item = engine.snooze(
                work_item_id, actor=actor, resume_at=resume_at, reason=reason
            )
        elif action == "resume":
            item = engine.resume(work_item_id, actor=actor, reason=reason)
        else:
            item = engine.cancel(work_item_id, actor=actor, reason=reason)
    except KeyError as exc:
        raise _not_found(work_item_id) from exc
    except ValueError as exc:
        raise McpInvalid(str(exc)) from exc
    encoded = jsonable_encoder(asdict(item))
    _fire_webhooks(runtime.state, f"work_item.{action}", encoded)
    _audit(
        runtime,
        caller,
        f"mcp.work_item.{action}",
        f"work-item:{work_item_id}",
        {"tenant_id": tenant, "actor": actor},
    )
    return encoded


def serve_promote_work_item(
    runtime: McpRuntime,
    caller: McpCaller,
    work_item_id: str,
    tenant_id: str,
    actor: str,
    reason: str = "",
) -> dict[str, Any]:
    tenant = require_tenant(caller, tenant_id)
    if not actor or len(actor) > 512:
        raise McpInvalid("actor must be 1..512 characters")
    try:
        runtime.service.get_work_item_detail(work_item_id, tenant)
        item = runtime.transitions.promote_to_works(
            work_item_id, actor=actor, reason=reason
        )
    except KeyError as exc:
        raise _not_found(work_item_id) from exc
    except PermissionError as exc:
        raise McpForbidden(str(exc)) from exc
    except ValueError as exc:
        raise McpInvalid(str(exc)) from exc
    encoded = jsonable_encoder(asdict(item))
    _fire_webhooks(runtime.state, "work_item.promoted", encoded)
    _audit(
        runtime,
        caller,
        "mcp.work_item.promoted",
        f"work-item:{work_item_id}",
        {"tenant_id": tenant, "actor": actor},
    )
    return encoded


def serve_merge_work_items(
    runtime: McpRuntime,
    caller: McpCaller,
    work_item_id: str,
    tenant_id: str,
    target_work_item_id: str,
    actor: str,
    reason: str = "",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Merge duplicate into canonical item.

    Faithful port of the REST merge flow (same idempotency + replay
    semantics); the endpoint body stays the canonical spec.
    """
    tenant = require_tenant(caller, tenant_id)
    if not actor or len(actor) > 512:
        raise McpInvalid("actor must be 1..512 characters")
    if work_item_id == target_work_item_id:
        raise McpInvalid("cannot merge a work item into itself")
    if idempotency_key is not None and not (8 <= len(idempotency_key) <= 128):
        raise McpInvalid("idempotency_key must be 8..128 characters")
    try:
        source = runtime.service.get_work_item_detail(work_item_id, tenant)
        runtime.service.get_work_item_detail(target_work_item_id, tenant)
    except KeyError as exc:
        raise _not_found(work_item_id) from exc
    engine = runtime.transitions
    store = runtime.store
    reason = reason or f"merged into {target_work_item_id}"
    key = idempotency_key
    previous_state = source.work_item.status

    def evidence(item: Any, replay: bool) -> dict[str, Any]:
        return {
            "actor": actor,
            "authenticated_credential": caller.identity,
            "tenant_id": tenant,
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
            "auth_method": caller.method,
            "trace_id": f"merge_{uuid.uuid4().hex[:12]}",
        }

    def respond(item: Any, replay: bool) -> dict[str, Any]:
        encoded = jsonable_encoder(asdict(item)) if not isinstance(item, dict) else item
        encoded["merged_into_work_item_id"] = target_work_item_id
        return {"work_item": encoded, "evidence": evidence(encoded, replay)}

    if key:
        for prior in store.find_transition_by_idempotency_key(key):
            if prior.work_item_id != work_item_id or prior.reason != reason:
                raise McpInvalid("idempotency key already used for a different merge")
    if previous_state == "CANCELLED":
        last = engine.last_transition(work_item_id)
        if last is not None and last.to_state == "CANCELLED" and last.reason == reason:
            return respond(source.work_item, replay=True)
        raise McpInvalid("work item already cancelled for another reason")
    try:
        item = engine.cancel(
            work_item_id, actor=actor, reason=reason, idempotency_key=key
        )
    except KeyError as exc:
        raise _not_found(work_item_id) from exc
    except ValueError as exc:
        raise McpInvalid(str(exc)) from exc
    result = respond(item, replay=False)
    _fire_webhooks(runtime.state, "work_item.merged", jsonable_encoder(result))
    _audit(
        runtime,
        caller,
        "mcp.work_item.merged",
        f"work-item:{work_item_id}",
        {"tenant_id": tenant, "target": target_work_item_id, "actor": actor},
    )
    return result


def serve_publish_work_item(
    runtime: McpRuntime,
    caller: McpCaller,
    work_item_id: str,
    tenant_id: str,
    destination: str,
) -> dict[str, Any]:
    tenant = require_tenant(caller, tenant_id)
    if not destination or len(destination) > 128:
        raise McpInvalid("destination must be 1..128 characters")
    pub = runtime.publisher
    if pub is None:
        raise McpUpstream("no publisher destinations configured")
    try:
        detail = runtime.service.get_work_item_detail(work_item_id, tenant)
    except KeyError as exc:
        raise _not_found(work_item_id) from exc
    try:
        receipt = pub.publish(destination, detail.work_item, detail.observations)
    except KeyError as exc:
        raise McpInvalid(str(exc)) from exc
    except RuntimeError as exc:
        raise McpUpstream(str(exc)) from exc
    publication = Publication(
        id=f"pub_{uuid.uuid4().hex}",
        work_item_id=work_item_id,
        destination=receipt.destination,
        external_id=receipt.external_id,
        response=receipt.response or {},
        published_at=utc_now(),
    )
    runtime.store.save_publication(publication)
    _audit(
        runtime,
        caller,
        "mcp.work_item.published",
        f"work-item:{work_item_id}",
        {"tenant_id": tenant, "destination": destination},
    )
    return jsonable_encoder(asdict(publication))


def serve_bulk_status(
    runtime: McpRuntime,
    caller: McpCaller,
    work_item_ids: list[str],
    tenant_id: str,
) -> dict[str, Any]:
    tenant = require_tenant(caller, tenant_id)
    if not work_item_ids or len(work_item_ids) > 100:
        raise McpInvalid("work_item_ids must hold 1..100 ids")
    items = []
    for item_id in work_item_ids:
        item = runtime.store.get_work_item(item_id, tenant)
        if item is None:
            items.append(
                {"id": item_id, "status": "NOT_FOUND", "title": None, "priority": None}
            )
        else:
            items.append(
                {
                    "id": item.id,
                    "status": item.status,
                    "title": item.title,
                    "priority": item.priority,
                }
            )
    return {"items": items, "count": len(items)}


def serve_get_evidence(
    runtime: McpRuntime, caller: McpCaller, work_item_id: str, tenant_id: str
) -> dict[str, Any]:
    tenant = require_tenant(caller, tenant_id)
    try:
        detail = runtime.service.get_work_item_detail(work_item_id, tenant)
    except KeyError as exc:
        raise _not_found(work_item_id) from exc
    payload = {
        "tenant_id": detail.work_item.tenant_id,
        "work_item_id": detail.work_item.id,
        "title": detail.work_item.title,
        "canonical_key": detail.work_item.canonical_key,
        "observations": [
            {
                "id": o.id,
                "source": o.source,
                "external_id": o.external_id,
                "actor": o.actor,
                "occurred_at": o.occurred_at.isoformat() if o.occurred_at else None,
                "text": o.text,
            }
            for o in detail.observations
        ],
    }
    return jsonable_encoder(build_evidence(payload, secret=runtime.evidence_secret))


def serve_get_transitions(
    runtime: McpRuntime, caller: McpCaller, work_item_id: str, tenant_id: str
) -> dict[str, Any]:
    tenant = require_tenant(caller, tenant_id)
    item = runtime.store.get_work_item(work_item_id, tenant)
    if item is None:
        raise _not_found(work_item_id)
    transitions = runtime.store.list_transitions(work_item_id)
    return {
        "work_item_id": work_item_id,
        "transitions": [
            {
                "id": t.id,
                "from_status": t.from_state,
                "to_status": t.to_state,
                "action": "approve" if t.to_state == "APPROVED" else t.to_state.lower(),
                "actor": t.actor,
                "reason": t.reason,
                "created_at": t.at.isoformat() if t.at else None,
            }
            for t in transitions
        ],
        "count": len(transitions),
    }


def serve_get_publications(
    runtime: McpRuntime, caller: McpCaller, work_item_id: str, tenant_id: str
) -> dict[str, Any]:
    tenant = require_tenant(caller, tenant_id)
    item = runtime.store.get_work_item(work_item_id, tenant)
    if item is None:
        raise _not_found(work_item_id)
    publications = runtime.store.publications_for_work_item(work_item_id)
    return {
        "work_item_id": work_item_id,
        "publications": jsonable_encoder([asdict(p) for p in publications]),
        "count": len(publications),
    }


def serve_get_execution_status(
    runtime: McpRuntime, caller: McpCaller, work_item_id: str, tenant_id: str
) -> dict[str, Any]:
    from .publishers import PublishRouter, WorksPublisher

    tenant = require_tenant(caller, tenant_id)
    item = runtime.store.get_work_item(work_item_id, tenant)
    if item is None:
        raise _not_found(work_item_id)
    publications = runtime.store.publications_for_work_item(work_item_id)
    works_pubs = [p for p in publications if p.destination == "works" and p.external_id]
    if not works_pubs:
        raise McpNotFound("work item not published to works")
    pub = runtime.publisher
    works_pub = None
    if isinstance(pub, PublishRouter):
        candidate = pub._destinations.get("works")
        if isinstance(candidate, WorksPublisher):
            works_pub = candidate
    elif isinstance(pub, WorksPublisher):
        works_pub = pub
    if works_pub is None:
        raise McpUpstream("works destination not configured")
    latest = works_pubs[-1]
    try:
        status_payload = works_pub.get_work_status(latest.external_id)
    except KeyError as exc:
        raise McpNotFound(str(exc)) from exc
    except RuntimeError as exc:
        raise McpUpstream(str(exc)) from exc
    return {
        "work_item_id": work_item_id,
        "destination": "works",
        "external_id": latest.external_id,
        "publication_id": latest.id,
        "status": status_payload,
    }


def serve_list_actions(
    runtime: McpRuntime, caller: McpCaller, work_item_id: str, tenant_id: str
) -> dict[str, Any]:
    tenant = require_tenant(caller, tenant_id)
    item = runtime.store.get_work_item(work_item_id, tenant)
    if item is None:
        raise _not_found(work_item_id)
    actions: list[str] = []
    if item.status == "OPEN":
        actions = ["approve", "reject", "snooze", "cancel"]
    elif item.status == "SNOOZED":
        actions = ["resume", "cancel"]
    elif item.status == "APPROVED":
        actions = ["publish", "promote", "cancel"]
        policy = runtime.policy_store.get(tenant)
        if policy and policy.allow_works:
            actions.append("promote")
    return {"work_item_id": work_item_id, "status": item.status, "actions": actions}


def serve_list_observations(
    runtime: McpRuntime,
    caller: McpCaller,
    tenant_id: str,
    source: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    tenant = require_tenant(caller, tenant_id)
    if source is not None and (not source or len(source) > 64):
        raise McpInvalid("source must be 1..64 characters")
    if not 1 <= limit <= 1000:
        raise McpInvalid("limit must be 1..1000")

    store = runtime.store
    sql = "SELECT * FROM intake_observations WHERE tenant_id = ?"
    params: list[Any] = [tenant]
    if source:
        sql += " AND source = ?"
        params.append(source)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with store._lock:
        rows = store._db.execute(sql, params).fetchall()
    observations = [
        {
            "id": row["id"],
            "tenant_id": row["tenant_id"],
            "source": row["source"],
            "external_id": row["external_id"],
            "actor": row["actor"],
            "text": row["text"],
            "occurred_at": _dt(row["occurred_at"]).isoformat()
            if row["occurred_at"]
            else None,
            "created_at": _dt(row["created_at"]).isoformat()
            if row["created_at"]
            else None,
        }
        for row in rows
    ]
    return {"observations": observations, "count": len(observations)}


def serve_list_tenants(runtime: McpRuntime, caller: McpCaller) -> dict[str, Any]:
    # Tenant listing needs no tenant gate (it enumerates them); auth gate stays.
    _ = caller
    store = runtime.store
    with store._lock:
        rows = store._db.execute(
            "SELECT tenant_id, COUNT(*) as cnt FROM intake_work_items"
            " GROUP BY tenant_id ORDER BY tenant_id"
        ).fetchall()
    tenants = [{"tenant_id": row[0], "work_item_count": row[1]} for row in rows]
    return {"tenants": tenants, "count": len(tenants)}


def serve_get_context(runtime: McpRuntime, caller: McpCaller) -> dict[str, Any]:
    """Real caller context (the REST /context stub is static text)."""
    _ = runtime
    scopes = (
        ["admin", "read", "write", "delete"] if caller.is_admin else ["read", "write"]
    )
    return {
        "actor": caller.identity,
        "auth_method": caller.method,
        "bound_tenant": caller.bound_tenant,
        "role": "admin" if caller.is_admin else "operator",
        "permissions": scopes,
        "via": "mcp",
    }


def serve_evaluate_decision(
    runtime: McpRuntime,
    caller: McpCaller,
    request_id: str,
    tenant_id: str,
    repository: str,
    ref: str,
    head_sha: str,
    event_key: str,
    capability: str,
    objective: str,
    impact_summary: str,
    evidence: list[dict[str, Any]],
    tests_passed: bool = False,
    patch_release: bool = False,
    exported_signatures_changed: bool = False,
    critical_file_touched: bool = False,
    auth_or_secret_touched: bool = False,
    proxy_or_ssl_touched: bool = False,
    transient_ci_error: bool = False,
    canary_error_rate: float | None = None,
    superseded_head: bool = False,
    stale_review: bool = False,
    retry_count: int = 0,
    test_coverage_delta: int = 0,
    author_permission_tier: int = 0,
    critical_path_penalty: int = 0,
    line_churn_penalty: int = 0,
    changed_files: list[str] | None = None,
) -> dict[str, Any]:
    """Evaluate a bounded autonomy proposal. Never executes anything."""
    tenant = require_tenant(caller, tenant_id)
    try:
        payload = AutonomyEvaluateRequest(
            request_id=request_id,
            tenant_id=tenant,
            repository=repository,
            ref=ref,
            head_sha=head_sha,
            event_key=event_key,
            capability=cast(Capability, capability),
            objective=objective,
            impact_summary=impact_summary,
            evidence=evidence,
            tests_passed=tests_passed,
            patch_release=patch_release,
            exported_signatures_changed=exported_signatures_changed,
            critical_file_touched=critical_file_touched,
            auth_or_secret_touched=auth_or_secret_touched,
            proxy_or_ssl_touched=proxy_or_ssl_touched,
            transient_ci_error=transient_ci_error,
            canary_error_rate=canary_error_rate,
            superseded_head=superseded_head,
            stale_review=stale_review,
            retry_count=retry_count,
            test_coverage_delta=test_coverage_delta,
            author_permission_tier=author_permission_tier,
            critical_path_penalty=critical_path_penalty,
            line_churn_penalty=line_churn_penalty,
            changed_files=changed_files or [],
        )
    except Exception as exc:
        raise McpInvalid(str(exc)) from exc
    limiter = runtime.rate_limiter
    if limiter is not None and not limiter.is_allowed(
        f"autonomy:{caller.method}", "/v1/autonomy/decisions/evaluate"
    ):
        raise McpInvalid("Autonomy evaluation rate limit exceeded")
    evaluation = evaluate_autonomy(AutonomyEvaluationInput(**payload.model_dump()))
    _persist_autonomy_decision(runtime.store, payload, evaluation)
    _audit(
        runtime,
        caller,
        "mcp.autonomy.evaluated",
        f"tenant:{tenant}",
        {"request_id": request_id, "decision": evaluation.get("decision")},
    )
    return {
        "schema": evaluation["schema"],
        "request_id": evaluation["request_id"],
        "subject": evaluation["subject"],
        "capability": evaluation["capability"],
        "intent": evaluation["intent"],
        "risk": evaluation["risk"],
        "confidence": evaluation["confidence"],
        "decision": evaluation["decision"],
        "human_action": evaluation["human_action"],
        "evidence": evaluation["evidence"],
        "authority": evaluation["authority"],
        "blast_radius": evaluation.get("blast_radius", {}),
    }


def serve_decision_history(
    runtime: McpRuntime,
    caller: McpCaller,
    tenant_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    if tenant_id is not None:
        tenant_id = require_tenant(caller, tenant_id)
    limit = min(max(limit, 1), 200)
    store = runtime.store
    query = "SELECT * FROM autonomy_decisions"
    params: list[Any] = []
    if tenant_id:
        query += " WHERE tenant_id = ?"
        params.append(tenant_id)
    query += " ORDER BY evaluated_at DESC LIMIT ?"
    params.append(limit)
    rows = store._db.execute(query, params).fetchall()
    columns = [
        desc[0]
        for desc in store._db.execute(
            "SELECT * FROM autonomy_decisions LIMIT 0"
        ).description
    ]
    return {
        "schema": "aftergraph.autonomy-decision-history/1.0",
        "count": len(rows),
        "decisions": [dict(zip(columns, row, strict=True)) for row in rows],
    }


def serve_submit_task(
    runtime: McpRuntime,
    caller: McpCaller,
    name: str,
    args: list[Any] | None = None,
    kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not name or len(name) > 256:
        raise McpInvalid("name must be 1..256 characters")
    task = runtime.task_queue.submit(name, *(args or []), **(kwargs or {}))
    _audit(runtime, caller, "mcp.task.submitted", f"task:{task.id}", {"name": name})
    return {"task_id": task.id, "name": task.name, "status": task.status}


def serve_task_stats(runtime: McpRuntime, caller: McpCaller) -> dict[str, Any]:
    _ = caller
    return dict(runtime.task_queue.get_stats())


def serve_list_tasks(
    runtime: McpRuntime, caller: McpCaller, status: str | None = None
) -> list[dict[str, Any]]:
    from .tasks import TaskStatus

    _ = caller
    task_status = None
    if status:
        try:
            task_status = TaskStatus(status)
        except ValueError as exc:
            raise McpInvalid(f"unknown task status: {status}") from exc
    tasks = runtime.task_queue.list_tasks(status=task_status)
    return [{"id": t.id, "name": t.name, "status": t.status} for t in tasks]


def serve_get_task(
    runtime: McpRuntime, caller: McpCaller, task_id: str
) -> dict[str, Any]:
    _ = caller
    task = runtime.task_queue.get_task(task_id)
    if not task:
        raise McpNotFound("Task not found")
    return {
        "id": task.id,
        "name": task.name,
        "status": task.status,
        "result": task.result,
        "error": task.error,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "retries": task.retries,
    }


def serve_version(runtime: McpRuntime, caller: McpCaller) -> dict[str, Any]:
    _ = (runtime, caller)
    return {
        "version": "0.2.0",
        "build": "production",
        "status": "active",
        "features": [
            "adapters",
            "policies",
            "transitions",
            "publishers",
            "evidence",
            "metrics",
        ],
        "mcp": True,
    }


def serve_usage(runtime: McpRuntime, caller: McpCaller) -> dict[str, Any]:
    _ = caller
    stats = getattr(runtime.state, "usage_stats", {}) or {}
    return {
        "total_requests": stats.get("requests", 0),
        "total_errors": stats.get("errors", 0),
        "by_path": dict(stats.get("by_path", {})),
        "by_status": dict(stats.get("by_status", {})),
    }


def serve_readiness(runtime: McpRuntime, caller: McpCaller) -> dict[str, Any]:
    from datetime import UTC, datetime

    _ = caller
    checks = {"database": False, "policy_store": False, "publisher": False}
    try:
        with runtime.store._lock:
            runtime.store._db.execute("SELECT 1").fetchone()
        checks["database"] = True
    except Exception:
        pass
    try:
        runtime.policy_store.get("test")
        checks["policy_store"] = True
    except Exception:
        pass
    checks["publisher"] = runtime.publisher is not None
    return {
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "timestamp": datetime.now(UTC).isoformat() + "Z",
    }


def serve_audit_log(
    runtime: McpRuntime,
    caller: McpCaller,
    event: str | None = None,
    actor: str | None = None,
    target: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    if not 1 <= limit <= 1000:
        raise McpInvalid("limit must be 1..1000")
    entries = runtime.audit.query(event=event, actor=actor, target=target, limit=limit)
    return {"entries": jsonable_encoder(entries)}


def serve_audit_stats(runtime: McpRuntime, caller: McpCaller) -> dict[str, Any]:
    _ = caller
    return {"total_entries": runtime.audit.count()}


# ---------------------------------------------------------------------------
# Admin plane (Bearer [REDACTED] only — mirrors api.require_admin)
# ---------------------------------------------------------------------------


def serve_get_policy(
    runtime: McpRuntime, caller: McpCaller, tenant_id: str
) -> dict[str, Any]:
    tenant = require_tenant(caller, tenant_id)
    policy = runtime.policy_store.get(tenant)
    if policy is None:
        raise McpNotFound("tenant policy not found")
    return {"tenant_id": tenant, "policy": jsonable_encoder(policy)}


def serve_set_policy(
    runtime: McpRuntime,
    caller: McpCaller,
    tenant_id: str,
    allowed_sources: list[str] | None = None,
    allowed_destinations: list[str] | None = None,
    max_work_items: int | None = None,
    max_priority: str | None = None,
    allow_works: bool | None = None,
    dedupe_threshold: float | None = None,
    auto_create_work_items: bool | None = None,
    require_approval_for_promotion: bool | None = None,
) -> dict[str, Any]:
    from .policy import TenantPolicy

    require_admin(caller)
    tenant = require_tenant(caller, tenant_id)
    existing = runtime.policy_store.get(tenant)
    sources = (
        set(allowed_sources)
        if allowed_sources is not None
        else (existing.allowed_sources if existing else set())
    )
    destinations = (
        set(allowed_destinations)
        if allowed_destinations is not None
        else (existing.allowed_destinations if existing else None)
    )
    max_wi = (
        max_work_items
        if max_work_items is not None
        else (existing.max_work_items if existing else 100)
    )
    priority = max_priority or (existing.max_priority if existing else "high")
    works = (
        allow_works
        if allow_works is not None
        else (existing.allow_works if existing else False)
    )
    threshold = (
        dedupe_threshold
        if dedupe_threshold is not None
        else (existing.dedupe_threshold if existing else 0.72)
    )
    auto_create = (
        auto_create_work_items
        if auto_create_work_items is not None
        else (existing.auto_create_work_items if existing else True)
    )
    require_approval = (
        require_approval_for_promotion
        if require_approval_for_promotion is not None
        else (existing.require_approval_for_promotion if existing else True)
    )
    policy = TenantPolicy(
        allowed_sources=sources,
        allowed_destinations=destinations,
        max_work_items=max_wi,
        max_priority=priority,
        allow_works=works,
        dedupe_threshold=threshold,
        auto_create_work_items=auto_create,
        require_approval_for_promotion=require_approval,
    )
    runtime.policy_store.put(tenant, policy)
    try:
        runtime.store.upsert_tenant_policy(
            tenant_id=tenant,
            allowed_sources=list(sources),
            auto_create_work_items=auto_create,
            max_work_items=max_wi,
            max_priority=priority,
            dedupe_threshold=threshold,
            allow_works=works,
            allowed_destinations=list(destinations)
            if destinations is not None
            else None,
            require_approval_for_promotion=require_approval,
        )
        persisted = True
    except Exception:
        persisted = False
    _audit(
        runtime,
        caller,
        "mcp.policy.updated",
        f"tenant:{tenant}",
        {"persisted": persisted},
    )
    return {"tenant_id": tenant, "updated": True, "persisted": persisted}


def serve_delete_policy(
    runtime: McpRuntime, caller: McpCaller, tenant_id: str
) -> dict[str, Any]:
    require_admin(caller)
    tenant = require_tenant(caller, tenant_id)
    deleted = runtime.store.delete_tenant_policy(tenant)
    if not deleted:
        raise McpNotFound("persisted policy not found")
    _audit(runtime, caller, "mcp.policy.deleted", f"tenant:{tenant}", {})
    return {"tenant_id": tenant, "deleted": True}


def serve_list_policies(runtime: McpRuntime, caller: McpCaller) -> dict[str, Any]:
    _ = caller
    policies = runtime.store.list_tenant_policies()
    return {"policies": jsonable_encoder(policies), "count": len(policies)}


def serve_create_api_key(
    runtime: McpRuntime,
    caller: McpCaller,
    name: str,
    permissions: list[str] | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    require_admin(caller)
    if not name or len(name) > 128:
        raise McpInvalid("name must be 1..128 characters")
    bound = (tenant_id or "").strip() or None
    if bound is not None and len(bound) > 128:
        raise McpInvalid("invalid tenant_id")
    key_id = f"key_{uuid.uuid4().hex[:16]}"
    api_key = f"ak_{uuid.uuid4().hex}"
    prefix = api_key[:12]
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    record = runtime.store.create_api_key(
        key_id, name, key_hash, prefix, tenant_id=bound
    )
    _audit(
        runtime,
        caller,
        "mcp.api_key.created",
        f"key:{key_id}",
        {"name": name, "tenant_id": bound},
    )
    return {
        "id": key_id,
        "name": name,
        "key": api_key,
        "prefix": prefix,
        "permissions": permissions or ["read"],
        "tenant_id": bound,
        "created_at": record["created_at"],
        "active": True,
        "_warning": "Store this key securely. It will not be shown again.",
    }


def serve_list_api_keys(runtime: McpRuntime, caller: McpCaller) -> dict[str, Any]:
    _ = caller
    keys = runtime.store.list_api_keys()
    return {"keys": keys, "count": len(keys)}


def serve_rotate_api_key(
    runtime: McpRuntime, caller: McpCaller, key_id: str
) -> dict[str, Any]:
    require_admin(caller)
    store = runtime.store
    old_key = next((k for k in store.list_api_keys() if k["id"] == key_id), None)
    if not old_key:
        raise McpNotFound("API key not found")
    store.deactivate_api_key(key_id)
    new_key_id = f"key_{uuid.uuid4().hex[:16]}"
    api_key = f"ak_{uuid.uuid4().hex}"
    prefix = api_key[:12]
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    bound = old_key.get("tenant_id")
    store.create_api_key(new_key_id, old_key["name"], key_hash, prefix, tenant_id=bound)
    _audit(
        runtime, caller, "mcp.api_key.rotated", f"key:{key_id}", {"new_id": new_key_id}
    )
    return {
        "old_id": key_id,
        "new_id": new_key_id,
        "key": api_key,
        "prefix": prefix,
        "name": old_key["name"],
        "tenant_id": bound,
        "_warning": "Store this key securely. It will not be shown again.",
    }


def serve_revoke_api_key(
    runtime: McpRuntime, caller: McpCaller, key_id: str
) -> dict[str, Any]:
    require_admin(caller)
    ok = runtime.store.deactivate_api_key(key_id)
    if not ok:
        raise McpNotFound("API key not found")
    _audit(runtime, caller, "mcp.api_key.revoked", f"key:{key_id}", {})
    return {"status": "revoked", "id": key_id}


def serve_get_rate_limit(
    runtime: McpRuntime, caller: McpCaller, client_id: str | None = None
) -> dict[str, Any]:
    _ = caller
    limiter = runtime.rate_limiter
    if client_id:
        return dict(limiter.get_usage(client_id))
    return {
        "default_limit": limiter.default_limit,
        "key_count": len(limiter.key_limits),
    }


def serve_set_rate_limit(
    runtime: McpRuntime, caller: McpCaller, key: str, limit: int
) -> dict[str, Any]:
    require_admin(caller)
    if not key:
        raise McpInvalid("key must be non-empty")
    if not 1 <= limit <= 10000:
        raise McpInvalid("limit must be 1..10000")
    runtime.rate_limiter.set_key_limit(key, limit)
    _audit(runtime, caller, "mcp.rate_limit.updated", f"limit:{key}", {"limit": limit})
    return {"key": key, "limit": limit, "updated": True}


def serve_clear_cache(runtime: McpRuntime, caller: McpCaller) -> dict[str, Any]:
    require_admin(caller)
    cleared = runtime.cache.clear()
    _audit(runtime, caller, "mcp.cache.cleared", "cache", {"cleared": cleared})
    return {"cleared": cleared, "status": "ok"}


def serve_delete_cache_key(
    runtime: McpRuntime, caller: McpCaller, key: str
) -> dict[str, Any]:
    require_admin(caller)
    deleted = runtime.cache.delete(key)
    if not deleted:
        raise McpNotFound("Cache key not found")
    _audit(runtime, caller, "mcp.cache.key_deleted", f"cache:{key}", {})
    return {"deleted": key}


# ---------------------------------------------------------------------------
# Server assembly
# ---------------------------------------------------------------------------


class _LazyRuntime(McpRuntime):
    """Defer runtime resolution to first tool call.

    Application state (store, service, …) only fills during lifespan
    startup, so a factory lets the server mount at build time while every
    tool still sees the live state. Fail-closed when nothing is ready.

    Subclasses McpRuntime so the deferred holder satisfies the same static
    type as a ready runtime; attribute reads delegate to the live state.
    """

    def __init__(self, factory: Any) -> None:
        object.__setattr__(self, "_factory", factory)

    def _real(self) -> McpRuntime:
        try:
            return object.__getattribute__(self, "_factory")()
        except AttributeError as exc:
            raise McpUpstream("service not ready") from exc

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real(), name)


def mcp_transport_security_from_env() -> TransportSecuritySettings | None:
    """Build explicit DNS-rebinding settings from AFTERGRAPH_MCP_PUBLIC_HOST.

    Returns None when unset so the SDK keeps its loopback-only default.
    When set (comma-separated hosts), protection stays ON and admits those
    hosts — with and without port — plus loopback for local access. Without
    this the SDK silently disables the guard for any non-loopback host.
    """
    hosts = [
        h.strip()
        for h in os.getenv("AFTERGRAPH_MCP_PUBLIC_HOST", "").split(",")
        if h.strip()
    ]
    if not hosts:
        return None
    allowed_hosts = [
        "127.0.0.1:*",
        "localhost:*",
        "[::1]:*",
    ]
    allowed_origins = [
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
    ]
    for host in hosts:
        allowed_hosts.extend((host, f"{host}:*"))
        allowed_origins.extend((f"https://{host}", f"https://{host}:*"))
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


def build_mcp_server(runtime: McpRuntime | Callable[[], McpRuntime]) -> MCPServer:
    """Assemble the MCP server with the full tool surface.

    Accepts a ready runtime or a zero-arg factory (for build-time mounts
    whose application state only fills at startup).
    """
    mcp = MCPServer("aftergraph-work-intelligence")
    _rt = _LazyRuntime(runtime) if callable(runtime) else runtime

    @mcp.tool()
    async def wi_search(
        q: str, tenant_id: str, limit: int = 50, ctx: Context | None = None
    ) -> dict:
        """Search work items by title or summary."""
        return serve_search(_rt, _caller(_rt, ctx), q, tenant_id, limit)

    @mcp.tool()
    async def wi_list_work_items(
        tenant_id: str,
        status: str | None = None,
        priority: str | None = None,
        limit: int = 100,
        ctx: Context | None = None,
    ) -> dict:
        """List work items for a tenant, with optional status/priority filters."""
        return serve_list_work_items(
            _rt, _caller(_rt, ctx), tenant_id, status, priority, limit
        )

    @mcp.tool()
    async def wi_get_work_item(
        work_item_id: str, tenant_id: str, ctx: Context | None = None
    ) -> dict:
        """Get one work item with observations and publications."""
        return serve_get_work_item(_rt, _caller(_rt, ctx), work_item_id, tenant_id)

    @mcp.tool()
    async def wi_create_observation(
        tenant_id: str,
        source: str,
        text: str,
        external_id: str | None = None,
        actor: str | None = None,
        occurred_at: str | None = None,
        metadata: dict | None = None,
        title_hint: str | None = None,
        owner_hint: str | None = None,
        due_hint: str | None = None,
        priority_hint: str | None = None,
        ctx: Context | None = None,
    ) -> dict:
        """Ingest an observation (may create or merge a work item)."""
        return serve_create_observation(
            _rt,
            _caller(_rt, ctx),
            tenant_id,
            source,
            text,
            external_id,
            actor,
            occurred_at,
            metadata,
            title_hint,
            owner_hint,
            due_hint,
            priority_hint,
        )

    @mcp.tool()
    async def wi_list_observations(
        tenant_id: str,
        source: str | None = None,
        limit: int = 100,
        ctx: Context | None = None,
    ) -> dict:
        """List observations for a tenant, optionally filtered by source."""
        return serve_list_observations(_rt, _caller(_rt, ctx), tenant_id, source, limit)

    @mcp.tool()
    async def wi_review_work_item(
        work_item_id: str,
        tenant_id: str,
        action: str,
        actor: str,
        reason: str = "",
        resume_at: str | None = None,
        ctx: Context | None = None,
    ) -> dict:
        """Review a work item: approve, reject, snooze, resume or cancel."""
        return serve_review_work_item(
            _rt,
            _caller(_rt, ctx),
            work_item_id,
            tenant_id,
            action,
            actor,
            reason,
            resume_at,
        )

    @mcp.tool()
    async def wi_promote_work_item(
        work_item_id: str,
        tenant_id: str,
        actor: str,
        reason: str = "",
        ctx: Context | None = None,
    ) -> dict:
        """Promote a work item to works execution."""
        return serve_promote_work_item(
            _rt, _caller(_rt, ctx), work_item_id, tenant_id, actor, reason
        )

    @mcp.tool()
    async def wi_merge_work_items(
        work_item_id: str,
        tenant_id: str,
        target_work_item_id: str,
        actor: str,
        reason: str = "",
        idempotency_key: str | None = None,
        ctx: Context | None = None,
    ) -> dict:
        """Merge a duplicate work item into its canonical target (idempotent)."""
        return serve_merge_work_items(
            _rt,
            _caller(_rt, ctx),
            work_item_id,
            tenant_id,
            target_work_item_id,
            actor,
            reason,
            idempotency_key,
        )

    @mcp.tool()
    async def wi_publish_work_item(
        work_item_id: str, tenant_id: str, destination: str, ctx: Context | None = None
    ) -> dict:
        """Publish a work item to a configured destination."""
        return serve_publish_work_item(
            _rt, _caller(_rt, ctx), work_item_id, tenant_id, destination
        )

    @mcp.tool()
    async def wi_bulk_status(
        work_item_ids: list[str], tenant_id: str, ctx: Context | None = None
    ) -> dict:
        """Get status of up to 100 work items in one call."""
        return serve_bulk_status(_rt, _caller(_rt, ctx), work_item_ids, tenant_id)

    @mcp.tool()
    async def wi_get_evidence(
        work_item_id: str, tenant_id: str, ctx: Context | None = None
    ) -> dict:
        """Get the sealed evidence envelope for a work item."""
        return serve_get_evidence(_rt, _caller(_rt, ctx), work_item_id, tenant_id)

    @mcp.tool()
    async def wi_get_transitions(
        work_item_id: str, tenant_id: str, ctx: Context | None = None
    ) -> dict:
        """Get transition history for a work item."""
        return serve_get_transitions(_rt, _caller(_rt, ctx), work_item_id, tenant_id)

    @mcp.tool()
    async def wi_get_publications(
        work_item_id: str, tenant_id: str, ctx: Context | None = None
    ) -> dict:
        """Get publication history for a work item."""
        return serve_get_publications(_rt, _caller(_rt, ctx), work_item_id, tenant_id)

    @mcp.tool()
    async def wi_get_execution_status(
        work_item_id: str, tenant_id: str, ctx: Context | None = None
    ) -> dict:
        """Get live works-execution status for a published work item."""
        return serve_get_execution_status(
            _rt, _caller(_rt, ctx), work_item_id, tenant_id
        )

    @mcp.tool()
    async def wi_list_actions(
        work_item_id: str, tenant_id: str, ctx: Context | None = None
    ) -> dict:
        """List allowed actions for a work item in its current state."""
        return serve_list_actions(_rt, _caller(_rt, ctx), work_item_id, tenant_id)

    @mcp.tool()
    async def wi_decision_history(
        tenant_id: str | None = None, limit: int = 50, ctx: Context | None = None
    ) -> dict:
        """Read the autonomy decision audit trail (read-only)."""
        return serve_decision_history(_rt, _caller(_rt, ctx), tenant_id, limit)

    @mcp.tool()
    async def wi_list_tenants(ctx: Context | None = None) -> dict:
        """List all tenants with work item counts."""
        return serve_list_tenants(_rt, _caller(_rt, ctx))

    @mcp.tool()
    async def wi_submit_task(
        name: str,
        args: list | None = None,
        kwargs: dict | None = None,
        ctx: Context | None = None,
    ) -> dict:
        """Submit a background task."""
        return serve_submit_task(_rt, _caller(_rt, ctx), name, args, kwargs)

    @mcp.tool()
    async def wi_task_stats(ctx: Context | None = None) -> dict:
        """Get background task queue statistics."""
        return serve_task_stats(_rt, _caller(_rt, ctx))

    @mcp.tool()
    async def wi_get_task(task_id: str, ctx: Context | None = None) -> dict:
        """Get background task status and result."""
        return serve_get_task(_rt, _caller(_rt, ctx), task_id)

    @mcp.tool()
    async def wi_list_tasks(
        status: str | None = None, ctx: Context | None = None
    ) -> list:
        """List background tasks, optionally filtered by status."""
        return serve_list_tasks(_rt, _caller(_rt, ctx), status)

    @mcp.tool()
    async def wi_version(ctx: Context | None = None) -> dict:
        """Service version and feature flags."""
        return serve_version(_rt, _caller(_rt, ctx))

    @mcp.tool()
    async def wi_usage(ctx: Context | None = None) -> dict:
        """API usage statistics."""
        return serve_usage(_rt, _caller(_rt, ctx))

    @mcp.tool()
    async def wi_readiness(ctx: Context | None = None) -> dict:
        """Integration health/readiness checks."""
        return serve_readiness(_rt, _caller(_rt, ctx))

    @mcp.tool()
    async def wi_audit_log(
        event: str | None = None,
        actor: str | None = None,
        target: str | None = None,
        limit: int = 100,
        ctx: Context | None = None,
    ) -> dict:
        """Query audit log entries."""
        return serve_audit_log(_rt, _caller(_rt, ctx), event, actor, target, limit)

    @mcp.tool()
    async def wi_audit_stats(ctx: Context | None = None) -> dict:
        """Audit log statistics."""
        return serve_audit_stats(_rt, _caller(_rt, ctx))

    @mcp.tool()
    async def wi_evaluate_decision(
        request_id: str,
        tenant_id: str,
        repository: str,
        ref: str,
        head_sha: str,
        event_key: str,
        capability: str,
        objective: str,
        impact_summary: str,
        evidence: list[dict],
        tests_passed: bool = False,
        patch_release: bool = False,
        exported_signatures_changed: bool = False,
        critical_file_touched: bool = False,
        auth_or_secret_touched: bool = False,
        proxy_or_ssl_touched: bool = False,
        transient_ci_error: bool = False,
        canary_error_rate: float | None = None,
        superseded_head: bool = False,
        stale_review: bool = False,
        retry_count: int = 0,
        test_coverage_delta: int = 0,
        author_permission_tier: int = 0,
        critical_path_penalty: int = 0,
        line_churn_penalty: int = 0,
        changed_files: list[str] | None = None,
        ctx: Context | None = None,
    ) -> dict:
        """Evaluate a bounded autonomy proposal. Never executes anything."""
        return serve_evaluate_decision(
            _rt,
            _caller(_rt, ctx),
            request_id,
            tenant_id,
            repository,
            ref,
            head_sha,
            event_key,
            capability,
            objective,
            impact_summary,
            evidence,
            tests_passed,
            patch_release,
            exported_signatures_changed,
            critical_file_touched,
            auth_or_secret_touched,
            proxy_or_ssl_touched,
            transient_ci_error,
            canary_error_rate,
            superseded_head,
            stale_review,
            retry_count,
            test_coverage_delta,
            author_permission_tier,
            critical_path_penalty,
            line_churn_penalty,
            changed_files,
        )

    @mcp.tool()
    async def wi_get_policy(tenant_id: str, ctx: Context | None = None) -> dict:
        """Get a tenant's policy."""
        return serve_get_policy(_rt, _caller(_rt, ctx), tenant_id)

    @mcp.tool()
    async def wi_set_policy(
        tenant_id: str,
        allowed_sources: list[str] | None = None,
        allowed_destinations: list[str] | None = None,
        max_work_items: int | None = None,
        max_priority: str | None = None,
        allow_works: bool | None = None,
        dedupe_threshold: float | None = None,
        auto_create_work_items: bool | None = None,
        require_approval_for_promotion: bool | None = None,
        ctx: Context | None = None,
    ) -> dict:
        """Create or update a tenant policy (admin only)."""
        return serve_set_policy(
            _rt,
            _caller(_rt, ctx),
            tenant_id,
            allowed_sources,
            allowed_destinations,
            max_work_items,
            max_priority,
            allow_works,
            dedupe_threshold,
            auto_create_work_items,
            require_approval_for_promotion,
        )

    @mcp.tool()
    async def wi_delete_policy(tenant_id: str, ctx: Context | None = None) -> dict:
        """Delete a tenant's persisted policy (admin only)."""
        return serve_delete_policy(_rt, _caller(_rt, ctx), tenant_id)

    @mcp.tool()
    async def wi_list_policies(ctx: Context | None = None) -> dict:
        """List all persisted tenant policies."""
        return serve_list_policies(_rt, _caller(_rt, ctx))

    @mcp.tool()
    async def wi_create_api_key(
        name: str,
        permissions: list[str] | None = None,
        tenant_id: str | None = None,
        ctx: Context | None = None,
    ) -> dict:
        """Create an API key, optionally bound to one tenant (admin only)."""
        return serve_create_api_key(
            _rt, _caller(_rt, ctx), name, permissions, tenant_id
        )

    @mcp.tool()
    async def wi_list_api_keys(ctx: Context | None = None) -> dict:
        """List API keys without secrets."""
        return serve_list_api_keys(_rt, _caller(_rt, ctx))

    @mcp.tool()
    async def wi_rotate_api_key(key_id: str, ctx: Context | None = None) -> dict:
        """Rotate an API key, preserving its tenant binding (admin only)."""
        return serve_rotate_api_key(_rt, _caller(_rt, ctx), key_id)

    @mcp.tool()
    async def wi_revoke_api_key(key_id: str, ctx: Context | None = None) -> dict:
        """Revoke an API key (admin only)."""
        return serve_revoke_api_key(_rt, _caller(_rt, ctx), key_id)

    @mcp.tool()
    async def wi_get_rate_limit(
        client_id: str | None = None, ctx: Context | None = None
    ) -> dict:
        """Check rate limit status."""
        return serve_get_rate_limit(_rt, _caller(_rt, ctx), client_id)

    @mcp.tool()
    async def wi_set_rate_limit(
        key: str, limit: int, ctx: Context | None = None
    ) -> dict:
        """Set a custom rate limit for a key (admin only)."""
        return serve_set_rate_limit(_rt, _caller(_rt, ctx), key, limit)

    @mcp.tool()
    async def wi_clear_cache(ctx: Context | None = None) -> dict:
        """Clear all cache entries (admin only)."""
        return serve_clear_cache(_rt, _caller(_rt, ctx))

    @mcp.tool()
    async def wi_delete_cache_key(key: str, ctx: Context | None = None) -> dict:
        """Delete one cache entry (admin only)."""
        return serve_delete_cache_key(_rt, _caller(_rt, ctx), key)

    return mcp
