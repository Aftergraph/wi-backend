"""Fail-closed autonomous decision evaluation.

This module evaluates bounded automation proposals. It never executes, approves,
merges, retries, rolls back, or mutates an external system. The returned
mapping is an evaluation envelope governed by the autonomy decision contract.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

Capability = Literal[
    "dependency.patch.merge",
    "ci.check.retry",
    "deployment.rollback.prepare",
    "github.status.sync",
    "github.suggestion.comment",
    "none",
]

_SHA_RE = r"^[0-9a-fA-F]{7,64}$"
_REPOSITORY_RE = r"^[^/\s]+/[^/\s]+$"

# ponytail: static critical-file matchers — ceiling: no content-level secret detection.
# Upgrade path: grep diff hunks for secret values / gitleaks-style entropy scan.
_CRITICAL_FILE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("auth", r"(^|/)(auth|session|token|login|permission|rbac|iam)(/|_|\.)"),
    ("secret", r"(^|/)(secret|credential|key|cert|certificate|\.env)(/|_|\.|$)"),
    ("proxy", r"(^|/)(caddyfile|proxy|gateway|ingress|caddy|nginx|tls|ssl|cors)(/|_|\.|$)"),
)


def _scan_critical_files(
    value: AutonomyEvaluationInput,
) -> tuple[bool, bool, bool]:
    """Derive trust-boundary flags by scanning changed_files directly.

    Fail-closed: a caller-declared flag can only ever set a bit HIGHER
    (never lower). The scan is additive with the caller's own signals so a
    lying or broken caller cannot downgrade a critical change.
    """
    auth_touched = value.auth_or_secret_touched
    proxy_touched = value.proxy_or_ssl_touched
    for path in value.changed_files:
        lowered = path.lower()
        for label, pattern in _CRITICAL_FILE_PATTERNS:
            import re

            if re.search(pattern, lowered):
                if label == "auth":
                    auth_touched = True
                elif label == "secret":
                    auth_touched = True
                elif label == "proxy":
                    proxy_touched = True
    return auth_touched, proxy_touched, auth_touched or proxy_touched

# ponytail: hardcoded blast-radius map — ceiling: no AST parsing, no import graph.
# Upgrade path: tree-sitter + route extractor for dynamic surface detection.
_BLAST_RADIUS_MAP: dict[str, list[str]] = {
    "src/auth": ["authentication", "session", "token_signing"],
    "src/middleware": ["request_pipeline", "rate_limiting", "cors"],
    "src/routes": ["api_surface", "request_handlers"],
    "src/publishers": ["publication_pipeline", "external_delivery"],
    "src/store": ["data_layer", "persistence"],
    "src/policy": ["policy_engine", "tenant_governance"],
    "src/transitions": ["state_machine", "lifecycle"],
    "src/tasks": ["background_processing", "worker_pool"],
    "src/tracing": ["observability", "telemetry"],
    "src/contracts": ["api_contracts", "schema_governance"],
    "src/observability": ["metrics", "logging", "alerting"],
    "src/security": ["trust_boundary", "encryption", "access_control"],
    "src/integrations": ["external_apis", "webhooks", "third_party"],
    "contracts": ["api_contracts", "schema_governance"],
    "Caddyfile": ["reverse_proxy", "tls", "ingress"],
    "docker": ["container_runtime", "deployment"],
    "pyproject.toml": ["dependencies", "build_system"],
    ".github": ["ci_pipeline", "automation"],
}


@dataclass(frozen=True, slots=True)
class AutonomyEvaluationInput:
    request_id: str
    tenant_id: str
    repository: str
    ref: str
    head_sha: str
    event_key: str
    capability: Capability
    objective: str
    impact_summary: str
    evidence: Sequence[Mapping[str, Any]]
    tests_passed: bool = False
    patch_release: bool = False
    exported_signatures_changed: bool = False
    critical_file_touched: bool = False
    auth_or_secret_touched: bool = False
    proxy_or_ssl_touched: bool = False
    transient_ci_error: bool = False
    canary_error_rate: float | None = None
    superseded_head: bool = False
    stale_review: bool = False
    retry_count: int = 0
    test_coverage_delta: int = 0
    author_permission_tier: int = 0
    critical_path_penalty: int = 0
    line_churn_penalty: int = 0
    changed_files: Sequence[str] = ()


def _compute_blast_radius(value: AutonomyEvaluationInput) -> dict[str, Any]:
    """Map changed files to affected surfaces via the route prefix map."""
    surfaces: set[str] = set()
    counts: dict[str, int] = {}
    for file_path in value.changed_files:
        matched = False
        for prefix, surface_tags in _BLAST_RADIUS_MAP.items():
            if file_path.startswith(prefix) or file_path == prefix:
                for tag in surface_tags:
                    surfaces.add(tag)
                counts[prefix] = counts.get(prefix, 0) + 1
                matched = True
                break
        if not matched:
            surfaces.add("unmapped")
            counts["unmapped"] = counts.get("unmapped", 0) + 1
    return {
        "changed_files": len(value.changed_files),
        "affected_surfaces": sorted(surfaces),
        "affected_surface_count": len(surfaces),
        "by_prefix": counts,
    }


def _validate_input(value: AutonomyEvaluationInput) -> None:
    import re

    if not re.fullmatch(r"adr_[a-zA-Z0-9_-]{8,128}", value.request_id):
        raise ValueError("request_id must match the autonomy contract")
    if not value.tenant_id or len(value.tenant_id) > 128:
        raise ValueError("tenant_id is required")
    if not re.fullmatch(_REPOSITORY_RE, value.repository):
        raise ValueError("repository must be owner/name")
    if not value.ref or len(value.ref) > 256:
        raise ValueError("ref is required")
    if not re.fullmatch(_SHA_RE, value.head_sha):
        raise ValueError("head_sha must be a hexadecimal commit identifier")
    if not value.event_key or len(value.event_key) > 512:
        raise ValueError("event_key is required")
    if not value.objective or len(value.objective) > 500:
        raise ValueError("objective is required")
    if not value.impact_summary or len(value.impact_summary) > 1000:
        raise ValueError("impact_summary is required")
    if not value.evidence:
        raise ValueError("at least one evidence reference is required")
    if value.retry_count < 0:
        raise ValueError("retry_count cannot be negative")
    if value.canary_error_rate is not None and value.canary_error_rate < 0:
        raise ValueError("canary_error_rate cannot be negative")
    for name, low, high in (
        ("test_coverage_delta", 0, 30),
        ("author_permission_tier", 0, 20),
        ("critical_path_penalty", 0, 40),
        ("line_churn_penalty", 0, 10),
    ):
        number = getattr(value, name)
        if not low <= number <= high:
            raise ValueError(f"{name} must be between {low} and {high}")


def _factors(
    value: AutonomyEvaluationInput,
    scanned_auth: bool | None = None,
    scanned_proxy: bool | None = None,
) -> list[str]:
    factors: list[str] = []
    if value.tests_passed:
        factors.append("full_test_suite")
    if value.exported_signatures_changed:
        factors.append("exported_signature_changed")
    if value.critical_file_touched:
        factors.append("critical_file_touched")
    if value.auth_or_secret_touched or scanned_auth:
        factors.append("auth_or_secret_touched")
    if value.proxy_or_ssl_touched or scanned_proxy:
        factors.append("proxy_or_ssl_touched")
    if value.transient_ci_error:
        factors.append("transient_ci_error")
    if value.canary_error_rate is not None and value.canary_error_rate > 0.005:
        factors.append("canary_error_rate_exceeded")
    if value.superseded_head:
        factors.append("superseded_head")
    if value.stale_review:
        factors.append("stale_review")
    return factors


def _risk_level(value: AutonomyEvaluationInput, factors: list[str]) -> str:
    if any(factor in factors for factor in (
        "auth_or_secret_touched",
        "proxy_or_ssl_touched",
        "canary_error_rate_exceeded",
    )):
        return "critical"
    if any(factor in factors for factor in (
        "exported_signature_changed",
        "critical_file_touched",
        "superseded_head",
        "stale_review",
    )):
        return "high"
    if not value.tests_passed or value.transient_ci_error:
        return "medium"
    return "low"


def _blocking_controls(value: AutonomyEvaluationInput, factors: list[str]) -> list[str]:
    controls: list[str] = []
    if "auth_or_secret_touched" in factors:
        controls.append("auth_or_secret_requires_human_signoff")
    if "proxy_or_ssl_touched" in factors:
        controls.append("proxy_or_ssl_requires_human_signoff")
    if "canary_error_rate_exceeded" in factors:
        controls.append("canary_drift_requires_human_signoff")
    if "exported_signature_changed" in factors:
        controls.append("exported_signature_change_requires_review")
    if "superseded_head" in factors:
        controls.append("superseded_head_is_not_promotable")
    if "stale_review" in factors:
        controls.append("stale_review_is_not_promotable")
    if not value.tests_passed:
        controls.append("full_test_suite_required")
    if value.capability == "ci.check.retry" and value.retry_count >= 1:
        controls.append("transient_retry_budget_exhausted")
    return controls


def _decision(value: AutonomyEvaluationInput, factors: list[str]) -> tuple[str, bool, str]:
    critical = {"auth_or_secret_touched", "proxy_or_ssl_touched"}
    if critical.intersection(factors):
        return "blocked", True, "Critical trust-boundary change requires human sign-off."
    if "canary_error_rate_exceeded" in factors:
        if value.capability == "deployment.rollback.prepare":
            return "prepare_rollback", True, "Canary drift detected; prepare rollback for human approval."
        return "requires_human_signoff", True, "Canary drift exceeds the promotion threshold."
    if "superseded_head" in factors or "stale_review" in factors:
        return "blocked", True, "The evaluation target is stale or superseded."
    if value.capability == "dependency.patch.merge":
        eligible = (
            value.patch_release
            and value.tests_passed
            and not value.exported_signatures_changed
            and not value.critical_file_touched
            and value.line_churn_penalty == 0
        )
        if eligible:
            return "auto_approve", False, "Patch-level dependency update passed the bounded low-risk gate."
    if value.capability == "ci.check.retry":
        if value.transient_ci_error and value.retry_count == 0:
            return "auto_retry", False, "Known transient CI failure is eligible for its single retry."
        return "requires_human_signoff", True, "CI retry is not eligible for another automatic attempt."
    return "requires_human_signoff", True, "No bounded automatic decision is eligible."


def evaluate_autonomy(value: AutonomyEvaluationInput) -> dict[str, Any]:
    """Return a schema-shaped, non-authorizing autonomy evaluation."""
    _validate_input(value)
    scanned_auth, scanned_proxy, _ = _scan_critical_files(value)
    # Fail-closed: caller-declared flags are OR'd with the file scan so a
    # broken/lying caller can never downgrade a critical change.
    factors = _factors(value, scanned_auth or None, scanned_proxy or None)
    risk_level = _risk_level(value, factors)
    controls = _blocking_controls(value, factors)
    decision, human_required, reason = _decision(value, factors)
    # ponytail: sublinear confidence scoring — ceiling: no ML-based weighting.
    # Upgrade path: train coefficients on historical approval/rejection data.
    base_score = 50
    coverage_bonus = min(value.test_coverage_delta, 15)  # cap at 15
    perm_bonus = min(value.author_permission_tier, 15)  # cap at 15
    critical_penalty = value.critical_path_penalty
    churn_penalty = value.line_churn_penalty * 2  # double weight for churn
    raw_score = base_score + coverage_bonus + perm_bonus - critical_penalty - churn_penalty
    score = max(0, min(80, raw_score))  # hard cap at 80 for auto_approve
    observed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    blast_radius = _compute_blast_radius(value)
    return {
        "schema": "aftergraph.autonomy-decision/1.0",
        "request_id": value.request_id,
        "subject": {
            "tenant_id": value.tenant_id,
            "repository": value.repository,
            "ref": value.ref,
            "head_sha": value.head_sha,
            "event_key": value.event_key,
        },
        "capability": value.capability,
        "intent": {
            "objective": value.objective,
            "impact_summary": value.impact_summary,
        },
        "risk": {
            "level": risk_level,
            "factors": factors,
            "blocking_controls": controls,
        },
        "confidence": {
            "score": score,
            "components": {
                "test_coverage_delta": value.test_coverage_delta,
                "author_permission_tier": value.author_permission_tier,
                "critical_path_penalty": value.critical_path_penalty,
                "line_churn_penalty": value.line_churn_penalty,
            },
        },
        "decision": decision,
        "human_action": {
            "required": human_required,
            "reason": reason,
            "action_label": "Review and sign off" if human_required else "No human action required",
        },
        "evidence": [dict(item) for item in value.evidence],
        "authority": {
            "execution_authority": "evaluation-only",
            "execution_state": "not_executed",
        },
        "blast_radius": blast_radius,
    }


__all__ = ["AutonomyEvaluationInput", "evaluate_autonomy"]
