from __future__ import annotations

import io
import json
import time
import urllib.error

import pytest
from fastapi.testclient import TestClient

import aftergraph_work_intelligence.api as api_module
from aftergraph_work_intelligence.api import create_app
from aftergraph_work_intelligence.pocket import (
    PocketRejected,
    PocketStore,
    sign_heypocket_body,
)
from aftergraph_work_intelligence.pocket_provider import (
    PocketProviderError,
    fetch_heypocket_recording,
    normalize_heypocket_recording,
    resolve_pocket_api_key,
)
from aftergraph_work_intelligence.store import SQLiteStore

TENANT = "ten_" + "1" * 32
TOKEN = "test-pocket-rest-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
WEBHOOK_SECRET = "test-pocket-live-secret"


class _Response:
    def __init__(self, payload: dict) -> None:
        self._raw = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._raw


def _rest_recording(
    *,
    text: str = "Canonical transcript from Pocket REST",
    updated_at: str = "2026-09-18T16:31:10.000Z",
) -> dict:
    return {
        "id": "rec_abc123",
        "title": "Pocket reconciliation test",
        "duration": 16,
        "state": "processed",
        "language": "Danish",
        "recording_at": "2026-09-18T16:30:45.000Z",
        "created_at": "2026-09-18T16:30:45.000Z",
        "updated_at": updated_at,
        "tags": [],
        "transcript": {
            "metadata": {},
            "text": text,
            "segments": [
                {
                    "start": 0.0,
                    "end": 2.0,
                    "speaker": "Speaker 0",
                    "text": text,
                    "originalText": text,
                }
            ],
        },
        "summarizations": {},
    }


def _live_payload(
    *,
    event: str = "transcription.completed",
    timestamp: str = "2026-09-18T16:31:01.000Z",
) -> dict:
    return {
        "event": event,
        "timestamp": timestamp,
        "user": {"id": "user_abc123", "email": "jonas@example.invalid"},
        "recording": {"id": "rec_abc123", "title": "Pocket webhook signal"},
        "transcript": [
            {
                "speaker": "Speaker 0",
                "text": "UNTRUSTED WEBHOOK TRANSCRIPT",
                "start": 0.0,
                "end": 2.0,
            }
        ],
    }


def _post_live(
    client: TestClient,
    payload: dict,
    *,
    delivery_timestamp: str = "1789749061000",
):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    signature = sign_heypocket_body(WEBHOOK_SECRET, delivery_timestamp, raw)
    return client.post(
        "/v1/webhook/pocket",
        content=raw,
        headers={
            "X-HeyPocket-Signature": signature,
            "X-HeyPocket-Timestamp": delivery_timestamp,
            "Content-Type": "application/json",
        },
    )


def _configure_live(monkeypatch) -> None:
    monkeypatch.setenv("AFTERGRAPH_POCKET_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv(
        "AFTERGRAPH_POCKET_TENANT_MAP",
        json.dumps({"user:user_abc123": TENANT}),
    )


def _wait_job(client: TestClient, job_id: str, *, timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(
            f"/v1/pocket/reconciliation-jobs/{job_id}",
            headers=AUTH,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] in {"completed", "failed"}:
            return body
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish in time")


def test_resolve_api_key_prefers_tenant_scope(monkeypatch):
    monkeypatch.setenv("AFTERGRAPH_POCKET_API_KEY", "global-key")
    monkeypatch.setenv(
        f"AFTERGRAPH_POCKET_API_KEY_{TENANT.upper()}",
        "tenant-key",
    )
    assert resolve_pocket_api_key(TENANT) == "tenant-key"
    assert resolve_pocket_api_key(None) == "global-key"


def test_fetch_recording_uses_bearer_and_documented_path():
    captured: dict[str, object] = {}

    def opener(request, timeout):
        captured["authorization"] = request.get_header("Authorization")
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return _Response({"success": True, "data": _rest_recording()})

    data = fetch_heypocket_recording(
        "secret-api-key",
        "rec_abc123",
        opener=opener,
    )
    assert data["id"] == "rec_abc123"
    assert captured["authorization"] == "Bearer secret-api-key"
    assert captured["url"] == (
        "https://public.heypocketai.com/api/v1/public/recordings/rec_abc123"
        "?include_transcript=true&include_summarizations=true"
    )
    assert captured["timeout"] == 10.0


@pytest.mark.parametrize(
    ("status", "retriable"),
    [(401, False), (403, False), (429, True), (503, True)],
)
def test_fetch_recording_classifies_provider_failures(status, retriable):
    def opener(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            status,
            "provider error",
            {},
            io.BytesIO(b"{}"),
        )

    with pytest.raises(PocketProviderError) as exc:
        fetch_heypocket_recording(
            "secret-api-key",
            "rec_abc123",
            opener=opener,
        )
    assert exc.value.retriable is retriable
    assert "secret-api-key" not in str(exc.value)


def test_normalize_rest_recording_is_authority_free_and_versioned():
    payload = normalize_heypocket_recording(
        _rest_recording(),
        TENANT,
        consent_ref="pocket:owner:user_abc123",
    )
    assert payload["schema"] == "pocket-source/0.1"
    assert payload["source_ref"] == "pocket:recording:rec_abc123"
    assert payload["materialization_ref"].startswith(
        "pocket:recording:rec_abc123:rev:"
    )
    assert payload["claims_principal_identity"] is False
    assert payload["claims_principal_authentication"] is False
    assert payload["claims_execution"] is False
    assert payload["self_executes"] is False
    assert payload["delivery_channel"] == "rest"
    assert payload["segments"][0]["text"] == "Canonical transcript from Pocket REST"


def test_normalize_rest_summary_fallback():
    recording = _rest_recording()
    recording["transcript"] = None
    recording["summarizations"] = {
        "sum_1": {
            "v2": {
                "summary": {
                    "markdown": "Canonical summary only",
                    "bulletPoints": [],
                }
            }
        }
    }
    payload = normalize_heypocket_recording(recording, TENANT)
    assert payload["segments"][0]["text"] == "Canonical summary only"
    assert payload["derivations"][0]["derivation"] == "summary"


def test_normalize_rest_not_ready_fails_closed():
    recording = _rest_recording()
    recording["transcript"] = None
    recording["summarizations"] = {}
    with pytest.raises(PocketRejected) as exc:
        normalize_heypocket_recording(recording, TENANT)
    assert exc.value.code == "PCK-LIVE-002"


def test_live_webhook_without_rest_key_stays_signal_only(monkeypatch):
    _configure_live(monkeypatch)
    monkeypatch.delenv("AFTERGRAPH_POCKET_API_KEY", raising=False)

    app = create_app(db_path=":memory:", api_token=TOKEN)
    with TestClient(app) as client:
        response = _post_live(client, _live_payload())
        assert response.status_code == 202, response.text
        assert response.json()["status"] == "signal_only"
        assert response.json()["reconciliation_required"] is True

        reads = client.get(
            "/v1/pocket/observations",
            params={"tenant_id": TENANT},
            headers=AUTH,
        )
    assert reads.status_code == 200
    assert reads.json()["count"] == 0


def test_live_webhook_queues_rest_reconciliation(monkeypatch):
    _configure_live(monkeypatch)
    monkeypatch.setenv("AFTERGRAPH_POCKET_API_KEY", "provider-api-key")
    monkeypatch.setattr(
        api_module,
        "fetch_heypocket_recording",
        lambda api_key, recording_id: _rest_recording(),
    )

    app = create_app(db_path=":memory:", api_token=TOKEN)
    with TestClient(app) as client:
        response = _post_live(client, _live_payload())
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["status"] == "reconciliation_queued"
        assert body["job_created"] is True
        assert body["webhook_claimed_as_truth"] is False

        job = _wait_job(client, body["job_id"])
        assert job["status"] == "completed"
        assert job["result"]["canonical_source"] == "rest"

        reads = client.get(
            "/v1/pocket/observations",
            params={"tenant_id": TENANT},
            headers=AUTH,
        )
    assert reads.status_code == 200
    assert reads.json()["count"] == 1
    assert reads.json()["observations"][0]["text"] == (
        "Canonical transcript from Pocket REST"
    )


def test_rest_revision_retires_previous_materialization(monkeypatch):
    _configure_live(monkeypatch)
    monkeypatch.setenv("AFTERGRAPH_POCKET_API_KEY", "provider-api-key")
    versions = [
        _rest_recording(text="Canonical v1", updated_at="2026-09-18T16:31:10Z"),
        _rest_recording(text="Canonical v2", updated_at="2026-09-18T16:32:10Z"),
    ]

    def fetch(api_key, recording_id):
        return versions.pop(0)

    monkeypatch.setattr(api_module, "fetch_heypocket_recording", fetch)
    app = create_app(db_path=":memory:", api_token=TOKEN)

    with TestClient(app) as client:
        first = _post_live(client, _live_payload())
        assert first.status_code == 202, first.text
        first_job = _wait_job(client, first.json()["job_id"])
        assert first_job["status"] == "completed"
        first_result = first_job["result"]
        assert first_result["superseded_materialization_ref"] is None

        second = _post_live(
            client,
            _live_payload(
                event="transcript.edited",
                timestamp="2026-09-18T16:32:01.000Z",
            ),
            delivery_timestamp="1789749121000",
        )
        assert second.status_code == 202, second.text
        second_job = _wait_job(client, second.json()["job_id"])
        assert second_job["status"] == "completed"
        assert second_job["result"]["superseded_materialization_ref"] == (
            first_result["materialization_ref"]
        )

        reads = client.get(
            "/v1/pocket/observations",
            params={"tenant_id": TENANT},
            headers=AUTH,
        )
        assert reads.status_code == 200
        assert reads.json()["count"] == 1
        assert reads.json()["observations"][0]["text"] == "Canonical v2"

        with app.state.store._lock:
            audit_count = app.state.store._db.execute(
                "SELECT COUNT(*) AS n FROM intake_observations"
                " WHERE tenant_id = ? AND source = 'pocket'",
                (TENANT,),
            ).fetchone()["n"]
        assert audit_count == 2


def test_manual_reconcile_endpoint_uses_rest_plane(monkeypatch):
    monkeypatch.setenv("AFTERGRAPH_POCKET_API_KEY", "provider-api-key")
    monkeypatch.setattr(
        api_module,
        "fetch_heypocket_recording",
        lambda api_key, recording_id: _rest_recording(),
    )

    app = create_app(db_path=":memory:", api_token=TOKEN)
    with TestClient(app) as client:
        response = client.post(
            "/v1/pocket/reconcile/rec_abc123",
            params={
                "tenant_id": TENANT,
                "consent_ref": "pocket:owner:user_abc123",
            },
            headers=AUTH,
        )
    assert response.status_code == 201, response.text
    assert response.json()["canonical_source"] == "rest"
    assert response.json()["execution_authority"] == "none"
    assert response.json()["webhook_claimed_as_truth"] is False


def test_durable_reconciliation_job_dedupes_active_recording():
    store = SQLiteStore(":memory:")
    pocket = PocketStore(store)
    try:
        first_id, first_created = pocket.enqueue_reconciliation(
            TENANT,
            "rec_abc123",
            "dlv_1",
            "pocket:owner:user_abc123",
        )
        second_id, second_created = pocket.enqueue_reconciliation(
            TENANT,
            "rec_abc123",
            "dlv_2",
            "pocket:owner:user_abc123",
        )
    finally:
        store.close()

    assert first_created is True
    assert second_created is False
    assert second_id == first_id


def test_interrupted_reconciliation_recovers_to_pending(tmp_path):
    db_path = tmp_path / "reconcile.db"
    store = SQLiteStore(db_path)
    pocket = PocketStore(store)
    job_id, created = pocket.enqueue_reconciliation(
        TENANT,
        "rec_abc123",
        "dlv_recover",
        None,
    )
    assert created is True
    running = pocket.start_reconciliation(job_id)
    assert running is not None
    assert running["status"] == "running"
    store.close()

    store = SQLiteStore(db_path)
    pocket = PocketStore(store)
    try:
        recovered = pocket.recover_reconciliation_jobs()
    finally:
        store.close()

    assert [job["job_id"] for job in recovered] == [job_id]
    assert recovered[0]["status"] == "pending"
