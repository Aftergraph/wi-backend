# Cloudflare Handoff — intel.rendetalje.dk

## State (verified 2026-09-06)

- `renos-control-tunnel-1`: Up, remote-managed via `TUNNEL_TOKEN` (ingress in Zero Trust dashboard, no local config).
- `wi-quick-tunnel`: temporary `cloudflared --url http://127.0.0.1:8090` (webhooks use this until permanent hostname exists).
- Backend `:8090` + frontend `:3001` healthy on VDS.
- Tunnel ingress is now configured remotely (Cloudflare config version 5):
  - `intel.rendetalje.dk` → `http://172.21.0.1:8090` (backend/webhooks)
  - `work-intelligence.rendetalje.dk` → `http://172.21.0.1:3001` (frontend UI)
- DNS CNAME records are present and proxied:
  - `intel.rendetalje.dk` → `8b80b1e3-886c-459a-bae9-c668d18aec1a.cfargotunnel.com`
  - `work-intelligence.rendetalje.dk` → `8b80b1e3-886c-459a-bae9-c668d18aec1a.cfargotunnel.com`
- Live verification: backend `/healthz` and frontend `/` + `/api/healthz` return HTTP 200.

## Current operational state

The permanent webhook hooks are configured on both repositories:

- `Aftergraph/work-intelligence-v2` hook `675062310`
- `Aftergraph/work-intelligence-web` hook `675062318`
- URL: `https://intel.rendetalje.dk/v1/webhook/github`
- Content type: JSON
- GitHub ping read-back: HTTP 202 / OK

The temporary `trycloudflare.com` URL is no longer used by those hooks. Keep the
`wi-quick-tunnel` container running until a later cleanup window confirms there
are no remaining consumers.

## Secret learning (2026-09-06)

Three enroll-secrets existed on VDS; only the running works-api process secret works.
`AFTERGRAPH_WORKS_ENROLL_SECRET` must match it.

Verify: enroll via `POST {works-url}/v1/workers/enroll`, then `POST {works-url}/v1/works` → 201.

| Symptom | Cause |
|---|---|
| `bad_challenge` | enroll secret mismatch (wrong one of the three) |
| `token_expired` | stale cached JWT (works-api restarted, process key rotated) |
