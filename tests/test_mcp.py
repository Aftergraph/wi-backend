"""Tests for the MCP server over Work Intelligence (approach A, full scope).

RED: these fail until mcp_server.py exists with the full tool surface,
the /mcp mount, and per-tenant + admin gating.

Notes:
- No static credential literals: the master token is generated per test
  run (a scrubber in the tool pipeline rewrites static Bearer literals,
  which once caused an impossible compare_digest mismatch).
- No async tests: the repo has no async plugin, so coroutines run inside
  sync tests via asyncio.run (skipped coroutines would be false green).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from aftergraph_work_intelligence.mcp_server import (
    McpForbidden,
    build_mcp_server,
    make_runtime,
    mcp_transport_security_from_env,
    require_admin,
    require_tenant,
    resolve_caller,
)
from aftergraph_work_intelligence.secure_api import create_app as create_secure_app

EXPECTED_TOOLS = {
    "wi_search",
    "wi_list_work_items",
    "wi_get_work_item",
    "wi_create_observation",
    "wi_list_observations",
    "wi_review_work_item",
    "wi_promote_work_item",
    "wi_merge_work_items",
    "wi_publish_work_item",
    "wi_bulk_status",
    "wi_get_evidence",
    "wi_get_transitions",
    "wi_get_publications",
    "wi_get_execution_status",
    "wi_list_actions",
    "wi_evaluate_decision",
    "wi_decision_history",
    "wi_list_tenants",
    "wi_submit_task",
    "wi_task_stats",
    "wi_get_task",
    "wi_list_tasks",
    "wi_version",
    "wi_usage",
    "wi_readiness",
    "wi_audit_log",
    "wi_audit_stats",
    "wi_get_policy",
    "wi_set_policy",
    "wi_delete_policy",
    "wi_list_policies",
    "wi_create_api_key",
    "wi_list_api_keys",
    "wi_rotate_api_key",
    "wi_revoke_api_key",
    "wi_get_rate_limit",
    "wi_set_rate_limit",
    "wi_clear_cache",
    "wi_delete_cache_key",
}


@pytest.fixture()
def master_token():
    return f"test-master-{uuid.uuid4().hex[:12]}"


@pytest.fixture()
def bearer(master_token):
    return {"Authorization": f"Bearer {master_token}"}


@pytest.fixture()
def app_data(tmp_path, master_token):
    app = create_secure_app(db_path=tmp_path / "test.db", api_token=master_token)
    return app


@pytest.fixture()
def client(app_data):
    with TestClient(app_data) as c:
        yield c


@pytest.fixture()
def runtime(app_data, client):
    # Depends on client so lifespan startup has filled app.state.
    return make_runtime(app_data)


def _tool_names(runtime):
    async def go():
        server = build_mcp_server(runtime)
        tools = await server.list_tools()
        return {t.name for t in tools}

    return asyncio.run(go())


class TestMcpSurface:
    def test_mcp_tools_registered(self, runtime):
        assert EXPECTED_TOOLS <= _tool_names(runtime)

    def test_mcp_mount_requires_auth(self, client):
        resp = client.post("/mcp", json={})
        assert resp.status_code == 401


class TestMcpTenantGate:
    def test_bound_key_restricted_to_tenant(
        self, client, runtime, bearer, master_token
    ):
        created = client.post(
            "/v1/api-keys",
            json={"name": "tenant-a-key", "tenant_id": "tenant-a"},
            headers=bearer,
        )
        assert created.status_code == 201
        key = created.json()["key"]
        caller = resolve_caller(
            {"authorization": "", "x-api-key": key},
            runtime.store,
            master_token,
        )
        assert require_tenant(caller, "tenant-a") == "tenant-a"
        with pytest.raises(McpForbidden):
            require_tenant(caller, "tenant-b")

    def test_bearer_may_use_any_explicit_tenant(self, runtime, bearer, master_token):
        caller = resolve_caller(
            {"authorization": bearer["Authorization"], "x-api-key": ""},
            runtime.store,
            master_token,
        )
        assert require_tenant(caller, "tenant-a") == "tenant-a"

    def test_unknown_key_rejected(self, runtime, master_token):
        with pytest.raises(McpForbidden):
            resolve_caller(
                {"authorization": "", "x-api-key": "ak_invalidkey123456"},
                runtime.store,
                master_token,
            )


class TestMcpAdminGate:
    def test_admin_tools_require_bearer(self, client, runtime, bearer, master_token):
        created = client.post(
            "/v1/api-keys",
            json={"name": "plain-key"},
            headers=bearer,
        )
        key = created.json()["key"]
        caller = resolve_caller(
            {"authorization": "", "x-api-key": key}, runtime.store, master_token
        )
        with pytest.raises(McpForbidden):
            require_admin(caller)
        admin = resolve_caller(
            {"authorization": bearer["Authorization"], "x-api-key": ""},
            runtime.store,
            master_token,
        )
        require_admin(admin)


def _sse_payloads(resp):
    import json

    out = []
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            out.append(json.loads(line[len("data: ") :]))
    return out


class TestMcpTransport:
    """Full JSON-RPC dance through the mounted app (real headers, sessions)."""

    @pytest.fixture()
    def mcp_client(self, app_data):
        with TestClient(app_data, base_url="http://127.0.0.1:8000") as c:
            yield c

    def _session(self, mcp_client, bearer):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **bearer,
        }
        init = mcp_client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            },
        )
        assert init.status_code == 200
        session = init.headers.get("mcp-session-id")
        assert session
        note = mcp_client.post(
            "/mcp",
            headers={**headers, "mcp-session-id": session},
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert note.status_code in (200, 202)
        return {**headers, "mcp-session-id": session}

    def test_initialize_lists_and_calls_tools(self, mcp_client, bearer):
        headers = self._session(mcp_client, bearer)
        listed = mcp_client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        assert listed.status_code == 200
        names = {t["name"] for p in _sse_payloads(listed) for t in p["result"]["tools"]}
        assert EXPECTED_TOOLS <= names
        schemas = {
            t["name"]: t.get("inputSchema", {})
            for p in _sse_payloads(listed)
            for t in p["result"]["tools"]
        }
        assert "ctx" not in schemas["wi_create_observation"].get("properties", {})

        called = mcp_client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "wi_version", "arguments": {}},
            },
        )
        assert called.status_code == 200
        results = _sse_payloads(called)
        import json as _json

        version = _json.loads(results[0]["result"]["content"][0]["text"])
        assert version["version"] == "0.2.0"

        tenants = mcp_client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "wi_list_tenants", "arguments": {}},
            },
        )
        assert tenants.status_code == 200
        listed_tenants = _json.loads(
            _sse_payloads(tenants)[0]["result"]["content"][0]["text"]
        )
        assert "tenants" in listed_tenants

    def test_transport_rejects_unauthenticated(self, mcp_client):
        resp = mcp_client.post(
            "/mcp",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert resp.status_code == 401


class TestMcpMigration:
    def test_migration_6_adds_tenant_binding(self, tmp_path):
        import sqlite3

        from aftergraph_work_intelligence.migrations import run_migrations
        from aftergraph_work_intelligence.store import SQLiteStore

        db = tmp_path / "v5.db"
        SQLiteStore(db)  # full current schema
        conn = sqlite3.connect(db)
        conn.execute("DROP INDEX IF EXISTS idx_api_keys_tenant")
        conn.execute("ALTER TABLE api_keys DROP COLUMN tenant_id")
        conn.commit()
        conn.close()
        result = run_migrations(db_path=db)
        assert result["ok"]
        assert result["current_version"] >= 6
        conn = sqlite3.connect(db)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(api_keys)").fetchall()]
        conn.close()
        assert "tenant_id" in cols


class TestMcpBehavior:
    def test_ingest_then_list_end_to_end(self, runtime, bearer, master_token):
        from aftergraph_work_intelligence.mcp_server import (
            serve_create_observation,
            serve_list_work_items,
        )

        caller = resolve_caller(
            {"authorization": bearer["Authorization"], "x-api-key": ""},
            runtime.store,
            master_token,
        )
        created = serve_create_observation(
            runtime,
            caller,
            tenant_id="tenant-a",
            source="conversation",
            text="Vi skal teste MCP-serverens ingest-sti før fredag",
        )
        assert created["action"] in {"created", "observed", "merged", "replayed"}
        listed = serve_list_work_items(runtime, caller, tenant_id="tenant-a", limit=10)
        assert listed["tenant_id"] == "tenant-a"
        assert listed["count"] >= 1

    def test_merge_replay_is_idempotent(self, runtime, bearer, master_token):
        from aftergraph_work_intelligence.mcp_server import (
            serve_create_observation,
            serve_list_work_items,
            serve_merge_work_items,
        )

        caller = resolve_caller(
            {"authorization": bearer["Authorization"], "x-api-key": ""},
            runtime.store,
            master_token,
        )
        items = serve_list_work_items(runtime, caller, tenant_id="tenant-b", limit=10)
        assert items["count"] == 0
        first = serve_create_observation(
            runtime,
            caller,
            tenant_id="tenant-b",
            source="conversation",
            text="Vi skal købe parfumefri rengøringsmidler før mandag",
        )
        second = serve_create_observation(
            runtime,
            caller,
            tenant_id="tenant-b",
            source="conversation",
            text="Vi skal sende kunden en bekræftelse",
        )
        assert first["action"] == "created"
        assert second["action"] == "created"
        first_id = first["work_item"]["id"]
        second_id = second["work_item"]["id"]
        assert first_id != second_id
        key = "mcp-test-key-12345"
        merged = serve_merge_work_items(
            runtime,
            caller,
            second_id,
            "tenant-b",
            first_id,
            actor="mcp-test",
            reason="duplikat",
            idempotency_key=key,
        )
        assert merged["evidence"]["idempotent_replay"] is False
        replayed = serve_merge_work_items(
            runtime,
            caller,
            second_id,
            "tenant-b",
            first_id,
            actor="mcp-test",
            reason="duplikat",
            idempotency_key=key,
        )
        assert replayed["evidence"]["idempotent_replay"] is True
        assert (
            replayed["work_item"]["status"]
            == merged["work_item"]["status"]
            == "CANCELLED"
        )

    def test_admin_round_trip(self, runtime, bearer, master_token):
        from aftergraph_work_intelligence.mcp_server import (
            serve_create_api_key,
            serve_delete_policy,
            serve_get_policy,
            serve_list_api_keys,
            serve_revoke_api_key,
            serve_rotate_api_key,
            serve_set_policy,
        )

        admin = resolve_caller(
            {"authorization": bearer["Authorization"], "x-api-key": ""},
            runtime.store,
            master_token,
        )
        policy = serve_set_policy(
            runtime, admin, "tenant-c", max_work_items=10, allow_works=False
        )
        assert policy["updated"] is True
        fetched = serve_get_policy(runtime, admin, "tenant-c")
        assert fetched["tenant_id"] == "tenant-c"
        created = serve_create_api_key(
            runtime, admin, name="bound-key", tenant_id="tenant-c"
        )
        assert created["tenant_id"] == "tenant-c"
        listed = serve_list_api_keys(runtime, admin)
        assert any(k["id"] == created["id"] for k in listed["keys"])
        rotated = serve_rotate_api_key(runtime, admin, created["id"])
        assert rotated["tenant_id"] == "tenant-c"
        revoked = serve_revoke_api_key(runtime, admin, rotated["new_id"])
        assert revoked["status"] == "revoked"
        with pytest.raises(McpForbidden):
            resolve_caller(
                {"authorization": "", "x-api-key": rotated["key"]},
                runtime.store,
                master_token,
            )
        deleted = serve_delete_policy(runtime, admin, "tenant-c")
        assert deleted["deleted"] is True

    def test_evaluate_decision_envelope(self, runtime, bearer, master_token):
        from aftergraph_work_intelligence.mcp_server import serve_evaluate_decision

        caller = resolve_caller(
            {"authorization": bearer["Authorization"], "x-api-key": ""},
            runtime.store,
            master_token,
        )
        result = serve_evaluate_decision(
            runtime,
            caller,
            request_id="adr_0123456789abcdef",
            tenant_id="tenant-d",
            repository="owner/repo",
            ref="refs/heads/main",
            head_sha="abcdef1",
            event_key="push",
            capability="none",
            objective="Routine dependency bump",
            impact_summary="No behavior change",
            evidence=[{"type": "ci", "result": "pass"}],
            tests_passed=True,
        )
        assert result["decision"] in {
            "auto_approve",
            "auto_retry",
            "blocked",
            "prepare_rollback",
            "requires_human_signoff",
        }
        assert result["authority"]["execution_authority"] == "evaluation-only"


class TestMcpBarePath:
    """Strict HTTP clients must reach the tools on bare /mcp (no 307)."""

    def test_bare_mcp_path_served_without_redirect(
        self, tmp_path, master_token, bearer
    ):
        app = create_secure_app(db_path=tmp_path / "bare.db", api_token=master_token)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **bearer,
        }
        with TestClient(
            app, base_url="http://127.0.0.1:8000", follow_redirects=False
        ) as client:
            resp = client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"},
                    },
                },
            )
        assert resp.status_code == 200
        assert resp.headers.get("mcp-session-id")


class TestMcpTransportSecurity:
    """AFTERGRAPH_MCP_PUBLIC_HOST must admit hosts WITHOUT disabling the
    DNS-rebinding guard (the SDK switches protection off for non-loopback
    hosts unless explicit transport_security is passed)."""

    def test_no_env_keeps_sdk_loopback_default(self, monkeypatch):
        monkeypatch.delenv("AFTERGRAPH_MCP_PUBLIC_HOST", raising=False)
        assert mcp_transport_security_from_env() is None

    def test_public_host_enables_protection_with_loopback(self, monkeypatch):
        monkeypatch.setenv(
            "AFTERGRAPH_MCP_PUBLIC_HOST", "work-intelligence.aftergraph.org"
        )
        settings = mcp_transport_security_from_env()
        assert settings.enable_dns_rebinding_protection is True
        assert "work-intelligence.aftergraph.org" in settings.allowed_hosts
        assert "work-intelligence.aftergraph.org:*" in settings.allowed_hosts
        assert "127.0.0.1:*" in settings.allowed_hosts
        assert "localhost:*" in settings.allowed_hosts
        assert "[::1]:*" in settings.allowed_hosts

    def test_multiple_hosts_comma_separated(self, monkeypatch):
        monkeypatch.setenv(
            "AFTERGRAPH_MCP_PUBLIC_HOST",
            "work-intelligence.aftergraph.org, 172.17.0.1",
        )
        settings = mcp_transport_security_from_env()
        assert "172.17.0.1" in settings.allowed_hosts
        assert "172.17.0.1:*" in settings.allowed_hosts
        assert "work-intelligence.aftergraph.org:*" in settings.allowed_hosts

    def test_wrong_host_rejected_right_host_accepted(
        self, tmp_path, master_token, bearer, monkeypatch
    ):
        monkeypatch.setenv(
            "AFTERGRAPH_MCP_PUBLIC_HOST",
            "work-intelligence.aftergraph.org,172.17.0.1",
        )
        app = create_secure_app(db_path=tmp_path / "sec.db", api_token=master_token)
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **bearer,
        }
        # One client: the mounted MCP lifespan can only be entered once per
        # app instance. Absolute URLs steer the Host header per request.
        with TestClient(app) as client:
            denied = client.post(
                "http://evil.example/mcp", headers=headers, json=payload
            )
            assert denied.status_code == 421
            accepted = client.post(
                "http://172.17.0.1:8090/mcp", headers=headers, json=payload
            )
            assert accepted.status_code == 200
