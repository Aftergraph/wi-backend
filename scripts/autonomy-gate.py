#!/usr/bin/env python3
"""Autonomy execution gate: turn the evaluator into a CI gate.

Closes the loop between the fail-closed autonomy evaluator and an actual
execution pipeline. The script:

  1. Scans `git diff <base>...<head> --name-only` for changed files.
  2. Derives the critical signals ITSELF (never trusts caller-declared flags):
     auth/secret/proxy/ssl/security-path touches are detected from filenames.
  3. POSTs an evaluation to /v1/autonomy/decisions/evaluate (bearer token).
  4. Maps the decision envelope to a shell exit code suitable for CI:
       0  = auto_approve  (safe to proceed)
       2  = manual_review (human required, gate blocks)
       3  = blocked       (fail-closed, gate blocks)
       4  = infra/API error (network, 401, 429) -- gate FAILS CLOSED

Usage (bash):
  AFTERGRAPH_API_TOKEN=... ./scripts/autonomy-gate.py \
      --api-url http://172.17.0.1:8090 \
      --repository myorg/myapp --ref refs/heads/feature \
      --capability dependency.patch.merge \
      --objective "Bump axios 1.7 -> 1.8" \
      --impact-summary "CVE-2025-1234 patch, low surface"

Exit codes are the contract; print the decision envelope for audit trails.

ponytail: stdlib-only (urllib), no requests dependency. Single auth path
(bearer) kept deliberately -- webhook HMAC is for push-event automation.
Upgrade path: add --webhook-secret to sign instead of bearer.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from urllib import error, request

# ---- signal derivation (independent of caller-declared flags) --------------
_AUTH_SECRET_PATTERNS = (
    "auth",
    "secret",
    "credential",
    "password",
    "token",
    ".env",
    "api_key",
    "apikey",
    "vault",
    "id_rsa",
    "private_key",
)
_PROXY_SSL_PATTERNS = (
    "proxy",
    "ssl",
    "tls",
    "certificate",
    "cert",
    "nginx",
    "caddy",
    "traefik",
    "haproxy",
    "firewall",
    "routing",
    "waf",
)
_SECURITY_PATH_PREFIXES = ("security/", "auth/", "observability/", "integrations/")


def derive_signals(changed_files: list[str]) -> dict:
    """Independently derive fail-closed signals from changed file paths."""
    auth_or_secret_touched = any(
        p in f.lower() for f in changed_files for p in _AUTH_SECRET_PATTERNS
    )
    proxy_or_ssl_touched = any(
        p in f.lower() for f in changed_files for p in _PROXY_SSL_PATTERNS
    )
    critical_file_touched = any(
        f.lower().startswith(_SECURITY_PATH_PREFIXES) for f in changed_files
    )
    return {
        "auth_or_secret_touched": auth_or_secret_touched,
        "proxy_or_ssl_touched": proxy_or_ssl_touched,
        "critical_file_touched": critical_file_touched,
        "changed_files": changed_files,
    }


def git_diff_names(base: str, head: str) -> list[str]:
    """List files changed between base and head (empty diff -> empty list)."""
    cmd = ["git", "diff", "--name-only", f"{base}...{head}"]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        # Fall back to a plain diff if the triple-dot range is unavailable.
        proc = subprocess.run(
            ["git", "diff", "--name-only", base, head], capture_output=True, text=True, check=False
        )
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def build_request(args: argparse.Namespace, signals: dict) -> dict:
    return {
        "request_id": f"adr_gate{uuid.uuid4().hex[:12]}",
        "tenant_id": args.tenant_id,
        "repository": args.repository,
        "ref": args.ref,
        "head_sha": args.head_sha,
        "event_key": f"ci-gate:{args.repository}:{args.head_sha}",
        "capability": args.capability,
        "objective": args.objective,
        "impact_summary": args.impact_summary,
        "evidence": [
            {
                "kind": "ci-gate",
                "source": "autonomy-gate.py",
                "detail": f"{len(signals['changed_files'])} files changed",
            }
        ],
        "tests_passed": args.tests_passed,
        "patch_release": args.patch_release,
        "auth_or_secret_touched": signals["auth_or_secret_touched"],
        "proxy_or_ssl_touched": signals["proxy_or_ssl_touched"],
        "critical_file_touched": signals["critical_file_touched"],
        "transient_ci_error": args.transient_ci_error,
        "retry_count": args.retry_count,
    }


def post_evaluation(api_url: str, api_token: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    req = request.Request(
        f"{api_url}/v1/autonomy/decisions/evaluate",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with request.urlopen(req, timeout=30) as resp:
            return {"status": resp.status, "body": json.loads(resp.read().decode())}
    except error.HTTPError as exc:
        return {
            "status": exc.code,
            "body": json.loads(exc.read().decode() or "{}"),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default=os.getenv("AFTERGRAPH_API_URL", "http://172.17.0.1:8090"))
    parser.add_argument("--api-token", default=os.getenv("AFTERGRAPH_API_TOKEN"))
    parser.add_argument("--repository", required=True, help="org/repo")
    parser.add_argument("--ref", default="refs/heads/HEAD")
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--base", default="main", help="git base ref to diff against")
    parser.add_argument("--tenant-id", default=os.getenv("AFTERGRAPH_TENANT_ID", "default"))
    parser.add_argument(
        "--capability",
        choices=[
            "dependency.patch.merge",
            "ci.check.retry",
            "deployment.rollback.prepare",
            "github.status.sync",
            "github.suggestion.comment",
            "none",
        ],
        default="dependency.patch.merge",
    )
    parser.add_argument("--objective", required=True)
    parser.add_argument("--impact-summary", required=True)
    parser.add_argument("--tests-passed", action="store_true")
    parser.add_argument("--patch-release", action="store_true")
    parser.add_argument("--transient-ci-error", action="store_true")
    parser.add_argument("--retry-count", type=int, default=0)
    args = parser.parse_args()

    if not args.api_token:
        print("FATAL: --api-token or AFTERGRAPH_API_TOKEN is required", file=sys.stderr)
        return 4

    signals = derive_signals(git_diff_names(args.base, args.head_sha))
    print(
        f"[autonomy-gate] derived signals: auth_or_secret={signals['auth_or_secret_touched']} "
        f"proxy_or_ssl={signals['proxy_or_ssl_touched']} critical={signals['critical_file_touched']} "
        f"({len(signals['changed_files'])} files)",
        file=sys.stderr,
    )

    payload = build_request(args, signals)
    result = post_evaluation(args.api_url, args.api_token, payload)

    if result["status"] != 200:
        print(f"[autonomy-gate] HTTP {result['status']}: {result['body']}", file=sys.stderr)
        return 4  # fail closed on any infra error

    decision = result["body"]
    outcome = decision.get("decision", "blocked")
    risk = decision.get("risk", {}).get("level", "unknown")
    confidence = decision.get("confidence", {}).get("score", 0)
    print(
        json.dumps(
            {
                "schema": decision.get("schema"),
                "decision": outcome,
                "risk_level": risk,
                "confidence": confidence,
                "request_id": decision.get("request_id"),
            },
            indent=2,
        )
    )
    # Exit-code contract: 0=auto_approve (proceed), otherwise the gate blocks.
    return 0 if outcome == "auto_approve" else (2 if outcome == "manual_review" else 3)


if __name__ == "__main__":
    sys.exit(main())
