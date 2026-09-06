#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="/opt/work-intelligence"
SERVICE="work-intelligence"
FRONTEND_SERVICE="work-intelligence-web"
ENV_FILE="/etc/work-intelligence-webhook.secret"
UNIT_SOURCE="deploy/systemd/work-intelligence.service"
VDS_BACKEND_OVERRIDE="deploy/systemd/work-intelligence-vds.conf"
VDS_FRONTEND_OVERRIDE="deploy/systemd/work-intelligence-web-vds.conf"
LOCAL_API="http://172.17.0.1:8090"
LOCAL_FRONTEND="http://127.0.0.1:3001"
PUBLIC_API="https://intel.rendetalje.dk"
PUBLIC_FRONTEND="https://work-intelligence.rendetalje.dk"
TARGET_SHA=""
INSTALL_UNIT=0
PREFLIGHT_ONLY=0
SKIP_PUBLIC=0

usage() {
  cat <<'EOF'
Usage:
  bash /tmp/work-intelligence-deploy.sh --sha <40-char-main-sha> [options]

Options:
  --sha SHA            Exact verified commit to deploy. Required.
  --install-unit       Install the checked-in VDS systemd overrides before restart.
  --preflight-only     Validate host/repo/env/unit prerequisites without changing production.
  --skip-public        Skip the final public-hostname security probe.
  --repo-dir PATH      Deployment checkout. Default: /opt/work-intelligence
  --service NAME       systemd service. Default: work-intelligence
  --env-file PATH      Production environment file. Default: /etc/work-intelligence-webhook.secret
  --local-api URL      Local API base. Default: http://172.17.0.1:8090
  --public-api URL     Public API base. Default: https://intel.rendetalje.dk
  -h, --help           Show this help.

Bootstrap the script from the verified target commit into /tmp before running
it. The deploy refuses stale/non-main SHAs, dirty worktrees, missing production
secrets, an unexpected DB/host/port/CORS configuration, or an insecure systemd
entrypoint. Secret values are never printed.
EOF
}

fail() {
  printf 'DEPLOYMENT=FAIL reason=%s\n' "$*" >&2
  exit 1
}

while (($#)); do
  case "$1" in
    --sha)
      (($# >= 2)) || fail "--sha requires a value"
      TARGET_SHA="$2"
      shift 2
      ;;
    --install-unit)
      INSTALL_UNIT=1
      shift
      ;;
    --preflight-only)
      PREFLIGHT_ONLY=1
      shift
      ;;
    --skip-public)
      SKIP_PUBLIC=1
      shift
      ;;
    --repo-dir)
      (($# >= 2)) || fail "--repo-dir requires a value"
      REPO_DIR="$2"
      shift 2
      ;;
    --service)
      (($# >= 2)) || fail "--service requires a value"
      SERVICE="$2"
      shift 2
      ;;
    --env-file)
      (($# >= 2)) || fail "--env-file requires a value"
      ENV_FILE="$2"
      shift 2
      ;;
    --local-api)
      (($# >= 2)) || fail "--local-api requires a value"
      LOCAL_API="$2"
      shift 2
      ;;
    --public-api)
      (($# >= 2)) || fail "--public-api requires a value"
      PUBLIC_API="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unknown argument: $1"
      ;;
  esac
done

[[ "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "--sha must be an exact lowercase 40-character Git SHA"

for cmd in git curl python3 systemctl uv; do
  command -v "$cmd" >/dev/null 2>&1 || fail "required command missing: $cmd"
done

SUDO=()
if ((EUID != 0)); then
  command -v sudo >/dev/null 2>&1 || fail "root privileges are required and sudo is unavailable"
  sudo -n true >/dev/null 2>&1 || fail "passwordless sudo is required for production deployment"
  SUDO=(sudo -n)
fi

root() {
  "${SUDO[@]}" "$@"
}

git_repo() {
  git -c "safe.directory=$REPO_DIR" -C "$REPO_DIR" "$@"
}

[[ -d "$REPO_DIR/.git" ]] || fail "deployment checkout missing: $REPO_DIR"
[[ -x "$REPO_DIR/.venv/bin/python" ]] || fail "production virtualenv missing: $REPO_DIR/.venv"
root test -r "$ENV_FILE" || fail "production environment file is not readable: $ENV_FILE"

if [[ -n "$(git_repo status --porcelain)" ]]; then
  fail "deployment checkout is dirty; refusing to overwrite local production state"
fi

git_repo fetch --prune origin main
REMOTE_SHA="$(git_repo rev-parse origin/main)"
[[ "$REMOTE_SHA" == "$TARGET_SHA" ]] || fail "requested SHA is not the current origin/main exact head ($REMOTE_SHA)"
git_repo cat-file -e "${TARGET_SHA}^{commit}" || fail "requested SHA is not available in the deployment checkout"
git_repo cat-file -e "${TARGET_SHA}:${UNIT_SOURCE}" || fail "canonical systemd unit is missing from requested target SHA"
git_repo cat-file -e "${TARGET_SHA}:${VDS_BACKEND_OVERRIDE}" || fail "VDS backend override is missing from requested target SHA"
git_repo cat-file -e "${TARGET_SHA}:${VDS_FRONTEND_OVERRIDE}" || fail "VDS frontend override is missing from requested target SHA"

DB_PATH="$(root python3 - "$ENV_FILE" <<'PY'
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
    key = key.strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    values[key] = value

required = (
    "AFTERGRAPH_API_TOKEN",
    "AFTERGRAPH_GITHUB_WEBHOOK_SECRET",
    "AFTERGRAPH_WORKS_URL",
    "AFTERGRAPH_WORKS_ENROLL_SECRET",
    "AFTERGRAPH_WORKS_WORKER_ID",
)
missing = [key for key in required if not values.get(key)]
if missing:
    raise SystemExit("missing required production env keys: " + ",".join(missing))

db_path = values.get("AFTERGRAPH_DB", "/var/lib/work-intelligence/wi.db")
host = values.get("AFTERGRAPH_HOST", "172.17.0.1")
port = values.get("AFTERGRAPH_PORT", "8090")
cors = values.get("AFTERGRAPH_CORS_ORIGINS", "https://work-intelligence.rendetalje.dk")

if cors != "https://work-intelligence.rendetalje.dk":
    raise SystemExit("AFTERGRAPH_CORS_ORIGINS is not the production frontend allowlist")
if values.get("AFTERGRAPH_DB", "/var/lib/work-intelligence/wi.db") != "/var/lib/work-intelligence/wi.db":
    raise SystemExit("AFTERGRAPH_DB must be /var/lib/work-intelligence/wi.db")
if values.get("AFTERGRAPH_HOST", "172.17.0.1") != "172.17.0.1":
    raise SystemExit("AFTERGRAPH_HOST must be 172.17.0.1 on the current named-tunnel topology")
if port != "8090":
    raise SystemExit("AFTERGRAPH_PORT must be 8090 on the current VDS topology")

print(db_path)
PY
)" || fail "production environment validation failed"

[[ -n "$DB_PATH" ]] || fail "AFTERGRAPH_DB resolved to an empty path"

if ((INSTALL_UNIT == 0)); then
  UNIT_TEXT="$(root systemctl cat "$SERVICE" 2>/dev/null)" || fail "systemd service is missing: $SERVICE"
  grep -Fq "ExecStart=/opt/work-intelligence/.venv/bin/aftergraph-work-intelligence" <<<"$UNIT_TEXT" \
    || fail "current systemd service bypasses the secure production entrypoint; rerun with --install-unit"
fi

printf 'preflight_target=%s\n' "$TARGET_SHA"
printf 'preflight_origin_main=%s\n' "$REMOTE_SHA"
printf 'preflight_repo=%s\n' "$REPO_DIR"
printf 'preflight_service=%s\n' "$SERVICE"
printf 'preflight_db=%s\n' "$DB_PATH"
printf 'preflight_install_unit=%s\n' "$INSTALL_UNIT"

if ((PREFLIGHT_ONLY)); then
  printf 'DEPLOYMENT_PREFLIGHT=PASS\n'
  exit 0
fi

PREVIOUS_SHA="$(git_repo rev-parse HEAD)"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="/var/backups/aftergraph"
BACKUP_PATH="none"
UNIT_BACKUP="none"

root install -d -m 0750 "$BACKUP_DIR"

if root test -f "$DB_PATH"; then
  BACKUP_PATH="$BACKUP_DIR/work-intelligence-${TIMESTAMP}.db"
  root python3 - "$DB_PATH" "$BACKUP_PATH" <<'PY'
from __future__ import annotations

import sqlite3
import sys

src_path, dst_path = sys.argv[1], sys.argv[2]
with sqlite3.connect(f"file:{src_path}?mode=ro", uri=True) as src:
    with sqlite3.connect(dst_path) as dst:
        src.backup(dst)
PY
  root chmod 0640 "$BACKUP_PATH"
  printf 'database_backup=%s\n' "$BACKUP_PATH"
else
  printf 'database_backup=skipped database_not_present_at=%s\n' "$DB_PATH"
fi

git_repo switch main
git_repo merge --ff-only "$TARGET_SHA"
[[ "$(git_repo rev-parse HEAD)" == "$TARGET_SHA" ]] || fail "checkout did not land on requested exact SHA"
[[ -f "$REPO_DIR/$UNIT_SOURCE" ]] || fail "canonical systemd unit missing after exact-head checkout"
[[ -f "$REPO_DIR/$VDS_BACKEND_OVERRIDE" ]] || fail "VDS backend override missing after exact-head checkout"
[[ -f "$REPO_DIR/$VDS_FRONTEND_OVERRIDE" ]] || fail "VDS frontend override missing after exact-head checkout"

uv pip install --python "$REPO_DIR/.venv/bin/python" -e "$REPO_DIR"

if ((INSTALL_UNIT)); then
  BACKEND_DROPIN_DIR="/etc/systemd/system/${SERVICE}.service.d"
  FRONTEND_DROPIN_DIR="/etc/systemd/system/${FRONTEND_SERVICE}.service.d"
  BACKEND_DROPIN="${BACKEND_DROPIN_DIR}/10-secure-entrypoint.conf"
  FRONTEND_DROPIN="${FRONTEND_DROPIN_DIR}/10-private-backend.conf"

  root install -d -m 0755 "$BACKEND_DROPIN_DIR" "$FRONTEND_DROPIN_DIR"
  if root test -f "$BACKEND_DROPIN"; then
    UNIT_BACKUP="$BACKUP_DIR/10-secure-entrypoint.conf.${TIMESTAMP}"
    root cp -a "$BACKEND_DROPIN" "$UNIT_BACKUP"
    printf 'backend_override_backup=%s\n' "$UNIT_BACKUP"
  fi
  if root test -f "$FRONTEND_DROPIN"; then
    FRONTEND_BACKUP="$BACKUP_DIR/10-private-backend.conf.${TIMESTAMP}"
    root cp -a "$FRONTEND_DROPIN" "$FRONTEND_BACKUP"
    printf 'frontend_override_backup=%s\n' "$FRONTEND_BACKUP"
  fi
  root install -m 0644 "$REPO_DIR/$VDS_BACKEND_OVERRIDE" "$BACKEND_DROPIN"
  root install -m 0644 "$REPO_DIR/$VDS_FRONTEND_OVERRIDE" "$FRONTEND_DROPIN"
fi

root systemctl daemon-reload
EFFECTIVE_EXEC="$(root systemctl show "$SERVICE" -p ExecStart --value 2>/dev/null)" || fail "unable to resolve effective systemd ExecStart"
grep -Fq "/opt/work-intelligence/.venv/bin/aftergraph-work-intelligence" <<<"$EFFECTIVE_EXEC" \
  || fail "effective systemd ExecStart does not use the secure production entrypoint"

root systemctl enable "$SERVICE" "$FRONTEND_SERVICE" >/dev/null
root systemctl restart "$SERVICE"
root systemctl restart "$FRONTEND_SERVICE"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

wait_for_health() {
  local base="$1"
  local code=""
  for _ in $(seq 1 30); do
    code="$(curl -sS --connect-timeout 2 --max-time 5 -o "$TMP_DIR/health-body" -w '%{http_code}' "$base/healthz" || true)"
    if [[ "$code" == "200" ]]; then
      return 0
    fi
    sleep 1
  done
  return 1
}

security_probe() {
  local base="$1"
  local label="$2"
  local protected_code cors_code
  local header_file="$TMP_DIR/${label}-headers"

  curl -sS --connect-timeout 5 --max-time 15 -D "$header_file" -o /dev/null "$base/healthz"

  protected_code="$(curl -sS --connect-timeout 5 --max-time 15 -o /dev/null -w '%{http_code}' \
    "$base/v1/work-items?tenant_id=smoke-prod")"
  if [[ "$protected_code" != "401" && "$protected_code" != "403" ]]; then
    fail "$label protected GET is not fail-closed (status=$protected_code)"
  fi

  cors_code="$(curl -sS --connect-timeout 5 --max-time 15 -X OPTIONS -o /dev/null -w '%{http_code}' \
    -H 'Origin: https://evil.example' \
    -H 'Access-Control-Request-Method: POST' \
    -H 'Access-Control-Request-Headers: authorization,x-api-key,content-type' \
    "$base/v1/observations")"
  [[ "$cors_code" == "403" ]] || fail "$label hostile CORS preflight was not denied (status=$cors_code)"

  for header in \
    strict-transport-security \
    content-security-policy \
    x-content-type-options \
    x-frame-options \
    referrer-policy \
    permissions-policy; do
    grep -qi "^${header}:" "$header_file" || fail "$label missing security header: $header"
  done

  printf '%s_security_probe=PASS protected=%s cors=%s\n' "$label" "$protected_code" "$cors_code"
}

wait_for_health "$LOCAL_API" || {
  root systemctl status --no-pager "$SERVICE" || true
  root journalctl -u "$SERVICE" -n 100 --no-pager || true
  fail "local health did not become ready after restart"
}

security_probe "$LOCAL_API" "local"

frontend_proxy_code="$(curl -sS --connect-timeout 5 --max-time 15 -o /dev/null -w '%{http_code}' \
  "$LOCAL_FRONTEND/api/v1/work-items?tenant_id=smoke-prod")"
[[ "$frontend_proxy_code" == "200" ]] || fail "local frontend API proxy failed (status=$frontend_proxy_code)"
printf 'local_frontend_proxy=PASS status=%s\n' "$frontend_proxy_code"

if ((SKIP_PUBLIC == 0)); then
  wait_for_health "$PUBLIC_API" || fail "public health did not become ready"
  security_probe "$PUBLIC_API" "public"
  public_frontend_code="$(curl -sS --connect-timeout 5 --max-time 15 -o /dev/null -w '%{http_code}' "$PUBLIC_FRONTEND/")"
  public_frontend_api_code="$(curl -sS --connect-timeout 5 --max-time 15 -o /dev/null -w '%{http_code}' \
    "$PUBLIC_FRONTEND/api/v1/work-items?tenant_id=smoke-prod")"
  [[ "$public_frontend_code" == "200" ]] || fail "public frontend failed (status=$public_frontend_code)"
  [[ "$public_frontend_api_code" == "200" ]] || fail "public frontend API proxy failed (status=$public_frontend_api_code)"
  printf 'public_frontend=PASS root=%s api=%s\n' "$public_frontend_code" "$public_frontend_api_code"
fi

printf 'deployed_previous_sha=%s\n' "$PREVIOUS_SHA"
printf 'deployed_target_sha=%s\n' "$TARGET_SHA"
printf 'database_backup=%s\n' "$BACKUP_PATH"
printf 'unit_backup=%s\n' "$UNIT_BACKUP"
printf 'DEPLOYMENT=PASS\n'
