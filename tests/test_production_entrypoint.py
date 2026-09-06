from __future__ import annotations

import tomllib
from pathlib import Path

from fastapi.testclient import TestClient

from aftergraph_work_intelligence import secure_api


def test_console_script_targets_secure_production_boundary() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert (
        pyproject["project"]["scripts"]["aftergraph-work-intelligence"]
        == "aftergraph_work_intelligence.secure_api:main"
    )


def test_secure_module_exposes_cli_main() -> None:
    assert callable(getattr(secure_api, "main", None))


def test_secure_factory_honors_aftergraph_db_when_path_is_omitted(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    env_db = tmp_path / "data" / "from-env.db"
    monkeypatch.setenv("AFTERGRAPH_DB", str(env_db))

    app = secure_api.create_app(api_token="test-token")
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200

    assert env_db.exists()
    assert not (tmp_path / "aftergraph-work-intelligence.db").exists()


def test_docker_entrypoint_targets_secure_factory() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "aftergraph_work_intelligence.secure_api:create_app" in dockerfile
    assert '"--factory"' in dockerfile


def test_dockerfile_installs_real_package_after_source_copy() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    copy_source = dockerfile.index("COPY src/ src/")
    install_package = dockerfile.index("RUN pip install --no-cache-dir .")
    assert copy_source < install_package
    assert "pip install --no-cache-dir -e ." not in dockerfile
    assert "|| pip install" not in dockerfile


def test_docker_runtime_persists_database_and_uses_stdlib_healthcheck() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "AFTERGRAPH_DB=/data/aftergraph-work-intelligence.db" in dockerfile
    assert "import urllib.request" in dockerfile
    assert "import httpx" not in dockerfile


def test_compose_keeps_database_on_volume_and_has_no_dev_only_health_dependency() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert "AFTERGRAPH_DB=/data/aftergraph-work-intelligence.db" in compose
    assert "import urllib.request" in compose
    assert "import httpx" not in compose


def test_systemd_documentation_uses_secure_console_script() -> None:
    deployment = Path("docs/DEPLOYMENT.md").read_text(encoding="utf-8")
    assert "ExecStart=/opt/work-intelligence/.venv/bin/aftergraph-work-intelligence" in deployment
    assert (
        "ExecStart=/opt/work-intelligence/.venv/bin/python -m aftergraph_work_intelligence.api"
        not in deployment
    )
