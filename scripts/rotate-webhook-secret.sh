#!/usr/bin/env bash
# Rotate AFTERGRAPH_WEBHOOK_SECRET in production env, then restart the unit.
# The new value is generated on the server and NEVER printed to stdout.
set -Eeuo pipefail

ENV_FILE="/etc/aftergraph/work-intelligence.env"
SERVICE="work-intelligence"

# Back up current env (contains the old secret, keep it out of the repo).
cp "$ENV_FILE" "${ENV_FILE}.bak.$(date +%Y%m%dT%H%M%S)"
chmod 600 "${ENV_FILE}.bak."*

# Generate a fresh 64-hex secret in-place (python3, no echo of the value).
python3 - <<'PY'
import re, secrets
path = "/etc/aftergraph/work-intelligence.env"
with open(path) as f:
    text = f.read()
new_secret = secrets.token_hex(32)
if re.search(r"^AFTERGRAPH_WEBHOOK_SECRET=.*$", text, re.M):
    text = re.sub(r"^AFTERGRAPH_WEBHOOK_SECRET=.*$", "AFTERGRAPH_WEBHOOK_SECRET=" + new_secret, text, flags=re.M)
else:
    text = text.rstrip("\n") + "\nAFTERGRAPH_WEBHOOK_SECRET=" + new_secret + "\n"
with open(path, "w") as f:
    f.write(text)
PY
chmod 600 "$ENV_FILE"

# EnvironmentFile changes require daemon-reload, not just restart.
systemctl daemon-reload
systemctl restart "$SERVICE"

# Wait for the service to come up, then probe readiness.
for i in $(seq 1 15); do
  if systemctl is-active --quiet "$SERVICE" && curl -sf -o /dev/null http://172.17.0.1:8090/healthz; then
    break
  fi
  sleep 2
done

systemctl is-active "$SERVICE"
curl -s -o /dev/null -w "healthz_http=%{http_code}\n" http://172.17.0.1:8090/healthz
# Verify the new secret is actually in the process environ (not just the file).
if tr '\0' '\n' < /proc/$(systemctl show -p MainPID --value "$SERVICE")/environ | grep -q '^AFTERGRAPH_WEBHOOK_SECRET=[0-9a-f]\{64\}$'; then
  echo "webhook_secret_rotated=YES (64-hex present in process environ)"
else
  echo "webhook_secret_rotated=NO"
  exit 1
fi
