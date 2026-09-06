#!/usr/bin/env bash
set -euo pipefail
TOKEN=$(grep AFTERGRAPH_API_TOKEN /etc/aftergraph/work-intelligence.env | cut -d= -f2)
WEBHOOK_SECRET=$(grep AFTERGRAPH_WEBHOOK_SECRET /etc/aftergraph/work-intelligence.env | cut -d= -f2)
BASE="http://172.17.0.1:8090"

# Full production request body matching Pydantic model
BODY='{"request_id":"adr_weak6_live","tenant_id":"default","repository":"Aftergraph/work-intelligence-v2","ref":"refs/heads/main","head_sha":"2d09c82dd1081db2ac459562ec05348e7d98cbcd","event_key":"push","capability":"dependency.patch.merge","objective":"Verify webhook HMAC","impact_summary":"Test webhook authentication","evidence":[{"kind":"test","url":"https://ci.example.com/123"}],"tests_passed":true,"patch_release":true,"changed_files":["src/api.py"],"author_permission_tier":15,"test_coverage_delta":10,"critical_path_penalty":0,"auth_or_secret_touched":false,"proxy_or_ssl_touched":false}'

echo "=== evaluate (bearer auth) ==="
curl -sS -X POST "$BASE/v1/autonomy/decisions/evaluate" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d "$BODY" | python3 -m json.tool | head -20

echo
echo "=== webhook HMAC auth (no bearer token) ==="
SIG=$(echo -n "$BODY" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" | awk '{print $2}')
echo "Signature: sha256=${SIG:0:16}..."
RESP=$(curl -sS -w "\n%{http_code}" -X POST "$BASE/v1/autonomy/decisions/evaluate" \
  -H "Content-Type: application/json" \
  -H "X-Hub-Signature-256: sha256=$SIG" \
  -d "$BODY")
HTTP_CODE=$(echo "$RESP" | tail -1)
BODY_RESP=$(echo "$RESP" | sed '$d')
echo "HTTP $HTTP_CODE"
echo "$BODY_RESP" | python3 -m json.tool | head -10

echo
echo "=== tampered body (valid sig, wrong body) ==="
TAMPERED='{"request_id":"adr_tampered","tenant_id":"default","repository":"Aftergraph/work-intelligence-v2","ref":"refs/heads/main","head_sha":"deadbeef00000000000000000000000000000000","event_key":"push","capability":"dependency.patch.merge","objective":"TAMPERED","impact_summary":"Should fail","evidence":[{"kind":"test","url":"https://ci.example.com/123"}],"tests_passed":true,"patch_release":true,"changed_files":["src/api.py"],"author_permission_tier":15,"test_coverage_delta":10,"critical_path_penalty":0,"auth_or_secret_touched":false,"proxy_or_ssl_touched":false}'
RESP2=$(curl -sS -w "\n%{http_code}" -X POST "$BASE/v1/autonomy/decisions/evaluate" \
  -H "Content-Type: application/json" \
  -H "X-Hub-Signature-256: sha256=$SIG" \
  -d "$TAMPERED")
HTTP_CODE2=$(echo "$RESP2" | tail -1)
echo "HTTP $HTTP_CODE2 (should be 401)"
echo "$(echo "$RESP2" | sed '$d')" | python3 -m json.tool

echo
echo "=== no auth at all ==="
HTTP_CODE3=$(curl -sS -w "%{http_code}" -o /dev/null -X POST "$BASE/v1/autonomy/decisions/evaluate" \
  -H "Content-Type: application/json" \
  -d "$BODY")
echo "HTTP $HTTP_CODE3 (should be 401)"
