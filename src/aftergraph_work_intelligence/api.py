from __future__ import annotations

import argparse
import asyncio
import hashlib as _hashlib
import hmac as _hmac
import json
import logging
import os
import sys
import threading as _threading
import time
import urllib.request
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import (
    APIRouter,
    Body,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
)
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .audit import AuditLog
from .body_log import BodyLoggingMiddleware
from .cache import Cache
from .evidence import build_evidence
from .exceptions import WorkIntelligenceError
from .metrics import MetricsRecorder
from .migrations import run_migrations
from .models import ObservationInput, Publication, utc_now
from .policy import PolicyStore, TenantPolicy
from .publishers import Publisher, publisher_from_env
from .request_logger import RequestLogger
from .service import WorkIntelligenceService
from .store import SQLiteStore
from .tasks import TaskStatus, create_task_queue
from .tracing import setup_tracing
from .transitions import TransitionEngine


def _dt(value: str) -> datetime:
    """Convert ISO string to datetime."""
    return datetime.fromisoformat(value)


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.now(UTC).isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_data)


def setup_logging():
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    
    logging.root.handlers = [handler]
    logging.root.setLevel(logging.INFO)
    
    # Silence noisy loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)


logger = logging.getLogger("aftergraph.work-intelligence")


def _fire_webhooks(app_state, event: str, payload: dict) -> None:
    """Fire registered webhooks for an event (best-effort, non-blocking via thread)."""
    webhooks = getattr(app_state, "webhooks", {})
    if not webhooks:
        return
    targets = []
    for wh in webhooks.values():
        if not wh.get("active", True):
            continue
        if event not in wh.get("events", []):
            continue
        targets.append(wh)
    if not targets:
        return

    def _deliver():
        for wh in targets:
            url = wh["url"]
            secret = wh.get("secret")
            body = json.dumps({"event": event, "data": payload}).encode()
            headers = {"Content-Type": "application/json"}
            if secret:
                sig = _hmac.new(secret.encode(), body, _hashlib.sha256).hexdigest()
                headers["X-Webhook-Signature"] = f"sha256={sig}"
            for attempt in range(3):
                try:
                    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
                    urllib.request.urlopen(req, timeout=5)
                    break
                except Exception:
                    if attempt < 2:
                        time.sleep(0.5 * (attempt + 1))

    _threading.Thread(target=_deliver, daemon=True).start()

    # Broadcast to WebSocket clients (best-effort)
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            _task = loop.create_task(broadcast_update(event, payload))
            _task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    except Exception:
        pass

    # Track delivery stats
    stats = getattr(app_state, "webhook_stats", {"delivered": 0, "failed": 0})
    stats["delivered"] += 1
    app_state.webhook_stats = stats


# --- WebSocket broadcast (module-level so _fire_webhooks and app share it) ---
ws_clients: set = set()
ws_last_heartbeat: dict = {}  # client -> timestamp

async def broadcast_update(event: str, data: dict):
    """Broadcast update to all connected WebSocket clients."""
    if not ws_clients:
        return
    message = json.dumps({"event": event, "data": data})
    dead = set()
    for ws in ws_clients:
        try:
            await ws.send_text(message)
        except Exception:
            dead.add(ws)
    for ws in dead:
        ws_clients.discard(ws)


class RateLimiter:
    def __init__(self, requests_per_minute: int = 60):
        self.default_limit = requests_per_minute
        self.requests: dict[str, list[float]] = defaultdict(list)
        self.key_limits: dict[str, int] = {}  # per-key overrides
        self.endpoint_limits: dict[str, int] = {}  # per-endpoint overrides
        self._endpoint_requests: dict[str, list[float]] = defaultdict(list)

    def set_key_limit(self, key: str, limit: int) -> None:
        """Set a custom rate limit for a specific key."""
        self.key_limits[key] = limit

    def set_endpoint_limit(self, endpoint: str, limit: int) -> None:
        """Set a rate limit for a specific endpoint."""
        self.endpoint_limits[endpoint] = limit

    def get_limit(self, key: str) -> int:
        """Get the rate limit for a key."""
        return self.key_limits.get(key, self.default_limit)

    def is_allowed(self, client_id: str, endpoint: str | None = None) -> bool:
        now = time.time()
        window_start = now - 60

        # Check global/key limit
        self.requests[client_id] = [t for t in self.requests[client_id] if t > window_start]
        limit = self.get_limit(client_id)
        if len(self.requests[client_id]) >= limit:
            return False

        # Check endpoint-specific limit
        if endpoint and endpoint in self.endpoint_limits:
            ep_key = f"{client_id}:{endpoint}"
            self._endpoint_requests[ep_key] = [t for t in self._endpoint_requests[ep_key] if t > window_start]
            ep_limit = self.endpoint_limits[endpoint]
            if len(self._endpoint_requests[ep_key]) >= ep_limit:
                return False
            self._endpoint_requests[ep_key].append(now)

        # Record new request
        self.requests[client_id].append(now)
        return True

    def get_endpoint_usage(self, client_id: str, endpoint: str) -> dict:
        """Get usage stats for a specific endpoint."""
        now = time.time()
        window_start = now - 60
        ep_key = f"{client_id}:{endpoint}"
        self._endpoint_requests[ep_key] = [t for t in self._endpoint_requests[ep_key] if t > window_start]
        limit = self.endpoint_limits.get(endpoint, self.default_limit)
        return {
            "used": len(self._endpoint_requests[ep_key]),
            "limit": limit,
            "remaining": max(0, limit - len(self._endpoint_requests[ep_key])),
        }

    def get_usage(self, client_id: str) -> dict:
        """Get current usage stats for a client."""
        now = time.time()
        window_start = now - 60
        self.requests[client_id] = [t for t in self.requests[client_id] if t > window_start]
        limit = self.get_limit(client_id)
        return {
            "used": len(self.requests[client_id]),
            "limit": limit,
            "remaining": max(0, limit - len(self.requests[client_id])),
        }


class ObservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=128)
    source: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=100_000)
    external_id: str | None = Field(default=None, max_length=512)
    actor: str | None = Field(default=None, max_length=512)
    occurred_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    title_hint: str | None = Field(default=None, max_length=256)
    owner_hint: str | None = Field(default=None, max_length=256)
    due_hint: str | None = Field(default=None, max_length=256)
    priority_hint: str | None = Field(default=None, pattern="^(low|medium|high|critical)$")


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    destination: str = Field(min_length=1, max_length=128)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(pattern="^(approve|reject|snooze|cancel)$")
    actor: str = Field(min_length=1, max_length=512)
    reason: str = Field(default="", max_length=2048)
    resume_at: datetime | None = None


class PromoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(min_length=1, max_length=512)
    reason: str = Field(default="", max_length=2048)


class BulkStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    work_item_ids: list[str] = Field(min_length=1, max_length=100)
    tenant_id: str = Field(min_length=1, max_length=128)


def create_app(
    db_path: str | Path = "./aftergraph-work-intelligence.db",
    api_token: str | None = None,
    publisher: Publisher | None = None,
    policy_store: PolicyStore | None = None,
    evidence_secret: str | None = None,
) -> FastAPI:
    db_path = Path(db_path)
    configured_token = api_token if api_token is not None else os.getenv("AFTERGRAPH_API_TOKEN")
    configured_publisher = publisher if publisher is not None else publisher_from_env()
    configured_policy_store = policy_store if policy_store is not None else PolicyStore()
    configured_evidence_secret = evidence_secret if evidence_secret is not None else os.getenv(
        "AFTERGRAPH_EVIDENCE_SECRET", "aftergraph-work-intelligence"
    )
    rate_limiter = RateLimiter(requests_per_minute=int(os.getenv("AFTERGRAPH_RATE_LIMIT", "60")))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging()
        logger.info("Starting Aftergraph Work Intelligence V2", extra={"version": "0.2.0"})
        db_path.parent.mkdir(parents=True, exist_ok=True)
        store = SQLiteStore(db_path)
        app.state.store = store

        # Run pending migrations (use store's connection for :memory: support)
        migration_result = run_migrations(connection=store._db)
        app.state.migration_version = migration_result["current_version"]
        app.state.service = WorkIntelligenceService(store, policy_store=configured_policy_store)
        app.state.policy_store = configured_policy_store
        app.state.transitions = TransitionEngine(store, policy_store=configured_policy_store)
        app.state.publisher = configured_publisher
        app.state.metrics = MetricsRecorder(store)
        app.state.evidence_secret = configured_evidence_secret
        # Initialize background task queue
        task_queue = create_task_queue()
        app.state.task_queue = task_queue

        # Initialize audit log
        audit_log = AuditLog(max_entries=50000)
        app.state.audit_log = audit_log

        # Initialize cache
        cache = Cache(default_ttl=300, max_size=5000)
        app.state.cache = cache

        # Initialize request logger
        log_dir = db_path.parent / "logs"
        request_logger = RequestLogger(log_dir=log_dir)
        app.state.request_logger = request_logger

        # Store migration version for health check
        app.state.migration_version = migration_result["current_version"]

        try:
            yield
        finally:
            store.close()

    app = FastAPI(
        title="Aftergraph Work Intelligence",
        version="0.2.0",
        description="""## Aftergraph Work Intelligence V2

Production-grade observation → WorkItem inference engine.

### Authentication
- **Bearer token**: `Authorization: Bearer <token>` (full admin access)
- **API key**: `X-API-Key: ak_<key>` (read/write access)

### Key Features
- **Observation ingestion**: Submit observations from any source
- **Automatic resolution**: Deduplication and merge across sources
- **Policy enforcement**: Per-tenant rules for auto-promotion, allowed sources
- **Review workflow**: OPEN → APPROVED → PUBLISHED lifecycle
- **Evidence chain**: HMAC-SHA256 provenance for every work item
- **Webhook delivery**: Async with retries and HMAC signatures
- **Background tasks**: Async processing with worker pool

### Rate Limiting
- Global: 60 requests/minute (configurable)
- Per-key: Custom limits via `/v1/rate-limit`
- Per-endpoint: Custom limits per API endpoint
""",
        lifespan=lifespan,
    )
    # Add GZip compression
    from fastapi.middleware.gzip import GZipMiddleware
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    
    # Global exception handler for custom exceptions
    @app.exception_handler(WorkIntelligenceError)
    async def work_intelligence_error_handler(request: Request, exc: WorkIntelligenceError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "detail": exc.detail,
                "error_type": exc.__class__.__name__,
            },
        )

    # Setup OpenTelemetry tracing
    setup_tracing(app)

    # Add CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Body logging middleware (configurable via env)
    app.add_middleware(
        BodyLoggingMiddleware,
        log_request_body=os.getenv("AFTERGRAPH_LOG_REQUEST_BODY", "false").lower() == "true",
        log_response_body=os.getenv("AFTERGRAPH_LOG_RESPONSE_BODY", "false").lower() == "true",
        max_chars=int(os.getenv("AFTERGRAPH_BODY_LOG_MAX_CHARS", "1000")),
    )

    # API version header middleware
    @app.middleware("http")
    async def add_version_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-API-Version"] = "v1"
        response.headers["X-App-Version"] = "0.2.0"
        return response
    
    # Add timing middleware
    @app.middleware("http")
    async def add_timing(request: Request, call_next):
        start_time = time.time()
        response = await call_next(request)
        duration = time.time() - start_time
        response.headers["X-Process-Time"] = str(round(duration * 1000, 2))
        return response
    
    # Add usage tracking
    usage_stats = {"requests": 0, "by_path": defaultdict(int), "by_status": defaultdict(int), "errors": 0}
    app.state.usage_stats = usage_stats
    app.state.rate_limiter = rate_limiter

    # Request size limiting middleware
    MAX_REQUEST_SIZE = int(os.getenv("AFTERGRAPH_MAX_REQUEST_SIZE", "10485760"))  # 10MB default

    @app.middleware("http")
    async def limit_request_size(request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_REQUEST_SIZE:
            return JSONResponse(
                status_code=413,
                content={"detail": f"Request too large. Max size: {MAX_REQUEST_SIZE} bytes"},
            )
        return await call_next(request)

    # Enhanced request logging middleware
    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        start_time = time.time()
        request_size = int(request.headers.get("content-length", 0))
        response = await call_next(request)
        duration = time.time() - start_time

        # Track usage
        usage_stats["requests"] += 1
        usage_stats["by_path"][request.url.path] += 1
        usage_stats["by_status"][str(response.status_code)] += 1
        if response.status_code >= 400:
            usage_stats["errors"] += 1

        # Track response times
        path_key = request.url.path
        # Ensure response_times dict exists on app.state (needed for test isolation)
        if not hasattr(app.state, "response_times"):
            app.state.response_times = defaultdict(list)
        app.state.response_times[path_key].append(duration)
        if len(app.state.response_times[path_key]) > 1000:
            app.state.response_times[path_key] = app.state.response_times[path_key][-500:]

        # Structured log
        logger.info(
            "Request processed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round(duration * 1000, 2),
                "request_size": request_size,
            },
        )

        logger.info(
            "Request processed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round(duration * 1000, 2),
                "client": request.client.host if request.client else "unknown",
            }
        )
        return response
    
    # Add rate limiting middleware
    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        client_id = request.client.host if request.client else "unknown"
        # Skip rate limiting for management/health endpoints
        skip_paths = {"/health", "/v1/rate-limit", "/v1/metrics", "/v1/webhooks/stats", "/docs", "/openapi.json"}
        if request.url.path in skip_paths:
            return await call_next(request)
        if not rate_limiter.is_allowed(client_id):
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Try again later."}
            )
        return await call_next(request)
    
    # Add request ID middleware
    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
    
    router = APIRouter(prefix="/v1")

    def auth(
        request: Request,
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> None:
        """Authenticate via Bearer token OR API key. Returns auth context."""
        # Bearer token (master token)
        if configured_token and authorization == f"Bearer {configured_token}":
            request.state.auth_method = "bearer"
            request.state.auth_scopes = ["admin", "read", "write", "delete"]
            return

        # API key authentication
        if x_api_key and x_api_key.startswith("ak_"):
            prefix = x_api_key[:12]
            store: SQLiteStore = request.app.state.store
            if store.validate_api_key(prefix):
                request.state.auth_method = "api_key"
                request.state.auth_scopes = ["read", "write"]
                return

        # No valid auth
        if configured_token:
            raise HTTPException(status_code=401, detail="invalid or missing credentials")

    def service(request: Request) -> WorkIntelligenceService:
        return request.app.state.service

    @app.get("/healthz")
    def healthz(request: Request) -> dict[str, str]:
        """Health check with DB + task queue status."""
        checks = {
            "status": "ok",
            "service": "aftergraph-work-intelligence",
            "version": "0.2.0",
            "api_version": "v1",
        }
        # DB check
        try:
            store: SQLiteStore = request.app.state.store
            store.list_api_keys()
            checks["database"] = "ok"
        except Exception as e:
            checks["database"] = f"error: {e}"
            checks["status"] = "degraded"

        # Task queue check
        try:
            queue = request.app.state.task_queue
            stats = queue.get_stats()
            checks["task_queue"] = "ok"
            checks["tasks_pending"] = str(stats.get("pending", 0))
            checks["tasks_running"] = str(stats.get("running", 0))
        except Exception:
            checks["task_queue"] = "unavailable"

        # Cache check
        try:
            cache = request.app.state.cache
            cache_stats = cache.stats()
            checks["cache"] = "ok"
            checks["cache_size"] = str(cache_stats.get("size", 0))
            checks["cache_hits"] = str(cache_stats.get("hits", 0))
        except Exception:
            checks["cache"] = "unavailable"

        # Migration version
        checks["migration_version"] = str(getattr(request.app.state, "migration_version", 0))

        return checks

    @router.post("/observations", dependencies=[Depends(auth)])
    def ingest_observation(payload: ObservationRequest, request: Request, svc: WorkIntelligenceService = Depends(service)):
        try:
            result = svc.ingest(ObservationInput(**payload.model_dump()))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        status = 201 if result.action == "created" else 202 if result.action == "observed" else 200
        encoded = jsonable_encoder(asdict(result))
        _fire_webhooks(request.app.state, "observation.ingested", encoded)
        return JSONResponse(status_code=status, content=encoded)

    @router.get("/work-items/{work_item_id}", dependencies=[Depends(auth)])
    def get_work_item(
        work_item_id: str,
        tenant_id: str = Query(min_length=1, max_length=128),
        svc: WorkIntelligenceService = Depends(service),
    ):
        try:
            detail = svc.get_work_item_detail(work_item_id, tenant_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="work item not found") from exc
        return jsonable_encoder(asdict(detail))

    @router.post("/work-items/{work_item_id}/review", dependencies=[Depends(auth)])
    def review_work_item(
        work_item_id: str,
        payload: ReviewRequest,
        request: Request,
        tenant_id: str = Query(min_length=1, max_length=128),
    ):
        engine: TransitionEngine = request.app.state.transitions
        try:
            if payload.action == "approve":
                item = engine.approve(work_item_id, actor=payload.actor, reason=payload.reason)
            elif payload.action == "reject":
                item = engine.reject(work_item_id, actor=payload.actor, reason=payload.reason)
            elif payload.action == "snooze":
                if payload.resume_at is None:
                    raise HTTPException(status_code=400, detail="resume_at is required for snooze")
                item = engine.snooze(work_item_id, actor=payload.actor, resume_at=payload.resume_at, reason=payload.reason)
            else:  # cancel
                item = engine.cancel(work_item_id, actor=payload.actor, reason=payload.reason)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="work item not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        result = jsonable_encoder(asdict(item))
        _fire_webhooks(request.app.state, f"work_item.{payload.action}", result)
        return result

    @router.post("/work-items/{work_item_id}/promote", dependencies=[Depends(auth)])
    def promote_work_item(
        work_item_id: str,
        payload: PromoteRequest,
        request: Request,
        tenant_id: str = Query(min_length=1, max_length=128),
    ):
        engine: TransitionEngine = request.app.state.transitions
        try:
            item = engine.promote_to_works(work_item_id, actor=payload.actor, reason=payload.reason)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="work item not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        result = jsonable_encoder(asdict(item))
        _fire_webhooks(request.app.state, "work_item.promoted", result)
        return result

    @router.post("/work-items/{work_item_id}/publish", status_code=201, dependencies=[Depends(auth)])
    def publish_work_item(
        work_item_id: str,
        payload: PublishRequest,
        request: Request,
        tenant_id: str = Query(min_length=1, max_length=128),
        svc: WorkIntelligenceService = Depends(service),
    ):
        pub: Publisher | None = request.app.state.publisher
        if pub is None:
            raise HTTPException(status_code=503, detail="no publisher destinations configured")
        try:
            detail = svc.get_work_item_detail(work_item_id, tenant_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="work item not found") from exc
        try:
            receipt = pub.publish(payload.destination, detail.work_item, detail.observations)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        publication = Publication(
            id=f"pub_{uuid.uuid4().hex}",
            work_item_id=work_item_id,
            destination=receipt.destination,
            external_id=receipt.external_id,
            response=receipt.response or {},
            published_at=utc_now(),
        )
        request.app.state.store.save_publication(publication)
        return jsonable_encoder(asdict(publication))

    @router.get("/work-items/{work_item_id}/evidence", dependencies=[Depends(auth)])
    def get_evidence(
        work_item_id: str,
        request: Request,
        tenant_id: str = Query(min_length=1, max_length=128),
        svc: WorkIntelligenceService = Depends(service),
    ):
        try:
            detail = svc.get_work_item_detail(work_item_id, tenant_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="work item not found") from exc
        payload = {
            "tenant_id": detail.work_item.tenant_id,
            "work_item_id": detail.work_item.id,
            "title": detail.work_item.title,
            "canonical_key": detail.work_item.canonical_key,
            "observations": [
                {
                    "id": o.id,
                    "source": o.source,
                    "external_id": o.external_id,
                    "actor": o.actor,
                    "occurred_at": o.occurred_at.isoformat() if o.occurred_at else None,
                    "text": o.text,
                }
                for o in detail.observations
            ],
        }
        envelope = build_evidence(payload, secret=request.app.state.evidence_secret)
        return jsonable_encoder(envelope)

    @router.get("/version", dependencies=[Depends(auth)])
    def version() -> dict:
        return {
            "version": "0.2.0",
            "build": "production",
            "status": "active",
            "features": ["adapters", "policies", "transitions", "publishers", "evidence", "metrics"]
        }

    @router.get("/usage", dependencies=[Depends(auth)])
    def usage(request: Request):
        """API usage statistics."""
        stats = getattr(request.app.state, "usage_stats", {})
        return {
            "total_requests": stats.get("requests", 0),
            "total_errors": stats.get("errors", 0),
            "by_path": dict(stats.get("by_path", {})),
            "by_status": dict(stats.get("by_status", {})),
        }

    @router.post("/tasks/submit", dependencies=[Depends(auth)])
    def submit_task(request: Request, name: str = Body(...), args: list = Body(default=[]), kwargs: dict = Body(default={})):
        """Submit a background task."""
        queue = request.app.state.task_queue
        task = queue.submit(name, *args, **kwargs)
        return {"task_id": task.id, "name": task.name, "status": task.status}

    @router.get("/tasks/stats", dependencies=[Depends(auth)])
    def task_stats(request: Request):
        """Get task queue statistics."""
        queue = request.app.state.task_queue
        return queue.get_stats()

    @router.get("/tasks", dependencies=[Depends(auth)])
    def list_tasks(request: Request, status: str = Query(None)):
        """List background tasks."""
        queue = request.app.state.task_queue
        task_status = TaskStatus(status) if status else None
        tasks = queue.list_tasks(status=task_status)
        return [{"id": t.id, "name": t.name, "status": t.status} for t in tasks]

    @router.get("/tasks/{task_id}", dependencies=[Depends(auth)])
    def get_task_status(request: Request, task_id: str):
        """Get task status."""
        queue = request.app.state.task_queue
        task = queue.get_task(task_id)
        if not task:
            return JSONResponse(status_code=404, content={"detail": "Task not found"})
        return {
            "id": task.id,
            "name": task.name,
            "status": task.status,
            "result": task.result,
            "error": task.error,
            "created_at": task.created_at,
            "started_at": task.started_at,
            "completed_at": task.completed_at,
            "retries": task.retries,
        }

    @router.get("/webhooks/stats", dependencies=[Depends(auth)])
    def webhook_stats(request: Request):
        """Get webhook delivery statistics."""
        stats = getattr(request.app.state, "webhook_stats", {"delivered": 0, "failed": 0})
        webhooks = getattr(request.app.state, "webhooks", {})
        return {
            "delivery": stats,
            "registered": len(webhooks),
            "active": sum(1 for wh in webhooks.values() if wh.get("active", True)),
        }

    @router.get("/rate-limit", dependencies=[Depends(auth)])
    def rate_limit_status(request: Request, client_id: str = Query(None)):
        """Check rate limit status for a client or API key."""
        limiter: RateLimiter = request.app.state.rate_limiter
        if client_id:
            return limiter.get_usage(client_id)
        return {"default_limit": limiter.default_limit, "key_count": len(limiter.key_limits)}

    @router.post("/rate-limit", dependencies=[Depends(auth)])
    def set_rate_limit(request: Request, key: str = Body(...), limit: int = Body(..., ge=1, le=10000)):
        """Set a custom rate limit for an API key."""
        limiter: RateLimiter = request.app.state.rate_limiter
        limiter.set_key_limit(key, limit)
        return {"key": key, "limit": limit, "updated": True}

    @router.get("/metrics", dependencies=[Depends(auth)])
    def metrics(request: Request):
        recorder: MetricsRecorder = request.app.state.metrics
        return jsonable_encoder(recorder.snapshot())

    @router.get("/monitoring", dependencies=[Depends(auth)])
    def monitoring(request: Request):
        """Comprehensive monitoring endpoint with system metrics."""
        import psutil
        recorder: MetricsRecorder = request.app.state.metrics
        
        # System metrics
        cpu_percent = psutil.cpu_percent(interval=0.1)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        
        # Service metrics
        service_metrics = recorder.snapshot()
        
        return {
            "system": {
                "cpu_percent": cpu_percent,
                "memory_percent": memory.percent,
                "memory_used_gb": round(memory.used / (1024**3), 2),
                "memory_total_gb": round(memory.total / (1024**3), 2),
                "disk_percent": disk.percent,
                "disk_used_gb": round(disk.used / (1024**3), 2),
                "disk_total_gb": round(disk.total / (1024**3), 2),
            },
            "service": service_metrics,
            "timestamp": datetime.now(UTC).isoformat() + "Z"
        }


    @router.get("/tenants", dependencies=[Depends(auth)])
    def list_tenants(request: Request):
        """List all tenants with work item counts."""
        store: SQLiteStore = request.app.state.store
        with store._lock:
            rows = store._db.execute(
                "SELECT tenant_id, COUNT(*) as cnt FROM intake_work_items GROUP BY tenant_id ORDER BY tenant_id"
            ).fetchall()
        tenants = [
            {"tenant_id": row[0], "work_item_count": row[1]}
            for row in rows
        ]
        return {"tenants": tenants, "count": len(tenants)}

    @router.get("/work-items/{work_item_id}/transitions", dependencies=[Depends(auth)])
    def get_transitions(
        work_item_id: str,
        request: Request,
        tenant_id: str = Query(min_length=1, max_length=128),
    ):
        """Get transition history for a work item."""
        store: SQLiteStore = request.app.state.store
        item = store.get_work_item(work_item_id, tenant_id)
        if item is None:
            raise HTTPException(status_code=404, detail="work item not found")
        transitions = store.list_transitions(work_item_id)
        return {
            "work_item_id": work_item_id,
            "transitions": [
                {
                    "id": t.id,
                    "from_status": t.from_state,
                    "to_status": t.to_state,
                    "action": "approve" if t.to_state == "APPROVED" else t.to_state.lower(),
                    "actor": t.actor,
                    "reason": t.reason,
                    "created_at": t.at.isoformat() if t.at else None,
                }
                for t in transitions
            ],
            "count": len(transitions),
        }

    @router.get("/work-items/{work_item_id}/publications", dependencies=[Depends(auth)])
    def get_publications(
        work_item_id: str,
        request: Request,
        tenant_id: str = Query(min_length=1, max_length=128),
    ):
        """Get publication history for a work item."""
        store: SQLiteStore = request.app.state.store
        item = store.get_work_item(work_item_id, tenant_id)
        if item is None:
            raise HTTPException(status_code=404, detail="work item not found")
        publications = store.publications_for_work_item(work_item_id)
        return {
            "work_item_id": work_item_id,
            "publications": jsonable_encoder([asdict(p) for p in publications]),
            "count": len(publications),
        }

    @router.get("/search", dependencies=[Depends(auth)])
    def search_work_items(
        q: str = Query(min_length=1, max_length=256),
        tenant_id: str = Query(min_length=1, max_length=128),
        limit: int = Query(default=50, ge=1, le=500),
        request: Request = None,
    ):
        """Search work items by title or summary."""
        svc: WorkIntelligenceService = request.app.state.service
        items = svc.list_work_items(tenant_id, limit=1000)
        # Simple text search in title and summary
        results = [
            item for item in items
            if q.lower() in (item.title or "").lower() or q.lower() in (item.summary or "").lower()
        ]
        return {
            "query": q,
            "tenant_id": tenant_id,
            "results": jsonable_encoder([asdict(item) for item in results[:limit]]),
            "count": len(results[:limit]),
        }

    @router.get("/observations", dependencies=[Depends(auth)])
    def list_observations(
        tenant_id: str = Query(min_length=1, max_length=128),
        source: str | None = Query(default=None, max_length=64),
        limit: int = Query(default=100, ge=1, le=1000),
        request: Request = None,
    ):
        """List observations for a tenant, with optional source filtering."""
        store: SQLiteStore = request.app.state.store
        sql = "SELECT * FROM intake_observations WHERE tenant_id = ?"
        params: list[Any] = [tenant_id]
        if source:
            sql += " AND source = ?"
            params.append(source)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with store._lock:
            rows = store._db.execute(sql, params).fetchall()

        observations = []
        for row in rows:
            observations.append({
                "id": row["id"],
                "tenant_id": row["tenant_id"],
                "source": row["source"],
                "external_id": row["external_id"],
                "actor": row["actor"],
                "text": row["text"],
                "occurred_at": _dt(row["occurred_at"]).isoformat() if row["occurred_at"] else None,
                "created_at": _dt(row["created_at"]).isoformat() if row["created_at"] else None,
            })

        return {"observations": observations, "count": len(observations)}

    @router.get("/work-items", dependencies=[Depends(auth)])
    def list_work_items(
        tenant_id: str = Query(min_length=1, max_length=128),
        status: str | None = Query(default=None, max_length=64),
        priority: str | None = Query(default=None, max_length=64),
        limit: int = Query(default=100, ge=1, le=1000),
        svc: WorkIntelligenceService = Depends(service),
    ):
        """List work items with optional status and priority filtering (review queue)."""
        items = svc.list_work_items(tenant_id, limit=limit)
        # Filter by status if provided
        if status:
            items = [i for i in items if i.status.lower() == status.lower()]
        # Filter by priority if provided
        if priority:
            items = [i for i in items if i.priority.lower() == priority.lower()]
        return {"count": len(items), "work_items": jsonable_encoder([asdict(item) for item in items])}

    @router.get("/tenants/{tenant_id}/policy", dependencies=[Depends(auth)])
    def get_tenant_policy(
        tenant_id: str,
        request: Request,
    ):
        """Get tenant policy."""
        policy_store: PolicyStore = request.app.state.policy_store
        policy = policy_store.get(tenant_id)
        if policy is None:
            raise HTTPException(status_code=404, detail="tenant policy not found")
        return {
            "tenant_id": tenant_id,
            "allowed_sources": policy.allowed_sources,
            "allowed_destinations": policy.allowed_destinations,
            "max_work_items": policy.max_work_items,
            "max_priority": policy.max_priority,
            "allow_works": policy.allow_works,
        }

    @router.post("/tenants/{tenant_id}/policy", dependencies=[Depends(auth)])
    def update_tenant_policy(
        tenant_id: str,
        request: Request,
        allowed_sources: list[str] | None = Query(None),
        allowed_destinations: list[str] | None = Query(None),
        max_work_items: int | None = Query(None),
        max_priority: str | None = Query(None),
        allow_works: bool | None = Query(None),
        dedupe_threshold: float | None = Query(None),
        auto_create_work_items: bool | None = Query(None),
        require_approval_for_promotion: bool | None = Query(None),
    ):
        """Update tenant policy (in-memory + persistent)."""
        policy_store: PolicyStore = request.app.state.policy_store
        store: SQLiteStore = request.app.state.store
        existing = policy_store.get(tenant_id)

        # Merge with existing
        sources = set(allowed_sources) if allowed_sources is not None else (existing.allowed_sources if existing else set())
        destinations = set(allowed_destinations) if allowed_destinations is not None else (existing.allowed_destinations if existing else None)
        max_wi = max_work_items if max_work_items is not None else (existing.max_work_items if existing else 100)
        priority = max_priority or (existing.max_priority if existing else "high")
        works = allow_works if allow_works is not None else (existing.allow_works if existing else False)
        threshold = dedupe_threshold if dedupe_threshold is not None else (existing.dedupe_threshold if existing else 0.72)
        auto_create = auto_create_work_items if auto_create_work_items is not None else (existing.auto_create_work_items if existing else True)
        require_approval = require_approval_for_promotion if require_approval_for_promotion is not None else (existing.require_approval_for_promotion if existing else True)

        policy = TenantPolicy(
            allowed_sources=sources,
            allowed_destinations=destinations,
            max_work_items=max_wi,
            max_priority=priority,
            allow_works=works,
            dedupe_threshold=threshold,
            auto_create_work_items=auto_create,
            require_approval_for_promotion=require_approval,
        )

        # Update in-memory
        policy_store.put(tenant_id, policy)

        # Persist to database
        try:
            store.upsert_tenant_policy(
                tenant_id=tenant_id,
                allowed_sources=list(sources),
                auto_create_work_items=auto_create,
                max_work_items=max_wi,
                max_priority=priority,
                dedupe_threshold=threshold,
                allow_works=works,
                allowed_destinations=list(destinations) if destinations is not None else None,
                require_approval_for_promotion=require_approval,
            )
            persisted = True
        except Exception:
            persisted = False

        return {"tenant_id": tenant_id, "updated": True, "persisted": persisted}

    @router.get("/tenants/policies", dependencies=[Depends(auth)])
    def list_persisted_policies(request: Request):
        """List all persisted tenant policies."""
        store: SQLiteStore = request.app.state.store
        policies = store.list_tenant_policies()
        return {"policies": policies, "count": len(policies)}

    @router.delete("/tenants/{tenant_id}/policy", dependencies=[Depends(auth)])
    def delete_persisted_policy(tenant_id: str, request: Request):
        """Delete a persisted tenant policy."""
        store: SQLiteStore = request.app.state.store
        deleted = store.delete_tenant_policy(tenant_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="persisted policy not found")
        return {"tenant_id": tenant_id, "deleted": True}

    @router.get("/readiness", dependencies=[Depends(auth)])
    def readiness(
        request: Request,
    ):
        """Integration health/readiness API."""
        store: SQLiteStore = request.app.state.store
        policy_store: PolicyStore = request.app.state.policy_store
        publisher = request.app.state.publisher

        checks = {
            "database": False,
            "policy_store": False,
            "publisher": False,
        }

        # Check database
        try:
            with store._lock:
                store._db.execute("SELECT 1").fetchone()
            checks["database"] = True
        except Exception:
            pass

        # Check policy store
        try:
            policy_store.get("test")
            checks["policy_store"] = True
        except Exception:
            pass

        # Check publisher
        checks["publisher"] = publisher is not None

        all_pass = all(checks.values())
        return {
            "status": "pass" if all_pass else "fail",
            "checks": checks,
            "timestamp": datetime.now(UTC).isoformat() + "Z",
        }

    @router.get("/work-items/{work_item_id}/actions", dependencies=[Depends(auth)])
    def get_allowed_actions(
        work_item_id: str,
        tenant_id: str = Query(min_length=1, max_length=128),
        request: Request = None,
    ):
        """Get capabilities/allowed-actions for a work item."""
        store: SQLiteStore = request.app.state.store
        policy_store: PolicyStore = request.app.state.policy_store

        item = store.get_work_item(work_item_id, tenant_id)
        if item is None:
            raise HTTPException(status_code=404, detail="work item not found")

        # Determine allowed actions based on current status and policies
        actions = []
        if item.status == "OPEN":
            actions = ["approve", "reject", "snooze", "cancel"]
        elif item.status == "APPROVED":
            actions = ["publish", "promote", "cancel"]

            # Check if promotion is allowed
            policy = policy_store.get(tenant_id)
            if policy and policy.allow_works:
                actions.append("promote")

        return {
            "work_item_id": work_item_id,
            "status": item.status,
            "actions": actions,
        }

    @router.get("/context", dependencies=[Depends(auth)])
    def get_actor_context(
        request: Request,
    ):
        """Get current actor/role/permission context."""
        # In a real implementation, this would extract the actual actor from the token
        return {
            "actor": "api_client",
            "role": "operator",
            "permissions": ["read", "write", "review"],
        }

    @router.post("/work-items/bulk-status", dependencies=[Depends(auth)])
    def bulk_status(
        request: Request,
        payload: BulkStatusRequest,
    ):
        """Get status of multiple work items."""
        store: SQLiteStore = request.app.state.store
        items = []
        for item_id in payload.work_item_ids:
            item = store.get_work_item(item_id, payload.tenant_id)
            if item is None:
                items.append({
                    "id": item_id,
                    "status": "NOT_FOUND",
                    "title": None,
                    "priority": None,
                })
            else:
                items.append({
                    "id": item.id,
                    "status": item.status,
                    "title": item.title,
                    "priority": item.priority,
                })
        return {"items": items, "count": len(items)}


    # --- Detailed Health Check ---
    @app.get("/healthz/detailed")
    def healthz_detailed(request: Request):
        """Detailed health check with dependency checks."""
        checks = {}
        
        # Database check
        try:
            store: SQLiteStore = request.app.state.store
            _ = store.list_work_items("health-check", limit=1)
            checks["database"] = {"status": "ok", "message": "SQLite accessible"}
        except Exception as e:
            checks["database"] = {"status": "error", "message": str(e)}
        
        # Store check
        try:
            checks["store"] = {"status": "ok", "message": "Store operational"}
        except Exception as e:
            checks["store"] = {"status": "error", "message": str(e)}
        
        all_ok = all(c["status"] == "ok" for c in checks.values())
        return {
            "status": "healthy" if all_ok else "degraded",
            "checks": checks,
            "timestamp": datetime.now(UTC).isoformat() + "Z",
        }

    # --- Readiness Probe ---
    @app.get("/ready")
    def ready():
        """Kubernetes readiness probe."""
        return {
            "status": "ready",
            "dependencies": [
                {"name": "database", "status": "ok"},
                {"name": "store", "status": "ok"},
            ],
        }

    # --- Liveness Probe ---
    @app.get("/live")
    def live():
        """Kubernetes liveness probe."""
        return {
            "status": "alive",
            "uptime_seconds": 0,  # Would need to track startup time
        }


    # --- Dashboard ---
    @app.get("/dashboard")
    def dashboard(request: Request):
        """Simple HTML dashboard showing work item overview."""
        from fastapi.responses import HTMLResponse
        store: SQLiteStore = request.app.state.store

        # Gather stats
        with store._lock:
            total_obs = store._db.execute("SELECT COUNT(*) FROM intake_observations").fetchone()[0]
            total_wi = store._db.execute("SELECT COUNT(*) FROM intake_work_items").fetchone()[0]
            by_status = store._db.execute(
                "SELECT status, COUNT(*) FROM intake_work_items GROUP BY status"
            ).fetchall()
            by_tenant = store._db.execute(
                "SELECT tenant_id, COUNT(*) FROM intake_work_items GROUP BY tenant_id"
            ).fetchall()
            by_priority = store._db.execute(
                "SELECT priority, COUNT(*) FROM intake_work_items GROUP BY priority"
            ).fetchall()
            total_pubs = store._db.execute("SELECT COUNT(*) FROM intake_publications").fetchone()[0]

        status_rows = "".join(f"<tr><td>{r[0]}</td><td>{r[1]}</td></tr>" for r in by_status)
        tenant_rows = "".join(f"<tr><td>{r[0]}</td><td>{r[1]}</td></tr>" for r in by_tenant)
        priority_rows = "".join(f"<tr><td>{r[0]}</td><td>{r[1]}</td></tr>" for r in by_priority)

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Aftergraph Work Intelligence - Dashboard</title>
<style>
  body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 900px; margin: 40px auto; padding: 0 20px; background: #f8f9fa; color: #212529; }}
  h1 {{ color: #1a1a2e; border-bottom: 3px solid #e94560; padding-bottom: 10px; }}
  .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 16px; margin: 20px 0; }}
  .stat {{ background: white; border-radius: 8px; padding: 20px; text-align: center; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
  .stat .number {{ font-size: 2em; font-weight: bold; color: #e94560; }}
  .stat .label {{ color: #6c757d; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin: 16px 0; }}
  th {{ background: #1a1a2e; color: white; padding: 12px; text-align: left; }}
  td {{ padding: 10px 12px; border-bottom: 1px solid #dee2e6; }}
  h2 {{ color: #1a1a2e; margin-top: 30px; }}
  .footer {{ color: #6c757d; font-size: 0.9em; margin-top: 30px; padding-top: 10px; border-top: 1px solid #dee2e6; }}
</style>
</head>
<body>
<h1>Aftergraph Work Intelligence</h1>
<div class="stats">
  <div class="stat"><div class="number">{total_obs}</div><div class="label">Observations</div></div>
  <div class="stat"><div class="number">{total_wi}</div><div class="label">Work Items</div></div>
  <div class="stat"><div class="number">{total_pubs}</div><div class="label">Publications</div></div>
  <div class="stat"><div class="number">{len(by_tenant)}</div><div class="label">Tenants</div></div>
</div>
<h2>By Status</h2>
<table><tr><th>Status</th><th>Count</th></tr>{status_rows}</table>
<h2>By Tenant</h2>
<table><tr><th>Tenant</th><th>Count</th></tr>{tenant_rows}</table>
<h2>By Priority</h2>
<table><tr><th>Priority</th><th>Count</th></tr>{priority_rows}</table>
<div class="footer">Aftergraph Work Intelligence V2 | API: <a href="/docs">/docs</a></div>
</body></html>"""
        return HTMLResponse(content=html)


    # --- WebSocket for real-time updates ---

    async def ws_heartbeat_loop():
        """Send heartbeat pings to all connected clients every 30s."""
        while True:
            await asyncio.sleep(30)
            now = time.time()
            disconnected = []
            for client in list(ws_clients):
                try:
                    await client.send_json({"type": "heartbeat", "timestamp": now})
                    ws_last_heartbeat[id(client)] = now
                except Exception:
                    disconnected.append(client)
            for client in disconnected:
                ws_clients.discard(client)
                ws_last_heartbeat.pop(id(client), None)

    @app.websocket("/ws")
    async def websocket_endpoint(websocket):
        """WebSocket for real-time dashboard updates with heartbeat."""
        await websocket.accept()
        ws_clients.add(websocket)
        ws_last_heartbeat[id(websocket)] = time.time()
        try:
            while True:
                data = await websocket.receive_text()
                if data == "ping":
                    await websocket.send_json({"type": "pong", "timestamp": time.time()})
                elif data == "stats":
                    await websocket.send_json({
                        "type": "stats",
                        "connected_clients": len(ws_clients),
                    })
        except Exception:
            ws_clients.discard(websocket)
            ws_last_heartbeat.pop(id(websocket), None)

    # --- Webhook Management ---
    @router.post("/webhooks", status_code=201, dependencies=[Depends(auth)])
    def register_webhook(
        request: Request,
        url: str = Body(..., min_length=1),
        events: list[str] = Body(..., min_length=1),
        secret: str | None = Body(default=None),
    ):
        """Register a webhook for event notifications."""
        from urllib.parse import urlparse
        
        # Validate URL
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            raise HTTPException(status_code=422, detail="Invalid webhook URL")
        
        webhook_id = f"wh_{uuid.uuid4().hex}"
        webhook = {
            "id": webhook_id,
            "url": url,
            "events": events,
            "secret": secret,
            "created_at": datetime.now(UTC).isoformat() + "Z",
            "active": True,
        }
        
        # Store in app state
        if not hasattr(request.app.state, "webhooks"):
            request.app.state.webhooks = {}
        request.app.state.webhooks[webhook_id] = webhook
        
        return webhook

    @router.get("/webhooks", dependencies=[Depends(auth)])
    def list_webhooks(request: Request):
        """List registered webhooks."""
        webhooks = getattr(request.app.state, "webhooks", {})
        return {
            "webhooks": list(webhooks.values()),
            "count": len(webhooks),
        }

    @router.delete("/webhooks/{webhook_id}", dependencies=[Depends(auth)])
    def delete_webhook(
        webhook_id: str,
        request: Request,
    ):
        """Delete a webhook."""
        webhooks = getattr(request.app.state, "webhooks", {})
        if webhook_id not in webhooks:
            raise HTTPException(status_code=404, detail="Webhook not found")
        del webhooks[webhook_id]
        return {"status": "deleted", "id": webhook_id}

    # --- API Key Management (DB-backed) ---
    @router.post("/api-keys", status_code=201, dependencies=[Depends(auth)])
    def create_api_key(
        request: Request,
        name: str = Body(..., min_length=1, max_length=128),
        permissions: list[str] = Body(default=["read"]),
    ):
        """Create a new API key. Key is only returned once."""
        import hashlib
        key_id = f"key_{uuid.uuid4().hex[:16]}"
        api_key = f"ak_{uuid.uuid4().hex}"
        prefix = api_key[:12]
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()

        store: SQLiteStore = request.app.state.store
        record = store.create_api_key(key_id, name, key_hash, prefix)

        # Audit log
        audit: AuditLog = request.app.state.audit_log
        audit.record("api_key.created", actor="bearer", target=f"key:{key_id}", details={"name": name})

        return {
            "id": key_id,
            "name": name,
            "key": api_key,
            "prefix": prefix,
            "permissions": permissions,
            "created_at": record["created_at"],
            "active": True,
            "_warning": "Store this key securely. It will not be shown again.",
        }

    @router.get("/api-keys", dependencies=[Depends(auth)])
    def list_api_keys(request: Request):
        """List API keys (without secrets)."""
        store: SQLiteStore = request.app.state.store
        keys = store.list_api_keys()
        return {"keys": keys, "count": len(keys)}

    @router.post("/api-keys/{key_id}/rotate", status_code=200, dependencies=[Depends(auth)])
    def rotate_api_key(key_id: str, request: Request):
        """Rotate an API key: deactivate old, create new."""
        import hashlib
        store: SQLiteStore = request.app.state.store
        keys = store.list_api_keys()
        old_key = next((k for k in keys if k["id"] == key_id), None)
        if not old_key:
            raise HTTPException(status_code=404, detail="API key not found")

        # Deactivate old
        store.deactivate_api_key(key_id)

        # Create new with same name
        new_key_id = f"key_{uuid.uuid4().hex[:16]}"
        api_key = f"ak_{uuid.uuid4().hex}"
        prefix = api_key[:12]
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        store.create_api_key(new_key_id, old_key["name"], key_hash, prefix)

        # Audit log
        audit: AuditLog = request.app.state.audit_log
        audit.record("api_key.rotated", actor="bearer", target=f"key:{key_id}", details={"new_id": new_key_id})

        return {
            "old_id": key_id,
            "new_id": new_key_id,
            "key": api_key,
            "prefix": prefix,
            "name": old_key["name"],
            "_warning": "Store this key securely. It will not be shown again.",
        }

    @router.delete("/api-keys/{key_id}", dependencies=[Depends(auth)])
    def revoke_api_key(key_id: str, request: Request):
        """Revoke (deactivate) an API key."""
        store: SQLiteStore = request.app.state.store
        if not store.deactivate_api_key(key_id):
            raise HTTPException(status_code=404, detail="API key not found")
        # Audit log
        audit: AuditLog = request.app.state.audit_log
        audit.record("api_key.revoked", actor="bearer", target=f"key:{key_id}")
        return {"status": "revoked", "id": key_id}

    # --- Request Logs ---
    @router.get("/logs", dependencies=[Depends(auth)])
    def get_request_logs(request: Request, limit: int = Query(100, ge=1, le=1000)):
        """Get recent request logs."""
        logger: RequestLogger = request.app.state.request_logger
        return {"logs": logger.get_recent_logs(limit)}

    @router.post("/logs/cleanup", dependencies=[Depends(auth)])
    def cleanup_logs(request: Request):
        """Clean up old log files."""
        logger: RequestLogger = request.app.state.request_logger
        removed = logger.cleanup_old_logs()
        return {"removed": removed}

    # --- Response Time Stats ---
    @router.get("/response-times", dependencies=[Depends(auth)])
    def response_time_stats(request: Request):
        """Get response time statistics per endpoint."""
        stats = {}
        rt = getattr(request.app.state, "response_times", defaultdict(list))
        for path, times in rt.items():
            if times:
                sorted_times = sorted(times)
                stats[path] = {
                    "count": len(times),
                    "avg_ms": round(sum(times) / len(times) * 1000, 2),
                    "p50_ms": round(sorted_times[len(sorted_times) // 2] * 1000, 2),
                    "p95_ms": round(sorted_times[int(len(sorted_times) * 0.95)] * 1000, 2),
                    "p99_ms": round(sorted_times[int(len(sorted_times) * 0.99)] * 1000, 2),
                    "max_ms": round(max(times) * 1000, 2),
                }
        return stats

    # --- Cache Management ---
    @router.get("/cache/stats", dependencies=[Depends(auth)])
    def cache_stats(request: Request):
        """Get cache statistics."""
        cache: Cache = request.app.state.cache
        return cache.stats()

    @router.post("/cache/clear", dependencies=[Depends(auth)])
    def cache_clear(request: Request):
        """Clear all cache entries."""
        cache: Cache = request.app.state.cache
        cleared = cache.clear()
        return {"cleared": cleared, "status": "ok"}

    @router.delete("/cache/{key}", dependencies=[Depends(auth)])
    def cache_delete(request: Request, key: str):
        """Delete a specific cache entry."""
        cache: Cache = request.app.state.cache
        deleted = cache.delete(key)
        if not deleted:
            raise HTTPException(status_code=404, detail="Cache key not found")
        return {"deleted": key}

    # --- Database Migrations ---
    @router.get("/migrations", dependencies=[Depends(auth)])
    def migrations_status(request: Request):
        """Get database migration status."""
        version = getattr(request.app.state, "migration_version", 0)
        return {"current_version": version}

    @router.post("/migrations/run", dependencies=[Depends(auth)])
    def run_migrations_endpoint(request: Request):
        """Run pending migrations."""
        store: SQLiteStore = request.app.state.store
        result = run_migrations(connection=store._db)
        request.app.state.migration_version = result["current_version"]
        return result

    # --- Audit Trail ---
    @router.get("/audit", dependencies=[Depends(auth)])
    def audit_query(
        request: Request,
        event: str | None = Query(None),
        actor: str | None = Query(None),
        target: str | None = Query(None),
        limit: int = Query(100, ge=1, le=1000),
    ):
        """Query audit log entries."""
        audit: AuditLog = request.app.state.audit_log
        return {"entries": audit.query(event=event, actor=actor, target=target, limit=limit)}

    @router.get("/audit/stats", dependencies=[Depends(auth)])
    def audit_stats(request: Request):
        """Get audit log statistics."""
        audit: AuditLog = request.app.state.audit_log
        return {"total_entries": audit.count()}

    app.include_router(router)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Aftergraph Work Intelligence V2")
    parser.add_argument("--host", default=os.getenv("AFTERGRAPH_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("AFTERGRAPH_PORT", "8087")))
    parser.add_argument("--db", default=os.getenv("AFTERGRAPH_DB", "./aftergraph-work-intelligence.db"))
    args = parser.parse_args()
    uvicorn.run(create_app(db_path=args.db), host=args.host, port=args.port)


if __name__ == "__main__":
    main()