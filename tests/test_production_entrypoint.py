from __future__ import annotations

import tomllib
from pathlib import Path

from aftergraph_work_intelligence import secure_api


def test_console_script_targets_secure_production_boundary() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert (
        pyproject["project"]["scripts"]["aftergraph-work-intelligence"]
        == "aftergraph_work_intelligence.secure_api:main"
    )


def test_secure_module_exposes_cli_main() -> None:
    assert callable(getattr(secure_api, "main", None))


def test_docker_entrypoint_targets_secure_factory() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "aftergraph_work_intelligence.secure_api:create_app" in dockerfile
    assert '"--factory"' in dockerfile


def test_systemd_documentation_uses_secure_console_script() -> None:
    deployment = Path("docs/DEPLOYMENT.md").read_text(encoding="utf-8")
    assert "ExecStart=/opt/work-intelligence/.venv/bin/aftergraph-work-intelligence" in deployment
    assert (
        "ExecStart=/opt/work-intelligence/.venv/bin/python -m aftergraph_work_intelligence.api"
        not in deployment
    )
