# Wie by Aftergraph — Production Integration Plan

**Status:** COMPLETE (TDD, base SHA `cda5483`, HEAD `f40b547`)
**Goal:** Promote the V1 reference implementation to V2 production integration with Aftergraph,
without breaking the canonical WorkItem invariant:

> **Work creates tickets. Humans do not create tickets.**

## Scope (V2 in scope)

1. Real **source adapters** (RenOS-job-status signals, conversation transcript, email, calendar,
   code-commit) — each producing V1-compatible `Observation` records with full provenance.
2. **Resolution / deduplication** — V1 already has key+s tokens+threshold; V2 hardens with:
   - per-source external-id replay (already there),
   - tenant-scoped merge (already there),
   - cross-source same-key dedupe across ALL sources (new),
   - re-resolution on observation update.
3. **Provenance / evidence**:
   - every observation carries `actor`, `source`, `external_id`, `occurred_at`, `metadata`,
   - every work-item carries the linked observation chain (already there),
   - V2 adds a content-addressed **evidence record** (HMAC-SHA256) bound to the work-item
     to mirror Aftergraph's L2 (`evidence.schema/1.1`) without requiring WORKS to be online.
4. **Tenant policies**:
   - per-tenant `TenantPolicy` (auto-promote on/off, dedupe threshold, allowed sources,
     max work-items per tenant, max work-item priority, etc.),
   - enforcement at ingest (reject or flag, never partial),
   - policy is versioned and stored with the work-item at promotion time.
5. **Review / approval flow**:
   - work-items start as `OPEN` (auto-inferred) and require explicit `approve` before
     they can be promoted to `APPROVED` → publishable,
   - `reject` and `snooze` are first-class,
   - the API exposes `POST /v1/work-items/{id}/review`.
6. **Destination publishers** (after review/approval):
   - **RenOS publisher**: pushes to Project-Renos `Job` (HTTP webhook or DB-direct depending
     on tenant policy),
   - **WORKS publisher**: pushes to works-execution `Work` payload (conforming to
     `work.schema/1.0`) via `/work` POST — **requires explicit `promote-to-works` flag**,
   - **generic webhook** (carries over from V1),
   - **deny by default** — destinations must be explicitly enabled in tenant policy.
7. **Observability**:
   - `/v1/metrics` (counted JSON: counts by action, by source, by tenant, by status),
   - structured logs at every state transition,
   - the canonical flow path is observable end-to-end.
8. **End-to-end tests**:
   - TDD-first; all V2 modules get a failing test, then implementation, then green,
   - **no mocks as final evidence**: tests run against an in-process WORKS-compatible
     FastAPI server (per `work.schema/1.0`) and an in-process RenOS-compatible FastAPI
     server (per Job model surface). Adapters that talk to real RenOS/WORKS in prod are
     validated against the in-process harness.
9. **Research/eval update**:
   - update `intelligence-systems-research/PROAD-V2-RESULTS.md` with the actual numbers.

## Out of scope (deferred)

- Cross-repo repo migration (the Aftergraph blueprint marks it `PROVISIONAL — awaiting owner
  approval before any repo-transfer`); V2 stays self-contained.
- LLM-backed extractor (V1 deterministisk baseline bevares; V2 lader en fremtidig
  `ModelExtractor` implementere samme interface).
- Multi-region / HA (single-node SQLite + WAL).
- Auth flow overhaul (V1 Bearer-token bevares; tenant policy includes optional token rotation).

## Architectural rules

- `WorkItem` (canonical) ≠ `WORKS Work` (executable). Promotion requires tenant-policy
  `allow_works: true` AND item status `APPROVED`.
- The flow is one-way except for review:
  `signal → observation → candidate → resolution → work item → review/approve → publish
  → optional WORKS promotion → evidence`.
- Every state transition persists a `Transition` record (`id`, `from`, `to`, `actor`,
  `reason`, `at`) — durable audit.
- No silent fallbacks. If a publisher fails, the receipt records the failure and the
  work-item is **not** marked promoted; a retry endpoint is exposed separately.