"""V2 source adapters.

Every adapter takes a source-specific payload (dict) and yields zero or more
canonical ``ObservationInput`` instances. Adapters do NOT create WorkItems —
that's the engine's job. They MUST preserve provenance: tenant_id, source,
external_id (stable per-observation dedupe key), actor, occurred_at, metadata.

To plug a new source:
1. Subclass ``SourceAdapter``.
2. Implement ``source`` (str) and ``observations(payload) -> Iterable[ObservationInput]``.
3. Register it in ``__all__`` and document the payload contract.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, ClassVar

from .models import ObservationInput


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 string into an aware datetime in UTC."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class SourceAdapter(ABC):
    """Base class for source adapters."""

    #: Stable string identifying the source family. Must match what the engine
    #: expects for tenant-allowed sources and what observers will cite.
    source: str = ""

    @abstractmethod
    def observations(self, payload: dict[str, Any]) -> Iterable[ObservationInput]:
        """Yield zero or more ObservationInput instances for the payload."""


class ConversationAdapter(SourceAdapter):
    """Adapter for chat/voice transcripts.

    Payload contract:
    {
      "tenant_id": str,
      "transcript_id": str,
      "actor": str | None,
      "occurred_at": ISO-8601 | None,
      "messages": [{"speaker": str, "text": str, "at": ISO-8601?}, ...]
    }

    Each non-empty message becomes one observation. The external_id is derived
    from the transcript id and message index so a replay of the same transcript
    is idempotent at the observation level.
    """

    source = "conversation"

    def observations(self, payload: dict[str, Any]) -> Iterable[ObservationInput]:
        tenant_id = payload.get("tenant_id")
        if not tenant_id:
            return
        transcript_id = payload.get("transcript_id") or ""
        actor = payload.get("actor")
        default_at = _parse_iso(payload["occurred_at"]) if payload.get("occurred_at") else datetime.now(UTC)
        for idx, msg in enumerate(payload.get("messages") or []):
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            at_str = msg.get("at")
            occurred_at = _parse_iso(at_str) if at_str else default_at
            yield ObservationInput(
                tenant_id=tenant_id,
                source=self.source,
                text=text,
                external_id=f"{transcript_id}:msg:{idx}" if transcript_id else None,
                actor=actor,
                occurred_at=occurred_at,
                metadata={
                    "transcript_id": transcript_id,
                    "speaker": msg.get("speaker"),
                    "message_index": idx,
                },
            )


class EmailAdapter(SourceAdapter):
    """Adapter for inbound email batches.

    Payload contract:
    {
      "tenant_id": str,
      "mailbox": str,
      "messages": [{"message_id": str, "from": str, "subject": str,
                    "body": str, "received_at": ISO-8601}, ...]
    }
    """

    source = "email"

    def observations(self, payload: dict[str, Any]) -> Iterable[ObservationInput]:
        tenant_id = payload.get("tenant_id")
        if not tenant_id:
            return
        mailbox = payload.get("mailbox")
        for msg in payload.get("messages") or []:
            body = (msg.get("body") or msg.get("subject") or "").strip()
            if not body:
                continue
            received_at = msg.get("received_at")
            yield ObservationInput(
                tenant_id=tenant_id,
                source=self.source,
                text=body,
                external_id=msg.get("message_id"),
                actor=msg.get("from"),
                occurred_at=_parse_iso(received_at) if received_at else datetime.now(UTC),
                title_hint=msg.get("subject"),
                metadata={
                    "mailbox": mailbox,
                    "from": msg.get("from"),
                    "subject": msg.get("subject"),
                    "message_id": msg.get("message_id"),
                },
            )


class CalendarAdapter(SourceAdapter):
    """Adapter for calendar events.

    Each event becomes ONE preparation observation, derived from title +
    description. The external_id is the calendar event_id.
    """

    source = "calendar"

    def observations(self, payload: dict[str, Any]) -> Iterable[ObservationInput]:
        tenant_id = payload.get("tenant_id")
        if not tenant_id:
            return
        for ev in payload.get("events") or []:
            title = (ev.get("title") or "").strip()
            description = (ev.get("description") or "").strip()
            starts_at = ev.get("starts_at")
            if not (title or description):
                continue
            event_id = ev.get("event_id")
            attendees = ev.get("attendees") or []
            when = starts_at.split("T")[0] if isinstance(starts_at, str) and "T" in starts_at else starts_at
            text = description or (f"Forbered {title}" if title else "")
            yield ObservationInput(
                tenant_id=tenant_id,
                source=self.source,
                text=text,
                external_id=event_id,
                actor=attendees[0] if attendees else None,
                occurred_at=_parse_iso(starts_at) if starts_at else datetime.now(UTC),
                title_hint=f"Forbered {title} ({when})" if title and when else None,
                priority_hint="medium" if attendees else "low",
                metadata={
                    "event_id": event_id,
                    "title": title,
                    "attendees": attendees,
                    "location": ev.get("location"),
                    "starts_at": starts_at,
                },
            )


class CodeAdapter(SourceAdapter):
    """Adapter for git commit batches.

    Payload contract:
    {
      "tenant_id": str,
      "commits": [{"sha": str, "message": str, "author": str,
                   "committed_at": ISO-8601, "repo": str}, ...]
    }

    Each line that begins with ``TODO`` or ``FIXME`` becomes one observation.
    The external_id combines repo+sha+index so a re-ingest of the same commit
    is idempotent.
    """

    source = "code"

    _TODO_RE = ("TODO", "FIXME")

    def observations(self, payload: dict[str, Any]) -> Iterable[ObservationInput]:
        tenant_id = payload.get("tenant_id")
        if not tenant_id:
            return
        todo_idx = 0
        for commit in payload.get("commits") or []:
            sha = commit.get("sha") or ""
            message = commit.get("message") or ""
            repo = commit.get("repo") or ""
            committed_at = commit.get("committed_at")
            occurred_at = _parse_iso(committed_at) if committed_at else datetime.now(UTC)
            todo_idx = 0
            for idx, line in enumerate(message.splitlines()):
                line = line.strip()
                if not line:
                    continue
                upper = line.upper()
                if not any(upper.startswith((f"{tag}:", f"{tag} ")) for tag in self._TODO_RE):
                    continue
                yield ObservationInput(
                    tenant_id=tenant_id,
                    source=self.source,
                    text=line,
                    external_id=f"{repo}:{sha}:TODO:{todo_idx}" if (repo and sha) else None,
                    actor=commit.get("author"),
                    occurred_at=occurred_at,
                    priority_hint="medium",
                    metadata={
                        "repo": repo,
                        "sha": sha,
                        "line_index": idx,
                        "author": commit.get("author"),
                    },
                )
                todo_idx += 1


class RenosAdapter(SourceAdapter):
    """Adapter for RenOS job-lifecycle signals.

    Payload contract:
    {
      "tenant_id": str,
      "company_id": str,
      "as_of": ISO-8601,
      "jobs": [{"job_id": str, "title": str, "status": str,
                "scheduled_end": ISO-8601, "customer_id": str,
                "completed_at": ISO-8601?}, ...]
    }

    Only emits observations for actionable signals:
      - jobs overdue past scheduled_end,
      - jobs completed without a followup.
    """

    source = "renos"

    _DONE_STATUSES: ClassVar[frozenset[str]] = frozenset({"completed", "done", "cancelled"})

    def observations(self, payload: dict[str, Any]) -> Iterable[ObservationInput]:
        tenant_id = payload.get("tenant_id")
        if not tenant_id:
            return
        company_id = payload.get("company_id")
        as_of_str = payload.get("as_of")
        as_of = _parse_iso(as_of_str) if as_of_str else datetime.now(UTC)
        for job in payload.get("jobs") or []:
            status = (job.get("status") or "").lower()
            scheduled_end = job.get("scheduled_end")
            job_id = job.get("job_id")
            title = job.get("title") or ""
            customer_id = job.get("customer_id")
            if not scheduled_end or not job_id:
                continue
            scheduled_end_dt = _parse_iso(scheduled_end)
            if status not in self._DONE_STATUSES and scheduled_end_dt < as_of:
                # Overdue
                priority = "high" if (as_of - scheduled_end_dt).total_seconds() >= 0 else "medium"
                yield ObservationInput(
                    tenant_id=tenant_id,
                    source=self.source,
                    text=f"Job '{title}' er forsinket (sidste planlagte slut: {scheduled_end})",
                    external_id=f"renos:{job_id}:overdue:{as_of.strftime('%Y-%m-%dT%H:%M:%SZ')}",
                    actor=f"company:{company_id}" if company_id else None,
                    occurred_at=as_of,
                    priority_hint=priority,
                    title_hint=f"Følg op på forsinket job: {title}" if title else None,
                    metadata={
                        "job_id": job_id,
                        "customer_id": customer_id,
                        "scheduled_end": scheduled_end,
                        "status": status,
                        "company_id": company_id,
                    },
                )


__all__ = [
    "CalendarAdapter",
    "CodeAdapter",
    "ConversationAdapter",
    "EmailAdapter",
    "RenosAdapter",
    "SourceAdapter",
]