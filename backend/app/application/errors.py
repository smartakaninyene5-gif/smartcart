"""Typed application errors. API maps each to a stable error code."""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    code = "APP_ERROR"
    http_status = 400

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationFailure(AppError):
    code = "VALIDATION_ERROR"
    http_status = 422


class NotFound(AppError):
    code = "NOT_FOUND"
    http_status = 404


class ConsentRequired(AppError):
    code = "EXPLICIT_CONSENT_REQUIRED"
    http_status = 400


class SnapshotMismatch(AppError):
    code = "SNAPSHOT_MISMATCH"
    http_status = 409


class QuoteChanged(AppError):
    code = "QUOTE_CHANGED"
    http_status = 409


class GuardDenied(AppError):
    code = "GUARD_DENIED"
    http_status = 409

    def __init__(self, message: str, *, decision: Any = None, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, details=details)
        self.decision = decision


class ApprovalNotValid(AppError):
    code = "APPROVAL_NOT_VALID"
    http_status = 409


class QuoteNotApprovable(AppError):
    code = "QUOTE_NOT_APPROVABLE"
    http_status = 409


class ApprovalExpired(AppError):
    code = "APPROVAL_EXPIRED"
    http_status = 409


class DuplicateCapture(AppError):
    code = "DUPLICATE_CAPTURE"
    http_status = 409


class IdempotencyInProgress(AppError):
    code = "IDEMPOTENCY_IN_PROGRESS"
    http_status = 409


class PaymentNotConfigured(AppError):
    code = "PAYPAL_NOT_CONFIGURED"
    http_status = 503


class PaymentNotApproved(AppError):
    code = "PAYMENT_NOT_APPROVED"
    http_status = 409


class PaymentProviderError(AppError):
    code = "PAYMENT_PROVIDER_ERROR"
    http_status = 502


class WebhookRejected(AppError):
    code = "WEBHOOK_REJECTED"
    http_status = 400


class SourceFailure(AppError):
    code = "SOURCE_FAILURE"
    http_status = 503


class AiUnavailable(AppError):
    """Gemini could not be used (auth, rate limit, malformed, model missing)."""

    code = "AI_UNAVAILABLE"
    http_status = 502


class AiNotConfigured(AppError):
    code = "AI_NOT_CONFIGURED"
    http_status = 503


class ShoppingState(AppError):
    """Shopping workflow used out of order (e.g. confirm before understand)."""

    code = "SHOPPING_STATE_INVALID"
    http_status = 409
