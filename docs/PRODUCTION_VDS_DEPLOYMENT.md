# Production VDS Deployment Runbook

This runbook is the production promotion path for the backend behind `https://intel.rendetalje.dk`.

## Safety model

The deployment is intentionally exact-SHA and fail-closed. It refuses to run when:

- the requested SHA is not the current `origin/main` head;
- the deployment checkout is dirty;
- required production environment keys are missing;
- CORS is not restricted to `https://work-intelligence.rendetalje.dk`;
- the backend is not bound to `127.0.0.1:8090` behind the tunnel;
- the public service bypasses the secure console entrypoint;
- local or public post-deploy security probes fail.

Secret values are never printed by the deployment script.

## 1. Read-only preflight

Run this first on the actual production VDS:

```bash
cd /opt/work-intelligence
bash scripts/deploy-production-vds.sh \
  --sha 9065e48575a941848720379e277a8320335c03e3 \
  --install-unit \
  --preflight-only
```

`--install-unit` in preflight mode means "validate that the canonical unit can be installed". It does not modify systemd during preflight.

Expected terminal marker:

```text
DEPLOYMENT_PREFLIGHT=PASS
```

## 2. Deploy the verified exact head

```bash
cd /opt/work-intelligence
bash scripts/deploy-production-vds.sh \
  --sha 9065e48575a941848720379e277a8320335c03e3 \
  --install-unit
```

The script performs, in order:

1. host/repo/environment preflight;
2. exact `origin/main` SHA verification;
3. online SQLite backup with Python's SQLite backup API;
4. fast-forward checkout to the verified target;
5. production package installation into the existing venv;
6. backup and installation of the canonical hardened systemd unit;
7. daemon reload, enable, and restart;
8. local health/auth/CORS/security-header verification;
9. public health/auth/CORS/security-header verification.

Successful completion ends with:

```text
DEPLOYMENT=PASS
```

It also prints the previous SHA and backup paths so rollback evidence is preserved.

## 3. Independent external gate

After the VDS script passes, rerun the repository workflow `Production Security Verify Once`. The production promotion is not closed until the external runner independently observes:

- `/healthz` = `200`;
- unauthenticated protected API = `401` or `403`;
- hostile-origin CORS preflight = `403`;
- hostile origin is not echoed as an allowed origin;
- HSTS, CSP, `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, and `Permissions-Policy` are present.

## 4. Failure handling

If restart or post-deploy verification fails, do not improvise destructive recovery. Record:

```bash
git -C /opt/work-intelligence rev-parse HEAD
systemctl status --no-pager work-intelligence
journalctl -u work-intelligence -n 100 --no-pager
```

The deploy script records the previous SHA, database backup, and any replaced unit-file backup. Use those exact artifacts for a deliberate rollback after diagnosing the failure.

## Current promotion target

The security-boundary remediation was merged as:

```text
9065e48575a941848720379e277a8320335c03e3
```

If `origin/main` moves beyond this SHA, the script intentionally refuses the command above. Verify the newer exact head and its CI before changing the deployment target. A green branch from yesterday is not evidence for today's production binary, despite computers' continuing campaign to make this seem optional.
