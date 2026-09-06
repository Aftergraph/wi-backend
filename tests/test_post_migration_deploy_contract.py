from pathlib import Path

SCRIPT = Path("scripts/deploy-production-vds.sh")


def test_deploy_helper_uses_canonical_post_migration_runtime() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'ENV_FILE="/etc/aftergraph/work-intelligence.env"' in text
    assert 'UNIT_SOURCE="deploy/systemd/work-intelligence-vds.service"' in text
    assert "work-intelligence-webhook.secret" not in text
    assert "VDS_BACKEND_OVERRIDE" not in text
    assert "VDS_FRONTEND_OVERRIDE" not in text
    assert "FRONTEND_DROPIN" not in text
    assert 'systemctl restart "$FRONTEND_SERVICE"' not in text


def test_install_unit_replaces_the_complete_backend_unit() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'UNIT_DEST="/etc/systemd/system/${SERVICE}.service"' in text
    assert 'root cp -a "$UNIT_DEST" "$UNIT_BACKUP"' in text
    assert 'root install -m 0644 "$REPO_DIR/$UNIT_SOURCE" "$UNIT_DEST"' in text
    assert 'systemctl show "$SERVICE" -p User --value' in text
    assert 'systemctl show "$SERVICE" -p Group --value' in text
    assert 'systemctl show "$SERVICE" -p EnvironmentFiles --value' in text
