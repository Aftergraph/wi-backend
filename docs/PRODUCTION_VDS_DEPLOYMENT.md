# Production VDS Deployment Runbook

This runbook is the production promotion path for the backend behind `https://intel.rendetalje.dk`.

## Safety model

The deployment is intentionally exact-SHA and fail-closed. It refuses to run when:

- the requested SHA is not the current `origin/main` head;
- the deployment checkout is dirty;
- required production environment keys are missing;
- CORS is not restricted to `https://work-intelligence.rendetalje.dk`;
- the database is not `/var/lib/work-intelligence/data.db`;
- the backend is not bound to `127.0.0.1:8090` behind the tunnel;
- the effective public systemd process bypasses the secure console entrypoint;
- local or public post-deploy security probes fail.

Secret values are never printed by the deployment script.

## 1. Bootstrap the deploy script from the verified exact head

Do not run a deployment helper from the stale production checkout. Fetch `main`, verify the exact green SHA you intend to promote, then materialize that script into `/tmp` without changing the worktree:

```bash
REPO=/opt/work-intelligence
TARGET=<VERIFIED_MAIN_SHA>

git -c "safe.directory=$REPO" -C "$REPO" fetch --prune origin main
test "$(git -c "safe.directory=$REPO" -C "$REPO" rev-parse origin/main)" = "$TARGET"
git -c "safe.directory=$REPO" -C "$REPO" show \
  "$TARGET:scripts/deploy-production-vds.sh" \
  > /tmp/work-intelligence-deploy.sh
bash -n /tmp/work-intelligence-deploy.sh
```

`TARGET` must be the exact `main` commit whose final CI you already verified. Copy-pasting an older green SHA is intentionally rejected.

## 2. Read-only preflight

Run the bootstrapped helper first with no production mutation:

```bash
bash /tmp/work-intelligence-deploy.sh \
  --sha "$TARGET" \
  --install-unit \
  --preflight-only
```

`--install-unit` in preflight mode means "validate that the canonical unit can be installed". It does not modify systemd or create the service user during preflight.

Expected terminal marker:

```text
DEPLOYMENT_PREFLIGHT=PASS
```

## 3. Deploy the verified exact head

```bash
bash /tmp/work-intelligence-deploy.sh \
  --sha "$TARGET" \
  --install-unit
```

The script performs, in order:

1. host/repo/environment preflight;
2. exact `origin/main` SHA verification;
3. exact-target verification that the canonical systemd unit exists;
4. online SQLite backup with Python's SQLite backup API;
5. fast-forward checkout to the verified target;
6. production package installation into the existing venv;
7. creation of the unprivileged `work-intelligence` system account if needed;
8. backup and installation of the canonical hardened systemd unit;
9. verification of the **effective** systemd `ExecStart`, including drop-ins;
10. daemon reload, enable, and restart;
11. local health/auth/CORS/security-header verification;
12. public health/auth/CORS/security-header verification.

Successful completion ends with:

```text
DEPLOYMENT=PASS
```

It also prints the previous SHA and backup paths so rollback evidence is preserved.

## 4. Independent external gate

After the VDS script passes, rerun the repository workflow `Production Security Verify Once`. The production promotion is not closed until the external runner independently observes:

- `/healthz` = `200`;
- unauthenticated protected API = `401` or `403`;
- hostile-origin CORS preflight = `403`;
- hostile origin is not echoed as an allowed origin;
- HSTS, CSP, `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, and `Permissions-Policy` are present.

## 5. Failure handling

If restart or post-deploy verification fails, do not improvise destructive recovery. Record:

```bash
git -C /opt/work-intelligence rev-parse HEAD
systemctl status --no-pager work-intelligence
journalctl -u work-intelligence -n 100 --no-pager
```

The deploy script records the previous SHA, database backup, and any replaced unit-file backup. Use those exact artifacts for a deliberate rollback after diagnosing the failure.

## Promotion authority

The deployment target is deliberately **not hard-coded in this document**. Every promotion uses the current exact `origin/main` SHA only after its final CI evidence is green. A green branch from yesterday is not evidence for today's production binary, despite computers' continuing campaign to make this seem optional.
