"""Proposal-only feedback plane for Aftergraph Work Intelligence.

Feedback is an observation source. It can be normalized and clustered into
proposals, but this module never promotes feedback into executable WORKS work.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Literal

FeedbackKind = Literal["request", "bug", "idea", "question", "docs", "release-feedback"]


@dataclass(frozen=True)
class FeedbackEvent:
    schema: str
    event_id: str
    observed_at: str
    source: str
    kind: FeedbackKind
    summary: str
    source_ref: str
    details: str = ""
    labels: tuple[str, ...] = ()
    tenant_id: str | None = None
    content_digest: str | None = None
    execution_authority: str = "none"
    promotion_required: bool = True

    def to_contract(self) -> dict:
        return {
            "schema": self.schema,
            "event_id": self.event_id,
            "observed_at": self.observed_at,
            "tenant_id": self.tenant_id,
            "source": self.source,
            "kind": self.kind,
            "body": {
                "summary": self.summary,
                "details": self.details,
                "labels": list(self.labels),
            },
            "authority": {
                "execution_authority": self.execution_authority,
                "promotion_required": self.promotion_required,
            },
            "provenance": {
                "source_ref": self.source_ref,
                "content_digest": self.content_digest,
            },
        }


@dataclass(frozen=True)
class FeedbackProposal:
    fingerprint: str
    kind: FeedbackKind
    summary: str
    event_refs: tuple[str, ...]
    labels: tuple[str, ...]
    execution_authority: str = "none"
    promotion_required: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def _fingerprint(kind: str, summary: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", summary.lower()).strip()
    payload = json.dumps(
        {"kind": kind, "summary": " ".join(normalized.split()[:24])},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def create_feedback_event(
    *,
    event_id: str,
    observed_at: str,
    source: str,
    kind: FeedbackKind,
    summary: str,
    source_ref: str,
    details: str = "",
    labels: tuple[str, ...] = (),
    tenant_id: str | None = None,
    content_digest: str | None = None,
) -> FeedbackEvent:
    for name, value in {
        "event_id": event_id,
        "observed_at": observed_at,
        "source": source,
        "summary": summary,
        "source_ref": source_ref,
    }.items():
        if not str(value).strip():
            raise ValueError(f"{name} is required")
    if kind not in {"request", "bug", "idea", "question", "docs", "release-feedback"}:
        raise ValueError("invalid feedback kind")

    return FeedbackEvent(
        schema="aftergraph.feedback-event/v1",
        event_id=event_id,
        observed_at=observed_at,
        tenant_id=tenant_id,
        source=source,
        kind=kind,
        summary=summary,
        details=details,
        labels=tuple(sorted(set(labels))),
        source_ref=source_ref,
        content_digest=content_digest,
    )


def cluster_feedback(events: list[FeedbackEvent]) -> list[FeedbackProposal]:
    groups: dict[str, list[FeedbackEvent]] = {}
    for event in events:
        key = _fingerprint(event.kind, event.summary)
        groups.setdefault(key, []).append(event)

    proposals: list[FeedbackProposal] = []
    for fingerprint in sorted(groups):
        group = groups[fingerprint]
        first = group[0]
        proposals.append(
            FeedbackProposal(
                fingerprint=fingerprint,
                kind=first.kind,
                summary=first.summary,
                event_refs=tuple(sorted(event.event_id for event in group)),
                labels=tuple(sorted({label for event in group for label in event.labels})),
            )
        )
    return proposals
