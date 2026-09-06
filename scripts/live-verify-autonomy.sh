#!/usr/bin/env bash
set -euo pipefail
TOKEN=$(grep AFTERGRAPH_API_TOKEN /etc/aftergraph/work-intelligence.env | cut -d= -f2)

curl -sS -X POST http://172.17.0.1:8090/v1/autonomy/decisions/evaluate \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "request_id":"adr_liveverify01",
    "tenant_id":"default",
    "repository":"Aftergraph/example",
    "ref":"refs/heads/main",
    "head_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "event_key":"test",
    "capability":"dependency.patch.merge",
    "objective":"test",
    "impact_summary":"test",
    "evidence":[{"kind":"test"}],
    "tests_passed":true,
    "patch_release":true,
    "author_permission_tier":20,
    "changed_files":["src/routes/api.py"]
  }'
