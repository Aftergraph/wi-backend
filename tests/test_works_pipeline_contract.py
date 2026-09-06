from pathlib import Path


def test_works_pipeline_matches_repository_ci_contract() -> None:
    pipeline = Path("works.yml").read_text(encoding="utf-8")

    assert "version: 1" in pipeline
    assert "- push" in pipeline
    assert "- pull_request" in pipeline
    assert "pool: avc-core" in pipeline
    assert "uv venv .venv-works" in pipeline
    assert "-e '.[dev]' ruff" in pipeline
    assert ".venv-works/bin/ruff check src/ tests/" in pipeline
    assert ".venv-works/bin/python -m pytest --tb=short -q" in pipeline
    assert "timeout_s: 600" in pipeline
