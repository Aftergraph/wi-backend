"""V2 destination publishers.

V1 shipped a single ``WebhookPublisher``. V2 introduces three concrete
destinations and a router that dispatches + enforces tenant policy.

- ``RenosPublisher``  → Project-Renos ``Job`` over HTTP (real REST call).
- ``WorksPublisher`` → works-execution ``Work`` over HTTP. The payload
                        conforms to ``contracts/schemas/work.schema.schema.json``
                        (work.schema/1.0). The engine never sends a work-item
                        to WORKS — promotion is always explicit.
- ``WebhookPublisher``→ unchanged from V1 (carries over for back-compat).

The ``PublishRouter`` adds policy enforcement: a tenant may disable a
destination by leaving it out of ``TenantPolicy.allowed_destinations``. An
unknown destination raises ``DestinationNotAllowed``.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from .models import Observation, WorkItem

# ---------- exceptions ----------


class DestinationNotAllowed(KeyError):
    """Raised when the destination is not allowed by tenant policy."""


# ---------- receipt + interface ----------


@dataclass(slots=True)
class PublishReceipt:
    destination: str
    external_id: str | None = None
    response: dict[str, Any] | None = None


class Publisher:
    def publish(self, destination: str, work_item: WorkItem, observations: list[Observation]) -> PublishReceipt:
        raise NotImplementedError


# ---------- shared JSON helpers ----------


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _http_post_json(url: str, payload: dict[str, Any], headers: Mapping[str, str], timeout_s: float) -> tuple[int, dict[str, Any] | str]:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    merged = {"Content-Type": "application/json", **headers}
    request = urllib.request.Request(url, data=body, headers=merged, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read(1024 * 1024).decode("utf-8", errors="replace")
            content_type = response.headers.get("Content-Type", "")
            if "json" in content_type and raw:
                parsed = json.loads(raw)
                return response.status, parsed if isinstance(parsed, dict) else {"value": parsed}
            return response.status, {"body": raw}
    except urllib.error.HTTPError as exc:
        body_text = exc.read(4096).decode("utf-8", errors="replace")
        raise RuntimeError(f"{url} returned HTTP {exc.code}: {body_text}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{url} failed: {exc.reason}") from exc


# ---------- WebhookPublisher (V1, carried over) ----------


class WebhookPublisher(Publisher):
    """Publish WorkItems only to operator-configured destination URLs."""

    def __init__(self, destinations: dict[str, str], secret: str | None = None, timeout_s: float = 10.0):
        self.destinations = {key.casefold(): value for key, value in destinations.items()}
        self.secret = secret.encode("utf-8") if secret else None
        self.timeout_s = timeout_s

    def publish(self, destination: str, work_item: WorkItem, observations: list[Observation]) -> PublishReceipt:
        key = destination.casefold()
        url = self.destinations.get(key)
        if not url:
            raise KeyError(f"webhook destination not configured: {destination}")
        payload = {
            "schema": "aftergraph.work-item/1.0",
            "destination": destination,
            "work_item": _jsonable(asdict(work_item)),
            "observations": [_jsonable(asdict(item)) for item in observations],
        }
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": "aftergraph-work-intelligence/0.2"}
        if self.secret:
            digest = hmac.new(self.secret, body, hashlib.sha256).hexdigest()
            headers["X-Aftergraph-Signature"] = f"sha256={digest}"
        _, parsed = _http_post_json(url, payload, headers, self.timeout_s)
        external_id = parsed.get("id") or parsed.get("external_id") or parsed.get("ticket_id")
        return PublishReceipt(destination=destination, external_id=str(external_id) if external_id is not None else None, response=parsed if isinstance(parsed, dict) else {"body": parsed})


def publisher_from_env() -> Publisher | None:
    raw = os.getenv("AFTERGRAPH_PUBLISHERS_JSON", "").strip()
    if not raw:
        return None
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()):
        raise ValueError("AFTERGRAPH_PUBLISHERS_JSON must be a JSON object of destination -> URL")
    return WebhookPublisher(parsed, secret=os.getenv("AFTERGRAPH_WEBHOOK_SECRET"))


# ---------- RenosPublisher ----------


# Mapping from canonical work-item priority to Project-Renos ``Job.status``.
# The RenOS Job status field is a string; we map ``critical|high`` → priority.
_RENOS_PRIORITY_FROM_WI = {
    "critical": "urgent",
    "high": "high",
    "medium": "medium",
    "low": "low",
}


class RenosPublisher(Publisher):
    """Publishes WorkItems as Project-Renos ``Job`` records.

    The fake and real surface used in tests is the same: ``POST /api/jobs``
    with a Job-shaped body. The real Project-Renos app uses Next.js Server
    Actions, but exposes a compatible REST surface in production builds.
    """

    destination = "renos"

    def __init__(self, base_url: str, company_id: str, timeout_s: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.company_id = company_id
        self.timeout_s = timeout_s

    def publish(self, destination: str, work_item: WorkItem, observations: list[Observation]) -> PublishReceipt:
        if destination.casefold() != self.destination:
            raise KeyError(f"renos publisher cannot handle destination: {destination}")
        url = f"{self.base_url}/api/jobs"
        # Take the first observation's metadata (provenance) and the latest
        # occurrence timestamp.
        occurred_at = observations[0].occurred_at if observations else work_item.updated_at
        body = {
            "companyId": self.company_id,
            "title": work_item.title,
            "description": work_item.summary,
            "priority": _RENOS_PRIORITY_FROM_WI.get(work_item.priority, "medium"),
            "scheduledStart": occurred_at.isoformat() if occurred_at else None,
            "scheduledEnd": None,
            "status": "planned",
            "pricingType": "fixed",
            "externalRefs": {
                "aftergraph_work_item_id": work_item.id,
                "aftergraph_canonical_key": work_item.canonical_key,
                "aftergraph_source": observations[0].source if observations else "unknown",
                "aftergraph_actor": observations[0].actor if observations else None,
            },
        }
        _, parsed = _http_post_json(url, body, {"User-Agent": "aftergraph-work-intelligence/0.2"}, self.timeout_s)
        external_id = parsed.get("id") if isinstance(parsed, dict) else None
        return PublishReceipt(destination=self.destination, external_id=external_id, response=parsed if isinstance(parsed, dict) else {"body": parsed})


# ---------- WorksPublisher ----------


def _work_idempotency_key(work_item: WorkItem) -> str:
    return f"aftergraph:{work_item.tenant_id}:{work_item.canonical_key}"


def _build_works_payload(work_item: WorkItem, observations: list[Observation]) -> dict[str, Any]:
    """Build a work.schema/1.0-conformant Work payload from a WorkItem."""
    # The first observation provides provenance; the rest are linked via the
    # source.observations list so works-execution can correlate.
    primary = observations[0] if observations else None
    src: dict[str, Any] = {
        "kind": "aftergraph.work-intelligence",
        "work_item_id": work_item.id,
        "tenant_id": work_item.tenant_id,
        "canonical_key": work_item.canonical_key,
        "observations": [
            {
                "observation_id": o.id,
                "source": o.source,
                "external_id": o.external_id,
                "actor": o.actor,
                "occurred_at": o.occurred_at.isoformat() if o.occurred_at else None,
            }
            for o in observations
        ],
    }
    return {
        "id": f"works:{hashlib.sha256(_work_idempotency_key(work_item).encode()).hexdigest()[:32]}",
        "idempotency_key": _work_idempotency_key(work_item),
        "created_at": work_item.created_at.isoformat(),
        "updated_at": work_item.updated_at.isoformat(),
        "source": src,
        "state": "CREATED",
        "objective": {
            "summary": work_item.summary,
            "title": work_item.title,
            "next_action": work_item.next_action,
            "priority": work_item.priority,
            "owner": work_item.owner,
            "due_hint": work_item.due_hint,
        },
        "graph": {
            "nodes": {
                "extract": {
                    "kind": "extract",
                    "input_source": "aftergraph.work_item",
                    "input_id": work_item.id,
                }
            }
        },
        "requirements": {
            "observations_min": work_item.observation_count,
        },
        "policy": {
            "tenant_id": work_item.tenant_id,
            "promotion_actor": "aftergraph.work-intelligence",
        },
        "verification": [
            {
                "criterion": "at least one observation present",
                "kind": "deterministic",
            },
            {
                "criterion": "primary actor present",
                "kind": "deterministic",
            },
        ] if primary else [],
    }


class WorksPublisher(Publisher):
    """Publishes WorkItems to works-execution as ``Work`` payloads.

    The payload conforms to ``contracts/schemas/work.schema.schema.json``
    (work.schema/1.0). Promotion is the operator's explicit action; this
    publisher is the wire to the durable execution plane.
    """

    destination = "works"

    def __init__(self, base_url: str, timeout_s: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def publish(self, destination: str, work_item: WorkItem, observations: list[Observation]) -> PublishReceipt:
        if destination.casefold() != self.destination:
            raise KeyError(f"works publisher cannot handle destination: {destination}")
        url = f"{self.base_url}/work"
        payload = _build_works_payload(work_item, observations)
        _, parsed = _http_post_json(
            url, payload, {"User-Agent": "aftergraph-work-intelligence/0.2"}, self.timeout_s
        )
        external_id = parsed.get("id") if isinstance(parsed, dict) else None
        return PublishReceipt(destination=self.destination, external_id=external_id, response=parsed if isinstance(parsed, dict) else {"body": parsed})


# ---------- PublishRouter ----------


class PublishRouter(Publisher):
    """Dispatch to the right concrete publisher + enforce tenant policy.

    The router is what the engine holds in ``app.state.publisher``. The
    destinations are configured at construction time; each tenant's
    ``TenantPolicy.allowed_destinations`` decides whether a given tenant may
    use that destination.

    When ``always_config`` is True (default for ``build_publish_router``),
    a destination that is not configured globally but is allowed by the
    tenant policy raises ``DestinationNotAllowed`` instead of ``KeyError``.
    """

    def __init__(self, destinations: dict[str, Publisher], policy_store=None, always_config: bool = False) -> None:
        self._destinations = {key.casefold(): pub for key, pub in destinations.items()}
        # Lazy import to avoid circular dep.
        if policy_store is None:
            from .policy import PolicyStore
            policy_store = PolicyStore()
        self.policy_store = policy_store
        self.always_config = always_config

    def publish(self, destination: str, work_item: WorkItem, observations: list[Observation]) -> PublishReceipt:
        key = destination.casefold()
        policy = self.policy_store.get(work_item.tenant_id)
        # First check tenant policy (deny by default if allowlist is set).
        if not policy.allows_destination(destination):
            raise DestinationNotAllowed(
                f"destination '{destination}' not allowed for tenant '{work_item.tenant_id}'"
            )
        if key not in self._destinations:
            if self.always_config:
                raise DestinationNotAllowed(
                    f"destination '{destination}' not allowed for tenant '{work_item.tenant_id}'"
                )
            raise KeyError(f"no publisher configured for destination: {destination}")
        return self._destinations[key].publish(destination, work_item, observations)


def build_publish_router(destinations: dict[str, Publisher], policy_store=None) -> PublishRouter:
    """Build a router that dispatches to the given destinations.

    To enforce tenant policy for destinations that may not be configured
    globally, pass ``always_config=False`` (default). When True, unknown
    destinations are accepted as long as the tenant policy allows them —
    useful in tests where the operator wants to assert policy even when no
    publisher is wired up.
    """
    return PublishRouter(destinations, policy_store=policy_store, always_config=True)


__all__ = [
    "DestinationNotAllowed",
    "PublishReceipt",
    "PublishRouter",
    "Publisher",
    "RenosPublisher",
    "WebhookPublisher",
    "WorksPublisher",
    "build_publish_router",
    "publisher_from_env",
]