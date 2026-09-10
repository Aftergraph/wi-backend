"""Strict-bool boundary for SensingRequest (lax-bool coercion hole).

SensingRequest.claims_execution / claims_admission / admitted_by_tg must
reject non-JSON-booleans ('yes'/'1'/1/'on'/...) with a validation error so
the core strict type gate is not dead at the HTTP boundary. Real bools
(True/False) must still be accepted.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from aftergraph_work_intelligence.api import SensingRequest, create_app

BASE = {
    "schema": "proactivity/0.1",
    "sensing_id": "sen_" + "1" * 32,
    "path": "wie",
    "native_ref": "wie:signal:000001",
    "candidate_kind": "opportunity",
    "claims_execution": False,
    "claims_admission": False,
    "admitted_by_tg": False,
    "correlated_paths": ["wie"],
    "asserted_at": "2026-09-08T12:00:00Z",
    "tenant_id": "ten_" + "1" * 32,
}

BOOL_FIELDS = ["claims_execution", "claims_admission", "admitted_by_tg"]
TRUTHY_NON_BOOLS = ["yes", "1", 1, "on", "true", "True", "YES"]
FALSY_NON_BOOLS = ["no", "0", 0, "off", "false", "False", "nope", "", None]


@pytest.mark.parametrize("field", BOOL_FIELDS)
@pytest.mark.parametrize("value", TRUTHY_NON_BOOLS)
def test_truthy_non_bool_rejected(field, value):
    with pytest.raises(ValidationError):
        SensingRequest(**{**BASE, field: value})


@pytest.mark.parametrize("field", BOOL_FIELDS)
@pytest.mark.parametrize("value", FALSY_NON_BOOLS)
def test_falsy_non_bool_rejected(field, value):
    with pytest.raises(ValidationError):
        SensingRequest(**{**BASE, field: value})


@pytest.mark.parametrize("field", BOOL_FIELDS)
@pytest.mark.parametrize("value", [True, False])
def test_real_bools_accepted(field, value):
    req = SensingRequest(**{**BASE, field: value})
    assert getattr(req, field) is value


def test_endpoint_rejects_string_bool_with_422(tmp_path):
    app = create_app(db_path=tmp_path / "strict-bool.db")
    with TestClient(app) as client:
        bad = dict(BASE, claims_execution="yes")
        resp = client.post("/v1/sensing", json=bad)
        assert resp.status_code == 422
        good = dict(BASE)
        resp = client.post("/v1/sensing", json=good)
        assert resp.status_code in (200, 201)
