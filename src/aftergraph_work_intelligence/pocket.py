"""Pocket provider subsystem (Wie, device-independent).

Owns the Pocket connector inside ``wi-backend`` as a provider subsystem under
Wie, per the ``pocket-source/0.1`` contract and the ``POCKET-SOURCE-V1`` seam
binding:

- ingest Pocket-source signals, keeping derivation lineage (transcript,
  speaker attribution, summary, action extraction) with distinct evidentiary
  weights and preserved uncertainty into the Wie Observation mapping;
- REST is the canonical/reconciliation plane; webhooks are event-plane signals
  guarded by HMAC-SHA256 signature binding, replay protection, and
  idempotency-key dedupe; MCP is optional interactive access, never canonical;
- consent/purpose lineage is attached at intake and propagated to derivatives;
  consent revocation or source deletion invalidates downstream use per
  provenance without rewriting historical audit (tombstone semantics);
- Pocket output is observation only and carries zero execution authority:
  transcript != identity, speaker attribution != principal authentication,
  spoken instruction != permission to execute.

HARD OUT OF SCOPE (BLOCKED_ON_POCKET_HARDWARE — nothing here touches it):
device audio capture, microphone arrays, acoustic environment, or any
realtime-audio pipeline. This module proves device-independent seams only,
with fixtures.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from .adapters import SourceAdapter
from .models import ObservationInput

SCHEMA = "pocket-source/0.1"

#: Distinct evidentiary weights per derivation kind (contract PCK-007).
DERIVATION_WEIGHTS: dict[str, float] = {
    "transcript": 0.7,
    "speaker_attribution": 0.4,
    "summary": 0.6,
    "action_extraction": 0.5,
}

#: Default uncertainty carried when a derivation arrives without its own.
DERIVATION_UNCERTAINTY_DEFAULTS: dict[str, float] = {
    "transcript": 0.3,
    "speaker_attribution": 0.6,
    "summary": 0.4,
    "action_extraction": 0.5,
}

_DERIVATION_KINDS = frozenset(DERIVATION_WEIGHTS)
_CLASSIFICATIONS = frozenset(
    {"authority", "enforcement", "runtime", "execution", "verification", "research"}
)
_CANDIDATE_KINDS = frozenset(
    {"observation_update", "attention_candidate", "commitment_candidate", "finding"}
)

_POCKET_ID_RE = re.compile(r"^pck_[a-f0-9]{32}$")
_TENANT_ID_RE = re.compile(r"^ten_[a-f0-9]{32}$")
_SPEAKER_CONFIDENCE_THRESHOLD = 0.5

_INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(all\s+)?(previous|prior)\s+instructions?", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?(previous|prior)\s+instructions?", re.IGNORECASE),
    re.compile(r"(^|[\s>])system\s*:", re.IGNORECASE),
    re.compile(r"\[system\]", re.IGNORECASE),
    re.compile(r"\brm\s+-rf\b", re.IGNORECASE),
    re.compile(r"\bsudo\b", re.IGNORECASE),
    re.compile(r"\bexfiltrat\w*", re.IGNORECASE),
    re.compile(r"grant\s+admin", re.IGNORECASE),
    re.compile(r"\bjailbreak\b", re.IGNORECASE),
    re.compile(r"\bdan\s+mode\b", re.IGNORECASE),
    re.compile(r"bypass\s+(safety|verification|approval|auth)", re.IGNORECASE),
)

#: Authority-conflation phrases that Pocket content must never assert.
_AUTHORITY_CONFLATION_PHRASES = (
    "pocket as principal",
    "pocket as authority",
    "pocket as executor",
    "pocket as oracle",
    "transcript as identity",
    "speaker as authentication",
    "spoken command as permission",
    "spoken-command as permission",
    "mcp as ingestion",
    "webhook as truth",
)


class PocketRejected(ValueError):
    """Fail-closed rejection of a Pocket record, keyed to a contract vector."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"[{code}] {reason}")
        self.code = code
        self.reason = reason


class PocketReplay(PocketRejected):
    """A delivery_id already seen for this tenant (PCK-012)."""

    def __init__(self, delivery_id: str) -> None:
        super().__init__("PCK-012", f"replayed delivery_id {delivery_id}")
        self.delivery_id = delivery_id


class PocketDuplicate(PocketRejected):
    """An idempotency key already materialized for this tenant (PCK-013)."""

    def __init__(self, idempotency_key: str, original_delivery_id: str | None) -> None:
        super().__init__(
            "PCK-013", f"duplicate idempotency_key {idempotency_key}"
        )
        self.idempotency_key = idempotency_key
        self.original_delivery_id = original_delivery_id


def scan_injection(text: str) -> bool:
    """Return True when transcript text carries prompt-injection markers.

    Injected content stays untrusted observation — it is flagged, never
    executed. Plain conversational text returns False.
    """
    candidate = text or ""
    return any(pattern.search(candidate) for pattern in _INJECTION_PATTERNS)


def contains_authority_conflation(text: str) -> bool:
    """Return True when text asserts a forbidden Pocket authority conflation."""
    lowered = (text or "").casefold()
    return any(phrase in lowered for phrase in _AUTHORITY_CONFLATION_PHRASES)


def sign_pocket_body(secret: str, body: bytes) -> str:
    """HMAC-SHA256 signature over the raw delivery body (``sha256=<hex>``)."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_pocket_signature(
    secret: str | None, body: bytes, signature_header: str | None
) -> bool:
    """Fail-closed verification of a Pocket webhook signature.

    The signature structurally binds the delivery_id because the delivery_id
    travels inside the signed body: any tampering breaks the digest.
    """
    if not secret or not signature_header:
        return False
    try:
        presented = signature_header
        presented = presented.removeprefix("sha256=")
        expected = bytes.fromhex(presented)
    except ValueError:
        return False
    computed = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(computed, expected)


def _tenant_secret_env_name(tenant_id: str) -> str:
    slug = re.sub(r"\W", "_", tenant_id).upper().strip("_") or "DEFAULT"
    return f"AFTERGRAPH_POCKET_WEBHOOK_SECRET_{slug}"


def resolve_pocket_secret(
    tenant_id: str | None, global_default: str | None = None
) -> str | None:
    """Resolve the webhook HMAC secret for a tenant.

    Per-tenant env override wins; otherwise the explicit global default, else
    the ``AFTERGRAPH_POCKET_WEBHOOK_SECRET`` env var. Fail-closed None.
    Provider credentials are tenant-scoped secret references — never raw
    secrets in Wie domain state.
    """
    if tenant_id:
        specific = os.getenv(_tenant_secret_env_name(tenant_id))
        if specific:
            return specific
    if global_default:
        return global_default
    return os.getenv("AFTERGRAPH_POCKET_WEBHOOK_SECRET")


def validate_pocket_record(
    payload: dict[str, Any], consent_store: PocketStore | None = None
) -> dict[str, Any]:
    """Validate a pocket-source/0.1 record; return the normalized record.

    Raises :class:`PocketRejected` (fail-closed) on any contract violation:
    tenant-scope mismatch (PCK-002), transcript-as-identity (PCK-003),
    speaker-as-authentication (PCK-004), self-execution/authority claims
    (PCK-005), revoked consent (PCK-021), or purpose mismatch (PCK-020).
    """
    if payload.get("schema") != SCHEMA:
        raise PocketRejected("PCK-001", "missing or unknown pocket-source schema")
    pocket_id = payload.get("pocket_id") or ""
    tenant_id = payload.get("tenant_id") or ""
    credential_scope = payload.get("credential_scope") or ""
    if not _POCKET_ID_RE.match(pocket_id):
        raise PocketRejected("PCK-001", "malformed pocket_id")
    if not _TENANT_ID_RE.match(tenant_id):
        raise PocketRejected("PCK-001", "malformed tenant_id")
    if credential_scope != tenant_id:
        raise PocketRejected(
            "PCK-002", "credential_scope is not scoped to tenant_id"
        )
    if payload.get("claims_principal_identity"):
        raise PocketRejected("PCK-003", "transcript is not principal identity")
    if payload.get("claims_principal_authentication"):
        raise PocketRejected(
            "PCK-004", "speaker attribution is not principal authentication"
        )
    if payload.get("self_executes") or payload.get("claims_execution"):
        raise PocketRejected(
            "PCK-005", "Pocket output never self-executes and claims no execution"
        )
    classification = payload.get("classification")
    if classification not in _CLASSIFICATIONS:
        raise PocketRejected("PCK-001", "unknown classification")
    candidate_kind = payload.get("candidate_kind")
    if candidate_kind not in _CANDIDATE_KINDS:
        raise PocketRejected("PCK-001", "unknown candidate_kind")
    derivations = payload.get("derivations")
    if not isinstance(derivations, list) or not 1 <= len(derivations) <= 4:
        raise PocketRejected("PCK-007", "derivations must list 1..4 entries")
    normalized_derivations = []
    for entry in derivations:
        if not isinstance(entry, dict):
            raise PocketRejected("PCK-007", "malformed derivation entry")
        kind = entry.get("derivation")
        weight = entry.get("weight")
        uncertainty = entry.get("uncertainty")
        lineage_ref = entry.get("lineage_ref")
        if kind not in _DERIVATION_KINDS:
            raise PocketRejected("PCK-007", f"unknown derivation {kind!r}")
        if not isinstance(weight, (int, float)) or not 0 <= weight <= 1:
            raise PocketRejected("PCK-007", "derivation weight must be within 0..1")
        if not isinstance(uncertainty, (int, float)) or not 0 <= uncertainty <= 1:
            raise PocketRejected(
                "PCK-007", "derivation uncertainty must be within 0..1"
            )
        if not lineage_ref:
            raise PocketRejected("PCK-007", "derivation lineage_ref is required")
        normalized_derivations.append(
            {
                "derivation": kind,
                "weight": float(weight),
                "uncertainty": float(uncertainty),
                "lineage_ref": lineage_ref,
            }
        )
    consent_ref = payload.get("consent_ref")
    purpose = payload.get("purpose")
    if consent_store is not None and consent_ref:
        expected_purpose = consent_store.consent_purpose(tenant_id, consent_ref)
        if expected_purpose is not None and purpose != expected_purpose:
            raise PocketRejected(
                "PCK-020", "purpose does not match consent lineage"
            )
        if consent_store.consent_revoked(tenant_id, consent_ref):
            raise PocketRejected(
                "PCK-021", "consent revoked: downstream use invalidated"
            )
    return {
        "pocket_id": pocket_id,
        "tenant_id": tenant_id,
        "source_ref": payload.get("source_ref") or "",
        "observation_ref": payload.get("observation_ref") or "",
        "classification": classification,
        "contains_instruction": bool(payload.get("contains_instruction")),
        "candidate_kind": candidate_kind,
        "admitted_by_tg": bool(payload.get("admitted_by_tg")),
        "governed_path_complete": bool(payload.get("governed_path_complete")),
        "derivations": normalized_derivations,
        "asserted_at": payload.get("asserted_at") or "",
        "consent_ref": consent_ref,
        "purpose": purpose,
        "conversation_id": payload.get("conversation_id") or "",
        "cross_conversation_lineage": bool(payload.get("cross_conversation_lineage")),
        "participants": list(payload.get("participants") or []),
        "segments": list(payload.get("segments") or []),
    }


class PocketAdapter(SourceAdapter):
    """Adapter for Pocket physical-world perception signals.

    Payload contract: a ``pocket-source/0.1`` record plus connector-level
    ``segments`` (transcript turns), ``conversation_id``, ``participants``
    and consent/purpose lineage. Each payload yields exactly one canonical
    ``ObservationInput`` with source ``"pocket"``; the external_id is stable
    per ``source_ref`` so redelivery is idempotent at the observation level.

    Speaker labels are derivation lineage only — ``actor`` is always None,
    because transcript is not identity and attribution is not authentication.
    """

    source = "pocket"

    def observations(
        self, payload: dict[str, Any], consent_store: PocketStore | None = None
    ) -> Iterable[ObservationInput]:
        record = validate_pocket_record(payload, consent_store)
        scope = record["conversation_id"]
        participants = set(record["participants"])
        usable: list[dict[str, Any]] = []
        for segment in record["segments"]:
            if not isinstance(segment, dict):
                continue
            text = (segment.get("text") or "").strip()
            if not text:
                continue
            seg_conversation = segment.get("conversation_id") or scope
            if seg_conversation != scope and not record["cross_conversation_lineage"]:
                raise PocketRejected(
                    "PCK-025",
                    "cross-conversation content without explicit lineage is rejected",
                )
            usable.append(
                {
                    "text": text,
                    "speaker": segment.get("speaker"),
                    "confidence": segment.get("speaker_confidence"),
                    "conversation_id": seg_conversation,
                }
            )
        if not usable:
            # Corrupt or empty transcripts fail closed with zero observations.
            raise PocketRejected("PCK-CORRUPT", "no usable transcript segments")

        injected = any(scan_injection(seg["text"]) for seg in usable)

        # Speaker attribution stays lineage: never principal identity.
        mismatch = False
        confidences: list[float] = []
        speaker_labels: list[str] = []
        for seg in usable:
            speaker = seg["speaker"]
            try:
                confidence = (
                    float(seg["confidence"]) if seg["confidence"] is not None else 0.5
                )
            except (TypeError, ValueError):
                confidence = 0.5
            confidence = min(1.0, max(0.0, confidence))
            confidences.append(confidence)
            if speaker is None:
                speaker_labels.append("unattributed")
                continue
            speaker_labels.append(str(speaker))
            if participants and speaker not in participants:
                mismatch = True
        uncertain = any(c < _SPEAKER_CONFIDENCE_THRESHOLD for c in confidences)
        attributed = (
            "unattributed"
            if mismatch or all(label == "unattributed" for label in speaker_labels)
            else speaker_labels[0]
        )

        derivations = list(record["derivations"])
        if injected:
            # Prompt-injected content yields no action extraction: injected
            # text is contained as untrusted observation, never instruction.
            derivations = [
                d for d in derivations if d["derivation"] != "action_extraction"
            ]
        if any(seg["speaker"] is not None for seg in usable) and not any(
            d["derivation"] == "speaker_attribution" for d in derivations
        ):
            worst_confidence = min(confidences) if confidences else 0.5
            derivations.append(
                {
                    "derivation": "speaker_attribution",
                    "weight": DERIVATION_WEIGHTS["speaker_attribution"],
                    "uncertainty": 1.0 - worst_confidence,
                    "lineage_ref": f"pocket:speaker:{record['pocket_id']}",
                }
            )

        text = "\n".join(seg["text"] for seg in usable)
        yield ObservationInput(
            tenant_id=record["tenant_id"],
            source=self.source,
            text=text,
            external_id=f"pocket:{record['source_ref']}",
            actor=None,
            metadata={
                "pocket_id": record["pocket_id"],
                "pocket_source_ref": record["source_ref"],
                "observation_ref": record["observation_ref"],
                "classification": record["classification"],
                "candidate_kind": record["candidate_kind"],
                "contains_instruction": record["contains_instruction"],
                "admitted_by_tg": record["admitted_by_tg"],
                "governed_path_complete": record["governed_path_complete"],
                "derivations": derivations,
                "consent_ref": record["consent_ref"],
                "purpose": record["purpose"],
                "conversation_id": scope,
                "cross_conversation_lineage": record["cross_conversation_lineage"],
                "speaker_attribution": attributed,
                "speaker_labels": speaker_labels,
                "speaker_confidence": min(confidences) if confidences else 0.5,
                "speaker_mismatch": mismatch,
                "speaker_uncertain": uncertain,
                "injection_contained": injected,
                # Zero execution authority: Pocket output is observation only.
                "execution_authority": "none",
            },
        )


def evaluate_commitment(record: dict[str, Any]) -> dict[str, Any]:
    """Gate a Pocket-derived commitment along the governed consequential path.

    A Pocket-derived commitment may become a ``CommitmentCandidate`` only when
    it carries an instruction, claims no execution, and the governed path
    (AIE -> Trust Gateway -> Runtime -> WORKS -> verification) is complete.
    Execution is never granted here: ``executed`` is always False — Pocket
    has zero execution authority.
    """
    admitted = (
        bool(record.get("contains_instruction"))
        and not bool(record.get("self_executes"))
        and not bool(record.get("claims_execution"))
        and record.get("candidate_kind") == "commitment_candidate"
        and bool(record.get("admitted_by_tg"))
        and bool(record.get("governed_path_complete"))
    )
    if admitted and contains_authority_conflation(
        str(record.get("candidate_label") or "")
    ):
        admitted = False
    return {
        "admitted_as": "commitment_candidate" if admitted else None,
        "executed": False,
        "execution_authority": "none",
    }


def mcp_answer(records: list[dict[str, Any]], access_mode: str) -> list[dict[str, Any]]:
    """Serve Pocket records over the optional MCP interactive plane.

    Interactive access returns the records unchanged and materializes nothing.
    Canonical access is rejected (PCK-027): MCP never supplies canonical data
    and never serves as the ingestion source.
    """
    if access_mode == "interactive":
        return list(records)
    raise PocketRejected(
        "PCK-027", "MCP is optional interactive access, never canonical"
    )


_POCKET_SCHEMA = """
CREATE TABLE IF NOT EXISTS pocket_deliveries (
    tenant_id TEXT NOT NULL,
    delivery_id TEXT NOT NULL,
    idempotency_key TEXT,
    sequence_number INTEGER NOT NULL DEFAULT 0,
    source_ref TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, delivery_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pocket_deliveries_idem
ON pocket_deliveries(tenant_id, idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS pocket_sequences (
    tenant_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    applied_sequence INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, conversation_id)
);

CREATE TABLE IF NOT EXISTS pocket_supersessions (
    tenant_id TEXT NOT NULL,
    superseded_delivery_id TEXT NOT NULL,
    superseding_delivery_id TEXT NOT NULL,
    superseded_source_ref TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tenant_id, superseded_delivery_id)
);

CREATE TABLE IF NOT EXISTS pocket_tombstones (
    tenant_id TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    delivery_id TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, source_ref)
);

CREATE TABLE IF NOT EXISTS pocket_consents (
    tenant_id TEXT NOT NULL,
    consent_ref TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT '',
    revoked INTEGER NOT NULL DEFAULT 0,
    at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, consent_ref)
);
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class PocketStore:
    """Durable event-plane and consent state for the Pocket connector.

    Backed by the Wie SQLite database (own tables; never touches intake
    tables). All guards are tenant-scoped: delivery_ids, idempotency keys,
    sequences, tombstones, and consents never leak across tenants.
    """

    def __init__(self, store: Any) -> None:
        self._db: sqlite3.Connection = store._db
        self._lock = store._lock
        with self._lock:
            self._db.executescript(_POCKET_SCHEMA)

    # -- event plane: replay + dedupe ------------------------------------

    def register_delivery(
        self,
        tenant_id: str,
        delivery_id: str,
        idempotency_key: str | None,
        sequence_number: int,
        source_ref: str,
    ) -> str:
        """Register a webhook delivery; return ``"accepted"``.

        Raises :class:`PocketReplay` when the delivery_id was seen for this
        tenant (PCK-012) and :class:`PocketDuplicate` when the idempotency
        key already materialized (PCK-013, accept-once dedupe).
        """
        with self._lock:
            seen = self._db.execute(
                "SELECT delivery_id FROM pocket_deliveries"
                " WHERE tenant_id = ? AND delivery_id = ?",
                (tenant_id, delivery_id),
            ).fetchone()
            if seen is not None:
                raise PocketReplay(delivery_id)
            original: str | None = None
            if idempotency_key:
                row = self._db.execute(
                    "SELECT delivery_id FROM pocket_deliveries"
                    " WHERE tenant_id = ? AND idempotency_key = ?",
                    (tenant_id, idempotency_key),
                ).fetchone()
                if row is not None:
                    original = str(row["delivery_id"])
                    raise PocketDuplicate(idempotency_key, original)
            self._db.execute(
                "INSERT INTO pocket_deliveries"
                " (tenant_id, delivery_id, idempotency_key,"
                " sequence_number, source_ref, received_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    tenant_id,
                    delivery_id,
                    idempotency_key,
                    int(sequence_number or 0),
                    source_ref or "",
                    _now_iso(),
                ),
            )
        return "accepted"

    def source_ref_for_delivery(
        self, tenant_id: str, delivery_id: str
    ) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT source_ref FROM pocket_deliveries"
                " WHERE tenant_id = ? AND delivery_id = ?",
                (tenant_id, delivery_id),
            ).fetchone()
        return str(row["source_ref"]) if row else None

    def apply_sequence(
        self, tenant_id: str, conversation_id: str, sequence_number: int
    ) -> bool:
        """Advance the applied high-water mark; False when stale (PCK-014)."""
        with self._lock:
            row = self._db.execute(
                "SELECT applied_sequence FROM pocket_sequences"
                " WHERE tenant_id = ? AND conversation_id = ?",
                (tenant_id, conversation_id),
            ).fetchone()
            current = int(row["applied_sequence"]) if row else -1
            if int(sequence_number) <= current:
                return False
            self._db.execute(
                "INSERT INTO pocket_sequences"
                " (tenant_id, conversation_id, applied_sequence)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(tenant_id, conversation_id) DO UPDATE SET"
                " applied_sequence=excluded.applied_sequence",
                (tenant_id, conversation_id, int(sequence_number)),
            )
        return True

    def applied_sequence(self, tenant_id: str, conversation_id: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT applied_sequence FROM pocket_sequences"
                " WHERE tenant_id = ? AND conversation_id = ?",
                (tenant_id, conversation_id),
            ).fetchone()
        return int(row["applied_sequence"]) if row else 0

    # -- edits, tombstones, withdrawal ------------------------------------

    def mark_superseded(
        self,
        tenant_id: str,
        superseded_delivery_id: str,
        superseding_delivery_id: str,
        superseded_source_ref: str,
    ) -> None:
        """Retire an edit's predecessor; lineage preserved, never rewritten."""
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO pocket_supersessions"
                " (tenant_id, superseded_delivery_id,"
                " superseding_delivery_id, superseded_source_ref)"
                " VALUES (?, ?, ?, ?)",
                (
                    tenant_id,
                    superseded_delivery_id,
                    superseding_delivery_id,
                    superseded_source_ref or "",
                ),
            )

    def apply_tombstone(
        self, tenant_id: str, source_ref: str, delivery_id: str, reason: str
    ) -> None:
        """Withdraw content from reads while retaining audit evidence."""
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO pocket_tombstones"
                " (tenant_id, source_ref, delivery_id, reason, at)"
                " VALUES (?, ?, ?, ?, ?)",
                (tenant_id, source_ref, delivery_id, reason, _now_iso()),
            )

    def _superseded_source_refs(self, tenant_id: str) -> set[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT superseded_delivery_id, superseded_source_ref"
                " FROM pocket_supersessions WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchall()
        refs = {str(r["superseded_source_ref"]) for r in rows if r["superseded_source_ref"]}
        with self._lock:
            for r in rows:
                if r["superseded_source_ref"]:
                    continue
                delivery = self.source_ref_for_delivery(
                    tenant_id, str(r["superseded_delivery_id"])
                )
                if delivery:
                    refs.add(delivery)
        return refs

    def is_withdrawn(
        self,
        tenant_id: str,
        source_ref: str,
        consent_ref: str | None = None,
    ) -> bool:
        """True when a source is tombstoned, superseded, or consent-revoked."""
        with self._lock:
            tomb = self._db.execute(
                "SELECT source_ref FROM pocket_tombstones"
                " WHERE tenant_id = ? AND source_ref = ?",
                (tenant_id, source_ref),
            ).fetchone()
        if tomb is not None:
            return True
        if source_ref and source_ref in self._superseded_source_refs(tenant_id):
            return True
        return bool(consent_ref and self.consent_revoked(tenant_id, consent_ref))

    # -- consent / purpose lineage -----------------------------------------

    def attach_consent(self, tenant_id: str, consent_ref: str, purpose: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO pocket_consents"
                " (tenant_id, consent_ref, purpose, revoked, at)"
                " VALUES (?, ?, ?, 0, ?)"
                " ON CONFLICT(tenant_id, consent_ref) DO UPDATE SET"
                " purpose=excluded.purpose"
                " WHERE pocket_consents.revoked = 0",
                (tenant_id, consent_ref, purpose or "", _now_iso()),
            )

    def revoke_consent(self, tenant_id: str, consent_ref: str) -> bool:
        """Revoke consent; returns False when the consent_ref is unknown."""
        with self._lock:
            row = self._db.execute(
                "SELECT consent_ref FROM pocket_consents"
                " WHERE tenant_id = ? AND consent_ref = ?",
                (tenant_id, consent_ref),
            ).fetchone()
            if row is None:
                return False
            self._db.execute(
                "UPDATE pocket_consents SET revoked = 1, at = ?"
                " WHERE tenant_id = ? AND consent_ref = ?",
                (_now_iso(), tenant_id, consent_ref),
            )
        return True

    def consent_revoked(self, tenant_id: str, consent_ref: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT revoked FROM pocket_consents"
                " WHERE tenant_id = ? AND consent_ref = ?",
                (tenant_id, consent_ref),
            ).fetchone()
        return bool(row and row["revoked"])

    def consent_purpose(self, tenant_id: str, consent_ref: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT purpose FROM pocket_consents"
                " WHERE tenant_id = ? AND consent_ref = ?",
                (tenant_id, consent_ref),
            ).fetchone()
        return str(row["purpose"]) if row else None

    # -- reconciliation reads ----------------------------------------------

    def has_pocket_state(self, tenant_id: str) -> bool:
        """Fast path: True when any pocket guard state exists for a tenant."""
        with self._lock:
            for table in (
                "pocket_tombstones",
                "pocket_consents",
                "pocket_supersessions",
            ):
                row = self._db.execute(
                    f"SELECT 1 FROM {table} WHERE tenant_id = ? LIMIT 1",
                    (tenant_id,),
                ).fetchone()
                if row is not None:
                    return True
        return False

    def recompute_projection(self, tenant_id: str) -> dict[str, Any]:
        """Recompute the derived read projection per provenance (PCK-023).

        Counts active vs withdrawn Pocket observations straight from current
        guard state, so revoked derivations are never stale-served as current.
        Historical audit rows are untouched.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT external_id, metadata_json FROM intake_observations"
                " WHERE tenant_id = ? AND source = 'pocket'",
                (tenant_id,),
            ).fetchall()
        active = 0
        withdrawn = 0
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except ValueError:
                metadata = {}
            source_ref = str(
                metadata.get("pocket_source_ref")
                or (row["external_id"] or "").removeprefix("pocket:")
            )
            consent_ref = metadata.get("consent_ref")
            if self.is_withdrawn(tenant_id, source_ref, consent_ref):
                withdrawn += 1
            else:
                active += 1
        return {
            "projection_recomputed": True,
            "stale_served_as_current": False,
            "active_observations": active,
            "withdrawn_observations": withdrawn,
        }


def filter_withdrawn_pocket_rows(
    store: Any, tenant_id: str, rows: list[Any]
) -> list[Any]:
    """Drop tombstoned/superseded/revoked Pocket observations from reads.

    Non-Pocket rows pass through untouched; when no Pocket guard state exists
    for the tenant the input is returned unchanged (zero-cost fast path).
    """
    try:
        pocket = PocketStore(store)
    except Exception:
        return rows
    try:
        if not pocket.has_pocket_state(tenant_id):
            return rows
    except Exception:
        return rows
    kept = []
    for row in rows:
        try:
            source = row["source"]
        except (KeyError, TypeError, IndexError):
            kept.append(row)
            continue
        if source != "pocket":
            kept.append(row)
            continue
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except ValueError:
            metadata = {}
        external_id = row["external_id"] or ""
        source_ref = str(
            metadata.get("pocket_source_ref") or external_id.removeprefix("pocket:")
        )
        try:
            if pocket.is_withdrawn(
                tenant_id, source_ref, metadata.get("consent_ref")
            ):
                continue
        except Exception:
            pass
        kept.append(row)
    return kept


__all__ = [
    "DERIVATION_UNCERTAINTY_DEFAULTS",
    "DERIVATION_WEIGHTS",
    "SCHEMA",
    "PocketAdapter",
    "PocketDuplicate",
    "PocketRejected",
    "PocketReplay",
    "PocketStore",
    "contains_authority_conflation",
    "evaluate_commitment",
    "filter_withdrawn_pocket_rows",
    "mcp_answer",
    "resolve_pocket_secret",
    "scan_injection",
    "sign_pocket_body",
    "validate_pocket_record",
    "verify_pocket_signature",
]
