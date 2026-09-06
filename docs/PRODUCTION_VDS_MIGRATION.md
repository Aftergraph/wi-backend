# Production VDS canonical migration

This runbook closes the remaining runtime-layout work tracked by issue #10 after the secure boundary and private Cloudflare origin have already been verified.

## Decision

The production API runs as the dedicated `work-intelligence` system identity. Secrets live in `/etc/aftergraph/work-intelligence.env`, owned `root:work-intelligence` with mode `0640`. The VDS installs `deploy/systemd/work-intelligence-vds.service` as the complete backend unit, so the temporary backend drop-in is no longer authoritative after migration.

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
4. it copies existing secret material rather than generating replacements;
5. it removes only the topology keys before appending their measured canonical values;
6. it backs up legacy env, canonical env, unit, and temporary backend drop-in under `/var/backups/aftergraph`;
7. it creates the dedicated service identity only when absent;
8. it installs the complete VDS unit before retiring the temporary backend drop-in;
9. it restarts the service and requires local health `200`, protected unauthenticated `401/403`, and hostile CORS `403`.

The migration does not rotate `AFTERGRAPH_API_TOKEN`, GitHub webhook material, WORKS enrollment material, or any other existing secret line.

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
ss -ltnp | grep ':8090'
```

Expected state:

- `User=work-intelligence`
- `Group=work-intelligence`
- secure `aftergraph-work-intelligence` ExecStart
- backend listener on `172.17.0.1:8090`
- no backend `10-secure-entrypoint.conf` dependency
- canonical env mode `0640`

Finally rerun the independent `Production Security Verify Once` GitHub workflow. Issue #10 is complete only when exact deployed source, local runtime checks, and the independent public verifier all agree.

## Rollback evidence

The migration prints the timestamped backup directory. Do not delete it during the same change window. If rollback is required, restore the backed-up unit/env/drop-in, run `systemctl daemon-reload`, restart `work-intelligence`, and rerun the same local plus external verification gates before declaring recovery.
