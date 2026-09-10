"""mcp_core: Runtime assembly, lazy lifespan holder, audit helper, transport guard."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from mcp.server.mcpserver.context import Context
from mcp.server.transport_security import TransportSecuritySettings

from .mcp_auth import (
    McpCaller,
    McpNotFound,
    McpUpstream,
    _headers_lower,
    resolve_caller,
)


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
