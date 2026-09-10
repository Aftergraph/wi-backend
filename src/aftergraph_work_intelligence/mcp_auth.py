"""mcp_auth: Credential resolution and per-tenant/admin gates for MCP tools."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from mcp.shared.exceptions import MCPError


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
