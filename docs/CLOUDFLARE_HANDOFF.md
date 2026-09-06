# Cloudflare Handoff — intel.rendetalje.dk

## State (verified 2026-09-06)

- `renos-control-tunnel-1`: Up, remote-managed via `TUNNEL_TOKEN` (ingress in Zero Trust dashboard, no local config).
- `wi-quick-tunnel`: temporary `cloudflared --url http://127.0.0.1:8090` (webhooks use this until permanent hostname exists).
- Backend `:8090` + frontend `:3001` healthy on VDS.
- Tunnel ingress is now configured remotely (Cloudflare config version 5):
  - `intel.rendetalje.dk` → `http://172.17.0.1:8090` (backend/webhooks)
  - `work-intelligence.rendetalje.dk` → `http://172.17.0.1:3001` (frontend UI)
- DNS CNAME records are still missing: current Wrangler OAuth has zone-read but not DNS-write permission, so both names remain unresolved.

## Jonas action (~2 min, Cloudflare DNS)

Create proxied CNAME records in zone `rendetalje.dk`:

| Name | Target | Proxy |
|---|---|---|
| `intel` | `8b80b1e3-886c-459a-bae9-c668d18aec1a.cfargotunnel.com` | Proxied |
| `work-intelligence` | `8b80b1e3-886c-459a-bae9-c668d18aec1a.cfargotunnel.com` | Proxied |

Then verify:

- `curl https://intel.rendetalje.dk/healthz` → backend JSON
- Open `https://work-intelligence.rendetalje.dk/` → Aftergraph Work Intelligence UI

## After ingress is live

1. Re-point webhook registrations from quick-tunnel URL to `https://intel.rendetalje.dk`.
2. Stop `wi-quick-tunnel` container.

## Secret learning (2026-09-06)

Three enroll-secrets existed on VDS; only the running works-api process secret works.
`AFTERGRAPH_WORKS_ENROLL_SECRET` must match it.

Verify: enroll via `POST {works-url}/v1/workers/enroll`, then `POST {works-url}/v1/works` → 201.

| Symptom | Cause |
|---|---|
| `bad_challenge` | enroll secret mismatch (wrong one of the three) |
| `token_expired` | stale cached JWT (works-api restarted, process key rotated) |
