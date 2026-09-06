from dataclasses import replace

import pytest

from aftergraph_work_intelligence.autonomy import (
    AutonomyEvaluationInput,
    evaluate_autonomy,
)

BASE = AutonomyEvaluationInput(
    request_id="adr_12345678",
    tenant_id="default",
    repository="Aftergraph/example",
    ref="refs/heads/main",
    head_sha="a" * 40,
    event_key="Aftergraph/example:main:" + "a" * 40,
    capability="dependency.patch.merge",
    objective="Merge a patch dependency update",
    impact_summary="Intent: apply a tested patch. Risk: an exported signature could change.",
    evidence=(
        {
            "kind": "ci",
            "source": "github",
            "observed_at": "2026-09-06T12:00:00Z",
            "reference": "run:123",
        },
    ),
    tests_passed=True,
    patch_release=True,
    test_coverage_delta=30,
    author_permission_tier=20,
)


def test_patch_release_is_auto_approved_only_inside_low_risk_boundary():
    result = evaluate_autonomy(BASE)

    assert result["decision"] == "auto_approve"
    assert result["risk"]["level"] == "low"
    assert result["human_action"]["required"] is False
    assert result["confidence"]["score"] == 80
    assert result["authority"] == {
        "execution_authority": "evaluation-only",
        "execution_state": "not_executed",
    }


def test_auth_secret_change_is_critical_and_blocked():
    result = evaluate_autonomy(replace(BASE, auth_or_secret_touched=True))

    assert result["decision"] == "blocked"
    assert result["risk"]["level"] == "critical"
    assert result["human_action"]["required"] is True
    assert "auth_or_secret_requires_human_signoff" in result["risk"]["blocking_controls"]


def test_proxy_change_is_critical_and_blocked():
    result = evaluate_autonomy(replace(BASE, proxy_or_ssl_touched=True))

    assert result["decision"] == "blocked"
    assert result["risk"]["level"] == "critical"
    assert result["human_action"]["required"] is True
    assert "proxy_or_ssl_requires_human_signoff" in result["risk"]["blocking_controls"]


def test_canary_drift_prepares_rollback_but_never_executes_it():
    result = evaluate_autonomy(replace(
        BASE,
        capability="deployment.rollback.prepare",
        canary_error_rate=0.006,
    ))

    assert result["decision"] == "prepare_rollback"
    assert result["human_action"]["required"] is True
    assert "canary_error_rate_exceeded" in result["risk"]["factors"]
    assert result["authority"]["execution_state"] == "not_executed"


def test_transient_ci_error_gets_exactly_one_automatic_retry():
    eligible = evaluate_autonomy(replace(
        BASE,
        capability="ci.check.retry",
        patch_release=False,
        transient_ci_error=True,
    ))
    exhausted = evaluate_autonomy(replace(
        BASE,
        capability="ci.check.retry",
        patch_release=False,
        transient_ci_error=True,
        retry_count=1,
    ))

    assert eligible["decision"] == "auto_retry"
    assert eligible["human_action"]["required"] is False
    assert exhausted["decision"] == "requires_human_signoff"
    assert exhausted["human_action"]["required"] is True
    assert "transient_retry_budget_exhausted" in exhausted["risk"]["blocking_controls"]


def test_stale_or_superseded_subject_is_blocked():
    for field in ("stale_review", "superseded_head"):
        result = evaluate_autonomy(replace(BASE, **{field: True}))
        assert result["decision"] == "blocked"
        assert result["human_action"]["required"] is True


def test_failed_tests_cannot_auto_approve_patch():
    result = evaluate_autonomy(replace(BASE, tests_passed=False))

    assert result["decision"] == "requires_human_signoff"
    assert result["human_action"]["required"] is True
    assert "full_test_suite_required" in result["risk"]["blocking_controls"]


def test_exported_signature_changes_disable_auto_approval():
    result = evaluate_autonomy(replace(BASE, exported_signatures_changed=True))

    assert result["decision"] == "requires_human_signoff"
    assert result["risk"]["level"] == "high"


def test_invalid_boundary_input_fails_closed_before_evaluation():
    with pytest.raises(ValueError, match="head_sha"):
        evaluate_autonomy(replace(BASE, head_sha="not-a-sha"))

    with pytest.raises(ValueError, match="evidence"):
        evaluate_autonomy(replace(BASE, evidence=()))
