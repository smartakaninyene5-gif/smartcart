"""Canonical journal event types. Strings are part of the audit contract."""

from __future__ import annotations


class JournalEvents:
    DISCOVERY_SEARCHED = "discovery.searched"
    OFFER_OBSERVED = "offer.observed"
    EVIDENCE_RECORDED = "evidence.recorded"
    QUOTE_PREPARED = "quote.prepared"
    QUOTE_REJECTED = "quote.rejected"
    GUARD_EVALUATED = "guard.evaluated"
    INTENT_CREATED = "intent.created"
    INTENT_STATUS_CHANGED = "intent.status_changed"
    APPROVAL_GRANTED = "approval.granted"
    APPROVAL_REJECTED = "approval.rejected"
    APPROVAL_SUPERSEDED = "approval.superseded"
    APPROVAL_EXPIRED = "approval.expired"
    APPROVAL_INVALIDATED = "approval.invalidated"
    REVALIDATION_PASSED = "approval.revalidated"
    PAYMENT_ORDER_CREATED = "payment.order_created"
    PAYMENT_ORDER_FAILED = "payment.order_failed"
    PAYMENT_PREPARATION_BLOCKED = "payment.preparation_blocked"
    PAYMENT_CAPTURED = "payment.captured"
    CAPTURE_BLOCKED = "payment.capture_blocked"
    WEBHOOK_RECEIVED = "payment.webhook_received"
    WEBHOOK_DUPLICATE = "payment.webhook_duplicate"
    WEBHOOK_REJECTED = "payment.webhook_rejected"
    FAILURE = "system.failure"
