from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class ObservationInput:
    tenant_id: str
    source: str
    text: str
    external_id: str | None = None
    actor: str | None = None
    occurred_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    title_hint: str | None = None
    owner_hint: str | None = None
    due_hint: str | None = None
    priority_hint: str | None = None


@dataclass(slots=True)
class Observation:
    id: str
    tenant_id: str
    source: str
    text: str
    external_id: str | None
    actor: str | None
    occurred_at: datetime
    created_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WorkCandidate:
    title: str
    summary: str
    next_action: str
    priority: str
    confidence: float
    canonical_key: str
    canonical_tokens: tuple[str, ...]
    owner: str | None = None
    due_hint: str | None = None
    reason: str = ""


@dataclass(slots=True)
class WorkItem:
    id: str
    tenant_id: str
    title: str
    summary: str
    status: str
    priority: str
    next_action: str
    confidence: float
    canonical_key: str
    canonical_tokens: tuple[str, ...]
    observation_count: int
    created_at: datetime
    updated_at: datetime
    owner: str | None = None
    due_hint: str | None = None


@dataclass(slots=True)
class Publication:
    id: str
    work_item_id: str
    destination: str
    external_id: str | None
    response: dict[str, Any]
    published_at: datetime


@dataclass(slots=True)
class WorkItemDetail:
    work_item: WorkItem
    observations: list[Observation]
    publications: list[Publication]


@dataclass(slots=True)
class IngestResult:
    action: str
    observation: Observation
    work_item: WorkItem | None
