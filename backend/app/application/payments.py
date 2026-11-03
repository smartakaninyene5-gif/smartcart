"""Payment use cases: create order, capture, reconcile webhooks.

Every money-moving path starts from a persisted quote and a revalidated
approval. The client never supplies an amount. A successful capture changes
payment state only; retailer purchase state stays ``unknown`` until retailer
evidence exists.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from app.application.approvals import RevalidationOutcome, revalidate_approval
from app.application.common import limits_from_policy
from app.application.errors import (
    ApprovalNotValid,
    DuplicateCapture,
    IdempotencyInProgress,
    NotFound,
    PaymentNotApproved,
    PaymentNotConfigured,
    PaymentProviderError,
    WebhookRejected,
)
from app.domain.enums import Actor, ApprovalStatus, GuardOutcome, RetailerPurchaseState
from app.domain.guard import GuardContext, GuardPolicy, evaluate
from app.domain.models import Quote
from app.domain.snapshot import canonical_json
from app.journal import JournalEvents, JournalWriter
from app.persistence.base import utcnow
from app.persistence.repositories import StoreError
from app.persistence.repositories import (
    ApprovalRepository,
    IdempotencyRepository,
    IntentRepository,
    PaymentRepository,
    QuoteRepository,
    TransactionRepository,
)
from app.ports import PaymentGateway
from app.security.idempotency import capture_key, create_order_key, expiry, fingerprint

CAPTURE_DISCLAIMER = (
    "PayPal payment captured. This is not proof of a retailer purchase: "
    "Smartcart has no retailer transaction evidence for this basket."
)


@dataclass
class PaymentOutcome:
    payment: Any
    quote: Quote
    revalidation: RevalidationOutcome
    replayed: bool = False


@dataclass
class CaptureOutcome:
    capture: Any
    payment: Any
    quote: Quote
    retailer_purchase_state: str
    disclaimer: str
    replayed: bool = False


def _require_gateway(gateway: PaymentGateway | None, journal: JournalWriter, *, user_id: str, subject: str) -> PaymentGateway:
    if gateway is None:
        journal.record(
            JournalEvents.PAYMENT_PREPARATION_BLOCKED,
            actor=Actor.SYSTEM,
            subject_type="payment",
            subject_id=subject,
            user_id=user_id,
            payload={"reason": "paypal_not_configured"},
        )
        raise PaymentNotConfigured(
            "PayPal is not configured. Set SMARTCART_PAYPAL_CLIENT_ID and "
            "SMARTCART_PAYPAL_CLIENT_SECRET to enable the sandbox payment flow."
        )
    return gateway


def prepare_payment(
    session: Session,
    *,
    user_id: str,
    approval_id: str,
    gateway: PaymentGateway | None,
    policy: GuardPolicy,
    now: datetime,
    environment: str,
    correlation_id: str | None = None,
) -> PaymentOutcome:
    journal = JournalWriter(session, environment=environment)
    gateway = _require_gateway(gateway, journal, user_id=user_id, subject=approval_id)

    revalidation = revalidate_approval(
        session,
        user_id=user_id,
        approval_id=approval_id,
        policy=policy,
        now=now,
        environment=environment,
        correlation_id=correlation_id,
    )
    if not revalidation.allowed:
        raise ApprovalNotValid(
            "approval failed revalidation; request a fresh quote and approval",
            details={"reasons": list(revalidation.reasons)},
        )
    quote = revalidation.quote

    key = create_order_key(user_id, approval_id, quote.snapshot_hash)
    request_fingerprint = fingerprint(
        {
            "approval_id": approval_id,
            "quote_id": quote.quote_id,
            "snapshot_hash": quote.snapshot_hash,
            "amount": str(quote.grand_total),
            "currency": quote.currency,
            "environment": gateway.environment,
        }
    )
    idempotency = IdempotencyRepository(session)
    row, is_new = idempotency.begin(
        key=key,
        scope="paypal.create_order",
        fingerprint=request_fingerprint,
        expires_at=expiry(now),
    )
    if not is_new:
        if row.status == "completed" and row.response_ref:
            payment = PaymentRepository(session).get(user_id, row.response_ref)
            if payment is not None:
                return PaymentOutcome(payment=payment, quote=quote, revalidation=revalidation, replayed=True)
        if row.status == "in_progress":
            raise IdempotencyInProgress("a payment order is already being created for this approval")
        row.status = "in_progress"

    try:
        prepared = gateway.create_order(quote, key)
    except Exception as exc:  # adapter errors are opaque by design
        idempotency.fail(key, type(exc).__name__)
        journal.record(
            JournalEvents.PAYMENT_ORDER_FAILED,
            actor=Actor.SYSTEM,
            subject_type="approval",
            subject_id=approval_id,
            user_id=user_id,
            correlation_id=correlation_id,
            payload={"error": type(exc).__name__},
        )
        raise PaymentProviderError(f"PayPal order creation failed: {type(exc).__name__}") from exc

    if prepared.amount != quote.grand_total or prepared.currency != quote.currency:
        idempotency.fail(key, "amount_mismatch")
        journal.record(
            JournalEvents.PAYMENT_ORDER_FAILED,
            actor=Actor.SYSTEM,
            subject_type="approval",
            subject_id=approval_id,
            user_id=user_id,
            correlation_id=correlation_id,
            payload={"error": "amount_mismatch"},
        )
        raise PaymentProviderError("PayPal returned an order whose amount does not match the quote")

    payment = PaymentRepository(session).add_order(
        user_id=user_id,
        approval_id=approval_id,
        quote_id=quote.quote_id,
        provider_order_id=prepared.provider_order_id,
        status=prepared.status,
        amount=prepared.amount,
        currency=prepared.currency,
        approve_url=prepared.approve_url,
        environment=prepared.environment,
        raw={k: v for k, v in prepared.raw.items() if k in {"id", "status", "intent"}},
    )
    idempotency.complete(key, payment.id)
    IntentRepository(session).set_status(user_id, quote.quote_id, "payment_pending")

    approve_host = urlsplit(prepared.approve_url).netloc if prepared.approve_url else None
    journal.record(
        JournalEvents.PAYMENT_ORDER_CREATED,
        actor=Actor.SYSTEM,
        subject_type="payment",
        subject_id=payment.id,
        user_id=user_id,
        correlation_id=correlation_id,
        payload={
            "provider": "paypal",
            "provider_order_id": prepared.provider_order_id,
            "status": prepared.status,
            "amount": str(prepared.amount),
            "currency": prepared.currency,
            "environment": prepared.environment,
            "approve_url_host": approve_host,
            "quote_id": quote.quote_id,
            "snapshot_hash": quote.snapshot_hash,
        },
    )
    return PaymentOutcome(payment=payment, quote=quote, revalidation=revalidation)


def _mark_captured(
    session: Session,
    *,
    payment: Any,
    quote: Quote,
    approval_id: str,
    provider_capture_id: str,
    amount,
    currency: str,
    now: datetime,
    environment: str,
    correlation_id: str | None,
    source: str,
) -> CaptureOutcome:
    payments = PaymentRepository(session)
    try:
        capture = payments.add_capture(
            user_id=payment.user_id,
            payment_order_id=payment.id,
            quote_id=quote.quote_id,
            snapshot_hash=quote.snapshot_hash,
            provider_capture_id=provider_capture_id,
            status="captured",
            amount=amount,
            currency=currency,
        )
    except StoreError:
        # Duplicate capture for the same quote + snapshot: deterministic,
        # no unrelated rollback (add_capture uses a savepoint). Map to the
        # stable application error so the API returns 409 DUPLICATE_CAPTURE.
        raise DuplicateCapture(
            "this quote and snapshot has already been captured",
            details={"quote_id": quote.quote_id},
        ) from None
    approval = ApprovalRepository(session).get(payment.user_id, approval_id)
    if approval is not None and approval.consumed_at is None:
        approval.consumed_at = now
        approval.status = ApprovalStatus.CONSUMED.value
    payments.set_status(payment, "captured")
    IntentRepository(session).set_status(payment.user_id, quote.quote_id, "paid")
    TransactionRepository(session).add_verified_event(
        user_id=payment.user_id,
        provider="paypal",
        event_type="PAYMENT.CAPTURE.COMPLETED",
        provider_event_id=provider_capture_id,
        payment_order_id=payment.id,
        signature_verified=False,
        payload={"source": source, "amount": str(amount), "currency": currency},
        retailer_purchase_state=RetailerPurchaseState.UNKNOWN.value,
        note="capture recorded from API response; retailer purchase not confirmed",
    )
    journal = JournalWriter(session, environment=environment)
    journal.record(
        JournalEvents.PAYMENT_CAPTURED,
        actor=Actor.EXTERNAL,
        subject_type="payment",
        subject_id=payment.id,
        user_id=payment.user_id,
        correlation_id=correlation_id,
        payload={
            "provider": "paypal",
            "provider_capture_id": provider_capture_id,
            "amount": str(amount),
            "currency": currency,
            "environment": environment,
            "quote_id": quote.quote_id,
            "snapshot_hash": quote.snapshot_hash,
            "payment_state": "captured",
            "retailer_purchase_state": RetailerPurchaseState.UNKNOWN.value,
            "source": source,
            "disclaimer": CAPTURE_DISCLAIMER,
        },
    )
    return CaptureOutcome(
        capture=capture,
        payment=payment,
        quote=quote,
        retailer_purchase_state=RetailerPurchaseState.UNKNOWN.value,
        disclaimer=CAPTURE_DISCLAIMER,
    )


def capture_payment(
    session: Session,
    *,
    user_id: str,
    payment_id: str,
    gateway: PaymentGateway | None,
    policy: GuardPolicy,
    now: datetime,
    environment: str,
    correlation_id: str | None = None,
) -> CaptureOutcome:
    journal = JournalWriter(session, environment=environment)
    gateway = _require_gateway(gateway, journal, user_id=user_id, subject=payment_id)
    payments = PaymentRepository(session)
    payment = payments.get(user_id, payment_id)
    if payment is None:
        raise NotFound(f"payment order {payment_id} not found")

    if payment.status == "captured":
        existing = payments.capture_for_order(payment.id)
        if existing is not None:
            quote = QuoteRepository(session).to_domain(QuoteRepository(session).get(user_id, payment.quote_id))
            return CaptureOutcome(
                capture=existing,
                payment=payment,
                quote=quote,
                retailer_purchase_state=existing.retailer_purchase_state,
                disclaimer=CAPTURE_DISCLAIMER,
                replayed=True,
            )

    revalidation = revalidate_approval(
        session,
        user_id=user_id,
        approval_id=payment.approval_id,
        policy=policy,
        now=now,
        environment=environment,
        correlation_id=correlation_id,
    )
    if not revalidation.allowed:
        raise ApprovalNotValid(
            "approval is no longer valid; capture refused",
            details={"reasons": list(revalidation.reasons)},
        )
    quote = revalidation.quote

    provider = gateway.get_order(payment.provider_order_id)

    if provider.status == "captured":
        if provider.capture_id is None or provider.amount is None or provider.currency is None:
            raise PaymentProviderError("PayPal says this order is captured but returned no capture details")
        if provider.amount != quote.grand_total or provider.currency != quote.currency:
            journal.failure(
                error_code="AMOUNT_MISMATCH",
                message="captured provider amount does not match the approved quote",
                subject_type="payment",
                subject_id=payment.id,
                user_id=user_id,
                correlation_id=correlation_id,
            )
            raise PaymentProviderError("captured amount does not match the approved quote")
        return _mark_captured(
            session,
            payment=payment,
            quote=quote,
            approval_id=payment.approval_id,
            provider_capture_id=provider.capture_id,
            amount=provider.amount,
            currency=provider.currency,
            now=now,
            environment=environment,
            correlation_id=correlation_id,
            source="reconciliation",
        )

    if provider.status != "approved":
        journal.record(
            JournalEvents.CAPTURE_BLOCKED,
            actor=Actor.SYSTEM,
            subject_type="payment",
            subject_id=payment.id,
            user_id=user_id,
            correlation_id=correlation_id,
            payload={"reason": "provider_order_not_approved", "provider_status": provider.status},
        )
        raise PaymentNotApproved(
            f"PayPal order status is {provider.status}; the user must approve it in PayPal first"
        )

    duplicate = payments.capture_exists(quote.quote_id, quote.snapshot_hash)
    decision = evaluate(
        quote,
        GuardContext(
            now=now,
            policy=policy,
            limits=limits_from_policy(policy),
            requirements=quote.requirements,
            stage="capture",
            approval=ApprovalRepository(session).facts(
                ApprovalRepository(session).get(user_id, payment.approval_id)
            ),
            duplicate_capture_exists=duplicate,
            payment_amount=provider.amount,
            payment_currency=provider.currency,
            order_environment=provider.environment,
            expected_environment=gateway.environment,
        ),
    )
    if decision.outcome is not GuardOutcome.ALLOW:
        journal.record(
            JournalEvents.CAPTURE_BLOCKED,
            actor=Actor.SYSTEM,
            subject_type="payment",
            subject_id=payment.id,
            user_id=user_id,
            correlation_id=correlation_id,
            payload={
                "reason": "guard_denied",
                "outcome": decision.outcome.value,
                "blocking_codes": list(decision.blocking_codes),
            },
        )
        raise ApprovalNotValid(
            "the Purchase Guard refused capture",
            details={"blocking_codes": list(decision.blocking_codes)},
        )

    try:
        result = gateway.capture(payment.provider_order_id, capture_key(payment.provider_order_id))
    except Exception as exc:
        journal.record(
            JournalEvents.FAILURE,
            actor=Actor.SYSTEM,
            subject_type="payment",
            subject_id=payment.id,
            user_id=user_id,
            correlation_id=correlation_id,
            payload={"error": type(exc).__name__, "stage": "capture"},
        )
        raise PaymentProviderError(f"PayPal capture failed: {type(exc).__name__}") from exc

    if result.amount != quote.grand_total or result.currency != quote.currency:
        journal.failure(
            error_code="AMOUNT_MISMATCH",
            message="capture amount does not match the approved quote",
            subject_type="payment",
            subject_id=payment.id,
            user_id=user_id,
            correlation_id=correlation_id,
        )
        raise PaymentProviderError("capture amount does not match the approved quote")

    return _mark_captured(
        session,
        payment=payment,
        quote=quote,
        approval_id=payment.approval_id,
        provider_capture_id=result.provider_capture_id,
        amount=result.amount,
        currency=result.currency,
        now=now,
        environment=environment,
        correlation_id=correlation_id,
        source="capture_api",
    )


def handle_paypal_webhook(
    session: Session,
    *,
    gateway: PaymentGateway | None,
    headers: dict[str, str],
    body: bytes,
    environment: str,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    journal = JournalWriter(session, environment=environment)
    if gateway is None:
        journal.record(
            JournalEvents.WEBHOOK_REJECTED,
            actor=Actor.EXTERNAL,
            subject_type="webhook",
            subject_id="paypal",
            payload={"reason": "paypal_not_configured"},
        )
        raise PaymentNotConfigured("PayPal webhooks cannot be verified without credentials")

    try:
        event = gateway.verify_webhook(headers, body)
    except Exception as exc:
        journal.record(
            JournalEvents.WEBHOOK_REJECTED,
            actor=Actor.EXTERNAL,
            subject_type="webhook",
            subject_id="paypal",
            correlation_id=correlation_id,
            payload={"reason": type(exc).__name__},
        )
        raise WebhookRejected(f"webhook rejected: {type(exc).__name__}") from exc

    event_id = event.provider_event_id or hashlib.sha256(body).hexdigest()[:32]
    payments = PaymentRepository(session)
    resource = event.payload.get("resource") if isinstance(event.payload.get("resource"), dict) else {}
    related = resource.get("supplementary_data", {}).get("related_ids", {}) if isinstance(resource, dict) else {}
    provider_order_id = (related or {}).get("order_id") or resource.get("id")
    payment = payments.by_provider_order(provider_order_id) if provider_order_id else None

    _row, created = TransactionRepository(session).add_verified_event(
        user_id=payment.user_id if payment else None,
        provider="paypal",
        event_type=event.event_type,
        provider_event_id=event_id,
        payment_order_id=payment.id if payment else None,
        signature_verified=event.signature_verified,
        payload=event.payload,
        retailer_purchase_state=RetailerPurchaseState.UNKNOWN.value,
        note="signed PayPal webhook; does not confirm a retailer purchase",
    )
    if not created:
        journal.record(
            JournalEvents.WEBHOOK_DUPLICATE,
            actor=Actor.EXTERNAL,
            subject_type="webhook",
            subject_id=event_id,
            user_id=payment.user_id if payment else None,
            correlation_id=correlation_id,
            payload={"event_type": event.event_type},
        )
        return {"received": True, "duplicate": True, "event_id": event_id, "applied": False}

    applied = False
    note = None
    if payment is not None:
        if event.event_type == "CHECKOUT.ORDER.APPROVED" and payment.status in {"created", "payer_action_required"}:
            payments.set_status(payment, "approved")
            applied = True
        elif event.event_type == "PAYMENT.CAPTURE.COMPLETED" and payment.status != "captured":
            amount = resource.get("amount") if isinstance(resource, dict) else None
            value = str(amount.get("value")) if isinstance(amount, dict) else None
            currency = amount.get("currency_code") if isinstance(amount, dict) else None
            if value is not None and currency is not None:
                quote_row = QuoteRepository(session).get(payment.user_id, payment.quote_id)
                quote = QuoteRepository(session).to_domain(quote_row) if quote_row else None
                if quote is not None and value == f"{quote.grand_total:.2f}" and currency == quote.currency:
                    payments.set_status(payment, "captured")
                    applied = True
                else:
                    note = "webhook capture amount did not match the approved quote"

    journal.record(
        JournalEvents.WEBHOOK_RECEIVED,
        actor=Actor.EXTERNAL,
        subject_type="webhook",
        subject_id=event_id,
        user_id=payment.user_id if payment else None,
        correlation_id=correlation_id,
        payload={
            "event_type": event.event_type,
            "signature_verified": True,
            "payment_order_id": payment.id if payment else None,
            "applied_to_payment_state": applied,
            "retailer_purchase_state": RetailerPurchaseState.UNKNOWN.value,
            "note": note,
        },
    )
    return {
        "received": True,
        "duplicate": False,
        "event_id": event_id,
        "event_type": event.event_type,
        "applied": applied,
        "retailer_purchase_state": RetailerPurchaseState.UNKNOWN.value,
        "note": note,
    }
