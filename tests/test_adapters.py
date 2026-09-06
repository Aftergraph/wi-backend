"""TDD tests for V2 source adapters.

Each adapter must turn a raw source signal into a canonical ObservationInput,
preserve provenance (source, external_id, actor, occurred_at, metadata),
and never produce a WorkItem directly (that's the engine's job).

These tests run WITHOUT a real RenOS / Gmail / Calendar — adapters are pure
transformers of payloads into ObservationInput. End-to-end adapters that
*fetch* are tested in tests/test_e2e_adapters.py against an in-process fake
service exposing the relevant endpoints.
"""
from __future__ import annotations

from datetime import UTC, datetime

from aftergraph_work_intelligence.adapters import (
    CalendarAdapter,
    CodeAdapter,
    ConversationAdapter,
    EmailAdapter,
    RenosAdapter,
)

# ---------- Conversation ----------

def test_conversation_adapter_keeps_canonical_provenance():
    payload = {
        "transcript_id": "transcript-2026-09-05-001",
        "tenant_id": "renos",
        "actor": "user:empir",
        "occurred_at": "2026-09-05T09:00:00Z",
        "messages": [
            {"speaker": "user", "text": "Hvornår skal vi have rengøring af kontoret?"},
            {"speaker": "assistant", "text": "Jeg foreslår fredag eftermiddag."},
            {"speaker": "user", "text": "Ok vi skal booke rengøring inden fredag"},
        ],
    }
    inputs = list(ConversationAdapter().observations(payload))
    assert len(inputs) == 3
    # All three become observations; the actionable one is "skal booke..."
    obs_actionable = [o for o in inputs if o.text.startswith("Ok vi skal booke")]
    assert len(obs_actionable) == 1
    o = obs_actionable[0]
    assert o.tenant_id == "renos"
    assert o.source == "conversation"
    assert o.actor == "user:empir"
    assert o.external_id == "transcript-2026-09-05-001:msg:2"
    assert o.occurred_at == datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
    assert o.metadata["transcript_id"] == "transcript-2026-09-05-001"
    assert o.metadata["speaker"] == "user"


def test_conversation_adapter_skips_empty_messages():
    payload = {
        "transcript_id": "t1",
        "tenant_id": "renos",
        "messages": [{"speaker": "user", "text": "  "}, {"speaker": "user", "text": "Vi skal ringe"}],
    }
    inputs = list(ConversationAdapter().observations(payload))
    assert len(inputs) == 1
    assert inputs[0].text == "Vi skal ringe"


# ---------- Email ----------

def test_email_adapter_emits_one_observation_per_message():
    payload = {
        "tenant_id": "renos",
        "mailbox": "ops@abde.dk",
        "messages": [
            {
                "message_id": "<msg-1@mail>",
                "from": "kunde@example.com",
                "subject": "Bekræftelse",
                "body": "Kan I bekræfte rengøringen inden fredag?",
                "received_at": "2026-09-05T10:00:00Z",
            },
            {
                "message_id": "<msg-2@mail>",
                "from": "kunde@example.com",
                "subject": "Opfølgning",
                "body": "Husk at svare på tilbuddet",
                "received_at": "2026-09-05T11:00:00Z",
            },
        ],
    }
    inputs = list(EmailAdapter().observations(payload))
    assert len(inputs) == 2
    for o in inputs:
        assert o.source == "email"
        assert o.tenant_id == "renos"
        assert o.metadata["mailbox"] == "ops@abde.dk"
        assert o.metadata["from"] == "kunde@example.com"
    assert inputs[0].external_id == "<msg-1@mail>"
    assert inputs[1].external_id == "<msg-2@mail>"


# ---------- Calendar ----------

def test_calendar_adapter_emits_preparation_observation_from_event():
    payload = {
        "tenant_id": "renos",
        "events": [
            {
                "event_id": "cal-1",
                "title": "Kundemøde på kontoret",
                "starts_at": "2026-09-10T13:00:00Z",
                "attendees": ["empir@abde.dk"],
                "location": "Aarhus kontor",
                "description": "Forbered tilbud og rengøringsplan inden mødet",
            }
        ]
    }
    inputs = list(CalendarAdapter().observations(payload))
    assert len(inputs) == 1
    o = inputs[0]
    assert o.source == "calendar"
    assert o.external_id == "cal-1"
    assert o.title_hint.casefold() == "forbered kundemøde på kontoret (2026-09-10)".casefold()
    assert "inden mødet" in o.text or "forbered" in o.text.lower()
    assert o.metadata["event_id"] == "cal-1"
    assert o.metadata["attendees"] == ["empir@abde.dk"]


# ---------- Code ----------

def test_code_adapter_emits_followup_from_commit_message():
    payload = {
        "tenant_id": "renos",
        "commits": [
            {
                "sha": "abc123",
                "message": "fix: ensure invoicing endpoint handles missing customer\n\nTODO: add retry policy",
                "author": "dev@abde.dk",
                "committed_at": "2026-09-05T12:34:56Z",
                "repo": "Project-Renos",
            }
        ]
    }
    inputs = list(CodeAdapter().observations(payload))
    # TODO lines become actionable observations
    assert len(inputs) == 1
    o = inputs[0]
    assert o.source == "code"
    assert o.external_id == "Project-Renos:abc123:TODO:0"
    assert "TODO" in o.text or "retry policy" in o.text
    assert o.metadata["repo"] == "Project-Renos"
    assert o.metadata["sha"] == "abc123"


# ---------- RenOS (job lifecycle signals) ----------

def test_renos_adapter_emits_followup_when_job_overdue():
    payload = {
        "tenant_id": "renos",
        "company_id": "company-123",
        "jobs": [
            {
                "job_id": "job-1",
                "title": "Rengøring — kunde X",
                "status": "planned",
                "scheduled_end": "2026-09-04T18:00:00Z",  # in the past
                "customer_id": "cust-1",
            },
            {
                "job_id": "job-2",
                "title": "Vinduespudsning",
                "status": "completed",
                "scheduled_end": "2026-09-05T18:00:00Z",
                "customer_id": "cust-2",
            },
        ],
        "as_of": "2026-09-05T18:30:00Z",
    }
    inputs = list(RenosAdapter().observations(payload))
    # Only overdue jobs (job-1) emit observations
    assert len(inputs) == 1
    o = inputs[0]
    assert o.source == "renos"
    assert o.external_id == "renos:job-1:overdue:2026-09-05T18:30:00Z"
    assert "overdue" in o.text.lower() or "forsinket" in o.text.lower()
    assert o.priority_hint in {"high", "critical"}
    assert o.metadata["job_id"] == "job-1"
    assert o.metadata["customer_id"] == "cust-1"


def test_renos_adapter_emits_completion_observation():
    payload = {
        "tenant_id": "renos",
        "company_id": "company-123",
        "jobs": [
            {
                "job_id": "job-3",
                "title": "Flytterengøring",
                "status": "completed",
                "scheduled_end": "2026-09-05T18:00:00Z",
                "customer_id": "cust-3",
                "completed_at": "2026-09-05T17:45:00Z",
            },
        ],
        "as_of": "2026-09-05T18:30:00Z",
    }
    inputs = list(RenosAdapter().observations(payload))
    # Completed jobs without followup flags: still no work to do.
    assert inputs == []