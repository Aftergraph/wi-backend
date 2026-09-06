from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path("scripts/deploy-production-vds.sh")
UNIT = Path("deploy/systemd/work-intelligence.service")


def test_deploy_script_has_valid_bash_syntax() -> None:
    subprocess.run(["bash", "-n", SCRIPT.as_posix()], check=True)


def test_deploy_help_is_non_mutating_and_documents_exact_sha_gate() -> None:
    result = subprocess.run(
        ["bash", SCRIPT.as_posix(), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "--sha <40-char-main-sha>" in result.stdout
    assert "--preflight-only" in result.stdout
    assert "--install-unit" in result.stdout
    assert "/tmp/work-intelligence-deploy.sh" in result.stdout


def test_deploy_script_fails_closed_on_source_env_auth_cors_and_headers() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert '[[ "$REMOTE_SHA" == "$TARGET_SHA" ]]' in text
    assert "git_repo status --porcelain" in text
    assert 'git_repo cat-file -e "${TARGET_SHA}:${UNIT_SOURCE}"' in text
    assert "AFTERGRAPH_API_TOKEN" in text
    assert '"AFTERGRAPH_EVIDENCE_SECRET",' not in text
    assert "AFTERGRAPH_GITHUB_WEBHOOK_SECRET" in text
    assert "AFTERGRAPH_CORS_ORIGINS" in text
    assert 'values.get("AFTERGRAPH_DB", "/var/lib/work-intelligence/wi.db") != "/var/lib/work-intelligence/wi.db"' in text
    assert 'values.get("AFTERGRAPH_HOST", "172.17.0.1") != "172.17.0.1"' in text
    assert 'if port != "8090":' in text
    assert "sqlite3.connect" in text
    assert "src.backup(dst)" in text
    assert "useradd --system --user-group" not in text
    assert 'uv pip install --python "$REPO_DIR/.venv/bin/python" -e "$REPO_DIR"' in text
    assert 'VDS_BACKEND_OVERRIDE="deploy/systemd/work-intelligence-vds.conf"' in text
    assert 'VDS_FRONTEND_OVERRIDE="deploy/systemd/work-intelligence-web-vds.conf"' in text
    assert 'systemctl restart "$FRONTEND_SERVICE"' in text
    assert 'systemctl show "$SERVICE" -p ExecStart --value' in text
    assert "/v1/work-items?tenant_id=smoke-prod" in text
    assert "Origin: https://evil.example" in text
    assert "strict-transport-security" in text
    assert "content-security-policy" in text
    assert "x-content-type-options" in text
    assert "x-frame-options" in text
    assert "referrer-policy" in text
    assert "permissions-policy" in text


def test_canonical_systemd_unit_uses_secure_entrypoint_and_hardening() -> None:
    text = UNIT.read_text(encoding="utf-8")

    assert "User=work-intelligence" in text
    assert "Group=work-intelligence" in text
    assert "EnvironmentFile=/etc/aftergraph/work-intelligence.env" in text
    assert "ExecStart=/opt/work-intelligence/.venv/bin/aftergraph-work-intelligence" in text
    assert "NoNewPrivileges=true" in text
    assert "PrivateTmp=true" in text
    assert "ProtectSystem=strict" in text
    assert "ProtectHome=true" in text
    assert "ReadWritePaths=/var/lib/work-intelligence /opt/work-intelligence/logs" in text


def test_vds_deploy_defaults_match_measured_production_contract() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    runbook = Path("docs/PRODUCTION_VDS_DEPLOYMENT.md").read_text(encoding="utf-8")

    assert 'ENV_FILE="/etc/work-intelligence-webhook.secret"' in text
    assert 'LOCAL_API="http://172.17.0.1:8090"' in text
    assert "/var/lib/work-intelligence/wi.db" in runbook
    assert "172.17.0.1:8090" in runbook
    assert "renos-control-edge" in runbook
    assert "172.21.0.0/16" in runbook
