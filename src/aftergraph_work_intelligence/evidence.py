"""V2 evidence builder.

The V2 Work Intelligence service emits a content-addressed evidence envelope
that mirrors the Aftergraph L2 evidence contract (``evidence.schema/1.1``)
in a portable, schema-versioned way:

    schema:       aftergraph.work-item-evidence/1.0
    bundle_id:    ev_<uuid>
    provider_id:  aftergraph.work-intelligence
    created_at:   ISO timestamp when the evidence was built
    identity_chain:
        tenant_id, work_item_id, canonical_key
    records:
        A flat list of observation provenance records (one per observation
        on the work-item at the time of evidence generation).
    digest:       HMAC-SHA256 over the canonical serialization of the
                  identity_chain + records + canonical payload fields.
                  Keyed by an operator-provided ``secret``.

The envelope is portable: it doesn't require works-execution to be online.
A downstream auditor can verify the digest given the canonical payload.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

_EVIDENCE_SCHEMA = "aftergraph.work-item-evidence/1.0"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _canonical_payload(payload: dict[str, Any]) -> bytes:
    """Serialize the payload in a stable order for HMAC computation.

    The canonical form ignores ``bundle_id`` and ``digest`` (which are derived
    from the rest) and uses sorted keys + ISO strings.
    """
    canonical = {
        "tenant_id": payload["tenant_id"],
        "work_item_id": payload["work_item_id"],
        "title": payload.get("title", ""),
        "canonical_key": payload["canonical_key"],
        "observations": [
            {
                "id": o["id"],
                "source": o["source"],
                "external_id": o.get("external_id"),
                "actor": o.get("actor"),
                "occurred_at": o.get("occurred_at"),
                "text": o.get("text", ""),
            }
            for o in payload["observations"]
        ],
    }
    return _canonical_json(canonical)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class EvidenceBuilder:
    secret: str
    provider_id: str = "aftergraph.work-intelligence"

    def build(self, payload: dict[str, Any]) -> dict[str, Any]:
        canonical = _canonical_payload(payload)
        digest = hmac.new(self.secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
        records = [
            {
                "kind": "observation",
                "observation_id": o["id"],
                "source": o["source"],
                "external_id": o.get("external_id"),
                "actor": o.get("actor"),
                "occurred_at": o.get("occurred_at"),
                "text_sha256": hashlib.sha256((o.get("text") or "").encode("utf-8")).hexdigest(),
            }
            for o in payload["observations"]
        ]
        return {
            "schema": _EVIDENCE_SCHEMA,
            "bundle_id": f"ev_{uuid.uuid4().hex}",
            "provider_id": self.provider_id,
            "created_at": _now().isoformat(),
            "identity_chain": {
                "tenant_id": payload["tenant_id"],
                "work_item_id": payload["work_item_id"],
                "canonical_key": payload["canonical_key"],
                "title": payload.get("title", ""),
            },
            "records": records,
            "observations_count": len(records),
            "observations": [
                {  # carry the full provenance for convenience
                    "id": o["id"],
                    "source": o["source"],
                    "external_id": o.get("external_id"),
                    "actor": o.get("actor"),
                    "occurred_at": o.get("occurred_at"),
                }
                for o in payload["observations"]
            ],
            "algorithm": "HMAC-SHA256",
            "digest": digest,
        }


def build_evidence(payload: dict[str, Any], *, secret: str) -> dict[str, Any]:
    return EvidenceBuilder(secret=secret).build(payload)


def verify_evidence(envelope: dict[str, Any], payload: dict[str, Any], *, secret: str) -> bool:
    if envelope.get("schema") != _EVIDENCE_SCHEMA:
        return False
    canonical = _canonical_payload(payload)
    expected = hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    return hmac.compare_digest(envelope.get("digest", ""), expected)


__all__ = ["EvidenceBuilder", "build_evidence", "verify_evidence"]