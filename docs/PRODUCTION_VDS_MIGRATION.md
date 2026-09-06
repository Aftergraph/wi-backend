# Production VDS canonical migration

This runbook closes the remaining runtime-layout work tracked by issue #10 after the secure boundary and private Cloudflare origin have already been verified.

## Decision

The production API runs as the dedicated `work-intelligence` system identity. Secrets live in `/etc/aftergraph/work-intelligence.env`, owned `root:work-intelligence` with mode `0640`. The VDS installs `deploy/systemd/work-intelligence-vds.service` as the complete backend unit, so the temporary backend drop-in is no longer authoritative after migration.

The measured VDS currently has a Python 3.11 virtualenv whose interpreter resolves below `/root/.local/share/uv/...`. That layout cannot survive `ProtectHome=true` plus a non-root service identity. The migration therefore installs Python 3.11.16 under `/opt/work-intelligence-runtime/python`, builds a versioned virtualenv under `/opt/work-intelligence-runtime/venvs`, verifies it as `work-intelligence`, and only then switches `/opt/work-intelligence/.venv` to the verified runtime.

The current VDS network contract remains:

- API listener: `172.17.0.1:8090`
- Cloudflare named-tunnel origin: `http://172.17.0.1:8090`
- frontend backend target: `http://172.17.0.1:8090`
- SQLite database: `/var/lib/work-intelligence/wi.db`
- public API: `https://intel.rendetalje.dk`

Do not change the API to loopback while the named tunnel still targets the Docker bridge address.

## Safety properties

`scripts/migrate-production-vds.sh` is deliberately fail-closed:

1. default invocation is preflight-only;
2. it requires the existing production checkout and secure console entrypoint;
3. it validates required environment key names without printing values;
4. it preserves all existing secret lines;
5. if `AFTERGRAPH_EVIDENCE_SECRET` is absent, apply mode generates a new high-entropy signing secret exactly once in the canonical env file and never prints it;
6. it removes only topology keys before appending their measured canonical values;
7. it backs up legacy env, canonical env, systemd unit, temporary backend drop-in, and the current virtualenv under `/var/backups/aftergraph`;
8. it creates the dedicated service identity only when absent;
9. it builds and verifies a service-user-executable Python 3.11 runtime outside `/root` before live cutover;
10. it installs the complete VDS unit before retiring the temporary backend drop-in;
11. failures after mutation trigger rollback of the prior virtualenv, env, unit, and drop-in before the old service is restarted;
12. successful apply requires local health `200`, protected unauthenticated `401/403`, and hostile CORS `403`.

The migration does not rotate `AFTERGRAPH_API_TOKEN`, GitHub webhook material, WORKS enrollment material, or any existing evidence signing secret.

## Execution order

First promote the exact green repository `main` SHA with `scripts/deploy-production-vds.sh`. Do not migrate a stale checkout merely because the host is reachable.

Then run the migration preflight:

```bash
cd /opt/work-intelligence
sudo bash scripts/migrate-production-vds.sh --preflight-only
```

Expected terminal marker:

```text
VDS_MIGRATION_PREFLIGHT=PASS
```

The preflight reports whether the current interpreter is root-bound and whether an explicit evidence signing secret already exists, but never prints secret values.

Apply only after the preflight passes and the external production verifier is currently green:

```bash
sudo bash scripts/migrate-production-vds.sh --apply
```

Expected terminal marker:

```text
VDS_MIGRATION=PASS
```

## Post-migration verification

Verify the effective unit and identity without exposing environment values:

```bash
systemctl show work-intelligence \
  -p User -p Group -p ExecStart -p FragmentPath -p DropInPaths --no-pager

stat -c '%n %U:%G %a' /etc/aftergraph/work-intelligence.env
readlink -f /opt/work-intelligence/.venv/bin/python
ss -ltnp | grep ':8090'
```

Expected state:

- `User=work-intelligence`
- `Group=work-intelligence`
- secure `aftergraph-work-intelligence` ExecStart
- Python interpreter under `/opt/work-intelligence-runtime/`
- backend listener on `172.17.0.1:8090`
- no backend `10-secure-entrypoint.conf` dependency
- canonical env mode `0640`

Finally rerun the independent `Production Security Verify Once` GitHub workflow. Issue #10 is complete only when exact deployed source, local runtime checks, and the independent public verifier all agree.

## Rollback evidence

The migration prints the timestamped backup directory. Do not delete it during the same change window. If rollback is required, restore the backed-up unit/env/drop-in and virtualenv, run `systemctl daemon-reload`, restart `work-intelligence`, and rerun the same local plus external verification gates before declaring recovery.
