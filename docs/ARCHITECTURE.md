# Aftergraph Work Intelligence — Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         SOURCES                                      │
│  GitHub ─┐     Gmail ─┐    Calendar ─┐    Slack ─┐    RenOS ─┐      │
│  (push/  │   (inbox/  │  (meetings/  │  (channels/│  (jobs/   │      │
│   PR)    │    triage) │    follow-up)│   decisions)│  dispatch)│     │
└─────┬────┴──────┬─────┴──────┬──────┴─────┬──────┴─────┬──────┘      │
      │           │            │            │            │             │
      ▼           ▼            ▼            ▼            ▼             │
┌─────────────────────────────────────────────────────────────────────┐
│                    INGEST ADAPTERS (observations)                    │
│         /v1/observations  —  source-neutral ingestion                │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                 WORK INTELLIGENCE ENGINE (FastAPI)                   │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────────────────┐  │
│  │ Extractor    │   │ Inferencer   │   │ Canonicalizer            │  │
│  │ (raw signal) │──▶│ (intent,     │──▶│ (dedup via canonical_key │  │
│  │              │   │  priority,   │   │  SHA-256 tokens)         │  │
│  └──────────────┘   │  confidence) │   └────────────┬─────────────┘  │
│                     └──────────────┘                │                │
│  ┌──────────────────────────────────────────────────▼──────────────┐ │
│  │ WorkItem lifecycle: OPEN → REVIEW → APPROVED → PUBLISHED         │ │
│  │ Review queue + human-in-the-loop gate                            │ │
│  └──────────────────────────────────────────────────────────────────┘ │
│                                                                       │
│  ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌───────────────────┐  │
│  │ Policy     │ │ Evidence   │ │ Webhooks   │ │ Audit log         │  │
│  │ engine     │ │ (HMAC-     │ │ (outbound) │ │ (JSONL rotation)  │  │
│  │ (per-      │ │  SHA256,   │ │            │ │                    │  │
│  │  tenant)   │ │  SHA-256)  │ │            │ │                    │  │
│  └────────────┘ └────────────┘ └────────────┘ └───────────────────┘  │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                    ┌───────────┼───────────┐
                    ▼           ▼           ▼
          ┌──────────────┐ ┌──────────┐ ┌──────────────┐
          │   SQLite     │ │  Cache   │ │   Task       │
          │  store       │ │ (TTL,    │ │   queue      │
          │  (migrations │ │  LRU,    │ │   (async)    │
          │   v3)        │ │  1000)   │ │              │
          └──────────────┘ └──────────┘ └──────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                            CONSUMERS                                  │
│                                                                       │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────────────────┐  │
│  │ Web UI       │   │ WebSocket    │   │ Registered webhooks      │  │
│  │ (React, Vite,│   │ clients      │   │ (outbound events)        │  │
│  │  Tailwind)   │   │ (heartbeat,  │   │                          │  │
│  │              │   │  stats)      │   │                          │  │
│  └──────────────┘   └──────────────┘   └──────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

## Data flow (end to end)

1. **Source event** happens (GitHub push, email, meeting, Slack decision, RenOS job)
2. **Adapter** calls `POST /v1/observations` with `{source, text, actor, ...}`
   — GitHub events arrive via `POST /v1/webhook/github` (HMAC-SHA256 verified,
   mapped through `GitHubAdapter`, then ingested identically)
3. **Extractor** normalizes the raw signal
4. **Inferencer** predicts intent, priority, confidence
5. **Canonicalizer** dedups via SHA-256 token key → creates or merges a `WorkItem`
6. **State machine** moves item `OPEN → REVIEW → APPROVED → PUBLISHED` with human gate
7. **Evidence bundle** (HMAC-SHA256 chain) is built on demand
8. **Webhooks** fire `observation.ingested` / `work_item.*` events to registered endpoints
9. **WebSocket** streams the same events to live UI clients
10. **Audit log** records every mutation (sealed, queryable)

## Frontend ↔ Backend

```
┌──────────────────────┐        ┌──────────────────────┐
│  React 19 + Vite     │  /api  │  Express BFF (dev)   │
│  (port 3000, proxy)  │───────▶│                      │
│                      │        │   /api → http://127.0.0.1:8087
│  Home / Work /       │        │                      │
│  Review / Activity / │◀───────│  X-API-Version: v1   │
│  Integrations        │  JSON  │                      │
│  + Workspace         │        │                      │
│  surfaces (Drive,    │        │                      │
│  Gmail, Calendar,    │        │                      │
│  Sheets, Docs, Keep) │        │                      │
└──────────────────────┘        └──────────────────────┘
```

## Deployment topology

```
┌─────────────────────────── VDS ───────────────────────────────┐
│  works-execution bridge (works-api :18191)                    │
│    └─ 3× avc-core workers (Docker sandbox node22+py3)         │
│  GitHub webhook → POST /v1/webhook/github                │
│    ├─ X-Hub-Signature-256 (HMAC-SHA256 verification)     │
│    ├─ GitHubAdapter.observations(payload)                │
│    └─ WorkIntelligenceService.ingest(obs)                │
│                                                               │
│  work-intelligence backend (FastAPI :8087)  ← future deploy   │
│  work-intelligence frontend (Vite/static)  ← future deploy    │
└───────────────────────────────────────────────────────────────┘
```