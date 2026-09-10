"""MCP server over Work Intelligence (approach A, full scope).

ponytail: thin facade — auth, runtime, data tools and admin tools live in
mcp_auth/mcp_core/mcp_data/mcp_admin; this module keeps build_mcp_server
plus the public re-export surface (tests and secure_api import from here).
Ceiling: no new tools or behavior in this file.
"""

from __future__ import annotations

from collections.abc import Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context

from .mcp_admin import (
    serve_clear_cache,
    serve_create_api_key,
    serve_delete_cache_key,
    serve_delete_policy,
    serve_get_policy,
    serve_get_rate_limit,
    serve_list_api_keys,
    serve_list_policies,
    serve_revoke_api_key,
    serve_rotate_api_key,
    serve_set_policy,
    serve_set_rate_limit,
)
from .mcp_auth import (
    McpCaller,
    McpForbidden,
    McpInvalid,
    McpNotFound,
    McpUpstream,
    require_admin,
    require_tenant,
    resolve_caller,
)
from .mcp_core import (
    McpRuntime,
    _caller,
    _LazyRuntime,
    make_runtime,
    mcp_transport_security_from_env,
)
from .mcp_data import (
    serve_audit_log,
    serve_audit_stats,
    serve_bulk_status,
    serve_create_observation,
    serve_decision_history,
    serve_evaluate_decision,
    serve_get_context,
    serve_get_evidence,
    serve_get_execution_status,
    serve_get_publications,
    serve_get_task,
    serve_get_transitions,
    serve_get_work_item,
    serve_list_actions,
    serve_list_observations,
    serve_list_tasks,
    serve_list_tenants,
    serve_list_work_items,
    serve_merge_work_items,
    serve_promote_work_item,
    serve_publish_work_item,
    serve_readiness,
    serve_review_work_item,
    serve_search,
    serve_submit_task,
    serve_task_stats,
    serve_usage,
    serve_version,
)

__all__ = [
    "McpCaller",
    "McpForbidden",
    "McpInvalid",
    "McpNotFound",
    "McpRuntime",
    "McpUpstream",
    "build_mcp_server",
    "make_runtime",
    "mcp_transport_security_from_env",
    "require_admin",
    "require_tenant",
    "resolve_caller",
    "serve_audit_log",
    "serve_audit_stats",
    "serve_bulk_status",
    "serve_clear_cache",
    "serve_create_api_key",
    "serve_create_observation",
    "serve_decision_history",
    "serve_delete_cache_key",
    "serve_delete_policy",
    "serve_evaluate_decision",
    "serve_get_context",
    "serve_get_evidence",
    "serve_get_execution_status",
    "serve_get_policy",
    "serve_get_publications",
    "serve_get_rate_limit",
    "serve_get_task",
    "serve_get_transitions",
    "serve_get_work_item",
    "serve_list_actions",
    "serve_list_api_keys",
    "serve_list_observations",
    "serve_list_policies",
    "serve_list_tasks",
    "serve_list_tenants",
    "serve_list_work_items",
    "serve_merge_work_items",
    "serve_promote_work_item",
    "serve_publish_work_item",
    "serve_readiness",
    "serve_review_work_item",
    "serve_revoke_api_key",
    "serve_rotate_api_key",
    "serve_search",
    "serve_set_policy",
    "serve_set_rate_limit",
    "serve_submit_task",
    "serve_task_stats",
    "serve_usage",
    "serve_version",
]


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
