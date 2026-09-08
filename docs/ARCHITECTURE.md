# Wie by Aftergraph — Architecture

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
6. **State machine** moves items `OPEN → APPROVED | REJECTED | SNOOZED | CANCELLED`,
   `APPROVED → PUBLISHED | PROMOTED_TO_WORKS | CANCELLED`,
   `SNOOZED → OPEN (explicit resume) | CANCELLED`, with human gate
   (`CANCELLED`/`REJECTED` terminal; every edge audited in `intake_transitions`)
7. **Evidence bundle** (HMAC-SHA256 chain) is built on demand
8. **Webhooks** fire `observation.ingested` / `work_item.*` events to registered endpoints
9. **WebSocket** streams the same events to live UI clients
10. **Audit log** records every mutation (sealed, queryable)
11. **Background tasks** (`POST /v1/tasks/submit`, worker pool of 4): generic
    job API with stub executors — not on the critical ingestion path; queue
    stats are thread-safe, retries are immediate (max 3), state is in-memory
    only and does not survive restarts

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
│  works-execution bridge (works-api :18191, screen PID 2301671)│
│    └─ 3× avc-core workers (Docker sandbox node22+py3)         │
│  GitHub webhook → POST /v1/webhook/github                │
│    ├─ X-Hub-Signature-256 (HMAC-SHA256 verification)     │
│    ├─ GitHubAdapter.observations(payload)                │
│    └─ WorkIntelligenceService.ingest(obs)                │
│                                                               │
│  work-intelligence backend (FastAPI :8090, systemd)      │
│    └─ WorksPublisher → POST {works-url}/v1/workers/enroll     │
│           └─ Bearer JWT → POST {works-url}/v1/works     │
│  work-intelligence frontend (:3001, systemd, node server.mjs)│
│    ├─ static SPA (dist/)                                  │
│    └─ /api/* reverse-proxy → backend :8090                    │
│       └─ publish/promote-knapper i Inspector (approved items) │
└───────────────────────────────────────────────────────────────┘
```