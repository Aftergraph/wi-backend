#!/usr/bin/env bash
set -Eeuo pipefail

LEGACY_ENV=/etc/work-intelligence-webhook.secret
CANONICAL_ENV=/etc/aftergraph/work-intelligence.env
SERVICE=work-intelligence
REPO_DIR=/opt/work-intelligence
UNIT_SOURCE=deploy/systemd/work-intelligence-vds.service
UNIT_PATH=/etc/systemd/system/work-intelligence.service
BACKEND_DROPIN=/etc/systemd/system/work-intelligence.service.d/10-secure-entrypoint.conf
BACKUP_DIR=/var/backups/aftergraph
LOCAL_API=http://172.17.0.1:8090
APPLY=0
PREFLIGHT_ONLY=0

usage() {
  cat <<'EOF'
Usage: sudo bash scripts/migrate-production-vds.sh [--preflight-only|--apply]

Migrates the measured production VDS from the legacy environment/drop-in layout
onto the canonical dedicated work-intelligence service contract. Existing secret
values are copied, never regenerated, and secret values are never printed.

  --preflight-only  Validate prerequisites without mutating the host.
  --apply           Backup current state, migrate, restart, and locally verify.
EOF
}

fail() {
  printf 'VDS_MIGRATION=FAIL reason=%s\n' "$*" >&2
  exit 1
}

while (($#)); do
  case "$1" in
    --preflight-only) PREFLIGHT_ONLY=1; shift ;;
    --apply) APPLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown argument: $1" ;;
  esac
done

if ((APPLY && PREFLIGHT_ONLY)); then
  fail "choose either --preflight-only or --apply"
fi
if ((APPLY == 0 && PREFLIGHT_ONLY == 0)); then
  PREFLIGHT_ONLY=1
fi

for cmd in python3 systemctl curl install cp rm grep getent runuser; do
  command -v "$cmd" >/dev/null 2>&1 || fail "required command missing: $cmd"
done

if ((EUID != 0)); then
  fail "run as root so legacy secret material can be read without weakening permissions"
fi

[[ -d "$REPO_DIR/.git" ]] || fail "production checkout missing: $REPO_DIR"
[[ -f "$REPO_DIR/$UNIT_SOURCE" ]] || fail "canonical VDS unit missing from checkout"
[[ -x "$REPO_DIR/.venv/bin/aftergraph-work-intelligence" ]] || fail "secure console entrypoint missing"
[[ -f "$LEGACY_ENV" || -f "$CANONICAL_ENV" ]] || fail "neither legacy nor canonical env file exists"

SOURCE_ENV="$CANONICAL_ENV"
if [[ ! -f "$SOURCE_ENV" ]]; then
  SOURCE_ENV="$LEGACY_ENV"
fi

python3 - "$SOURCE_ENV" <<'PY'
from __future__ import annotations

import pathlib
import sys

path = pathlib.Path(sys.argv[1])
keys: set[str] = set()
for raw in path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    if value.strip():
        keys.add(key.strip())
required = {
    "AFTERGRAPH_API_TOKEN",
    "AFTERGRAPH_EVIDENCE_SECRET",
    "AFTERGRAPH_GITHUB_WEBHOOK_SECRET",
    "AFTERGRAPH_WORKS_URL",
    "AFTERGRAPH_WORKS_ENROLL_SECRET",
    "AFTERGRAPH_WORKS_WORKER_ID",
}
missing = sorted(required - keys)
if missing:
    raise SystemExit("missing required production env keys: " + ",".join(missing))
print("env_required_keys=PASS")
PY

printf 'source_env=%s\n' "$SOURCE_ENV"
printf 'canonical_env=%s\n' "$CANONICAL_ENV"
printf 'service_user=work-intelligence\n'
printf 'listener=172.17.0.1:8090\n'

if ((PREFLIGHT_ONLY)); then
  if getent passwd work-intelligence >/dev/null; then
    runuser -u work-intelligence -- "$REPO_DIR/.venv/bin/aftergraph-work-intelligence" --help >/dev/null \
      || fail "existing work-intelligence identity cannot execute the production runtime"
    printf 'service_identity_runtime=PASS\n'
  else
    printf 'service_identity_runtime=deferred user_not_created_yet\n'
  fi
  printf 'VDS_MIGRATION_PREFLIGHT=PASS\n'
  exit 0
fi

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_BACKUP="$BACKUP_DIR/work-intelligence-migration-$TIMESTAMP"
install -d -m 0750 "$RUN_BACKUP"

if [[ -f "$LEGACY_ENV" ]]; then
  cp -a "$LEGACY_ENV" "$RUN_BACKUP/legacy.env"
fi
if [[ -f "$CANONICAL_ENV" ]]; then
  cp -a "$CANONICAL_ENV" "$RUN_BACKUP/canonical.env"
fi
if [[ -f "$UNIT_PATH" ]]; then
  cp -a "$UNIT_PATH" "$RUN_BACKUP/work-intelligence.service"
fi
if [[ -f "$BACKEND_DROPIN" ]]; then
  cp -a "$BACKEND_DROPIN" "$RUN_BACKUP/10-secure-entrypoint.conf"
fi

if ! getent group work-intelligence >/dev/null; then
  groupadd --system work-intelligence
fi
if ! getent passwd work-intelligence >/dev/null; then
  useradd --system --gid work-intelligence --home-dir /nonexistent --shell /usr/sbin/nologin work-intelligence
fi

# Prove the exact service identity can traverse the checkout, invoke the venv
# interpreter behind the console script, and import the package before changing
# the live unit. This catches root-only uv/python symlink layouts fail-closed.
runuser -u work-intelligence -- "$REPO_DIR/.venv/bin/aftergraph-work-intelligence" --help >/dev/null \
  || fail "work-intelligence identity cannot execute the production runtime; leave the current service untouched"
printf 'service_identity_runtime=PASS\n'

install -d -m 0755 /etc/aftergraph
install -d -o work-intelligence -g work-intelligence -m 0750 /var/lib/work-intelligence
install -d -o work-intelligence -g work-intelligence -m 0750 "$REPO_DIR/logs"

TMP_ENV="$(mktemp)"
trap 'rm -f "$TMP_ENV"' EXIT
python3 - "$SOURCE_ENV" "$TMP_ENV" <<'PY'
from __future__ import annotations

import pathlib
import sys

source = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
runtime = {
    "AFTERGRAPH_DB": "/var/lib/work-intelligence/wi.db",
    "AFTERGRAPH_HOST": "172.17.0.1",
    "AFTERGRAPH_PORT": "8090",
    "AFTERGRAPH_CORS_ORIGINS": "https://work-intelligence.rendetalje.dk",
}
kept: list[str] = []
for raw in source.read_text(encoding="utf-8").splitlines():
    stripped = raw.strip()
    if stripped and not stripped.startswith("#") and "=" in stripped:
        key = stripped.split("=", 1)[0].strip()
        if key in runtime:
            continue
    kept.append(raw)
kept.append("")
kept.append("# Canonical VDS runtime values; non-secret and topology-bound.")
for key, value in runtime.items():
    kept.append(f"{key}={value}")
target.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")
PY

install -o root -g work-intelligence -m 0640 "$TMP_ENV" "$CANONICAL_ENV"
chown -R work-intelligence:work-intelligence /var/lib/work-intelligence "$REPO_DIR/logs"
install -o root -g root -m 0644 "$REPO_DIR/$UNIT_SOURCE" "$UNIT_PATH"

# The full VDS unit now owns the secure entrypoint and private listener contract.
# Retire the temporary backend override only after the canonical unit is installed.
rm -f "$BACKEND_DROPIN"

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
systemctl restart "$SERVICE"

for _ in $(seq 1 30); do
  code="$(curl -sS --connect-timeout 2 --max-time 5 -o /tmp/work-intelligence-migrate-health -w '%{http_code}' "$LOCAL_API/healthz" || true)"
  [[ "$code" == "200" ]] && break
  sleep 1
done
[[ "${code:-000}" == "200" ]] || fail "local health did not recover after canonical service migration"

protected="$(curl -sS --connect-timeout 3 --max-time 10 -o /dev/null -w '%{http_code}' "$LOCAL_API/v1/work-items?tenant_id=smoke-prod")"
[[ "$protected" == "401" || "$protected" == "403" ]] || fail "protected endpoint is not fail-closed after migration"

cors="$(curl -sS --connect-timeout 3 --max-time 10 -X OPTIONS -o /dev/null -w '%{http_code}' \
  -H 'Origin: https://evil.example' \
  -H 'Access-Control-Request-Method: POST' \
  "$LOCAL_API/v1/observations")"
[[ "$cors" == "403" ]] || fail "hostile CORS preflight is not denied after migration"

printf 'backup_dir=%s\n' "$RUN_BACKUP"
printf 'local_health=200\n'
printf 'local_protected=%s\n' "$protected"
printf 'local_hostile_cors=%s\n' "$cors"
printf 'VDS_MIGRATION=PASS\n'
