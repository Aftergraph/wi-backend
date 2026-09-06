# ADR-009: Formal Threat-Model Review (Evaluator + Merge Write Path)

**Status:** Adopted
**Date:** 2026-09-07
**Scope:** `POST /v1/autonomy/decisions/evaluate`, `POST /v1/work-items/{id}/merge`,
`ProductionSecurityMiddleware`, web BFF (`server.js` + `server-policy.mjs`)
**Supersedes:** ADR-008 § "Threat Model Gaps (Future Work)"

---

## 1. Assets

| Asset | Impact of compromise |
|-------|----------------------|
| `AFTERGRAPH_WEBHOOK_SECRET` / `AFTERGRAPH_API_TOKEN` (VDS env) | Full API impersonation |
| Canonical work-items + transitions (SQLite) | Silent state forgery |
| Evaluator decisions (`auto_approve`) | Malicious change auto-approved |
| BFF server token (`AFTERGRAPH_API_TOKEN` in `work-intelligence-web.env`) | Backend calls as web tier |

## 2. Trust boundaries

1. **Internet → `ProductionSecurityMiddleware`** — the only production boundary.
   Core `api.py` auth (`Depends(auth)`) is defense-in-depth, not sufficient alone.
2. **BFF → backend** — BFF strips browser credentials and injects its own
   server token; browsers never hold backend credentials.
3. **Evaluator (read-only) vs merge (write)** — evaluate has no side effects
   by construction; merge mutates state and carries stricter controls (§4).

## 3. Abuse paths and controls

| # | Path | Control | Test |
|---|------|---------|------|
| T1 | Caller forges `is_security_change: false` to get `auto_approve` | Independent `changed_files` signal scan; flags never override | `test_adversarial*.py` |
| T2 | Stolen/low-privilege token calls merge cross-tenant | Both source and target resolved via tenant-scoped `get_work_item_detail`; mismatch → 404, no existence oracle beyond 404 | `test_merge.py::test_merge_is_tenant_scoped` |
| T3 | Replay of a merge request duplicates effects | Idempotent replay: same `(source, target, reason)` → 200 with no new transition | `test_merge.py::test_merge_replay_is_idempotent` |
| T4 | Merge of already-resolved item destroys audit trail | Terminal-state guard: CANCELLED-for-other-reason → 409; other terminal states → 400 via engine; nothing is deleted, transitions append-only | `test_merge.py::test_merge_conflicts_when_already_cancelled_for_another_reason` |
| T5 | Self-merge corrupts state | Explicit `source == target` → 400 | `test_merge.py::test_merge_rejects_self_merge_and_unknown_target` |
| T6 | Webhook HMAC bypass (timing/format) | `hmac.compare_digest`, `sha256=` or raw-hex accepted, failures → 401/503 fail-closed | `test_webhook_delivery.py`, `test_production_security.py` |
| T7 | CORS / credential leakage via browser | BFF allowlist in `server-policy.mjs`; middleware strips cookies/`x-api-key`/`authorization` from proxied requests; `*` origins rejected in secure mode | `frontend-ci` BFF live contract, `bff-policy.test.mjs` |
| T8 | Rate-limit starvation across tenants | Per-endpoint keys namespaced by auth method; global per-identity limit | `test_rate_limit_webhook_stats.py` |
| T9 | Evaluator confidence treated as certainty | Hard cap at 80, sublinear weighting | ADR-008 decision table (unchanged) |

## 4. Merge write-path rules (new)

- Merge is **audited cancellation**, not deletion: the duplicate transitions to
  `CANCELLED` with reason `merged into <target>`; the canonical target is
  never modified by the merge itself.
- Requires authenticated caller (middleware + `Depends(auth)`), explicit
  `actor`, and same-tenant visibility of both items.
- Webhook event `work_item.merged` fires on success for downstream audit.
- Future merge semantics (observation re-linking, unmerge) require a new ADR.

## 5. Residual risks (accepted, not hidden)

- R1 (partially closed 2026-09-07): per-tenant webhook secrets are supported
  via `AFTERGRAPH_WEBHOOK_SECRET_<TENANT>` (uppercased, non-alphanumerics to
  `_`) with global fallback; the handler resolves by claimed tenant, so a key
  for tenant A never verifies tenant B (`test_webhook_tenant_secrets.py`).
  Rotation per tenant: `scripts/rotate-webhook-secret.sh [TENANT]`. The
  production middleware defers signature-present requests to the handler when
  the per-tenant namespace is configured; the endpoint still 401s on mismatch.
- R2: No per-tenant database isolation; isolation is query-scoped
  (`tenant_id` on every read/write). Isolation maturity: **L1 (enforced query
  scoping)** — every new endpoint must add a tenant-scoping test following
  `test_merge_is_tenant_scoped`. L2 (PostgreSQL RLS) or higher requires a
  storage-backend migration and is explicitly out of scope for SQLite.
- R3: Local dev mode (`create_app` without token) is unauthenticated by
  design; it must never bind a public interface (deploy script binds
  `172.17.0.1` only).

## Decision

**Adopted.** The evaluator keeps its ADR-008 contract unchanged. The merge
write path is admitted under the §4 rules with the Table §3 controls and
tests. Any new write-side effect, confidence-cap change, or relaxation of a
fail-closed rule requires a new ADR.

---
See also: [ADR-008](ADR-008-AUTONOMY-BOUNDARY-SECURITY.md), [ARCHITECTURE.md](ARCHITECTURE.md)
