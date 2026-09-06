from fastapi.testclient import TestClient

from aftergraph_work_intelligence.api import create_app


def _make_client(tmp_path):
    return TestClient(create_app(db_path=tmp_path / "merge.db"))


def _ingest(client, tenant, text, external_id):
    response = client.post(
        "/v1/observations",
        json={"tenant_id": tenant, "source": "conversation", "external_id": external_id, "text": text},
    )
    assert response.status_code in (200, 201, 202), response.text
    return response.json()["work_item"]["id"]


def _merge(client, source_id, tenant, target_id, actor="merge-test"):
    return client.post(
        f"/v1/work-items/{source_id}/merge",
        params={"tenant_id": tenant},
        json={"actor": actor, "target_work_item_id": target_id},
    )


def test_merge_marks_duplicate_cancelled_and_links_target(tmp_path):
    with _make_client(tmp_path) as client:
        canonical = _ingest(client, "renos", "Vi skal købe parfumefri sæbe før mandag", "m-1")
        duplicate = _ingest(client, "renos", "Vi skal bestille nye håndklæder til omklædningen", "m-2")

        response = _merge(client, duplicate, "renos", canonical)
        assert response.status_code == 200, response.text
        assert response.json()["merged_into_work_item_id"] == canonical

        detail = client.get(f"/v1/work-items/{duplicate}", params={"tenant_id": "renos"})
        assert detail.json()["work_item"]["status"] == "CANCELLED"

        canonical_detail = client.get(f"/v1/work-items/{canonical}", params={"tenant_id": "renos"})
        assert canonical_detail.json()["work_item"]["status"] == "OPEN"


def test_merge_replay_is_idempotent(tmp_path):
    with _make_client(tmp_path) as client:
        canonical = _ingest(client, "renos", "Kunden mangler svar på tilbuddet senest fredag", "m-3")
        duplicate = _ingest(client, "renos", "Vi skal sende fakturaen til kunden i dag", "m-4")

        assert _merge(client, duplicate, "renos", canonical).status_code == 200
        replay = _merge(client, duplicate, "renos", canonical)
        assert replay.status_code == 200
        assert replay.json()["merged_into_work_item_id"] == canonical


def test_merge_rejects_self_merge_and_unknown_target(tmp_path):
    with _make_client(tmp_path) as client:
        item = _ingest(client, "renos", "Vi skal ringe til leverandøren i morgen", "m-5")

        self_merge = _merge(client, item, "renos", item)
        assert self_merge.status_code == 400

        unknown = _merge(client, item, "renos", "no-such-item")
        assert unknown.status_code == 404


def test_merge_is_tenant_scoped(tmp_path):
    with _make_client(tmp_path) as client:
        renos_item = _ingest(client, "renos", "Vi skal opdatere prislisten inden fredag", "m-6")
        other_item = _ingest(client, "other", "Kontakt elektrikeren angående lyset på lageret", "m-7")

        cross = _merge(client, renos_item, "renos", other_item)
        assert cross.status_code == 404

        # Source itself must also be tenant-visible.
        foreign_source = _merge(client, other_item, "renos", renos_item)
        assert foreign_source.status_code == 404


def test_merge_conflicts_when_already_cancelled_for_another_reason(tmp_path):
    with _make_client(tmp_path) as client:
        canonical = _ingest(client, "renos", "Vi skal betale huslejen senest mandag", "m-8")
        cancelled = _ingest(client, "renos", "Aftal tid med revisoren i næste uge", "m-9")

        cancel = client.post(
            f"/v1/work-items/{cancelled}/review",
            params={"tenant_id": "renos"},
            json={"action": "cancel", "actor": "merge-test"},
        )
        assert cancel.status_code == 200

        conflict = _merge(client, cancelled, "renos", canonical)
        assert conflict.status_code == 409
