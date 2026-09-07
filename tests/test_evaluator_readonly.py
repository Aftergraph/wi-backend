"""Evaluator read-only contract (ADR-008): evaluating a proposal must never
mutate domain state. The ONLY permitted write is the evaluator's own
autonomy_decisions audit row. Work items, observations, transitions,
publications, api_keys, policies and audit_log must be byte-identical
(count-wise) across evaluations — including auto_approve-shaped proposals.
"""

import uuid

from fastapi.testclient import TestClient

from aftergraph_work_intelligence.api import create_app

_TOKEN = "readonly-test-token"

DOMAIN_TABLES = [
    "api_keys",
    "audit_log",
    "intake_observations",
    "intake_publications",
    "intake_replays",
    "intake_transitions",
    "intake_work_item_observations",
    "intake_work_items",
    "schema_migrations",
    "tenant_policies",
]


def _client(tmp_path):
    return TestClient(create_app(db_path=tmp_path / "readonly.db", api_token=_TOKEN))


def _bearer():
    return {"Authorization": f"Bearer {_TOKEN}"}


def _counts(client):
    store = client.app.state.store
    with store._lock:
        return {
            table: store._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in DOMAIN_TABLES
        }


def _decisions(client):
    store = client.app.state.store
    with store._lock:
        return store._db.execute("SELECT COUNT(*) FROM autonomy_decisions").fetchone()[0]


def _proposal(**overrides):
    body = {
        "request_id": f"adr_{uuid.uuid4().hex[:16]}",
        "tenant_id": "default",
        "repository": "Aftergraph/example",
        "ref": "refs/heads/main",
        "head_sha": "b" * 40,
        "event_key": "Aftergraph/example:main:" + "b" * 40,
        "capability": "dependency.patch.merge",
        "objective": "Apply a tested patch",
        "impact_summary": "Intent: apply. Risk: none.",
        "evidence": [{"kind": "ci", "source": "github", "observed_at": "2026-09-06T12:00:00Z", "reference": "run:1"}],
        "tests_passed": True,
        "patch_release": True,
        "test_coverage_delta": 30,
        "author_permission_tier": 20,
    }
    body.update(overrides)
    return body


def test_evaluate_leaves_domain_state_untouched(tmp_path):
    with _client(tmp_path) as client:
        seed = client.post(
            "/v1/observations",
            json={"tenant_id": "default", "source": "conversation", "external_id": "ro-1", "text": "Vi skal købe parfumefri sæbe før mandag"},
            headers=_bearer(),
        )
        assert seed.status_code in (200, 201, 202), seed.text

        before = _counts(client)
        decisions_before = _decisions(client)
        # falsification guard: the seeded domain state must be non-empty,
        # otherwise "unchanged" would pass vacuously on empty tables.
        assert before["intake_observations"] >= 1

        shapes = [
            {},  # clean auto_approve-leaning proposal
            {"tests_passed": False, "transient_ci_error": True},  # manual_review-leaning
            {"auth_or_secret_touched": True},  # blocked-leaning
        ]
        for shape in shapes:
            response = client.post(
                "/v1/autonomy/decisions/evaluate", json=_proposal(**shape), headers=_bearer()
            )
            assert response.status_code == 200, response.text

        assert _counts(client) == before
        assert _decisions(client) == decisions_before + 3
