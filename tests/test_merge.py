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
        assert response.json()["work_item"]["merged_into_work_item_id"] == canonical

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
        assert replay.json()["work_item"]["merged_into_work_item_id"] == canonical
        assert replay.json()["evidence"]["idempotent_replay"] is True


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


def test_merge_evidence_envelope_is_reconstructable(tmp_path):
    from fastapi.testclient import TestClient

    from aftergraph_work_intelligence.api import create_app

    with TestClient(create_app(db_path=tmp_path / "merge-ev.db")) as client:
        canonical = _ingest(client, "renos", "Vi skal smøre låsen i bagdøren", "me-1")
        duplicate = _ingest(client, "renos", "Vi skal pudse spejlet i forhallen", "me-2")

        response = client.post(
            f"/v1/work-items/{duplicate}/merge",
            params={"tenant_id": "renos"},
            json={"actor": "e2e", "target_work_item_id": canonical, "idempotency_key": "key-evidence-1"},
        )
        assert response.status_code == 200, response.text
        evidence = response.json()["evidence"]
        assert evidence["actor"] == "e2e"
        assert evidence["tenant_id"] == "renos"
        assert evidence["source_work_item_id"] == duplicate
        assert evidence["target_work_item_id"] == canonical
        assert evidence["previous_state"] == "OPEN"
        assert evidence["resulting_state"] == "CANCELLED"
        assert evidence["idempotency_key"] == "key-evidence-1"
        assert evidence["idempotent_replay"] is False
        assert evidence["decided_at"]
        assert evidence["trace_id"]


def test_merge_idempotency_key_replay_and_reuse_conflict(tmp_path):
    from fastapi.testclient import TestClient

    from aftergraph_work_intelligence.api import create_app

    with TestClient(create_app(db_path=tmp_path / "merge-key.db")) as client:
        canonical = _ingest(client, "renos", "Vi skal male gavlen til foråret", "mk-1")
        other = _ingest(client, "renos", "Vi skal rense tagrenderne", "mk-2")
        duplicate = _ingest(client, "renos", "Vi skal ordne bedet ved muren", "mk-3")

        first = client.post(
            f"/v1/work-items/{duplicate}/merge",
            params={"tenant_id": "renos"},
            json={"actor": "e2e", "target_work_item_id": canonical, "idempotency_key": "key-reuse-1"},
        )
        assert first.status_code == 200

        replay = client.post(
            f"/v1/work-items/{duplicate}/merge",
            params={"tenant_id": "renos"},
            json={"actor": "e2e", "target_work_item_id": canonical, "idempotency_key": "key-reuse-1"},
        )
        assert replay.status_code == 200
        assert replay.json()["evidence"]["idempotent_replay"] is True

        reuse = client.post(
            f"/v1/work-items/{duplicate}/merge",
            params={"tenant_id": "renos"},
            json={"actor": "e2e", "target_work_item_id": other, "idempotency_key": "key-reuse-1"},
        )
        assert reuse.status_code == 409


def test_migration_v5_adds_idempotency_column(tmp_path):
    from aftergraph_work_intelligence.migrations import run_migrations

    result = run_migrations(db_path=tmp_path / "mig5.db")
    assert result["current_version"] >= 5
    assert any(m["version"] == 5 for m in result["migrations"])


def test_merge_retry_with_new_key_is_idempotent(tmp_path):
    """UI timeout retries generate a fresh key per attempt — same target must replay 200."""
    from fastapi.testclient import TestClient

    from aftergraph_work_intelligence.api import create_app

    with TestClient(create_app(db_path=tmp_path / "merge-retry.db")) as client:
        canonical = _ingest(client, "renos", "Vi skal skifte filteret i emhætten", "mr-1")
        duplicate = _ingest(client, "renos", "Vi skal tørre vinduerne i gangen", "mr-2")

        def merge(key):
            return client.post(
                f"/v1/work-items/{duplicate}/merge",
                params={"tenant_id": "renos"},
                json={"actor": "e2e", "target_work_item_id": canonical, "idempotency_key": key},
            )

        assert merge("retry-key-attempt-1").status_code == 200
        retry = merge("retry-key-attempt-2")
        assert retry.status_code == 200
        assert retry.json()["evidence"]["idempotent_replay"] is True
