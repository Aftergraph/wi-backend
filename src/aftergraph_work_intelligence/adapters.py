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


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 string into an aware datetime in UTC, or None if absent."""
    if not value:
        return None
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


class GitHubAdapter(SourceAdapter):
    """Adapter for GitHub webhook payloads (push, pull_request, issues,
    check_run, workflow_run, issue_comment).

    Payload contract: a raw GitHub webhook event dict with:
    {
      "tenant_id": str,
      "repository": {"full_name": str, "name": str},
      "ref": str,                # push only
      "head_commit": {...},      # push only
      "commits": [...],          # push only
      "action": str,             # non-push events
      "pull_request": {...},     # pull_request only
      "issue": {...},            # issues / issue_comment
      "check_run": {...},        # check_run only
      "workflow_run": {...},     # workflow_run only
      "comment": {...},          # issue_comment only
      "pusher": {"name": str},   # push only
    }

    Emits observations only for ACTIONABLE signals:
      - push commits (each commit = 1 observation), non-bot authors
      - pull_request opened / review_requested / closed(merged)
      - issues opened / closed
      - check_run / workflow_run completed with conclusion=failure
      - issue_comment by non-bot users
    Success states (check_run success, workflow success, bot comments,
    non-merged PR close) emit nothing.
    """

    source = "github"

    _BOT_SUFFIXES = ("[bot]", "-bot", "dependabot", "renovate")

    @staticmethod
    def _is_bot(login: str | None) -> bool:
        if not login:
            return True
        lowered = login.lower()
        return any(suffix in lowered for suffix in GitHubAdapter._BOT_SUFFIXES)

    @staticmethod
    def _repo_name(payload: dict[str, Any]) -> str:
        repo = payload.get("repository") or {}
        return repo.get("full_name") or repo.get("name") or "unknown"

    def _base(self, payload: dict[str, Any], event: str, actor: str | None, occurred_at: str | None) -> ObservationInput | None:
        tenant_id = payload.get("tenant_id")
        if not tenant_id:
            return None
        return ObservationInput(
            tenant_id=tenant_id,
            source=self.source,
            text="",
            actor=actor,
            occurred_at=_parse_iso(occurred_at) if occurred_at else None,
            metadata={"event": event, "repo": self._repo_name(payload)},
        )

    def observations(self, payload: dict[str, Any]) -> Iterable[ObservationInput]:
        tenant_id = payload.get("tenant_id")
        if not tenant_id:
            return
        event = self._detect_event(payload)
        if event == "push":
            yield from self._on_push(payload)
        elif event == "pull_request":
            yield from self._on_pull_request(payload)
        elif event == "issues":
            yield from self._on_issues(payload)
        elif event == "check_run":
            yield from self._on_check_run(payload)
        elif event == "workflow_run":
            yield from self._on_workflow_run(payload)
        elif event == "issue_comment":
            yield from self._on_issue_comment(payload)
        # Unknown / unactionable events produce nothing.

    @staticmethod
    def _detect_event(payload: dict[str, Any]) -> str | None:
        if "head_commit" in payload or "commits" in payload:
            return "push"
        if "pull_request" in payload:
            return "pull_request"
        if "check_run" in payload:
            return "check_run"
        if "workflow_run" in payload:
            return "workflow_run"
        if "issue" in payload and "comment" in payload:
            return "issue_comment"
        if "issue" in payload:
            return "issues"
        return None

    # ---- push ----

    def _on_push(self, payload: dict[str, Any]) -> Iterable[ObservationInput]:
        tenant_id = payload["tenant_id"]
        repo_full = self._repo_name(payload)
        repo_short = repo_full.split("/")[-1]
        ref = payload.get("ref") or ""
        branch = ref.removeprefix("refs/heads/") or "unknown"
        pusher = (payload.get("pusher") or {}).get("name")
        if self._is_bot(pusher):
            return
        commits = payload.get("commits")
        if not commits:
            head = payload.get("head_commit") or {}
            if head.get("id"):
                commits = [head]
        for commit in commits or []:
            sha = commit.get("id") or commit.get("sha") or ""
            message = commit.get("message") or ""
            author = (commit.get("author") or {}).get("username") or (commit.get("author") or {}).get("name")
            if self._is_bot(author):
                continue
            occurred = commit.get("timestamp")
            first_line = message.splitlines()[0] if message else ""
            yield ObservationInput(
                tenant_id=tenant_id,
                source=self.source,
                text=first_line or message or f"Push to {repo_short}",
                external_id=f"{repo_full}:{sha}",
                actor=author or pusher,
                occurred_at=_parse_iso(occurred) if occurred else None,
                title_hint=f"Push to {repo_short}: {first_line}" if first_line else f"Push to {repo_short}",
                metadata={
                    "event": "push",
                    "repo": repo_full,
                    "branch": branch,
                    "sha": sha,
                    "commit": message,
                    "pusher": pusher,
                },
            )

    # ---- pull_request ----

    def _on_pull_request(self, payload: dict[str, Any]) -> Iterable[ObservationInput]:
        tenant_id = payload["tenant_id"]
        repo_full = self._repo_name(payload)
        pr = payload.get("pull_request") or {}
        action = payload.get("action") or "unknown"
        number = pr.get("number")
        title = pr.get("title") or ""
        user = (pr.get("user") or {}).get("login") or (payload.get("sender") or {}).get("login")
        if self._is_bot(user):
            return
        # review_requested
        if action == "review_requested":
            reviewers = [r.get("login") for r in (payload.get("requested_reviewers") or []) if r.get("login")]
            if not reviewers:
                return
            yield ObservationInput(
                tenant_id=tenant_id,
                source=self.source,
                text=f"PR #{number} '{title}' awaits review from {', '.join(reviewers)}",
                external_id=f"{repo_full}:pr:{number}:review_requested",
                actor=user,
                occurred_at=_parse_iso(pr.get("created_at")) if pr.get("created_at") else None,
                title_hint=f"PR #{number} review requested: {title}",
                priority_hint="high",
                metadata={
                    "event": "pull_request",
                    "action": action,
                    "repo": repo_full,
                    "pr_number": number,
                    "title": title,
                    "requested_reviewers": reviewers,
                    "url": pr.get("html_url"),
                },
            )
            return
        # merged
        if action == "closed" and pr.get("merged"):
            yield ObservationInput(
                tenant_id=tenant_id,
                source=self.source,
                text=f"PR #{number} merged: {title}",
                external_id=f"{repo_full}:pr:{number}:merged",
                actor=user,
                occurred_at=_parse_iso(pr.get("merged_at")) if pr.get("merged_at") else None,
                title_hint=f"PR #{number} merged: {title}",
                metadata={
                    "event": "pull_request",
                    "action": "closed",
                    "repo": repo_full,
                    "pr_number": number,
                    "title": title,
                    "merged": True,
                    "url": pr.get("html_url"),
                },
            )
            return
        # opened / reopened
        if action in ("opened", "reopened"):
            yield ObservationInput(
                tenant_id=tenant_id,
                source=self.source,
                text=f"PR #{number} opened: {title}",
                external_id=f"{repo_full}:pr:{number}:{action}",
                actor=user,
                occurred_at=_parse_iso(pr.get("created_at")) if pr.get("created_at") else None,
                title_hint=f"PR #{number} opened: {title}",
                priority_hint="medium",
                metadata={
                    "event": "pull_request",
                    "action": action,
                    "repo": repo_full,
                    "pr_number": number,
                    "title": title,
                    "url": pr.get("html_url"),
                },
            )

    # ---- issues ----

    def _on_issues(self, payload: dict[str, Any]) -> Iterable[ObservationInput]:
        tenant_id = payload["tenant_id"]
        repo_full = self._repo_name(payload)
        issue = payload.get("issue") or {}
        action = payload.get("action") or "unknown"
        number = issue.get("number")
        title = issue.get("title") or ""
        user = (issue.get("user") or {}).get("login")
        if self._is_bot(user):
            return
        if action not in ("opened", "closed", "reopened"):
            return
        labels = [label.get("name") for label in (issue.get("labels") or []) if label.get("name")]
        priority = "high" if "bug" in labels else "medium"
        yield ObservationInput(
            tenant_id=tenant_id,
            source=self.source,
            text=f"Issue #{number} {action}: {title}" + (f" ({', '.join(labels)})" if labels else ""),
            external_id=f"{repo_full}:issue:{number}:{action}",
            actor=user,
            occurred_at=_parse_iso(issue.get("created_at")) if issue.get("created_at") else None,
            priority_hint=priority,
            title_hint=f"Issue #{number} {action}: {title}",
            metadata={
                "event": "issues",
                "action": action,
                "repo": repo_full,
                "issue_number": number,
                "title": title,
                "labels": labels,
                "state": issue.get("state"),
                "url": issue.get("html_url"),
            },
        )

    # ---- check_run / workflow_run ----

    def _on_check_run(self, payload: dict[str, Any]) -> Iterable[ObservationInput]:
        tenant_id = payload["tenant_id"]
        repo_full = self._repo_name(payload)
        check = payload.get("check_run") or {}
        conclusion = (check.get("conclusion") or "").lower()
        if conclusion not in ("failure", "timed_out", "cancelled"):
            return
        name = check.get("name") or "check"
        sha = (check.get("head_sha") or "")[:7]
        yield ObservationInput(
            tenant_id=tenant_id,
            source=self.source,
            text=f"Check '{name}' failed on {sha}",
            external_id=f"{repo_full}:check:{check.get('id')}:{conclusion}",
            actor=None,
            occurred_at=_parse_iso(check.get("completed_at")) if check.get("completed_at") else None,
            priority_hint="high",
            title_hint=f"CI check failed: {name} on {sha}",
            metadata={
                "event": "check_run",
                "repo": repo_full,
                "conclusion": conclusion,
                "check_id": check.get("id"),
                "name": name,
                "sha": sha,
                "url": check.get("html_url"),
            },
        )

    def _on_workflow_run(self, payload: dict[str, Any]) -> Iterable[ObservationInput]:
        tenant_id = payload["tenant_id"]
        repo_full = self._repo_name(payload)
        run = payload.get("workflow_run") or {}
        conclusion = (run.get("conclusion") or "").lower()
        if conclusion not in ("failure", "timed_out", "cancelled"):
            return
        name = run.get("name") or "workflow"
        sha = (run.get("head_sha") or "")[:7]
        yield ObservationInput(
            tenant_id=tenant_id,
            source=self.source,
            text=f"Workflow '{name}' failed on {sha}",
            external_id=f"{repo_full}:workflow:{run.get('id')}:{conclusion}",
            actor=None,
            occurred_at=_parse_iso(run.get("updated_at")) if run.get("updated_at") else None,
            priority_hint="high",
            title_hint=f"CI workflow failed: {name} on {sha}",
            metadata={
                "event": "workflow_run",
                "repo": repo_full,
                "conclusion": conclusion,
                "workflow_id": run.get("id"),
                "name": name,
                "sha": sha,
                "branch": run.get("head_branch"),
                "url": run.get("html_url"),
            },
        )

    # ---- issue_comment ----

    def _on_issue_comment(self, payload: dict[str, Any]) -> Iterable[ObservationInput]:
        tenant_id = payload["tenant_id"]
        repo_full = self._repo_name(payload)
        comment = payload.get("comment") or {}
        issue = payload.get("issue") or {}
        user = (comment.get("user") or {}).get("login")
        if self._is_bot(user):
            return
        body = (comment.get("body") or "").strip()
        if not body:
            return
        yield ObservationInput(
            tenant_id=tenant_id,
            source=self.source,
            text=body[:500],
            external_id=f"{repo_full}:comment:{comment.get('id')}",
            actor=user,
            occurred_at=_parse_iso(comment.get("created_at")) if comment.get("created_at") else None,
            title_hint=f"Comment on issue #{issue.get('number')}: {body[:80]}",
            metadata={
                "event": "issue_comment",
                "repo": repo_full,
                "issue_number": issue.get("number"),
                "comment_id": comment.get("id"),
                "url": comment.get("html_url"),
            },
        )


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
        as_of = _parse_iso(as_of_str) or datetime.now(UTC)
        for job in payload.get("jobs") or []:
            status = (job.get("status") or "").lower()
            scheduled_end = job.get("scheduled_end")
            job_id = job.get("job_id")
            title = job.get("title") or ""
            customer_id = job.get("customer_id")
            if not scheduled_end or not job_id:
                continue
            scheduled_end_dt = _parse_iso(scheduled_end)
            if scheduled_end_dt is None:
                raise ValueError(f"unparseable scheduled_end: {scheduled_end!r}")
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
    "GitHubAdapter",
    "RenosAdapter",
    "SourceAdapter",
]