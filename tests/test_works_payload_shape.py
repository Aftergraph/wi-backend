"""Works-payload shape contract: our builder must keep emitting every top-level
key the frozen works-execution work.schema requires.

Verified 2026-09-07 by validating a real built payload against
works-execution contracts/schemas/work.schema.schema.json
(freeze manifest 2d2f1d27474a908a19aafb9c152be5e27c80987400f21cdfca94080b8bf14a86):
0 errors. This test pins OUR side (no vendored copy, no cross-repo path):
if the builder ever drops a required key, it fails here first.
If works-execution amends the frozen schema, re-run the manual validation
and update REQUIRED_KEYS + the manifest sha cited above.
"""

from fastapi.testclient import TestClient

from aftergraph_work_intelligence.api import create_app
from aftergraph_work_intelligence.publishers import _build_works_payload

_TOKEN = "works-shape-token"

# Required top-level keys of the frozen work.schema (see module docstring).
REQUIRED_KEYS = {
    "id",
    "created_at",
    "updated_at",
    "source",
    "objective",
    "graph",
    "requirements",
    "policy",
    "state",
}


def test_works_payload_keeps_required_shape(tmp_path):
    app = create_app(db_path=tmp_path / "shape.db", api_token=_TOKEN)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {_TOKEN}"}
        ingest = client.post(
            "/v1/observations",
            json={"tenant_id": "renos", "source": "conversation", "external_id": "ws-1", "text": "Vi skal købe parfumefri sæbe før mandag"},
            headers=headers,
        )
        assert ingest.status_code in (200, 201, 202), ingest.text
        work_item_id = ingest.json()["work_item"]["id"]
        detail = app.state.service.get_work_item_detail(work_item_id, "renos")
        payload = _build_works_payload(detail.work_item, detail.observations)
        assert REQUIRED_KEYS <= set(payload.keys())
        assert payload["state"] == "CREATED"
        assert payload["objective"]["description"]
        assert payload["source"]["work_item_id"] == work_item_id
