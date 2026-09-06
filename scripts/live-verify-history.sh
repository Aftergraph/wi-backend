#!/usr/bin/env bash
set -euo pipefail
TOKEN=$(grep AFTERGRAPH_API_TOKEN /etc/aftergraph/work-intelligence.env | cut -d= -f2)
curl -sS "http://172.17.0.1:8090/v1/autonomy/decisions/history?limit=3" -H "Authorization: Bearer $TOKEN"
