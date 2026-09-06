# Aftergraph Work Intelligence V2 — Deployment Guide

## Quick Start (Local Development)

### 1. Clone and Setup

```bash
git clone https://github.com/Aftergraph/work-intelligence-v2.git
cd work-intelligence-v2

python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# or
.venv\Scripts\activate  # Windows

pip install -e ".[dev]"
```

### 2. Run Tests

```bash
pytest tests/ -v

# Optional live RenOS integration
RENOS_SESSION_TOKEN=your-token pytest tests/ -v
```

### 3. Start a Local Development Server

The core `api` module remains available for local development and test compatibility. Do not use it as a public production entrypoint.

```bash
# Default local development server (127.0.0.1:8087)
python -m aftergraph_work_intelligence.api

# Custom local host/port
python -m aftergraph_work_intelligence.api --host 127.0.0.1 --port 9000
```

For any public or production process, use the fail-closed `aftergraph-work-intelligence` console command documented below.

---

## Docker Deployment

The Docker image starts `aftergraph_work_intelligence.secure_api:create_app`, so the public container uses the production security boundary by default.

### 1. Build Image

```bash
docker build -t aftergraph-work-intelligence .
```

### 2. Run Container

The container listens on port `8000`.

```bash
docker run -d \
  -p 8087:8000 \
  -e AFTERGRAPH_API_TOKEN="$AFTERGRAPH_API_TOKEN" \
  -e AFTERGRAPH_EVIDENCE_SECRET="$AFTERGRAPH_EVIDENCE_SECRET" \
  -e AFTERGRAPH_GITHUB_WEBHOOK_SECRET="$AFTERGRAPH_GITHUB_WEBHOOK_SECRET" \
  -e AFTERGRAPH_CORS_ORIGINS="https://work-intelligence.rendetalje.dk" \
  -e AFTERGRAPH_RATE_LIMIT=120 \
  -v work-intelligence-data:/data \
  --name work-intelligence \
  aftergraph-work-intelligence
```

### 3. Docker Compose

`docker-compose.yml` fails closed when the required secrets are missing.

```bash
docker-compose up -d
docker-compose ps
docker-compose logs -f work-intelligence
docker-compose down
```

---

## Production Deployment

### Security Boundary

The canonical public entrypoint is:

```bash
aftergraph-work-intelligence
```

It resolves to `aftergraph_work_intelligence.secure_api:main` and wraps the core application with the production security middleware. The boundary provides fail-closed authentication, full API-key hash verification, explicit CORS allowlisting, baseline browser security headers, no-store headers for sensitive API responses, and fail-closed GitHub webhook configuration.

Do **not** expose `python -m aftergraph_work_intelligence.api` directly to the public network.

### 1. Environment Variables

Store production secrets in a root-owned environment file such as `/etc/aftergraph/work-intelligence.env` rather than embedding them in the unit file.

```bash
# Required for the current public deployment
AFTERGRAPH_API_TOKEN=replace-with-strong-token
AFTERGRAPH_EVIDENCE_SECRET=replace-with-strong-hmac-secret
AFTERGRAPH_GITHUB_WEBHOOK_SECRET=replace-with-github-webhook-secret
AFTERGRAPH_CORS_ORIGINS=https://work-intelligence.rendetalje.dk

# Runtime
AFTERGRAPH_DB=/var/lib/work-intelligence/data.db
AFTERGRAPH_HOST=127.0.0.1
AFTERGRAPH_PORT=8087
AFTERGRAPH_RATE_LIMIT=60

# Optional integrations
RENOS_SESSION_TOKEN=replace-if-used
```

Protect the file:

```bash
sudo chown root:work-intelligence /etc/aftergraph/work-intelligence.env
sudo chmod 0640 /etc/aftergraph/work-intelligence.env
```

### 2. Systemd Service (Linux)

Create `/etc/systemd/system/work-intelligence.service`:

```ini
[Unit]
Description=Aftergraph Work Intelligence V2
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=work-intelligence
Group=work-intelligence
WorkingDirectory=/opt/work-intelligence
EnvironmentFile=/etc/aftergraph/work-intelligence.env
ExecStart=/opt/work-intelligence/.venv/bin/aftergraph-work-intelligence
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/work-intelligence /opt/work-intelligence/logs

[Install]
WantedBy=multi-user.target
```

Enable and restart:

```bash
sudo systemctl daemon-reload
sudo systemctl enable work-intelligence
sudo systemctl restart work-intelligence
sudo systemctl status --no-pager work-intelligence
```

Verify the local boundary before changing public routing:

```bash
curl -fsS http://127.0.0.1:8087/healthz
curl -i http://127.0.0.1:8087/v1/work-items?tenant_id=smoke-prod
curl -i -X OPTIONS http://127.0.0.1:8087/v1/observations \
  -H 'Origin: https://evil.example' \
  -H 'Access-Control-Request-Method: POST'
```

Expected: health `200`, protected unauthenticated request `401`, hostile CORS preflight `403`.

### 3. Reverse Proxy

A conventional Nginx deployment can proxy to the loopback listener:

```nginx
server {
    listen 80;
    server_name work-intelligence.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8087;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Request-ID $request_id;
    }
}
```

Use TLS at the public edge. The current Aftergraph deployment uses Cloudflare Tunnel rather than this generic Nginx example.

### Current Aftergraph VDS Topology

Measured on `vmi3517816` on 2026-09-06 after the production security rollout:

- backend: `172.17.0.1:8090`, exposed as `https://intel.rendetalje.dk` through the named Cloudflare Tunnel
- frontend: VDS port `3001`, exposed as `https://work-intelligence.rendetalje.dk`
- frontend API proxy target: `http://172.17.0.1:8090`
- named tunnel network: `renos-control-edge` (`172.21.0.0/16`)
- named tunnel origin for the API: `http://172.17.0.1:8090`
- UFW permits backend `8090/tcp` from `172.21.0.0/16`; the backend no longer listens on every VDS interface
- SQLite database: `/var/lib/work-intelligence/wi.db`
- current legacy secret source: `/etc/work-intelligence-webhook.secret`

The VDS-specific systemd overrides are checked in under `deploy/systemd/`. Install them as drop-ins for `work-intelligence.service` and `work-intelligence-web.service`. The generic example above remains loopback-oriented for installations where the reverse proxy runs in the host namespace.

Do not change this VDS backend to `127.0.0.1:8090` while the named tunnel origin remains `172.17.0.1:8090`; that mismatch was reproduced as a public `502`. An obsolete random `trycloudflare.com` quick tunnel was removed after the named tunnel was independently verified.

---

## Monitoring

### Health Check

```bash
curl http://localhost:8087/healthz
# {"status": "ok", "service": "aftergraph-work-intelligence", "version": "0.2.0", ...}
```

### Metrics

```bash
curl -H "Authorization: Bearer $AFTERGRAPH_API_TOKEN" \
  http://localhost:8087/v1/metrics
```

### Logs

```bash
# Docker
docker logs -f work-intelligence

# systemd
journalctl -u work-intelligence -f
```

---

## Production Security Verification

Run these checks against the public hostname after every production restart or deployment:

```bash
API=https://intel.rendetalje.dk

curl -fsS "$API/healthz"

# Must fail closed without credentials
curl -i "$API/v1/work-items?tenant_id=smoke-prod"

# Must reject an arbitrary origin
curl -i -X OPTIONS "$API/v1/observations" \
  -H 'Origin: https://evil.example' \
  -H 'Access-Control-Request-Method: POST' \
  -H 'Access-Control-Request-Headers: authorization,x-api-key,content-type'

# Inspect security headers
curl -sSI "$API/healthz" | grep -iE \
  '^(strict-transport-security|content-security-policy|x-content-type-options|x-frame-options|referrer-policy|permissions-policy|cache-control):'
```

A production deployment is not considered verified until these checks pass against the public hostname. Source-level CI is necessary, but it does not prove the VDS is running the new revision.

---

## Backup and Recovery

### Database Backup

```bash
cp /var/lib/work-intelligence/data.db /backup/work-intelligence-$(date +%Y%m%d).db

# Or using sqlite3
sqlite3 /var/lib/work-intelligence/data.db ".backup /backup/work-intelligence-$(date +%Y%m%d).db"
```

### Database Recovery

```bash
sudo systemctl stop work-intelligence
cp /backup/work-intelligence-20260905.db /var/lib/work-intelligence/data.db
sudo systemctl start work-intelligence
```

---

## Scaling

### Horizontal Scaling

For multiple instances behind a load balancer:

1. Move from node-local SQLite to a datastore designed for multi-writer access before scaling write traffic horizontally.
2. Configure the same `AFTERGRAPH_API_TOKEN` across instances.
3. Use the same `AFTERGRAPH_EVIDENCE_SECRET` for consistent HMAC signatures.
4. Preserve trusted client-address information at the proxy boundary before relying on IP-based rate limiting.

### Vertical Scaling

- Increase `AFTERGRAPH_RATE_LIMIT` only after measuring legitimate traffic.
- Allocate sufficient memory for the API, task queue, and SQLite cache.
- Use durable SSD-backed storage for the database.

---

## Troubleshooting

### Common Issues

**`invalid or missing credentials`**
- Verify `AFTERGRAPH_API_TOKEN` or the API key is present.
- Check `Authorization: Bearer <token>` formatting.
- Confirm the public process is running the secure entrypoint.

**GitHub webhook returns `503`**
- `AFTERGRAPH_GITHUB_WEBHOOK_SECRET` is absent in secure mode.
- Configure the secret and restart the service.

**Hostile CORS origin is accepted**
- The running service is stale or bypassing `secure_api`.
- Check `systemctl cat work-intelligence` and confirm `ExecStart` uses `aftergraph-work-intelligence`.
- Restart only after the deployed checkout/package matches the intended revision.

**`No publisher destinations configured`**
- Set `AFTERGRAPH_PUBLISHER=renos` or `AFTERGRAPH_PUBLISHER=works` as required.
- Ensure the configured destination is reachable.

**Database locked**
- Check for concurrent writers.
- Verify file ownership and permissions.
- Do not use shared SQLite/NFS as a multi-writer scaling strategy.

### Debug Mode

For production-like debugging, keep the secure boundary:

```bash
export AFTERGRAPH_LOG_LEVEL=DEBUG
aftergraph-work-intelligence
```

Use `python -m aftergraph_work_intelligence.api` only for isolated local development.

---

## Security Considerations

1. **Fail closed**: Public traffic must enter through `secure_api`.
2. **Token rotation**: Rotate `AFTERGRAPH_API_TOKEN` and API keys through a controlled process.
3. **Webhook HMAC**: Configure and rotate `AFTERGRAPH_GITHUB_WEBHOOK_SECRET` deliberately.
4. **Evidence HMAC**: Use a strong random `AFTERGRAPH_EVIDENCE_SECRET`.
5. **CORS**: Use explicit production origins. Never deploy `*` with credentialed CORS.
6. **Secrets**: Keep secrets out of source, compose files, unit files, and shell history.
7. **Database permissions**: Restrict SQLite data and backup permissions.
8. **TLS**: Terminate HTTPS at a trusted edge or reverse proxy.
9. **Verification**: Re-run the public security probe after every deployment.

---

## Support

- GitHub: https://github.com/Aftergraph/work-intelligence-v2
- Issues: https://github.com/Aftergraph/work-intelligence-v2/issues
