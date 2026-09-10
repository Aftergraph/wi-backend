"""Wie proactivity sensing (contract ``proactivity/0.1``).

Canonical owner: ``Aftergraph/wi-backend`` (Wie sensing path).
Seam binding: ``docs/PROACTIVITY-ORG-V1.md`` in after-graph-governance.

A native Wie signal projects to a sensing candidate (opportunity,
attention, commitment) that preserves native meaning — the ``native_ref``
is carried through verbatim — while claiming nothing: no execution, no
admission. Candidates never self-admit: a ``claims_admission`` record
without Trust Gateway admission (``admitted_by_tg`` exactly ``True``)
fails closed. Sensing never executes: ``claims_execution`` fails closed
on every path (the Cron path retains zero execution authority).

Same-signal correlation: a native signal seen via Wie and Cron dedupes to
a single attention candidate. The dedupe key is deliberately
path-independent — :func:`sensing_dedupe_key` uses only the tenant scope
and the verbatim native reference — so ``aftergraph-cron-fabric``
(PRO-002/PRO-006) computes the identical key and both owners agree on
which sightings are the same signal.

This module never executes, admits, dispatches, or persists. Candidates
are ephemeral proposals; the registry below is process-local on purpose.
Durable state stays in observations/work-items owned by the intake.
"""
from __future__ import annotations

import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

SCHEMA = "proactivity/0.1"

SENSING_PATHS: tuple[str, ...] = ("wie", "runtime", "cron")

CANDIDATE_KINDS: tuple[str, ...] = (
    "opportunity",
    "attention_candidate",
    "commitment_candidate",
    "observation_update",
    "finding",
)

# Seam binding: Wie external signals become possible work or commitment
# candidates. Findings belong to the Cron sensing path, observation updates
# enter through intake — Wie never emits them.
WIE_CANDIDATE_KINDS: tuple[str, ...] = (
    "opportunity",
    "attention_candidate",
    "commitment_candidate",
)

SENSING_ID_RE = re.compile(r"^sen_[a-f0-9]{32}$")
TENANT_ID_RE = re.compile(r"^ten_[a-f0-9]{32}$")

_FUTURE_LEEWAY = timedelta(minutes=5)
_DEFAULT_STALE_AFTER = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class SensingDecision:
    accepted: bool
    reason: str
    stale: bool = False


@dataclass(frozen=True, slots=True)
class SensingCandidate:
    sensing_id: str
    path: str
    native_ref: str
    candidate_kind: str
    claims_execution: bool
    claims_admission: bool
    admitted_by_tg: bool
    correlated_paths: tuple[str, ...] = field(default_factory=tuple)
    asserted_at: str = ""
    tenant_id: str = ""
    dedupe_key: str = ""


class SensingRejected(ValueError):
    """A sensing record failed closed and was never registered."""


def sensing_dedupe_key(tenant_id: str, native_ref: str) -> str:
    """Return the path-independent same-signal dedupe key.

    Compatibility contract with aftergraph-cron-fabric: the key is
    ``"<tenant_id>\\x00<native_ref>"`` after stripping surrounding
    whitespace, with no path component. Both owners must compute it
    identically — a Wie sighting and a Cron sighting of the same native
    signal share one key, while distinct signals (or distinct tenants)
    never share one. Case is preserved: references are opaque identifiers
    and casefolding could collide distinct signals.
    """
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant_id is required")
    if not isinstance(native_ref, str) or not native_ref.strip():
        raise ValueError("native_ref is required")
    return f"{tenant_id.strip()}\x00{native_ref.strip()}"


def _parse_asserted_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    text = value.strip().replace("Z", "+00:00") if value.strip().endswith("Z") else value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def is_stale(asserted_at: str, now: datetime, *, max_age: timedelta = _DEFAULT_STALE_AFTER) -> bool:
    """Return True when a sensing assertion is older than ``max_age``.

    Unparseable timestamps count as stale (fail-closed advisory): a signal
    whose age cannot be established must never be treated as fresh.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    parsed = _parse_asserted_at(asserted_at)
    if parsed is None:
        return True
    return now - parsed > max_age


def _utcnow(now: datetime | None) -> datetime:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current


def evaluate_sensing(
    record: Mapping[str, Any],
    *,
    now: datetime | None = None,
    max_age: timedelta | None = None,
) -> SensingDecision:
    """Evaluate one ``proactivity/0.1`` sensing record, fail-closed.

    ``max_age`` is None by default so pinned acceptance vectors replay
    identically at any wall-clock time; pass an explicit bound to fail
    closed on stale signals as well. A future-dated ``asserted_at``
    (beyond clock-skew leeway) always rejects: sensing cannot assert
    from the future.
    """
    current = _utcnow(now)
    if not isinstance(record, Mapping):
        return SensingDecision(False, "record must be a proactivity/0.1 mapping")
    if record.get("schema") != SCHEMA:
        return SensingDecision(False, f"unknown schema {record.get('schema')!r}; expected {SCHEMA!r}")

    sensing_id = record.get("sensing_id")
    if not isinstance(sensing_id, str) or not SENSING_ID_RE.fullmatch(sensing_id):
        return SensingDecision(False, "sensing_id must match sen_<32 hex>")
    tenant_id = record.get("tenant_id")
    if not isinstance(tenant_id, str) or not TENANT_ID_RE.fullmatch(tenant_id):
        return SensingDecision(False, "tenant_id is scope-bound: must match ten_<32 hex>")
    path = record.get("path")
    if path not in SENSING_PATHS:
        return SensingDecision(False, f"path must be one of {list(SENSING_PATHS)}")
    kind = record.get("candidate_kind")
    if kind not in CANDIDATE_KINDS:
        return SensingDecision(False, f"candidate_kind must be one of {list(CANDIDATE_KINDS)}")
    for flag in ("claims_execution", "claims_admission", "admitted_by_tg"):
        if type(record.get(flag)) is not bool:
            return SensingDecision(False, f"{flag} must be a boolean; ambiguous authority fails closed")
    native_ref = record.get("native_ref")
    if not isinstance(native_ref, str) or not native_ref.strip() or len(native_ref) > 512:
        return SensingDecision(False, "native_ref must be a non-empty reference preserving native meaning")
    correlated = record.get("correlated_paths")
    if (
        not isinstance(correlated, (list, tuple))
        or not 1 <= len(correlated) <= 3
        or any(item not in SENSING_PATHS for item in correlated)
    ):
        return SensingDecision(False, "correlated_paths must list 1-3 known sensing paths")
    if path not in correlated:
        return SensingDecision(False, "asserting path must be among correlated_paths")
    asserted_at = record.get("asserted_at")
    parsed_at = _parse_asserted_at(asserted_at)
    if parsed_at is None:
        return SensingDecision(False, "asserted_at must be a parseable timestamp")
    stale = current - parsed_at > _DEFAULT_STALE_AFTER
    if parsed_at > current + _FUTURE_LEEWAY:
        return SensingDecision(False, "asserted_at is in the future; incoherent sensing fails closed", stale=stale)

    if record.get("claims_execution") is True:
        return SensingDecision(False, "sensing never executes; execution claims fail closed", stale=stale)
    if record.get("claims_admission") is True and record.get("admitted_by_tg") is not True:
        return SensingDecision(
            False,
            "candidates never self-admit: admission claims require Trust Gateway admission",
            stale=stale,
        )
    if max_age is not None and current - parsed_at > max_age:
        return SensingDecision(False, "stale signal fails closed under the caller staleness bound", stale=True)
    return SensingDecision(
        True,
        f"{path} signal projected to {kind}; native meaning preserved; nothing claimed",
        stale=stale,
    )


def project_wie_signal(
    *,
    tenant_id: str,
    native_ref: str,
    candidate_kind: str = "opportunity",
    sensing_id: str | None = None,
    asserted_at: str | None = None,
) -> dict[str, Any]:
    """Project a native Wie signal to a claim-free sensing candidate record.

    The native reference is preserved verbatim (surrounding whitespace is
    transport noise, never meaning). The projection always claims neither
    execution nor admission: only the Trust Gateway path can admit, and
    only Runtime decides.
    """
    if candidate_kind not in WIE_CANDIDATE_KINDS:
        raise ValueError(f"Wie signals project to {list(WIE_CANDIDATE_KINDS)}; got {candidate_kind!r}")
    if not isinstance(native_ref, str) or not native_ref.strip() or len(native_ref) > 512:
        raise ValueError("native_ref must be a non-empty reference preserving native meaning")
    if not isinstance(tenant_id, str) or not TENANT_ID_RE.fullmatch(tenant_id):
        raise ValueError("tenant_id is scope-bound: must match ten_<32 hex>")
    resolved_id = sensing_id if sensing_id is not None else f"sen_{secrets.token_hex(16)}"
    if not SENSING_ID_RE.fullmatch(resolved_id):
        raise ValueError("sensing_id must match sen_<32 hex>")
    stamped = asserted_at if asserted_at is not None else datetime.now(UTC).isoformat().replace("+00:00", "Z")
    if _parse_asserted_at(stamped) is None:
        raise ValueError("asserted_at must be a parseable timestamp")
    return {
        "schema": SCHEMA,
        "sensing_id": resolved_id,
        "path": "wie",
        "native_ref": native_ref.strip(),
        "candidate_kind": candidate_kind,
        "claims_execution": False,
        "claims_admission": False,
        "admitted_by_tg": False,
        "correlated_paths": ["wie"],
        "asserted_at": stamped,
        "tenant_id": tenant_id,
    }


class SensingRegistry:
    """Process-local same-signal correlation of sensing candidates.

    Ephemeral by design: sensing proposes, it does not store authority.
    Rejected records raise :class:`SensingRejected` and are never stored.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, SensingCandidate] = {}

    def __len__(self) -> int:
        return len(self._by_key)

    def get(self, tenant_id: str, native_ref: str) -> SensingCandidate | None:
        try:
            return self._by_key.get(sensing_dedupe_key(tenant_id, native_ref))
        except ValueError:
            return None

    def register(
        self,
        record: Mapping[str, Any],
        *,
        now: datetime | None = None,
        max_age: timedelta | None = None,
    ) -> tuple[SensingCandidate, bool]:
        """Evaluate and correlate one record.

        Returns ``(candidate, deduped)``. A repeat sighting of the same
        native signal merges correlation paths into the single stored
        candidate; when sightings span more than one path the survivor is
        an attention candidate. Rejections raise and store nothing.
        """
        decision = evaluate_sensing(record, now=now, max_age=max_age)
        if not decision.accepted:
            raise SensingRejected(decision.reason)
        key = sensing_dedupe_key(str(record["tenant_id"]), str(record["native_ref"]))
        candidate = SensingCandidate(
            sensing_id=str(record["sensing_id"]),
            path=str(record["path"]),
            native_ref=str(record["native_ref"]),
            candidate_kind=str(record["candidate_kind"]),
            claims_execution=bool(record["claims_execution"]),
            claims_admission=bool(record["claims_admission"]),
            admitted_by_tg=bool(record["admitted_by_tg"]),
            correlated_paths=tuple(str(item) for item in record["correlated_paths"]),  # type: ignore[union-attr]
            asserted_at=str(record["asserted_at"]),
            tenant_id=str(record["tenant_id"]),
            dedupe_key=key,
        )
        existing = self._by_key.get(key)
        if existing is None:
            self._by_key[key] = candidate
            return candidate, False
        merged_paths = existing.correlated_paths + tuple(
            item for item in candidate.correlated_paths if item not in existing.correlated_paths
        )
        kind = existing.candidate_kind
        if len(set(merged_paths)) > 1:
            kind = "attention_candidate"
        merged = replace(existing, correlated_paths=merged_paths, candidate_kind=kind)
        self._by_key[key] = merged
        return merged, True


__all__ = [
    "CANDIDATE_KINDS",
    "SCHEMA",
    "SENSING_PATHS",
    "WIE_CANDIDATE_KINDS",
    "SensingCandidate",
    "SensingDecision",
    "SensingRegistry",
    "SensingRejected",
    "evaluate_sensing",
    "is_stale",
    "project_wie_signal",
    "sensing_dedupe_key",
]
