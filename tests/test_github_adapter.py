"""TDD tests for the GitHub source adapter.

The adapter receives raw GitHub webhook payloads (push, pull_request, issues,
check_run, workflow_run, issue_comment, pull_request_review) and yields
canonical ObservationInput instances — never WorkItems directly.
"""
from __future__ import annotations

from aftergraph_work_intelligence.adapters import GitHubAdapter, SourceAdapter


def _push_payload(**overrides):
    payload = {
        "tenant_id": "default",
        "repository": {
            "full_name": "Aftergraph/work-intelligence-v2",
            "name": "work-intelligence-v2",
        },
        "ref": "refs/heads/main",
        "after": "37d9b07d2818c63088d0351112b5892bd0fabfdf",
        "head_commit": {
            "id": "37d9b07d2818c63088d0351112b5892bd0fabfdf",
            "message": "feat: add GitHub adapter",
            "author": {"name": "Jonas Abde", "username": "JonasAbde"},
            "timestamp": "2026-09-05T10:00:00Z",
        },
        "commits": [
            {
                "id": "37d9b07d2818c63088d0351112b5892bd0fabfdf",
                "message": "feat: add GitHub adapter",
                "author": {"name": "Jonas Abde", "username": "JonasAbde"},
                "timestamp": "2026-09-05T10:00:00Z",
            }
        ],
        "pusher": {"name": "JonasAbde"},
    }
    payload.update(overrides)
    return payload


def _pr_payload(**overrides):
    payload = {
        "tenant_id": "default",
        "action": "opened",
        "pull_request": {
            "number": 42,
            "title": "Add GitHub adapter",
            "body": "Full org-wide integration.",
            "state": "open",
            "user": {"login": "JonasAbde"},
            "created_at": "2026-09-05T10:00:00Z",
            "html_url": "https://github.com/Aftergraph/work-intelligence-v2/pull/42",
        },
        "repository": {"full_name": "Aftergraph/work-intelligence-v2"},
    }
    payload.update(overrides)
    return payload


def _issue_payload(**overrides):
    payload = {
        "tenant_id": "default",
        "action": "opened",
        "issue": {
            "number": 101,
            "title": "Bug: rate limit too low",
            "body": "Customers hit 429 on burst.",
            "state": "open",
            "user": {"login": "reporter-xyz"},
            "created_at": "2026-09-05T10:00:00Z",
            "labels": [{"name": "bug"}],
            "html_url": "https://github.com/Aftergraph/work-intelligence-v2/issues/101",
        },
        "repository": {"full_name": "Aftergraph/work-intelligence-v2"},
    }
    payload.update(overrides)
    return payload


def _check_run_payload(**overrides):
    payload = {
        "tenant_id": "default",
        "action": "completed",
        "check_run": {
            "id": 555,
            "head_sha": "37d9b07d",
            "status": "completed",
            "conclusion": "failure",
            "name": "cross-repo",
            "html_url": "https://github.com/Aftergraph/work-intelligence-v2/actions/runs/555",
            "started_at": "2026-09-05T10:00:00Z",
            "completed_at": "2026-09-05T10:05:00Z",
        },
        "repository": {"full_name": "Aftergraph/work-intelligence-v2"},
    }
    payload.update(overrides)
    return payload


def _workflow_run_payload(**overrides):
    payload = {
        "tenant_id": "default",
        "action": "completed",
        "workflow_run": {
            "id": 777,
            "name": "CI",
            "head_branch": "main",
            "head_sha": "37d9b07d",
            "status": "completed",
            "conclusion": "success",
            "display_title": "ci: fix runtime.image path",
            "html_url": "https://github.com/Aftergraph/work-intelligence-v2/actions/runs/777",
            "created_at": "2026-09-05T10:00:00Z",
            "updated_at": "2026-09-05T10:06:00Z",
        },
        "repository": {"full_name": "Aftergraph/work-intelligence-v2"},
    }
    payload.update(overrides)
    return payload


def _issue_comment_payload(**overrides):
    payload = {
        "tenant_id": "default",
        "action": "created",
        "issue": {
            "number": 101,
            "title": "Bug: rate limit too low",
            "user": {"login": "reporter-xyz"},
            "html_url": "https://github.com/Aftergraph/work-intelligence-v2/issues/101",
        },
        "comment": {
            "id": 999,
            "user": {"login": "JonasAbde"},
            "body": "This is fixed in #43. Closing.",
            "created_at": "2026-09-05T12:00:00Z",
        },
        "repository": {"full_name": "Aftergraph/work-intelligence-v2"},
    }
    payload.update(overrides)
    return payload


def test_github_adapter_is_source_adapter():
    assert issubclass(GitHubAdapter, SourceAdapter)
    assert GitHubAdapter().source == "github"


# ---------- push ----------

def test_push_emits_one_observation_per_commit():
    payload = _push_payload()
    inputs = list(GitHubAdapter().observations(payload))
    assert len(inputs) == 1
    o = inputs[0]
    assert o.source == "github"
    assert o.actor == "JonasAbde"
    assert o.external_id == "Aftergraph/work-intelligence-v2:37d9b07d2818c63088d0351112b5892bd0fabfdf"
    assert o.metadata["event"] == "push"
    assert o.metadata["repo"] == "Aftergraph/work-intelligence-v2"
    assert o.metadata["branch"] == "main"
    assert o.metadata["sha"] == "37d9b07d2818c63088d0351112b5892bd0fabfdf"
    assert o.metadata["commit"] == "feat: add GitHub adapter"
    assert o.title_hint == "Push to work-intelligence-v2: feat: add GitHub adapter"


def test_push_with_multiple_commits_emits_all():
    payload = _push_payload(commits=[
        {"id": "a1", "message": "first commit", "author": {"username": "dev1"},
         "timestamp": "2026-09-05T10:00:00Z"},
        {"id": "b2", "message": "second commit", "author": {"username": "dev2"},
         "timestamp": "2026-09-05T10:01:00Z"},
    ])
    inputs = list(GitHubAdapter().observations(payload))
    assert len(inputs) == 2
    assert {o.metadata["sha"] for o in inputs} == {"a1", "b2"}
    assert {o.external_id.split(":")[-1] for o in inputs} == {"a1", "b2"}


def test_push_to_non_main_branch_marks_branch():
    payload = _push_payload(ref="refs/heads/dev/feature-x")
    inputs = list(GitHubAdapter().observations(payload))
    assert len(inputs) == 1
    assert inputs[0].metadata["branch"] == "dev/feature-x"


# ---------- pull_request ----------

def test_pr_opened_emits_observation():
    payload = _pr_payload(action="opened")
    inputs = list(GitHubAdapter().observations(payload))
    assert len(inputs) == 1
    o = inputs[0]
    assert o.source == "github"
    assert o.metadata["event"] == "pull_request"
    assert o.metadata["action"] == "opened"
    assert o.metadata["pr_number"] == 42
    assert o.metadata["title"] == "Add GitHub adapter"
    assert o.actor == "JonasAbde"
    assert o.external_id == "Aftergraph/work-intelligence-v2:pr:42:opened"
    assert o.title_hint == "PR #42 opened: Add GitHub adapter"


def test_pr_review_requested_emits_observation():
    payload = _pr_payload(action="review_requested",
                          requested_reviewers=[{"login": "senior-dev"}])
    inputs = list(GitHubAdapter().observations(payload))
    assert len(inputs) == 1
    o = inputs[0]
    assert o.metadata["action"] == "review_requested"
    assert o.metadata["requested_reviewers"] == ["senior-dev"]
    assert "review" in o.text.lower() or "review" in o.title_hint.lower()


def test_pr_merged_emits_observation():
    payload = _pr_payload(
        action="closed",
        pull_request={
            "number": 42,
            "title": "Add GitHub adapter",
            "body": "Merged now.",
            "state": "closed",
            "merged": True,
            "merged_at": "2026-09-05T11:00:00Z",
            "user": {"login": "JonasAbde"},
            "html_url": "https://github.com/Aftergraph/work-intelligence-v2/pull/42",
        },
    )
    inputs = list(GitHubAdapter().observations(payload))
    assert len(inputs) == 1
    o = inputs[0]
    assert o.metadata["action"] == "closed"
    assert o.metadata["merged"] is True
    assert "merged" in o.text.lower() or "merged" in o.title_hint.lower()


# ---------- issues ----------

def test_issue_opened_emits_observation():
    payload = _issue_payload()
    inputs = list(GitHubAdapter().observations(payload))
    assert len(inputs) == 1
    o = inputs[0]
    assert o.source == "github"
    assert o.metadata["event"] == "issues"
    assert o.metadata["action"] == "opened"
    assert o.metadata["issue_number"] == 101
    assert o.metadata["labels"] == ["bug"]
    assert o.actor == "reporter-xyz"
    assert o.external_id == "Aftergraph/work-intelligence-v2:issue:101:opened"
    assert o.title_hint == "Issue #101 opened: Bug: rate limit too low"


def test_issue_closed_emits_observation():
    payload = _issue_payload(action="closed",
                             issue={**_issue_payload()["issue"], "state": "closed"})
    inputs = list(GitHubAdapter().observations(payload))
    assert len(inputs) == 1
    assert inputs[0].metadata["action"] == "closed"
    assert inputs[0].metadata["state"] == "closed"


# ---------- check_run / workflow_run ----------

def test_check_run_failure_emits_observation():
    payload = _check_run_payload(action="completed", check_run={
        **_check_run_payload()["check_run"], "conclusion": "failure"})
    inputs = list(GitHubAdapter().observations(payload))
    assert len(inputs) == 1
    o = inputs[0]
    assert o.metadata["event"] == "check_run"
    assert o.metadata["conclusion"] == "failure"
    assert o.priority_hint in {"high", "critical"}
    assert "failed" in o.text.lower() or "failed" in o.title_hint.lower()


def test_check_run_success_no_observation():
    payload = _check_run_payload(action="completed", check_run={
        **_check_run_payload()["check_run"], "conclusion": "success"})
    inputs = list(GitHubAdapter().observations(payload))
    # Success is not actionable — no observation
    assert inputs == []


def test_workflow_run_failure_emits_observation():
    payload = _workflow_run_payload(action="completed", workflow_run={
        **_workflow_run_payload()["workflow_run"], "conclusion": "failure"})
    inputs = list(GitHubAdapter().observations(payload))
    assert len(inputs) == 1
    o = inputs[0]
    assert o.metadata["event"] == "workflow_run"
    assert o.metadata["conclusion"] == "failure"
    assert "failed" in o.text.lower() or "failed" in o.title_hint.lower()


def test_workflow_run_success_no_observation():
    payload = _workflow_run_payload(action="completed", workflow_run={
        **_workflow_run_payload()["workflow_run"], "conclusion": "success"})
    inputs = list(GitHubAdapter().observations(payload))
    assert inputs == []


# ---------- issue_comment / pull_request_review ----------

def test_issue_comment_mentions_followup():
    payload = _issue_comment_payload(body="Let's do this now. @team")
    inputs = list(GitHubAdapter().observations(payload))
    assert len(inputs) == 1
    o = inputs[0]
    assert o.metadata["event"] == "issue_comment"
    assert o.actor == "JonasAbde"
    assert o.external_id == "Aftergraph/work-intelligence-v2:comment:999"
    assert o.metadata["issue_number"] == 101


def test_issue_comment_bot_no_observation():
    payload = _issue_comment_payload(
        comment={**_issue_comment_payload()["comment"], "user": {"login": "github-actions[bot]"}})
    inputs = list(GitHubAdapter().observations(payload))
    assert inputs == []


def test_pr_review_requested_comment():
    payload = _pr_payload(action="review_requested",
                          requested_reviewers=[{"login": "team-lead"}])
    inputs = list(GitHubAdapter().observations(payload))
    assert len(inputs) == 1
    assert "team-lead" in inputs[0].text or "team-lead" in inputs[0].title_hint


# ---------- resilience ----------

def test_unknown_event_emits_nothing():
    payload = _push_payload()
    payload = {"tenant_id": "default", "repository": {"full_name": "x/y"}, "action": "ping"}
    inputs = list(GitHubAdapter().observations(payload))
    assert inputs == []


def test_missing_repository_safe():
    inputs = list(GitHubAdapter().observations({"tenant_id": "default"}))
    assert inputs == []


def test_ignores_bot_users_globally():
    bot_commit = {"id": "x1", "message": "chore: bump", "author": {"username": "dependabot[bot]"}}
    payload = _push_payload(head_commit=bot_commit, commits=[bot_commit])
    inputs = list(GitHubAdapter().observations(payload))
    assert inputs == []


def test_tenant_id_preserved():
    payload = _push_payload(tenant_id="acme")
    inputs = list(GitHubAdapter().observations(payload))
    assert all(o.tenant_id == "acme" for o in inputs)