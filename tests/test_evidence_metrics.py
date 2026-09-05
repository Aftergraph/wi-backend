"""TDD tests for V2 provenance/evidence + observability.

Provenance:
- Each observation carries source, external_id, actor, occurred_at, metadata.
- Each work-item carries a hash of the linked observations chain (provenance
  digest). This is the L2-equivalent evidence in the Aftergraph 4-layer
  model: HMAC-SHA256 over the canonical (tenant_id, work_item_id,
  observations[]) tuple, keyed by an operator secret.

Observability:
- ``MetricsRecorder`` captures per-tenant, per-source, per-action counters.
- ``/v1/metrics`` exposes them as JSON.
- A structured ``log_event`` helper emits JSON lines to a configurable sink.
"""
from __future__ import annotations

import io
import json

from aftergraph_work_intelligence.evidence import (
    EvidenceBuilder,
    build_evidence,
    verify_evidence,
)
from aftergraph_work_intelligence.metrics import MetricsRecorder
from aftergraph_work_intelligence.models import ObservationInput
from aftergraph_work_intelligence.observability import configure_logging, log_event
from aftergraph_work_intelligence.service import WorkIntelligenceService
from aftergraph_work_intelligence.store import SQLiteStore


def test_evidence_digest_is_deterministic_and_verifiable():
    svc_payload = {
        "tenant_id": "renos",
        "work_item_id": "wi_abc",
        "title": "Send bekræftelse",
        "canonical_key": "k1",
        "observations": [
            {"id": "obs1", "source": "conversation", "external_id": "t-1", "actor": "u:1",
             "occurred_at": "2026-09-05T09:00:00+00:00", "text": "send"},
            {"id": "obs2", "source": "email", "external_id": "<m@1>", "actor": "kunde@example.com",
             "occurred_at": "2026-09-05T10:00:00+00:00", "text": "send bekræftelse"},
        ],
    }
    secret = "op-secret-1"
    e1 = build_evidence(svc_payload, secret=secret)
    e2 = build_evidence(svc_payload, secret=secret)
    assert e1["digest"] == e2["digest"]  # deterministic
    assert e1["algorithm"] == "HMAC-SHA256"
    assert verify_evidence(e1, svc_payload, secret=secret) is True
    # Tamper with text → fails verification
    tampered = dict(svc_payload)
    tampered["observations"] = [
        dict(svc_payload["observations"][0]),
        dict(svc_payload["observations"][1], text="DIFFERENT"),
    ]
    assert verify_evidence(e1, tampered, secret=secret) is False


def test_evidence_includes_provenance_for_each_observation():
    payload = {
        "tenant_id": "renos",
        "work_item_id": "wi_x",
        "title": "x",
        "canonical_key": "k",
        "observations": [
            {"id": "o1", "source": "renos", "external_id": "job-1", "actor": "company:c",
             "occurred_at": "2026-09-05T11:00:00+00:00", "text": "follow up"},
        ],
    }
    e = build_evidence(payload, secret="s")
    assert e["observations_count"] == 1
    assert e["observations"][0]["external_id"] == "job-1"
    assert e["observations"][0]["actor"] == "company:c"


def test_metrics_recorder_counts_actions_sources_and_statuses(tmp_path):
    store = SQLiteStore(tmp_path / "wi.db")
    svc = WorkIntelligenceService(store)
    svc.ingest(ObservationInput(tenant_id="renos", source="conversation",
                                text="Vi skal sende kunden en bekræftelse"))
    svc.ingest(ObservationInput(tenant_id="renos", source="email",
                                text="Kunden bor i Aarhus"))  # non-actionable
    m = MetricsRecorder.from_store(store)
    snap = m.snapshot()
    assert snap["count_by_action"]["created"] == 1
    assert snap["count_by_action"]["observed"] == 1
    assert snap["count_by_source"]["conversation"] == 1
    assert snap["count_by_source"]["email"] == 1
    assert snap["count_by_tenant"]["renos"] == 2


def test_metrics_recorder_counts_replays(tmp_path):
    store = SQLiteStore(tmp_path / "wi.db")
    svc = WorkIntelligenceService(store)
    payload = ObservationInput(tenant_id="renos", source="email",
                               external_id="msg-1",
                               text="Send bekræftelse til kunden")
    svc.ingest(payload)
    svc.ingest(payload)  # replay
    svc.ingest(payload)  # replay
    m = MetricsRecorder.from_store(store)
    snap = m.snapshot()
    assert snap["count_by_action"]["created"] == 1
    assert snap["count_by_action"]["replayed"] == 2


def test_metrics_recorder_open_work_items_count(tmp_path):
    store = SQLiteStore(tmp_path / "wi.db")
    svc = WorkIntelligenceService(store)
    svc.ingest(ObservationInput(tenant_id="renos", source="conversation",
                                text="Vi skal sende kunden en bekræftelse"))
    svc.ingest(ObservationInput(tenant_id="renos", source="conversation",
                                text="Husk at bestille nye rengøringsklude til kontoret"))
    m = MetricsRecorder.from_store(store)
    snap = m.snapshot()
    assert snap["open_work_items"]["renos"] == 2


def test_log_event_emits_structured_json():
    sink = io.StringIO()
    logger = configure_logging(sink=sink)
    log_event(logger, "ingest.created", tenant_id="renos", source="conversation", work_item_id="wi_1")
    line = sink.getvalue().strip()
    record = json.loads(line)
    assert record["event"] == "ingest.created"
    assert record["tenant_id"] == "renos"
    assert record["source"] == "conversation"
    assert record["work_item_id"] == "wi_1"
    assert "timestamp" in record


def test_evidence_builder_emits_conformant_envelope():
    payload = {
        "tenant_id": "renos",
        "work_item_id": "wi_1",
        "title": "x",
        "canonical_key": "k",
        "observations": [
            {"id": "o1", "source": "renos", "external_id": "j1", "actor": "company:c",
             "occurred_at": "2026-09-05T11:00:00+00:00", "text": "follow up"},
        ],
    }
    envelope = EvidenceBuilder(secret="s").build(payload)
    assert envelope["schema"] == "aftergraph.work-item-evidence/1.0"
    assert envelope["provider_id"] == "aftergraph.work-intelligence"
    assert envelope["bundle_id"].startswith("ev_")
    assert envelope["identity_chain"]["tenant_id"] == "renos"
    assert envelope["identity_chain"]["work_item_id"] == "wi_1"