import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _workflow_job_block(workflow: str, job_name: str) -> str:
    lines = workflow.splitlines()
    marker = f"  {job_name}:"
    start = lines.index(marker) + 1
    block = []

    for line in lines[start:]:
        if line.startswith("  ") and not line.startswith("    "):
            break
        block.append(line)

    return "\n".join(block)


def test_workflow_job_block_excludes_pinned_decoys_outside_the_privileged_job() -> None:
    workflow = """jobs:
  auto-merge:
    uses: Aftergraph/.github/.github/workflows/auto-merge-dependabot.yml@main
  decoy:
    run: |
      uses: Aftergraph/.github/.github/workflows/auto-merge-dependabot.yml@56d70aace61364978e23cd75c9925a54aa706e18
"""

    auto_merge_job = _workflow_job_block(workflow, "auto-merge")

    assert "@main" in auto_merge_job
    assert "56d70aace61364978e23cd75c9925a54aa706e18" not in auto_merge_job


def test_runtime_venv_symlink_is_ignored() -> None:
    patterns = {
        line.strip()
        for line in Path(".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert ".venv" in patterns


def test_generated_egg_info_is_not_tracked() -> None:
    tracked = subprocess.check_output(
        ["git", "ls-files", "src/*.egg-info/*"],
        text=True,
    ).splitlines()

    assert tracked == []


def test_privileged_reusable_workflow_is_pinned_to_an_immutable_sha() -> None:
    workflow = (REPO / ".github" / "workflows" / "auto-merge.yml").read_text(encoding="utf-8")
    auto_merge_job = _workflow_job_block(workflow, "auto-merge")

    assert "pull_request_target" in workflow
    assert "pull-requests: write" in workflow
    assert re.search(
        r"^    secrets:\n      automerge_pat: \$\{\{ secrets\.AUTOMERGE_PAT \}\}$",
        auto_merge_job,
        flags=re.MULTILINE,
    )
    uses = re.search(
        r"^    uses:\s+(Aftergraph/\.github/\.github/workflows/auto-merge-dependabot\.yml@[^\s#]+)",
        auto_merge_job,
        flags=re.MULTILINE,
    )
    assert uses
    assert re.fullmatch(
        r"Aftergraph/\.github/\.github/workflows/auto-merge-dependabot\.yml@[0-9a-f]{40}",
        uses.group(1),
    )


def test_required_workflows_support_merge_queue_checks() -> None:
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    codeql = (REPO / ".github" / "workflows" / "codeql.yml").read_text(encoding="utf-8")

    merge_group_trigger = re.compile(
        r"^  merge_group:\n    types: \[checks_requested\]$",
        flags=re.MULTILINE,
    )

    for workflow in (ci, codeql):
        assert "  push:" in workflow
        assert "  pull_request:" in workflow
        assert merge_group_trigger.search(workflow)

    assert "  schedule:" in codeql
    assert 'python-version: ["3.11", "3.12"]' in ci
    assert "  production-container-smoke:" in ci
    assert "  analyze:" in codeql
    assert "    name: Analyze" in codeql
    assert "        language: [python]" in codeql
