"""Live HeyPocket REST reconciliation client.

The webhook plane only signals change. Canonical Pocket materialization comes
from the documented Public API using a tenant-scoped API key. Secrets are read
from environment variables and are never returned in domain records.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .pocket import (
    DERIVATION_UNCERTAINTY_DEFAULTS,
    DERIVATION_WEIGHTS,
    SCHEMA,
    PocketRejected,
)

DEFAULT_POCKET_API_BASE_URL = "https://public.heypocketai.com/api/v1"
_API_KEY_PREFIX = "AFTERGRAPH_POCKET_API_KEY"
class PocketProviderError(RuntimeError):
    """Provider/API failure that must not leak credentials."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retriable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retriable = retriable


def _tenant_env_suffix(tenant_id: str) -> str:
    return re.sub(r"\W", "_", tenant_id).upper().strip("_") or "DEFAULT"


def resolve_pocket_api_key(tenant_id: str | None) -> str | None:
    """Resolve tenant-specific Pocket API key, falling back to global."""
    if tenant_id:
        specific = os.getenv(f"{_API_KEY_PREFIX}_{_tenant_env_suffix(tenant_id)}")
        if specific:
            return specific
    return os.getenv(_API_KEY_PREFIX)


def any_pocket_api_keys() -> bool:
    return any(
        value
        for key, value in os.environ.items()
        if key == _API_KEY_PREFIX or key.startswith(f"{_API_KEY_PREFIX}_")
    )
def fetch_heypocket_recording(
    api_key: str,
    recording_id: str,
    *,
    base_url: str | None = None,
    timeout: float = 10.0,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    """Fetch canonical recording details from the documented Pocket API."""
    if not api_key:
        raise PocketProviderError("Pocket API key is not configured")
    if not recording_id or len(recording_id) > 256:
        raise PocketProviderError("invalid Pocket recording id")

    root = (base_url or os.getenv("AFTERGRAPH_POCKET_API_BASE_URL")
            or DEFAULT_POCKET_API_BASE_URL).rstrip("/")
    if not root.startswith("https://"):
        raise PocketProviderError("Pocket API base URL must use HTTPS")

    encoded_id = urllib.parse.quote(recording_id, safe="")
    url = (
        f"{root}/public/recordings/{encoded_id}"
        "?include_transcript=true&include_summarizations=true"
    )
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "Aftergraph-Wie/0.2",
        },
        method="GET",
    )
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        if status in {401, 403}:
            raise PocketProviderError(
                "Pocket API authentication failed",
                status_code=status,
                retriable=False,
            ) from exc
        if status == 404:
            raise PocketProviderError(
                "Pocket recording was not found",
                status_code=status,
                retriable=False,
            ) from exc
        raise PocketProviderError(
            "Pocket API request failed",
            status_code=status,
            retriable=status == 429 or status >= 500,
        ) from exc
    except urllib.error.URLError as exc:
        raise PocketProviderError(
            "Pocket API is unreachable",
            retriable=True,
        ) from exc

    try:
        envelope = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise PocketProviderError(
            "Pocket API returned invalid JSON",
            retriable=True,
        ) from exc
    if not isinstance(envelope, dict) or envelope.get("success") is not True:
        raise PocketProviderError("Pocket API returned an unsuccessful response")
    data = envelope.get("data")
    if not isinstance(data, dict):
        raise PocketProviderError("Pocket API response is missing recording data")
    if str(data.get("id") or "") != recording_id:
        raise PocketProviderError("Pocket API recording identity mismatch")
    return data


def _timestamp_ms(value: Any) -> int:
    if not isinstance(value, str) or not value:
        return 0
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return 0
    return int(parsed.timestamp() * 1000)


def _summary_text(summarizations: Any) -> str:
    if not isinstance(summarizations, dict):
        return ""
    for candidate in summarizations.values():
        if not isinstance(candidate, dict):
            continue
        v2 = candidate.get("v2")
        if not isinstance(v2, dict):
            continue
        summary = v2.get("summary")
        if not isinstance(summary, dict):
            continue
        markdown = summary.get("markdown")
        if isinstance(markdown, str) and markdown.strip():
            return markdown.strip()
        bullets = summary.get("bulletPoints")
        if isinstance(bullets, list):
            text = "\n".join(
                str(item).strip()
                for item in bullets
                if str(item).strip()
            )
            if text:
                return text
    return ""


def normalize_heypocket_recording(
    recording: dict[str, Any],
    tenant_id: str,
    *,
    consent_ref: str | None = None,
) -> dict[str, Any]:
    """Normalize canonical REST recording data into pocket-source/0.1."""
    recording_id = str(recording.get("id") or "")
    if not recording_id:
        raise PocketRejected("PCK-LIVE-001", "Pocket REST recording.id is required")

    transcript = recording.get("transcript")
    transcript_obj = transcript if isinstance(transcript, dict) else {}
    raw_segments = transcript_obj.get("segments")
    segments: list[dict[str, Any]] = []
    if isinstance(raw_segments, list):
        for item in raw_segments:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or item.get("originalText") or "").strip()
            if not text:
                continue
            segments.append(
                {
                    "text": text,
                    "speaker": item.get("speaker"),
                    "speaker_confidence": (
                        item.get("speakerConfidence")
                        if "speakerConfidence" in item
                        else item.get("speaker_confidence")
                    ),
                    "conversation_id": recording_id,
                }
            )

    transcript_text = str(transcript_obj.get("text") or "").strip()
    if not segments and transcript_text:
        segments.append(
            {
                "text": transcript_text,
                "speaker": None,
                "speaker_confidence": None,
                "conversation_id": recording_id,
            }
        )

    derivations: list[dict[str, Any]] = []
    content_kind = "transcript"
    if segments:
        derivations.append(
            {
                "derivation": "transcript",
                "weight": DERIVATION_WEIGHTS["transcript"],
                "uncertainty": DERIVATION_UNCERTAINTY_DEFAULTS["transcript"],
                "lineage_ref": f"pocket:rest:transcript:{recording_id}",
            }
        )
    else:
        summary = _summary_text(recording.get("summarizations"))
        if summary:
            content_kind = "summary"
            segments.append(
                {
                    "text": summary,
                    "speaker": None,
                    "speaker_confidence": None,
                    "conversation_id": recording_id,
                }
            )
            derivations.append(
                {
                    "derivation": "summary",
                    "weight": DERIVATION_WEIGHTS["summary"],
                    "uncertainty": DERIVATION_UNCERTAINTY_DEFAULTS["summary"],
                    "lineage_ref": f"pocket:rest:summary:{recording_id}",
                }
            )

    if not segments:
        raise PocketRejected(
            "PCK-LIVE-002",
            "canonical Pocket recording has no usable transcript or summary yet",
        )

    asserted_at = str(
        recording.get("updated_at")
        or recording.get("created_at")
        or recording.get("recording_at")
        or ""
    )
    digest_basis = (
        f"{recording_id}|{asserted_at}|"
        + hashlib.sha256("\n".join(s["text"] for s in segments).encode()).hexdigest()
    )
    digest = hashlib.sha256(digest_basis.encode()).hexdigest()
    return {
        "schema": SCHEMA,
        "pocket_id": "pck_" + digest[:32],
        "tenant_id": tenant_id,
        "credential_scope": tenant_id,
        "source_ref": f"pocket:recording:{recording_id}",
        "materialization_ref": f"pocket:recording:{recording_id}:rev:{digest[:16]}",
        "observation_ref": f"wie:observation:{digest[:32]}",
        "classification": "research",
        "claims_principal_identity": False,
        "claims_principal_authentication": False,
        "contains_instruction": False,
        "self_executes": False,
        "claims_execution": False,
        "candidate_kind": "observation_update",
        "admitted_by_tg": False,
        "governed_path_complete": False,
        "derivations": derivations,
        "asserted_at": asserted_at,
        "consent_ref": consent_ref or "pocket:owner:unattributed",
        "purpose": "physical_world_context",
        "conversation_id": recording_id,
        "participants": [],
        "segments": segments,
        "delivery_channel": "rest",
        "sequence_number": _timestamp_ms(asserted_at),
        "rest_content_kind": content_kind,
        "rest_recording_state": recording.get("state"),
    }
