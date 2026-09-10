"""mcp_data: Tenant-gated data-plane serve_* functions (mirror /v1 shapes)."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Any, cast

from fastapi.encoders import jsonable_encoder

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
from .mcp_auth import (
    McpCaller,
    McpForbidden,
    McpInvalid,
    McpNotFound,
    McpUpstream,
    require_tenant,
)
from .mcp_core import McpRuntime, _audit, _not_found
from .merge_kernel import (
    MergeConflict,
    MergeCredential,
    MergeInvalid,
    MergeNotFound,
    result_as_dict,
    run_merge,
)
from .models import ObservationInput, Publication, utc_now


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

    Thin adapter over merge_kernel.run_merge (shared with REST); errors
    map to Mcp* with identical messages.
    """
    tenant = require_tenant(caller, tenant_id)
    try:
        result = run_merge(
            service=runtime.service,
            engine=runtime.transitions,
            store=runtime.store,
            tenant_id=tenant,
            work_item_id=work_item_id,
            target_work_item_id=target_work_item_id,
            actor=actor,
            reason=reason,
            idempotency_key=idempotency_key,
            credential=MergeCredential(
                authenticated_credential=caller.identity,
                auth_method=caller.method,
                trace_id=f"merge_{uuid.uuid4().hex[:12]}",
            ),
            fire=lambda event, payload: _fire_webhooks(runtime.state, event, payload),
        )
    except MergeNotFound as exc:
        raise _not_found(work_item_id) from exc
    except (MergeConflict, MergeInvalid) as exc:
        raise McpInvalid(str(exc)) from exc
    _audit(
        runtime,
        caller,
        "mcp.work_item.merged",
        f"work-item:{work_item_id}",
        {"tenant_id": tenant, "target": target_work_item_id, "actor": actor},
    )
    return result_as_dict(result)


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
