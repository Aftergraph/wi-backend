# MCP server over Work Intelligence

The secure app mounts a Streamable HTTP MCP server at `/mcp`
(`secure_api.create_app` → `mcp_server.build_mcp_server`). Same process,
same production middleware: unauthenticated `/mcp` traffic gets 401 before
it reaches any tool.

## Run it

```bash
uv sync
AFTERGRAPH_API_TOKEN=<master-token> uv run aftergraph-work-intelligence
```

`POST/GET/DELETE http://127.0.0.1:8087/mcp` speaks MCP Streamable HTTP
(bare `/mcp` and `/mcp/` both answer directly, no redirect, so strict
clients work).
Unset, only loopback `Host` values are admitted (421 otherwise). For
non-loopback access set `AFTERGRAPH_MCP_PUBLIC_HOST` to a comma-separated
host list — the guard stays ON and admits exactly those hosts (bare and
any port) plus loopback:

```bash
AFTERGRAPH_MCP_PUBLIC_HOST=work-intelligence.aftergraph.org,172.17.0.1
```

Without this the SDK would silently disable the guard for non-loopback
hosts, so the server builds explicit `transport_security` instead of
relying on the SDK default (see `mcp_transport_security_from_env`).

## Deploy matrix

- `docker-compose.yml` passes `AFTERGRAPH_MCP_PUBLIC_HOST` through
  (empty = loopback-only).
- `deploy/systemd/work-intelligence-vds.conf` sets the public hostname
  plus the VDS bridge IP clients use on-box.
- Hosts using `work-intelligence.service` add the same variable to
  `/etc/aftergraph/work-intelligence.env` when serving non-loopback.
- Probes/healthchecks are unchanged (`/healthz` on the main app).

## Connect a client

Point any Streamable-HTTP MCP client at `/mcp` with the credential as
a header (per-tenant `ak_*` key or master Bearer token):

```json
{
  "mcpServers": {
    "wi": {
      "url": "https://work-intelligence.aftergraph.org/mcp",
      "headers": { "Authorization": "Bearer <ak_*_or_master_token>" }
    }
  }
}
```

Smoke-test without a client:

```bash
curl -sS -D - -o /dev/null -X POST https://work-intelligence.aftergraph.org/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H "Authorization: Bearer $AFTERGRAPH_API_TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"1"}}}'
# expect 200 + an mcp-session-id header
```

## Auth (mirrors the REST rules, never weaker)

- `Authorization: Bearer <AFTERGRAPH_API_TOKEN>` — every explicit tenant,
  admin tools allowed.
- `X-API-Key: ak_*` — the key's bound tenant only (bind with
  `POST /v1/api-keys {"name": ..., "tenant_id": ...}`); unbound legacy keys
  keep working with an explicit tenant.
- Every tool takes an explicit `tenant_id`. Admin tools
  (policy writes, keys, limits, cache) require the Bearer [REDACTED]

## Tools (39)

Data: `wi_search`, `wi_list/get_work_items`, `wi_create/list_observations`,
`wi_review/promote/merge/publish_work_item(s)`, `wi_bulk_status`,
`wi_get_evidence/transitions/publications/execution_status`,
`wi_list_actions`, `wi_evaluate/decision_history`, `wi_list_tenants`,
`wi_submit_task/task_stats/get_task/list_tasks`, `wi_version/usage/readiness`,
`wi_audit_log/stats`, `wi_get_context`.
Admin: `wi_get/set/delete/list_policies`, `wi_create/list/rotate/revoke_api_key`,
`wi_get/set_rate_limit`, `wi_clear_cache/delete_cache_key`.

Mutating data tools and all admin tools write audit rows
(`actor=mcp:{identity}`).

## Deliberately not exposed

- Webhook management + stats (HMAC secret surface).
- `migrations/run`, request logs + cleanup (infra / PII / destructive).
- `metrics`, `monitoring`, `response-times`, `cache-stats` (ops scrapes;
  version/usage/readiness cover health).

## Verify

```bash
uv run pytest tests/test_mcp.py -q   # 18 tests: surface, gates, transport, replay, admin, evaluate, host-guard, bare-path
uv run pytest tests/ -q              # full suite
uvx ruff check src/aftergraph_work_intelligence/ tests/test_mcp.py
```
