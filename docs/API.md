# Aftergraph Work Intelligence V2 — API Reference

Base URL: `http://127.0.0.1:8087`  
API versioning: `X-API-Version: v1` header on responses.  
Auth: `Authorization: Bearer <api_key>` (SHA-256 hashed at rest).

## Endpoint map

### System & health

| Method | Path | Description |
|---|---|---|
| GET | `/healthz` | Liveness: service + database + queue + cache status |
| GET | `/healthz/detailed` | Per-component health breakdown |
| GET | `/live` | Liveness probe (k8s-style) |
| GET | `/ready` | Readiness probe |
| GET | `/dashboard` | System dashboard summary |
| GET | `/v1/version` | Service version + API version |
| GET | `/v1/readiness` | Readiness with checks + timestamp |
| GET | `/v1/monitoring` | Monitoring snapshot |
| GET | `/v1/context` | Actor context |
| GET | `/v1/usage` | Request/error counters by path and status |
| GET | `/v1/response-times` | Latency statistics |

### Observations (ingest)

| Method | Path | Description |
|---|---|---|
| POST | `/v1/observations` | Ingest an observation from any source (email, chat, calendar, code). Returns `{action, observation, work_item}` — may create or merge into an existing work item. |
| GET | `/v1/observations` | List observations (filter: `source`, `tenant_id`, pagination) |

**ObservationRequest fields:**

| Field | Type | Notes |
|---|---|---|
| `tenant_id` | string (1-128) | Required |
| `source` | string (1-64) | e.g. `github`, `gmail`, `calendar`, `slack` |
| `text` | string (1-100000) | The observed signal |
| `external_id` | string? | Dedup key |
| `actor` | string? | Who/what produced it |
| `occurred_at` | datetime? | Event time |
| `metadata` | object? | Free-form |
| `title_hint` | string? | Suggested work item title |
| `owner_hint` | string? | Suggested owner |
| `due_hint` | string? | Suggested due date |
| `priority_hint` | `low\|medium\|high\|critical`? | Suggested priority |

### Work items

| Method | Path | Description |
|---|---|---|
| GET | `/v1/work-items` | List work items |
| GET | `/v1/work-items/{id}` | Detail: work item + observations + publications |
| GET | `/v1/work-items/{id}/actions` | Allowed state transitions |
| GET | `/v1/work-items/{id}/evidence` | HMAC-SHA256 evidence bundle |
| GET | `/v1/work-items/{id}/transitions` | Transition history |
| GET | `/v1/work-items/{id}/publications` | Publication records |
| POST | `/v1/work-items/{id}/review` | Human review decision |
| POST | `/v1/work-items/{id}/publish` | Publish to destination |
| POST | `/v1/work-items/{id}/promote` | Promote state |
| POST | `/v1/work-items/bulk-status` | Bulk status updates |
| GET | `/v1/search` | Full-text search across work items |

### Keys, tenants, policies

| Method | Path | Description |
|---|---|---|
| GET/POST | `/v1/api-keys` | List / create API keys |
| DELETE | `/v1/api-keys/{id}` | Revoke |
| POST | `/v1/api-keys/{id}/rotate` | Rotate secret |
| GET | `/v1/tenants` | List tenants |
| GET/POST/DELETE | `/v1/tenants/{tenant_id}/policy` | Per-tenant policy CRUD |
| GET | `/v1/tenants/policies` | All persisted policies |

### Webhooks (inbound)

| Method | Path | Description |
|---|---|---|
| POST | `/v1/webhook/github` | Inbound GitHub webhook — HMAC-SHA256 verified, maps via GitHubAdapter, ingests observations |

**POST /v1/webhook/github**

Ingests GitHub webhook events (push, pull_request, issues, check_run, workflow_run, issue_comment). Signature verification uses `X-Hub-Signature-256` header with HMAC-SHA256 against `AFTERGRAPH_GITHUB_WEBHOOK_SECRET`. If the secret is unset, verification is skipped (dev mode).

**Headers:**

| Header | Required | Description |
|---|---|---|
| `X-Hub-Signature-256` | Yes (prod) | `sha256=<hex>` HMAC-SHA256 of raw body |
| `X-GitHub-Event` | No | Event type (defaults to `push`) |
| `Content-Type` | Yes | `application/json` |

**Request body:** Raw GitHub webhook payload JSON. Optional `tenant_id` field at root level (defaults to `"default"`).

**Response (201 Created):**
```json
{
  "event": "push",
  "status": "ingested",
  "observations_created": 2,
  "observations_replayed": 0,
  "work_item": {
    "id": "wi_abc123",
    "source": "github",
    "priority": "medium",
    "observation_count": 3
  }
}
```

**Response (202 Accepted):** Event ignored (no actionable observations).

**Response (401 Unauthorized):** Invalid or missing signature.

### Work item lifecycle: publish & promote

**POST /v1/work-items/{id}/publish?tenant_id=default**

Publishes an APPROVED work item to an external destination (e.g. `works`). The publish router dispatches to the destination's concrete publisher; for `works`, the `WorksPublisher` self-enrolls via `POST {AFTERGRAPH_WORKS_URL}/v1/workers/enroll` (challenge = `AFTERGRAPH_WORKS_ENROLL_SECRET`, worker_id = `AFTERGRAPH_WORKS_WORKER_ID`), then POSTs the normalized Work payload to `{AFTERGRAPH_WORKS_URL}/v1/works` with the enrollment JWT as Bearer. On 401/403 it re-enrolls once automatically. `idempotency_key` is derived from the work item id so re-publishing yields the same external work id.

**Request body (strict, no extra fields):**
```json
{ "destination": "works" }
```

**Response (201 Created):**
```json
{
  "id": "pub_ff1086e04d5e4ffa88068c54403c2d6f",
  "work_item_id": "wi_abc123",
  "destination": "works",
  "external_id": "works:435162063fff2970ec6144d6272dc568",
  "response": { "id": "works:435162063fff2970ec6144d6272dc568", "state": "CREATED", "objective": { "type": "custom", "description": "...", "constraints": { "work_item_id": "wi_abc123" } }, "graph": { "nodes": { "extract": { "id": "extract", "kind": "extract", "run": "aftergraph-extract" } } } }
}
```

**Response (502 Bad Gateway):** Destination unreachable/authorization failed — detail contains the upstream error (`bad_challenge` = enroll secret mismatch, `token_expired` = stale cached JWT; fix: point `AFTERGRAPH_WORKS_ENROLL_SECRET` at the *running* works-api process secret and restart).

**POST /v1/work-items/{id}/promote?tenant_id=default**

Promotes a work item to the works-execution queue (transition to `promoted` state).

**Request body:**
```json
{ "actor": "wi-web", "reason": "Promoted to works-execution from Work Intelligence Web" }
```

**Response (200 OK):** The updated work item (state-aware payload).

**Errors:** 404 unknown item, 403 not permitted (e.g. not APPROVED), 400 invalid transition.

### Webhooks (outbound)

| Method | Path | Description |
|---|---|---|
| GET/POST | `/v1/webhooks` | List / register webhook (events: `observation.ingested`, `work_item.created`, `work_item.updated`, `work_item.reviewed`, `work_item.promoted`) |
| DELETE | `/v1/webhooks/{id}` | Remove |
| GET | `/v1/webhooks/stats` | Delivered/failed counters |

### Operations

| Method | Path | Description |
|---|---|---|
| GET/POST | `/v1/tasks`, `/v1/tasks/stats`, `/v1/tasks/{id}` | Background task queue |
| POST | `/v1/tasks/submit` | Submit a task |
| GET | `/v1/cache/stats` | Cache stats (TTL, LRU, max 1000) |
| POST | `/v1/cache/clear`, DELETE `/v1/cache/{key}` | Cache control |
| GET/POST | `/v1/rate-limit` | Per-endpoint rate limit config |
| GET | `/v1/logs` | Request logs (JSONL) |
| POST | `/v1/logs/cleanup` | Rotate/cleanup logs |
| GET/POST | `/v1/migrations` | Migration status / run |
| GET | `/v1/metrics` | Counts by action, source, tenant, status |
| GET | `/v1/audit`, `/v1/audit/stats` | Audit log query + stats |

### WebSocket

`/ws` — real-time event stream.

- Heartbeat every 30s (`type: heartbeat`)
- Client tracking: connect/disconnect events
- Stats: connected clients, messages sent

## Response headers

| Header | Value |
|---|---|
| `X-API-Version` | `v1` |
| `X-App-Version` | `0.2.0` |
| `X-Request-ID` | per-request UUID |

## Error model

9 custom exception classes; errors return:

```json
{
  "detail": "human readable message"
}
```

## Example flows

### Ingest an observation

```bash
curl -X POST http://127.0.0.1:8087/v1/observations \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "default",
    "source": "gmail",
    "text": "Customer needs API docs by Friday",
    "actor": "customer@example.com",
    "priority_hint": "high"
  }'
```

### Create an API key

```bash
curl -X POST http://127.0.0.1:8087/v1/api-keys \
  -H "Content-Type: application/json" \
  -d '{"name": "my-app", "scopes": ["ingest", "read", "review"]}'
```

### Review a work item

```bash
curl -X POST "http://127.0.0.1:8087/v1/work-items/wi_xxx/review" \
  -H "Authorization: Bearer $API_KEY" \
  -d '{"decision": "approve", "actor": "ops-user"}'
```