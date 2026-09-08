"""Custom exceptions for Wie."""

from __future__ import annotations


class WorkIntelligenceError(Exception):
    """Base exception for all Work Intelligence errors."""

    status_code: int = 500
    detail: str = "Internal server error"

    def __init__(self, detail: str | None = None):
        self.detail = detail or self.__class__.detail
        super().__init__(self.detail)


class ObservationError(WorkIntelligenceError):
    """Error processing an observation."""
    status_code: int = 422
    detail: str = "Failed to process observation"


class DeduplicationError(WorkIntelligenceError):
    """Error during deduplication."""
    status_code: int = 422
    detail: str = "Deduplication failed"


class PolicyViolationError(WorkIntelligenceError):
    """Tenant policy violation."""
    status_code: int = 403
    detail: str = "Policy violation"


class PromotioinError(WorkIntelligenceError):
    """Error promoting work item."""
    status_code: int = 422
    detail: str = "Promotion failed"


class PublicationError(WorkIntelligenceError):
    """Error publishing to destination."""
    status_code: int = 502
    detail: str = "Publication failed"


class AuthenticationError(WorkIntelligenceError):
    """Authentication failed."""
    status_code: int = 401
    detail: str = "Authentication failed"


class AuthorizationError(WorkIntelligenceError):
    """Authorization failed."""
    status_code: int = 403
    detail: str = "Insufficient permissions"


class NotFoundError(WorkIntelligenceError):
    """Resource not found."""
    status_code: int = 404
    detail: str = "Resource not found"


class RateLimitError(WorkIntelligenceError):
    """Rate limit exceeded."""
    status_code: int = 429
    detail: str = "Rate limit exceeded"


class ValidationError(WorkIntelligenceError):
    """Input validation failed."""
    status_code: int = 422
    detail: str = "Validation error"
