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
RUNTIME_PYTHON_DIR="/opt/work-intelligence-runtime/python"
VENV_ROOT="/opt/work-intelligence-runtime/venvs"
RUNTIME_PYTHON_VERSION=3.11.16
APPLY=0
PREFLIGHT_ONLY=0
MUTATION_STARTED=0
MIGRATION_SUCCEEDED=0
RUN_BACKUP=""
VENV_SLOT=""
HAD_CANONICAL_ENV=0
HAD_UNIT=0
HAD_DROPIN=0

usage() {
  cat <<'EOF'
Usage: sudo bash scripts/migrate-production-vds.sh [--preflight-only|--apply]

Migrates the measured production VDS onto the canonical dedicated
work-intelligence service contract. Existing secret values are preserved and
secret values are never printed. If the legacy host has no explicit evidence
signing secret, apply mode creates one exactly once in the canonical env file.

  --preflight-only  Validate prerequisites without mutating the host.
  --apply           Backup current state, migrate, restart, and locally verify.
EOF
}

fail() {
  printf 'VDS_MIGRATION=FAIL reason=%s\n' "$*" >&2
  exit 1
}

rollback() {
  local rc=$?
  if ((rc != 0 && MUTATION_STARTED && MIGRATION_SUCCEEDED == 0)); then
    printf 'rollback=attempting\n' >&2
    systemctl stop "$SERVICE" >/dev/null 2>&1 || true

    if [[ -n "$RUN_BACKUP" && -e "$RUN_BACKUP/venv" ]]; then
      rm -rf "$REPO_DIR/.venv"
      cp -a "$RUN_BACKUP/venv" "$REPO_DIR/.venv" || true
    fi
    if ((HAD_CANONICAL_ENV)); then
      cp -a "$RUN_BACKUP/canonical.env" "$CANONICAL_ENV" || true
    else
      rm -f "$CANONICAL_ENV"
    fi
    if ((HAD_UNIT)); then
      cp -a "$RUN_BACKUP/work-intelligence.service" "$UNIT_PATH" || true
    fi
    if ((HAD_DROPIN)); then
      install -d -m 0755 "$(dirname "$BACKEND_DROPIN")"
      cp -a "$RUN_BACKUP/10-secure-entrypoint.conf" "$BACKEND_DROPIN" || true
    else
      rm -f "$BACKEND_DROPIN"
    fi
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl restart "$SERVICE" >/dev/null 2>&1 || true
    printf 'rollback=attempted\n' >&2
  fi
  exit "$rc"
}
trap rollback EXIT

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

for cmd in python3 systemctl curl install cp rm grep getent runuser uv ln readlink; do
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

ENV_STATE="$(python3 - "$SOURCE_ENV" <<'PY'
from __future__ import annotations

import pathlib
import sys

path = pathlib.Path(sys.argv[1])
values: dict[str, str] = {}
for raw in path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    values[key.strip()] = value.strip()
required = {
    "AFTERGRAPH_API_TOKEN",
    "AFTERGRAPH_GITHUB_WEBHOOK_SECRET",
    "AFTERGRAPH_WORKS_URL",
    "AFTERGRAPH_WORKS_ENROLL_SECRET",
    "AFTERGRAPH_WORKS_WORKER_ID",
}
missing = sorted(key for key in required if not values.get(key))
if missing:
    raise SystemExit("missing required production env keys: " + ",".join(missing))
print("present" if values.get("AFTERGRAPH_EVIDENCE_SECRET") else "absent")
PY
)" || fail "production env validation failed"

CURRENT_PYTHON="$(readlink -f "$REPO_DIR/.venv/bin/python" 2>/dev/null || true)"
printf 'source_env=%s\n' "$SOURCE_ENV"
printf 'canonical_env=%s\n' "$CANONICAL_ENV"
printf 'service_user=work-intelligence\n'
printf 'listener=172.17.0.1:8090\n'
printf 'evidence_secret_present=%s\n' "$ENV_STATE"
printf 'current_python=%s\n' "$CURRENT_PYTHON"
case "$CURRENT_PYTHON" in
  /root/*) printf 'current_runtime_root_bound=yes\n' ;;
  *) printf 'current_runtime_root_bound=no\n' ;;
esac

if ((PREFLIGHT_ONLY)); then
  printf 'VDS_MIGRATION_PREFLIGHT=PASS\n'
  exit 0
fi

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_BACKUP="$BACKUP_DIR/work-intelligence-migration-$TIMESTAMP"
VENV_SLOT="$VENV_ROOT/$TIMESTAMP"
install -d -m 0750 "$RUN_BACKUP"
MUTATION_STARTED=1

if [[ -f "$LEGACY_ENV" ]]; then
  cp -a "$LEGACY_ENV" "$RUN_BACKUP/legacy.env"
fi
if [[ -f "$CANONICAL_ENV" ]]; then
  HAD_CANONICAL_ENV=1
  cp -a "$CANONICAL_ENV" "$RUN_BACKUP/canonical.env"
fi
if [[ -f "$UNIT_PATH" ]]; then
  HAD_UNIT=1
  cp -a "$UNIT_PATH" "$RUN_BACKUP/work-intelligence.service"
fi
if [[ -f "$BACKEND_DROPIN" ]]; then
  HAD_DROPIN=1
  cp -a "$BACKEND_DROPIN" "$RUN_BACKUP/10-secure-entrypoint.conf"
fi
cp -a "$REPO_DIR/.venv" "$RUN_BACKUP/venv"

if ! getent group work-intelligence >/dev/null; then
  groupadd --system work-intelligence
fi
if ! getent passwd work-intelligence >/dev/null; then
  useradd --system --gid work-intelligence --home-dir /nonexistent --shell /usr/sbin/nologin work-intelligence
fi

install -d -o root -g root -m 0755 /opt/work-intelligence-runtime "$RUNTIME_PYTHON_DIR" "$VENV_ROOT"
uv python install --install-dir "$RUNTIME_PYTHON_DIR" --no-bin "$RUNTIME_PYTHON_VERSION"
RUNTIME_PYTHON="$(UV_PYTHON_INSTALL_DIR="$RUNTIME_PYTHON_DIR" uv python find 3.11.16 --managed-python --resolve-links)"
case "$RUNTIME_PYTHON" in
  "$RUNTIME_PYTHON_DIR"/*) ;;
  *) fail "uv resolved runtime outside canonical runtime directory" ;;
esac
chmod -R a+rX /opt/work-intelligence-runtime

uv venv --python "$RUNTIME_PYTHON" "$VENV_SLOT"
uv pip install --python "$VENV_SLOT/bin/python" "$REPO_DIR"
runuser -u work-intelligence -- "$VENV_SLOT/bin/aftergraph-work-intelligence" --help >/dev/null \
  || fail "work-intelligence identity cannot execute canonical production runtime"
printf 'service_identity_runtime=PASS\n'

install -d -m 0755 /etc/aftergraph
install -d -o work-intelligence -g work-intelligence -m 0750 /var/lib/work-intelligence
install -d -o work-intelligence -g work-intelligence -m 0750 /var/lib/work-intelligence/logs
install -d -o work-intelligence -g work-intelligence -m 0750 "$REPO_DIR/logs"
if [[ -e /var/lib/work-intelligence/wi.db ]]; then
  chown work-intelligence:work-intelligence /var/lib/work-intelligence/wi.db
  chmod 0640 /var/lib/work-intelligence/wi.db
fi

TMP_ENV="$(mktemp)"
EVIDENCE_STATE="$(python3 - "$SOURCE_ENV" "$TMP_ENV" <<'PY'
from __future__ import annotations

import pathlib
import secrets
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
evidence_present = False
for raw in source.read_text(encoding="utf-8").splitlines():
    stripped = raw.strip()
    if stripped and not stripped.startswith("#") and "=" in stripped:
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key in runtime:
            continue
        if key == "AFTERGRAPH_EVIDENCE_SECRET":
            if value.strip():
                evidence_present = True
                kept.append(raw)
            continue
    kept.append(raw)
if not evidence_present:
    kept.append(f"AFTERGRAPH_EVIDENCE_SECRET={secrets.token_urlsafe(48)}")
kept.append("")
kept.append("# Canonical VDS runtime values; non-secret and topology-bound.")
for key, value in runtime.items():
    kept.append(f"{key}={value}")
target.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")
print("preserved" if evidence_present else "generated")
PY
)" || fail "failed to build canonical production env"
install -o root -g work-intelligence -m 0640 "$TMP_ENV" "$CANONICAL_ENV"
rm -f "$TMP_ENV"
if [[ "$EVIDENCE_STATE" == "preserved" ]]; then
  printf 'evidence_secret=preserved\n'
else
  printf 'evidence_secret=generated\n'
fi

install -o root -g root -m 0644 "$REPO_DIR/$UNIT_SOURCE" "$UNIT_PATH"

systemctl stop "$SERVICE"
rm -rf "$REPO_DIR/.venv"
ln -s "$VENV_SLOT" "$REPO_DIR/.venv"
runuser -u work-intelligence -- "$REPO_DIR/.venv/bin/aftergraph-work-intelligence" --help >/dev/null \
  || fail "canonical .venv link is not executable by service identity"

# The complete VDS unit now owns the secure entrypoint and private listener.
rm -f "$BACKEND_DROPIN"
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
systemctl restart "$SERVICE"

code=000
for _ in $(seq 1 30); do
  code="$(curl -sS --connect-timeout 2 --max-time 5 -o /tmp/work-intelligence-migrate-health -w '%{http_code}' "$LOCAL_API/healthz" || true)"
  [[ "$code" == "200" ]] && break
  sleep 1
done
[[ "$code" == "200" ]] || fail "local health did not recover after canonical service migration"

protected="$(curl -sS --connect-timeout 3 --max-time 10 -o /dev/null -w '%{http_code}' "$LOCAL_API/v1/work-items?tenant_id=smoke-prod")"
[[ "$protected" == "401" || "$protected" == "403" ]] || fail "protected endpoint is not fail-closed after migration"

cors="$(curl -sS --connect-timeout 3 --max-time 10 -X OPTIONS -o /dev/null -w '%{http_code}' \
  -H 'Origin: https://evil.example' \
  -H 'Access-Control-Request-Method: POST' \
  "$LOCAL_API/v1/observations")"
[[ "$cors" == "403" ]] || fail "hostile CORS preflight is not denied after migration"

MIGRATION_SUCCEEDED=1
printf 'backup_dir=%s\n' "$RUN_BACKUP"
printf 'runtime_python=%s\n' "$RUNTIME_PYTHON"
printf 'runtime_venv=%s\n' "$VENV_SLOT"
printf 'local_health=200\n'
printf 'local_protected=%s\n' "$protected"
printf 'local_hostile_cors=%s\n' "$cors"
printf 'VDS_MIGRATION=PASS\n'
