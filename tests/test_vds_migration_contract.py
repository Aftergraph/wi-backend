from pathlib import Path


def test_vds_has_full_canonical_service_unit() -> None:
    unit = Path("deploy/systemd/work-intelligence-vds.service").read_text(encoding="utf-8")

    assert "User=work-intelligence" in unit
    assert "Group=work-intelligence" in unit
    assert "EnvironmentFile=/etc/aftergraph/work-intelligence.env" in unit
    assert "ExecStart=/opt/work-intelligence/.venv/bin/aftergraph-work-intelligence" in unit
    assert "Environment=AFTERGRAPH_DB=/var/lib/work-intelligence/wi.db" in unit
    assert "Environment=AFTERGRAPH_HOST=172.17.0.1" in unit
    assert "Environment=AFTERGRAPH_PORT=8090" in unit


def test_vds_migration_is_reversible_and_preserves_legacy_secrets() -> None:
    script = Path("scripts/migrate-production-vds.sh").read_text(encoding="utf-8")

    assert "LEGACY_ENV=/etc/work-intelligence-webhook.secret" in script
    assert "CANONICAL_ENV=/etc/aftergraph/work-intelligence.env" in script
    assert "--preflight-only" in script
    assert "work-intelligence-vds.service" in script
    assert "/var/backups/aftergraph" in script
    assert "cp -a \"$LEGACY_ENV\"" in script
    assert "rm -f \"$BACKEND_DROPIN\"" in script
    assert "secret values are never printed" in script.lower()


def test_vds_migration_bootstraps_evidence_secret_only_when_absent() -> None:
    script = Path("scripts/migrate-production-vds.sh").read_text(encoding="utf-8")

    assert '"AFTERGRAPH_EVIDENCE_SECRET"' in script
    assert "secrets.token_urlsafe" in script
    assert "evidence_secret=preserved" in script
    assert "evidence_secret=generated" in script


def test_vds_migration_rehomes_python_outside_root_before_service_user_cutover() -> None:
    script = Path("scripts/migrate-production-vds.sh").read_text(encoding="utf-8")

    assert 'RUNTIME_PYTHON_DIR="$REPO_DIR/.runtime-python"' in script
    assert 'uv python install --install-dir "$RUNTIME_PYTHON_DIR"' in script
    assert "UV_PYTHON_INSTALL_DIR=\"$RUNTIME_PYTHON_DIR\"" in script
    assert 'cp -a "$REPO_DIR/.venv" "$RUN_BACKUP/venv"' in script
    assert 'runuser -u work-intelligence -- "$REPO_DIR/.venv/bin/aftergraph-work-intelligence" --help' in script
