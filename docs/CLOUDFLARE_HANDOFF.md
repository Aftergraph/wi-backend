# Cloudflare Handoff — intel.rendetalje.dk

## State (verified 2026-09-06)

- `renos-control-tunnel-1`: Up, remote-managed via `TUNNEL_TOKEN` (ingress in Zero Trust dashboard, no local config).
- `wi-quick-tunnel`: temporary `cloudflared --url http://127.0.0.1:8090` (webhooks use this until permanent hostname exists).
- Backend `:8090` + frontend `:3001` healthy on VDS; `intel.rendetalje.dk` returns HTTP 000 (no ingress yet).

## Jonas action (~2 min, Zero Trust dashboard)

Networks → Tunnels → `renos-control-tunnel-1` → Public Hostnames → Add:

- Subdomain: `intel`, Domain: `rendetalje.dk`
- Service: copy pattern from existing hostnames (target: backend `:8090`)

Verify: `curl https://intel.rendetalje.dk/healthz` → backend JSON.

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
