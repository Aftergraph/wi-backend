"""Wie proactivity sensing — acceptance vectors PRO-001 / PRO-003 / PRO-005.

Contract (vendored): contracts/proactivity/0.1.json
Canonical source: Aftergraph/after-graph-governance docs/contracts/proactivity/0.1.json
Seam binding: docs/PROACTIVITY-ORG-V1.md (governance)

Scope: wi-backend owns the Wie sensing path. PRO-002/PRO-006 (Cron) and
PRO-004 (Runtime) are sibling-owned; they are replayed here as
compatibility probes only, proving the shared fail-closed semantics and
the path-independent same-signal dedupe key this repo shares with
aftergraph-cron-fabric.

Invariants:
1. Wie signals project to candidates preserving native meaning (native_ref
   carried through verbatim) while claiming nothing.
2. Candidates never self-admit: claims_admission without Trust Gateway
   admission fails closed.
3. Sensing never executes: claims_execution always fails closed.
4. The same native signal seen via Wie and Cron dedupes to a single
   attention candidate via a path-independent dedupe key.

Run: uv run --frozen pytest tests/test_proactivity_sensing.py -q
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aftergraph_work_intelligence.api import create_app
from aftergraph_work_intelligence.proactivity import (
    SensingRegistry,
    SensingRejected,
    evaluate_sensing,
    is_stale,
    project_wie_signal,
    sensing_dedupe_key,
)

CONTRACT = Path(__file__).resolve().parent.parent / "contracts" / "proactivity" / "0.1.json"

# Pinned acceptance inputs, verbatim from
# docs/platform-conformance/v0.1/vectors.json (governance).
PRO_001 = {
    "schema": "proactivity/0.1",
    "sensing_id": "sen_11111111111111111111111111111111",
    "path": "wie",
    "native_ref": "wie:signal:000001",
    "candidate_kind": "opportunity",
    "claims_execution": False,
    "claims_admission": False,
    "admitted_by_tg": False,
    "correlated_paths": ["wie"],
    "asserted_at": "2026-09-08T12:00:00Z",
    "tenant_id": "ten_11111111111111111111111111111111",
}
PRO_002 = {
    "schema": "proactivity/0.1",
    "sensing_id": "sen_22222222222222222222222222222222",
    "path": "cron",
    "native_ref": "cron:reading:000002",
    "candidate_kind": "finding",
    "claims_execution": True,
    "claims_admission": False,
    "admitted_by_tg": False,
    "correlated_paths": ["cron"],
    "asserted_at": "2026-09-08T12:00:01Z",
    "tenant_id": "ten_11111111111111111111111111111111",
}
PRO_003 = {
    "schema": "proactivity/0.1",
    "sensing_id": "sen_33333333333333333333333333333333",
    "path": "wie",
    "native_ref": "wie:signal:000003",
    "candidate_kind": "commitment_candidate",
    "claims_execution": False,
    "claims_admission": True,
    "admitted_by_tg": False,
    "correlated_paths": ["wie"],
    "asserted_at": "2026-09-08T12:00:02Z",
    "tenant_id": "ten_11111111111111111111111111111111",
}
PRO_004 = {
    "schema": "proactivity/0.1",
    "sensing_id": "sen_44444444444444444444444444444444",
    "path": "runtime",
    "native_ref": "runtime:mission:000004",
    "candidate_kind": "opportunity",
    "claims_execution": False,
    "claims_admission": False,
    "admitted_by_tg": False,
    "correlated_paths": ["runtime"],
    "asserted_at": "2026-09-08T12:00:03Z",
    "tenant_id": "ten_11111111111111111111111111111111",
}
PRO_005 = {
    "schema": "proactivity/0.1",
    "sensing_id": "sen_55555555555555555555555555555555",
    "path": "wie",
    "native_ref": "pocket:observation:000005",
    "candidate_kind": "attention_candidate",
    "claims_execution": False,
    "claims_admission": False,
    "admitted_by_tg": False,
    "correlated_paths": ["wie", "cron"],
    "asserted_at": "2026-09-08T12:00:04Z",
    "tenant_id": "ten_11111111111111111111111111111111",
}
PRO_006 = {
    "schema": "proactivity/0.1",
    "sensing_id": "sen_66666666666666666666666666666666",
    "path": "cron",
    "native_ref": "cron:reading:000006",
    "candidate_kind": "finding",
    "claims_execution": False,
    "claims_admission": False,
    "admitted_by_tg": False,
    "correlated_paths": ["cron"],
    "asserted_at": "2026-09-08T12:00:05Z",
    "tenant_id": "ten_11111111111111111111111111111111",
}

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)


def test_contract_identity():
    with open(CONTRACT) as f:
        contract = json.load(f)
    assert contract["$id"].endswith("proactivity/0.1.json")
    assert contract["title"] == "Aftergraph Proactivity"
    assert contract["properties"]["schema"]["const"] == "proactivity/0.1"


def test_pro_001_wie_opportunity_claiming_nothing_accepts():
    decision = evaluate_sensing(PRO_001, now=NOW)
    assert decision.accepted is True


def test_pro_003_commitment_claiming_admission_without_tg_rejects():
    decision = evaluate_sensing(PRO_003, now=NOW)
    assert decision.accepted is False
    assert "admission" in decision.reason


def test_pro_005_same_signal_wie_and_cron_dedupes_to_single_attention_candidate():
    registry = SensingRegistry()
    cron_sighting = dict(
        PRO_005,
        sensing_id="sen_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        path="cron",
        correlated_paths=["cron"],
    )
    first, first_deduped = registry.register(PRO_005, now=NOW)
    assert first_deduped is False
    second, second_deduped = registry.register(cron_sighting, now=NOW)
    assert second_deduped is True
    assert second.sensing_id == first.sensing_id
    assert second.candidate_kind == "attention_candidate"
    assert sorted(second.correlated_paths) == ["cron", "wie"]
    assert len(registry) == 1


def test_sibling_vectors_agree_with_shared_contract_semantics():
    # Cron-fabric owned: execution claim rejects, claim-free finding accepts.
    assert evaluate_sensing(PRO_002, now=NOW).accepted is False
    assert evaluate_sensing(PRO_006, now=NOW).accepted is True
    # Runtime owned: mission-state opportunity accepts.
    assert evaluate_sensing(PRO_004, now=NOW).accepted is True


def test_project_wie_signal_preserves_native_meaning_and_claims_nothing():
    record = project_wie_signal(
        sensing_id="sen_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        tenant_id="ten_11111111111111111111111111111111",
        native_ref="wie:signal:000001",
        candidate_kind="opportunity",
        asserted_at="2026-09-08T12:00:00Z",
    )
    assert record["native_ref"] == "wie:signal:000001"
    assert record["claims_execution"] is False
    assert record["claims_admission"] is False
    assert record["admitted_by_tg"] is False
    assert record["correlated_paths"] == ["wie"]
    assert evaluate_sensing(record, now=NOW).accepted is True


def test_project_wie_signal_rejects_non_wie_kinds():
    with pytest.raises(ValueError):
        project_wie_signal(
            sensing_id="sen_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            tenant_id="ten_11111111111111111111111111111111",
            native_ref="wie:signal:000009",
            candidate_kind="finding",
        )


def test_dedupe_key_is_path_independent_but_signal_and_tenant_scoped():
    tenant = "ten_11111111111111111111111111111111"
    wie_key = sensing_dedupe_key(tenant, "pocket:observation:000005")
    cron_key = sensing_dedupe_key(tenant, "pocket:observation:000005")
    assert wie_key == cron_key  # cron-fabric must compute this same key
    assert sensing_dedupe_key(tenant, "pocket:observation:000006") != wie_key
    assert sensing_dedupe_key("ten_22222222222222222222222222222222", "pocket:observation:000005") != wie_key


def test_distinct_signals_never_collide_in_registry():
    registry = SensingRegistry()
    registry.register(PRO_001, now=NOW)
    other = dict(PRO_001, sensing_id="sen_cccccccccccccccccccccccccccccccc", native_ref="wie:signal:000007")
    registry.register(other, now=NOW)
    assert len(registry) == 2


def test_execution_claim_rejects_on_any_path():
    assert evaluate_sensing(dict(PRO_001, claims_execution=True), now=NOW).accepted is False
    assert evaluate_sensing(PRO_002, now=NOW).accepted is False


def test_self_admission_variants_fail_closed():
    base = dict(PRO_001, candidate_kind="commitment_candidate")
    assert evaluate_sensing(dict(base, claims_admission=True, admitted_by_tg=False), now=NOW).accepted is False
    # Non-boolean admission evidence is not admission.
    assert evaluate_sensing(dict(base, claims_admission=True, admitted_by_tg="yes"), now=NOW).accepted is False
    assert evaluate_sensing(dict(base, claims_admission="yes", admitted_by_tg=False), now=NOW).accepted is False
    # Genuine Trust Gateway admission with no new claim is not self-admission.
    admitted = dict(base, claims_admission=False, admitted_by_tg=True)
    assert evaluate_sensing(admitted, now=NOW).accepted is True


def test_scope_unbound_or_malformed_records_reject():
    assert evaluate_sensing(dict(PRO_001, tenant_id="renos"), now=NOW).accepted is False
    assert evaluate_sensing(dict(PRO_001, tenant_id=""), now=NOW).accepted is False
    assert evaluate_sensing(dict(PRO_001, sensing_id="sen_nothex"), now=NOW).accepted is False
    assert evaluate_sensing(dict(PRO_001, schema="proactivity/9.9"), now=NOW).accepted is False
    assert evaluate_sensing(dict(PRO_001, correlated_paths=[]), now=NOW).accepted is False
    assert evaluate_sensing(dict(PRO_001, correlated_paths=["cron"]), now=NOW).accepted is False
    assert evaluate_sensing(dict(PRO_001, native_ref=""), now=NOW).accepted is False
    assert evaluate_sensing("not-a-record", now=NOW).accepted is False  # type: ignore[arg-type]


def test_future_asserted_at_rejects_and_stale_is_flagged_not_silent():
    future = dict(PRO_001, asserted_at="2999-01-01T00:00:00Z")
    assert evaluate_sensing(future, now=NOW).accepted is False
    stale = dict(PRO_001, asserted_at="2020-01-01T00:00:00Z")
    assert is_stale(stale["asserted_at"], NOW) is True
    assert is_stale(PRO_001["asserted_at"], NOW) is False
    # Advisory by default so pinned vectors replay at any wall-clock time...
    assert evaluate_sensing(stale, now=NOW).accepted is True
    # ...while a strict caller can fail closed on stale signals.
    assert evaluate_sensing(stale, now=NOW, max_age=timedelta(days=30)).accepted is False


def test_rejected_records_never_enter_registry():
    registry = SensingRegistry()
    with pytest.raises(SensingRejected):
        registry.register(PRO_003, now=NOW)
    assert len(registry) == 0


def test_sensing_endpoint_accepts_rejects_and_dedupes(tmp_path):
    app = create_app(db_path=tmp_path / "sensing.db")
    with TestClient(app) as client:
        accepted = client.post("/v1/sensing", json=PRO_001)
        assert accepted.status_code == 201
        assert accepted.json()["accepted"] is True

        rejected = client.post("/v1/sensing", json=PRO_003)
        assert rejected.status_code == 200
        body = rejected.json()
        assert body["accepted"] is False
        assert body["candidate"] is None

        malformed = client.post("/v1/sensing", json={"schema": "proactivity/0.1"})
        assert malformed.status_code == 422

        first = client.post("/v1/sensing", json=PRO_005)
        assert first.status_code == 201
        cron_sighting = dict(
            PRO_005,
            sensing_id="sen_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            path="cron",
            correlated_paths=["cron"],
        )
        second = client.post("/v1/sensing", json=cron_sighting)
        assert second.status_code == 200
        merged = second.json()
        assert merged["accepted"] is True
        assert merged["deduped"] is True
        assert merged["candidate"]["candidate_kind"] == "attention_candidate"
        assert sorted(merged["candidate"]["correlated_paths"]) == ["cron", "wie"]
