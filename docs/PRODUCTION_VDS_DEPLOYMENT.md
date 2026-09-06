# Production VDS Deployment Runbook

This is the exact-SHA promotion path for the Work Intelligence backend behind
`https://intel.rendetalje.dk` on the measured production host `vmi3517816`.

## Measured production contract

Verified on 2026-09-06:

- checkout: `/opt/work-intelligence`
- backend service: `work-intelligence.service`
- frontend service: `work-intelligence-web.service`
- backend listener: `172.17.0.1:8090`
- frontend listener: port `3001`
- frontend API proxy: `http://172.17.0.1:8090`
- named Cloudflare Tunnel network: `renos-control-edge` (`172.21.0.0/16`)
- named tunnel API origin: `http://172.17.0.1:8090`
- SQLite database: `/var/lib/work-intelligence/wi.db`
- current secret source: `/etc/work-intelligence-webhook.secret`
- UFW permits `8090/tcp` from `172.21.0.0/16`

The backend must not be changed to `127.0.0.1:8090` while the named tunnel
origin remains `172.17.0.1:8090`. That mismatch was reproduced in production as
an external `502`. Binding to every interface is also unnecessary: the current
contract binds only the Docker bridge address used by the named tunnel.

The old random `trycloudflare.com` quick tunnel is not part of production and
was removed after the named tunnel was independently verified.

## Safety model

The deployment is exact-SHA and fail-closed. It refuses to promote when:

- the requested SHA is not the current `origin/main` head;
- the deployment worktree is dirty;
- the measured production secret file is unreadable or core auth/integration
  keys are missing;
- DB, listener, port, or CORS settings conflict with the measured VDS contract;
- the VDS backend/frontend override files are absent from the target SHA;
- the effective backend process bypasses the secure console entrypoint;
- local or public post-deploy security probes fail.

`AFTERGRAPH_EVIDENCE_SECRET` is intentionally not made a deploy prerequisite in
this migration. The currently running service historically used the application's
legacy evidence-secret default. Rotating that material requires a versioned
key-rotation design so old evidence does not become unverifiable.

## 1. Bootstrap from the verified exact head

```bash
REPO=/opt/work-intelligence
TARGET=<VERIFIED_MAIN_SHA>

git -c "safe.directory=$REPO" -C "$REPO" fetch --prune origin main
test "$(git -c "safe.directory=$REPO" -C "$REPO" rev-parse origin/main)" = "$TARGET"
git -c "safe.directory=$REPO" -C "$REPO" show   "$TARGET:scripts/deploy-production-vds.sh"   > /tmp/work-intelligence-deploy.sh
bash -n /tmp/work-intelligence-deploy.sh
```

## 2. Read-only preflight

```bash
bash /tmp/work-intelligence-deploy.sh   --sha "$TARGET"   --install-unit   --preflight-only
```

`--install-unit` is retained for CLI compatibility. On this VDS it means
"install the checked-in VDS systemd overrides", not "replace the legacy base
unit with a different service-account/runtime layout".

Expected marker:

```text
DEPLOYMENT_PREFLIGHT=PASS
```

## 3. Deploy

```bash
bash /tmp/work-intelligence-deploy.sh   --sha "$TARGET"   --install-unit
```

The helper performs:

1. exact-head and clean-worktree checks;
2. production secret/config validation without printing secret values;
3. online SQLite backup with Python's backup API;
4. fast-forward checkout to the verified SHA;
5. editable package refresh with `uv pip` against the existing production venv;
6. backup and installation of the checked-in backend and frontend VDS drop-ins;
7. backend and frontend restart;
8. secure-entrypoint verification;
9. local API health/auth/CORS/security-header checks;
10. local frontend authenticated `/api/*` proxy check;
11. public API health/auth/CORS/security-header checks;
12. public frontend and `/api/*` proxy checks.

Successful completion ends with `DEPLOYMENT=PASS` and prints the previous SHA
and backup paths.

## 4. Independent external gate

After any production restart or topology change, rerun `Production Security
Verify Once`. Production is not verified until an external runner observes:

- health `200`;
- unauthenticated protected API `401` or `403`;
- hostile-origin preflight `403`;
- HSTS, CSP, `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, and
  `Permissions-Policy` on the API response.

## 5. Failure handling

Capture evidence before changing anything else:

```bash
git -C /opt/work-intelligence rev-parse HEAD
systemctl status --no-pager work-intelligence work-intelligence-web
journalctl -u work-intelligence -n 100 --no-pager
```

Use the exact database and drop-in backups emitted by the deployment helper for
a deliberate rollback. Do not improvise around a failed security probe. A green
CI badge is charming, but it is not a substitute for the process actually
serving the public hostname.
