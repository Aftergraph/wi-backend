"""Configurable request/response body logging middleware."""

from __future__ import annotations

import json
import logging

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("aftergraph.work-intelligence.body_log")


class BodyLoggingMiddleware(BaseHTTPMiddleware):
    """Logs request/response bodies for debugging.

    Configure via env vars:
    - AFTERGRAPH_LOG_REQUEST_BODY=true  (default: false)
    - AFTERGRAPH_LOG_RESPONSE_BODY=true (default: false)
    - AFTERGRAPH_BODY_LOG_MAX_CHARS=1000 (max chars to log per body)
    """

    def __init__(
        self,
        app,
        log_request_body: bool = False,
        log_response_body: bool = False,
        max_chars: int = 1000,
        skip_paths: frozenset[str] | None = None,
    ):
        super().__init__(app)
        self.log_request_body = log_request_body
        self.log_response_body = log_response_body
        self.max_chars = max_chars
        self.skip_paths = skip_paths or frozenset({"/healthz", "/metrics", "/ws"})

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Skip logging for certain paths
        if request.url.path in self.skip_paths:
            return await call_next(request)

        # Log request body
        request_body = None
        if self.log_request_body and request.method in ("POST", "PUT", "PATCH"):
            try:
                raw = await request.body()
                request_body = raw[:self.max_chars].decode("utf-8", errors="replace")
                # Try to parse as JSON for cleaner logging
                try:
                    parsed = json.loads(request_body)
                    request_body = json.dumps(parsed, indent=2)[:self.max_chars]
                except (json.JSONDecodeError, ValueError):
                    pass
            except Exception:
                request_body = "<read error>"

        # Process request
        response = await call_next(request)

        # Log response body
        response_body = None
        if self.log_response_body:
            try:
                # Note: can't read response body in middleware easily
                # This is a limitation of Starlette's middleware design
                response_body = "<streaming>"
            except Exception:
                response_body = "<read error>"

        # Log everything
        log_data = {
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
        }
        if request_body:
            log_data["request_body"] = request_body
        if response_body and response_body != "<streaming>":
            log_data["response_body"] = response_body

        logger.info(
            "Request/Response",
            extra=log_data,
        )

        return response
