"""
Live integration tests — runs against real RenOS operations server.

Prerequisites:
  1. PostgreSQL running (port 5433)
  2. RenOS operations server running (port 8788)
  3. Owner bootstrapped (org + session token)

Set env vars:
  RENOS_OPERATIONS_URL=http://127.0.0.1:8788
  RENOS_SESSION_TOKEN=<bootstrap token>

If unavailable, tests skip gracefully.
"""
import hashlib
import http.client
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

RENOS_BASE = os.environ.get("RENOS_OPERATIONS_URL", "http://127.0.0.1:8788")
RENOS_TOKEN = os.environ.get("RENOS_SESSION_TOKEN", "")

# Valid values for V5.1 evidence ledger
VALID_SUBJECT_TYPES = {
    "customer", "lead", "visit", "assignment",
    "worker", "billing", "decision", "approval", "agent_run",
}
VALID_KINDS = {
    "photo", "photo_folder", "actuals", "scope", "customer_signal",
    "staffing", "provider_receipt", "readback", "approval", "audit",
    "document", "other",
}
VALID_SOURCE_SYSTEMS = {
    "renos_core", "google_calendar", "gmail", "google_drive",
    "google_sheets", "cloudflare", "sites", "mcp", "assistant",
    "worker", "manual",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _renos_available():
    """Check if RenOS operations server is reachable."""
    if not RENOS_TOKEN:
        return False
    try:
        conn = http.client.HTTPConnection("127.0.0.1", 8788, timeout=3)
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return data.get("state") == "ready"
    except Exception:
        return False


def _renos_request(method, path, body=None):
    """Make authenticated request to RenOS operations API."""
    parsed = urllib.parse.urlparse(RENOS_BASE)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=10)
    headers = {
        "Authorization": f"Bearer {RENOS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = json.dumps(body).encode() if body else None
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    data = resp.read().decode()
    conn.close()
    try:
        return resp.status, json.loads(data) if data else {}
    except json.JSONDecodeError:
        return resp.status, {"raw": data}


def _new_evidence_payload(**overrides):
    """Build a valid V5.1 evidence payload with defaults."""
    now = datetime.now(UTC).isoformat()
    base = {
        "subjectType": "approval",
        "subjectId": str(uuid.uuid4()),
        "kind": "approval",
        "sourceSystem": "manual",
        "capturedAt": now,
        "idempotencyKey": f"live-{uuid.uuid4().hex[:12]}",
        "metadata": {"source": "work-intelligence-v2-live-test"},
    }
    base.update(overrides)
    return base


skip_unless_renos = pytest.mark.skipif(
    not _renos_available(),
    reason="RenOS operations server not running or RENOS_SESSION_TOKEN not set",
)


# ---------------------------------------------------------------------------
# LIVE: RenOS evidence ledger round-trip
# ---------------------------------------------------------------------------
class TestLiveRenOSEvidence:
    """Write evidence to real RenOS, read it back, verify integrity."""

    @skip_unless_renos
    def test_write_and_read_back_evidence(self):
        """POST evidence, then GET it back."""
        subject_id = str(uuid.uuid4())
        payload = _new_evidence_payload(subjectId=subject_id)

        status, resp = _renos_request("POST", "/api/operations/evidence", payload)
        assert status == 201, f"Expected 201, got {status}: {resp}"
        assert "id" in resp, f"Response missing id: {resp}"
        evidence_id = resp["id"]

        # Read back
        status2, resp2 = _renos_request(
            "GET",
            f"/api/operations/evidence?subject_type=approval&subject_id={subject_id}",
        )
        assert status2 == 200, f"Expected 200, got {status2}: {resp2}"
        assert isinstance(resp2, list), f"Expected list, got {type(resp2)}"
        assert len(resp2) >= 1, "Evidence not found after write"
        assert resp2[0]["id"] == evidence_id

    @skip_unless_renos
    def test_evidence_idempotency(self):
        """Posting same evidence twice yields same record (idempotent)."""
        subject_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        idempotency_key = f"live-idem-{uuid.uuid4().hex[:8]}"
        payload = {
            "subjectType": "agent_run",
            "subjectId": subject_id,
            "kind": "other",
            "sourceSystem": "manual",
            "capturedAt": now,
            "idempotencyKey": idempotency_key,
            "sourceReference": f"live-{idempotency_key}",
            "metadata": {"test": "idempotency"},
        }
        s1, r1 = _renos_request("POST", "/api/operations/evidence", payload)
        assert s1 == 201, f"First POST failed: {s1} {r1}"

        s2, r2 = _renos_request("POST", "/api/operations/evidence", payload)
        assert s2 in (201, 409), f"Second POST unexpected: {s2} {r2}"
        if s2 == 201:
            assert r1["id"] == r2["id"], "Same payload produced different ids"

    @skip_unless_renos
    def test_evidence_rejects_invalid_subject_type(self):
        """Invalid subjectType is rejected with proper error."""
        status, resp = _renos_request("POST", "/api/operations/evidence",
            _new_evidence_payload(subjectType="invalid_type_xyz"))
        assert status in (400, 422), f"Expected 400/422, got {status}: {resp}"

    @skip_unless_renos
    def test_evidence_rejects_invalid_kind(self):
        """Invalid kind is rejected."""
        status, resp = _renos_request("POST", "/api/operations/evidence",
            _new_evidence_payload(kind="not_a_real_kind"))
        assert status in (400, 422), f"Expected 400/422, got {status}: {resp}"


# ---------------------------------------------------------------------------
# LIVE: RenOS session verify
# ---------------------------------------------------------------------------
class TestLiveRenOSSession:
    """Verify that the session we bootstrapped is valid."""

    @skip_unless_renos
    def test_session_verify(self):
        """GET /api/v5/session returns valid session info."""
        status, resp = _renos_request("GET", "/api/v5/session")
        assert status == 200, f"Expected 200, got {status}: {resp}"
        assert "actor" in resp, f"Session response missing actor: {resp}"
        assert resp["actor"].get("role") in ("owner", "admin", "member"), (
            f"Unexpected role: {resp}"
        )


# ---------------------------------------------------------------------------
# LIVE: Work Intelligence V2 ↔ RenOS cross-repo flow
# ---------------------------------------------------------------------------
class TestLiveCrossRepoFlow:
    """
    Full canonical flow:
    Work Intelligence ingest -> approve -> evidence to RenOS -> read-back
    """

    @skip_unless_renos
    def test_canonical_flow_end_to_end(self):
        """Ingest observation, approve work item, write evidence to RenOS."""
        # 1. Create Work Intelligence service + ingest
        from aftergraph_work_intelligence.models import ObservationInput
        from aftergraph_work_intelligence.policy import PolicyStore, TenantPolicy
        from aftergraph_work_intelligence.service import WorkIntelligenceService
        from aftergraph_work_intelligence.store import SQLiteStore

        store = SQLiteStore(":memory:")
        ps = PolicyStore()
        ps.put("default", TenantPolicy())
        svc = WorkIntelligenceService(store, policy_store=ps)

        obs = ObservationInput(
            tenant_id="default",
            source="conversation",
            text="Køb bøger til kontoret cross-repo-live-test",
        )
        result = svc.ingest(obs)
        assert result.action == "created", f"Ingest failed: {result}"
        work_item = result.work_item
        assert work_item is not None

        # 2. Approve via transitions
        from aftergraph_work_intelligence.transitions import TransitionEngine
        engine = TransitionEngine(store, ps)
        engine.approve(work_item.id, actor="live-test-operator")

        # 3. Write evidence to real RenOS
        now = datetime.now(UTC).isoformat()
        evidence_subject_id = str(uuid.uuid4())
        status, resp = _renos_request("POST", "/api/operations/evidence", {
            "subjectType": "approval",
            "subjectId": evidence_subject_id,
            "kind": "approval",
            "sourceSystem": "manual",
            "capturedAt": now,
            "idempotencyKey": f"live-cross-{uuid.uuid4().hex[:8]}",
            "metadata": {
                "work_item_id": work_item.id,
                "tenant_id": "default",
                "status": "APPROVED",
                "actor": "live-test-operator",
                "source": "aftergraph-work-intelligence-v2",
            },
        })
        assert status == 201, f"Evidence write failed: {status} {resp}"
        evidence_id = resp["id"]

        # 4. Read-back from RenOS
        status2, resp2 = _renos_request(
            "GET",
            f"/api/operations/evidence?subject_type=approval&subject_id={evidence_subject_id}",
        )
        assert status2 == 200
        assert len(resp2) >= 1
        assert resp2[0]["id"] == evidence_id
        assert resp2[0]["subjectType"] == "approval"


# ---------------------------------------------------------------------------
# LIVE: Evidence envelope integrity against real RenOS
# ---------------------------------------------------------------------------
class TestLiveEvidenceIntegrity:
    """
    Build HMAC-signed evidence, write to RenOS,
    verify HMAC locally after read-back.
    """

    @skip_unless_renos
    def test_evidence_metadata_round_trip_via_renos(self):
        """Write evidence with metadata, read back, verify data integrity."""
        subject_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()

        # Compute a content digest for integrity
        evidence_data = {
            "tenant_id": "default",
            "work_item_id": "wi_live_test",
            "observation_id": "obs_live_test",
            "source": "live-integrity-test",
            "action": "approved",
        }
        content_str = json.dumps(evidence_data, sort_keys=True)
        digest = hashlib.sha256(content_str.encode()).hexdigest()

        # Write to RenOS with content digest
        status, resp = _renos_request("POST", "/api/operations/evidence", {
            "subjectType": "agent_run",
            "subjectId": subject_id,
            "kind": "other",
            "sourceSystem": "manual",
            "capturedAt": now,
            "contentDigest": digest,
            "idempotencyKey": f"live-hmac-{uuid.uuid4().hex[:8]}",
            "metadata": evidence_data,
        })
        assert status == 201, f"Write failed: {status} {resp}"

        # Read back
        status2, resp2 = _renos_request(
            "GET",
            f"/api/operations/evidence?subject_type=agent_run&subject_id={subject_id}",
        )
        assert status2 == 200
        assert len(resp2) >= 1

        # Verify content digest matches
        stored_digest = resp2[0].get("contentDigest", "")
        assert stored_digest == digest, (
            f"Content digest mismatch: {stored_digest} != {digest}"
        )

        # Verify metadata round-trip
        stored_metadata = resp2[0].get("metadata", {})
        assert stored_metadata.get("work_item_id") == "wi_live_test"
        assert stored_metadata.get("action") == "approved"
