from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from .api import create_app as create_core_app
from .policy import PolicyStore
from .publishers import Publisher

_DEFAULT_CORS_ORIGINS = (
    "https://work-intelligence.rendetalje.dk",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3001",
)
_PUBLIC_PATHS = {
    "/healthz",
    "/docs",
    "/docs/oauth2-redirect",
    "/openapi.json",
    "/redoc",
}
_GITHUB_WEBHOOK_PATH = "/v1/webhook/github"
_CORS_HEADERS = {
    "access-control-allow-origin",
    "access-control-allow-credentials",
    "access-control-allow-methods",
    "access-control-allow-headers",
    "access-control-expose-headers",
    "access-control-max-age",
}


def _parse_origins(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return _DEFAULT_CORS_ORIGINS
    values = tuple(value.strip().rstrip("/") for value in raw.split(",") if value.strip())
    if "*" in values:
        raise ValueError("AFTERGRAPH_CORS_ORIGINS must not contain '*' in secure mode")
    return values


def _secure_headers(response: Response) -> None:
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; object-src 'none'; "
        "form-action 'self'; script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
        "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
        "img-src 'self' data: https:; connect-src 'self'"
    )


class ProductionSecurityMiddleware(BaseHTTPMiddleware):
    """Fail-closed boundary for the public deployment.

    The legacy core intentionally supports unauthenticated local development. This
    middleware is the production boundary: protected HTTP routes require a full
    bearer token or full API-key hash match, CORS is allowlisted, and baseline
    browser security headers are emitted on every response.
    """

    def __init__(
        self,
        app,
        *,
        api_token: str | None,
        cors_origins: Iterable[str],
    ) -> None:
        super().__init__(app)
        self.api_token = api_token
        self.cors_origins = frozenset(origin.rstrip("/") for origin in cors_origins)

    def _api_key_valid(self, request: Request, candidate: str | None) -> bool:
        if not candidate or not candidate.startswith("ak_") or len(candidate) < 16:
            return False
        store = getattr(request.app.state, "store", None)
        if store is None:
            return False

        prefix = candidate[:12]
        candidate_hash = hashlib.sha256(candidate.encode()).hexdigest()
        try:
            with store._lock:
                row = store._db.execute(
                    "SELECT key_hash, active FROM api_keys WHERE prefix = ?",
                    (prefix,),
                ).fetchone()
                if row is None or not row["active"]:
                    return False
                if not hmac.compare_digest(str(row["key_hash"]), candidate_hash):
                    return False
                store._db.execute(
                    "UPDATE api_keys SET last_used_at = ? WHERE prefix = ?",
                    (datetime.now(UTC).isoformat(), prefix),
                )
            return True
        except Exception:
            return False

    def _authorized(self, request: Request) -> bool:
        authorization = request.headers.get("authorization")
        if self.api_token and authorization:
            expected = f"Bearer {self.api_token}"
            if hmac.compare_digest(authorization, expected):
                return True
        return self._api_key_valid(request, request.headers.get("x-api-key"))

    def _apply_cors(self, response: Response, origin: str | None) -> None:
        for header in tuple(response.headers.keys()):
            if header.lower() in _CORS_HEADERS:
                del response.headers[header]
        if origin and origin.rstrip("/") in self.cors_origins:
            response.headers["Access-Control-Allow-Origin"] = origin.rstrip("/")
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Vary"] = "Origin"

    def _finalize(self, response: Response, origin: str | None, path: str) -> Response:
        self._apply_cors(response, origin)
        _secure_headers(response)
        if path.startswith("/v1/") or path in {"/dashboard", "/healthz"}:
            response.headers["Cache-Control"] = "no-store"
        return response

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        origin = request.headers.get("origin")
        normalized_origin = origin.rstrip("/") if origin else None

        if request.method == "OPTIONS" and origin:
            if normalized_origin not in self.cors_origins:
                return self._finalize(
                    JSONResponse(status_code=403, content={"detail": "CORS origin denied"}),
                    origin,
                    path,
                )
            response = Response(status_code=204)
            response.headers["Access-Control-Allow-Origin"] = normalized_origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = (
                "Authorization, Content-Type, X-API-Key, X-Request-ID"
            )
            response.headers["Access-Control-Max-Age"] = "600"
            response.headers["Vary"] = "Origin"
            _secure_headers(response)
            return response

        if path == _GITHUB_WEBHOOK_PATH:
            if not os.getenv("AFTERGRAPH_GITHUB_WEBHOOK_SECRET"):
                return self._finalize(
                    JSONResponse(
                        status_code=503,
                        content={"detail": "GitHub webhook secret is not configured"},
                    ),
                    origin,
                    path,
                )
        elif path not in _PUBLIC_PATHS and not self._authorized(request):
            return self._finalize(
                JSONResponse(
                    status_code=401,
                    content={"detail": "invalid or missing credentials"},
                    headers={"WWW-Authenticate": "Bearer"},
                ),
                origin,
                path,
            )

        response = await call_next(request)
        return self._finalize(response, origin, path)


def create_app(
    db_path: str | Path = "./aftergraph-work-intelligence.db",
    api_token: str | None = None,
    publisher: Publisher | None = None,
    policy_store: PolicyStore | None = None,
    evidence_secret: str | None = None,
) -> FastAPI:
    """Create the public/production application with fail-closed security."""
    resolved_token = api_token if api_token is not None else os.getenv("AFTERGRAPH_API_TOKEN")
    app = create_core_app(
        db_path=db_path,
        api_token=resolved_token,
        publisher=publisher,
        policy_store=policy_store,
        evidence_secret=evidence_secret,
    )
    app.add_middleware(
        ProductionSecurityMiddleware,
        api_token=resolved_token,
        cors_origins=_parse_origins(os.getenv("AFTERGRAPH_CORS_ORIGINS")),
    )
    return app


__all__ = ["ProductionSecurityMiddleware", "create_app"]
