"""mcp_admin: Bearer-only admin-plane serve_* functions (policies, keys, limits, cache)."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from fastapi.encoders import jsonable_encoder

from .mcp_auth import (
    McpCaller,
    McpInvalid,
    McpNotFound,
    require_admin,
    require_tenant,
)
from .mcp_core import McpRuntime, _audit


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
