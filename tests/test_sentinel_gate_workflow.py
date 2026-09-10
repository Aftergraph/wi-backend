from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_sentinel_caller_grants_only_required_read_permissions() -> None:
    workflow = (REPO / ".github" / "workflows" / "sentinel-gate.yml").read_text(encoding="utf-8")

    assert "permissions: {}" not in workflow
    assert "permissions:\n  contents: read\n  checks: read\n  pull-requests: read\n" in workflow
