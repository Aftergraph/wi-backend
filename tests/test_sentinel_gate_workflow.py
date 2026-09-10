import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "sentinel-gate.yml"
EXPECTED_ENGINE_SHA = "8240ed40c376240795b324d46520d13ca8023e04"


def load_workflow() -> dict:
    # BaseLoader keeps GitHub's `on` key as the literal string "on".
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_sentinel_caller_has_exact_security_boundary() -> None:
    workflow = load_workflow()

    assert workflow["permissions"] == {
        "contents": "read",
        "checks": "read",
        "pull-requests": "read",
    }
    assert set(workflow["jobs"]) == {"sentinel-gate"}
    job = workflow["jobs"]["sentinel-gate"]
    assert set(job) == {"uses"}


def test_sentinel_caller_has_exact_triggers() -> None:
    triggers = load_workflow()["on"]
    assert set(triggers) == {"pull_request_target", "push"}
    assert triggers["pull_request_target"] == {
        "types": ["opened", "synchronize", "ready_for_review"]
    }
    assert triggers["push"] == {"branches": ["main"]}


def test_sentinel_caller_pins_current_engine_without_secret_passthrough() -> None:
    job = load_workflow()["jobs"]["sentinel-gate"]
    uses = job["uses"]
    assert re.fullmatch(
        r"Aftergraph/\.github/\.github/workflows/agent-review\.yml@[0-9a-f]{40}",
        uses,
    )
    assert uses.endswith(f"@{EXPECTED_ENGINE_SHA}")
    assert "secrets" not in job
    assert "with" not in job
    assert "permissions" not in job
