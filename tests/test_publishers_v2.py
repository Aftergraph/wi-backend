"""TDD tests for V2 destination publishers.

V2 has three concrete destinations in addition to V1's webhook:

  - ``renos``     → Project-Renos Job (HTTP or DB-direct)
  - ``works``     → works-execution Work (POST /work, conforms to
                    contracts/schemas/work.schema.schema.json)
  - ``webhook``   → V1's generic webhook (kept for back-compat)

Each destination has its own adapter class with a tight contract. The
``PublishRouter`` dispatches based on destination name and enforces a per-
destination allowlist in the tenant policy.

End-to-end tests use an in-process FastAPI server that mimics the relevant
surface of Project-Renos / works-execution — no mocks as final evidence,
real HTTP round-trips against a conformant fake.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from aftergraph_work_intelligence.models import (
    Observation,
    WorkItem,
    WorkItemDetail,
    utc_now,
)
from aftergraph_work_intelligence.policy import PolicyStore, TenantPolicy
from aftergraph_work_intelligence.publishers import (
    RenosPublisher,
    WebhookPublisher,
    WorksPublisher,
    build_publish_router,
)

# ---------------- helpers ----------------


def _make_fake_renos() -> tuple[FastAPI, list]:
    """Build an in-process fake RenOS server that accepts Job POSTs."""
    app = FastAPI(title="fake-renos")
    jobs: list[dict] = []

    @app.post("/api/jobs")
    def create_job(payload: dict):
        # Simulate Project-Renos Job shape.
        job_id = f"job_{len(jobs) + 1}"
        record = {"id": job_id, **payload}
        jobs.append(record)
        return record

    @app.get("/api/jobs")
    def list_jobs():
        return {"jobs": jobs}

    return app, jobs


def _make_fake_works() -> tuple[FastAPI, list]:
    """Build an in-process fake works-execution server that accepts Work POSTs.

    Conforms to the published contract: a Work has
    ``id, created_at, updated_at, source, objective, graph, requirements, policy, state``.
    The fake validates the minimum required fields and stores accepted works.
    """
    app = FastAPI(title="fake-works")
    works: list[dict] = []

    @app.post("/work")
    def submit_work(payload: dict):
        for required in ("id", "created_at", "source", "objective", "graph", "state"):
            if required not in payload:
                raise HTTPException(status_code=400, detail=f"missing required field: {required}")
        works.append(payload)
        return {"id": payload["id"], "state": payload.get("state", "CREATED")}

    @app.get("/work")
    def list_works():
        return {"works": works}

    return app, works


def _make_work_item(tenant_id: str = "renos") -> WorkItem:
    now = utc_now()
    return WorkItem(
        id=f"wi_{hashlib.sha256(b'wi-1').hexdigest()[:24]}",
        tenant_id=tenant_id,
        title="Send bekræftelse til kunde",
        summary="Vi skal sende kunden en bekræftelse før mandag",
        status="APPROVED",
        priority="high",
        owner="jonas",
        due_hint="fredag",
        next_action="Send bekræftelse",
        confidence=0.92,
        canonical_key="abc123",
        canonical_tokens=("send", "bekræft", "kunde"),
        observation_count=1,
        created_at=now,
        updated_at=now,
    )


def _make_observation(tenant_id: str = "renos") -> Observation:
    now = utc_now()
    return Observation(
        id=f"obs_{hashlib.sha256(b'obs-1').hexdigest()[:24]}",
        tenant_id=tenant_id,
        source="conversation",
        external_id="t-1",
        actor="user:empir",
        text="Vi skal sende kunden en bekræftelse før mandag",
        metadata={"transcript_id": "t-1"},
        occurred_at=now,
        created_at=now,
    )


# ---------------- WebhookPublisher ----------------


def test_webhook_publisher_posts_signed_payload_and_returns_external_id():
    app = FastAPI()
    received: list[dict] = []

    @app.post("/hook/renos")
    async def hook(req: Request):
        body = await req.body()
        received.append({"json": json.loads(body), "sig": req.headers.get("X-Aftergraph-Signature")})
        return {"id": "ext-99"}

    client = TestClient(app)
    # Use the FastAPI server in a thread to drive TestClient.
    with client:
        # TestClient needs the app context — easier: drive via requests/thread.
        pass

    # Use real HTTP via uvicorn in a thread.
    import socket

    import uvicorn

    # Find free port
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        # Wait for ready
        deadline = time.time() + 5
        while time.time() < deadline and not server.started:
            time.sleep(0.05)
        # Publish
        publisher = WebhookPublisher({"renos": f"http://127.0.0.1:{port}/hook/renos"}, secret="s3cr3t")
        receipt = publisher.publish("renos", _make_work_item(), [_make_observation()])
        assert receipt.destination == "renos"
        assert receipt.external_id == "ext-99"
        # Verify signature
        assert received, "no payload received"
        body = json.dumps(received[0]["json"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        expected = "sha256=" + hmac.new(b"s3cr3t", body, hashlib.sha256).hexdigest()
        assert received[0]["sig"] == expected
    finally:
        server.should_exit = True
        thread.join(timeout=2)


# ---------------- RenosPublisher ----------------


def test_renos_publisher_creates_job_in_real_http_round_trip():
    app, jobs = _make_fake_renos()
    import socket

    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline and not server.started:
            time.sleep(0.05)
        publisher = RenosPublisher(base_url=f"http://127.0.0.1:{port}", company_id="company-123")
        receipt = publisher.publish("renos", _make_work_item("renos"), [_make_observation("renos")])
        assert receipt.external_id == "job_1"
        assert jobs[0]["title"] == "Send bekræftelse til kunde"
        assert jobs[0]["companyId"] == "company-123"
        assert jobs[0]["priority"] == "high"
    finally:
        server.should_exit = True
        thread.join(timeout=2)


# ---------------- WorksPublisher ----------------


def test_works_publisher_posts_conformant_work_payload():
    app, works = _make_fake_works()
    import socket

    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline and not server.started:
            time.sleep(0.05)
        publisher = WorksPublisher(base_url=f"http://127.0.0.1:{port}")
        receipt = publisher.publish("works", _make_work_item(), [_make_observation()])
        assert receipt.destination == "works"
        assert works, "no work submitted"
        submitted = works[0]
        # Must conform to work.schema/1.0 required fields.
        for required in ("id", "created_at", "updated_at", "source", "objective", "graph", "state"):
            assert required in submitted, f"missing {required}"
        # Source carries provenance
        assert submitted["source"]["kind"] == "aftergraph.work-intelligence"
        assert submitted["source"]["work_item_id"] == _make_work_item().id
        # Objective reflects the canonical work-item
        assert submitted["objective"]["summary"] == "Vi skal sende kunden en bekræftelse før mandag"
    finally:
        server.should_exit = True
        thread.join(timeout=2)


# ---------------- PublishRouter ----------------


def test_publish_router_dispatches_by_destination_and_enforces_tenant_policy():
    app, _jobs = _make_fake_renos()
    import socket

    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline and not server.started:
            time.sleep(0.05)
        policy_store = PolicyStore()
        # tenant allows renos, NOT works
        policy_store.put("renos", TenantPolicy(allowed_destinations={"renos"}))
        pub = build_publish_router(
            destinations={
                "renos": RenosPublisher(base_url=f"http://127.0.0.1:{port}", company_id="company-123"),
            },
            policy_store=policy_store,
        )
        item = _make_work_item()
        detail = WorkItemDetail(work_item=item, observations=[_make_observation()], publications=[])
        receipt = pub.publish("renos", item, [detail.observations[0]])
        assert receipt.destination == "renos"
        # Works is denied by policy
        from aftergraph_work_intelligence.publishers import DestinationNotAllowed
        with pytest.raises(DestinationNotAllowed):
            pub.publish("works", item, [detail.observations[0]])
    finally:
        server.should_exit = True
        thread.join(timeout=2)