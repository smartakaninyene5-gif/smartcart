"""HTTP API. Thin adapters over application use cases."""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.api.deps import get_container, get_correlation_id, get_session, get_user_id
from app.api.schemas import (
    ApprovalRequest,
    PaymentOrderRequest,
    QuoteRequest,
    RejectRequest,
    SearchRequest,
    approval_json,
    decision_json,
    evidence_json,
    offer_json,
    payment_json,
    product_json,
    quote_json,
)
from app.application.approvals import approve_quote, reject_quote, revalidate_approval
from app.application.common import limits_from_policy, tighten_limits
from app.application.discovery import search_products
from app.application.errors import NotFound
from app.application.payments import handle_paypal_webhook, capture_payment, prepare_payment
from app.application.quotes import evidence_for_quote, prepare_quote
from app.container import Container
from app.domain.enums import DataMode
from app.domain.models import RequirementSet
from app.domain.snapshot import canonical_json
from app.journal import JournalWriter
from app.persistence.repositories import (
    ApprovalRepository,
    JournalRepository,
    ProductRepository,
    QuoteRepository,
)
from app.ports import ProductQuery

router = APIRouter()


@router.get("/health", tags=["meta"])
def health(
    container: Container = Depends(get_container),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    chain_ok, broken_at = JournalRepository(session).verify_chain()
    settings = container.settings
    return {
        "status": "ok" if chain_ok else "degraded",
        "environment": settings.env,
        "data_mode": settings.data_mode.value,
        "product_source": settings.product_source,
        "channel3_configured": settings.channel3_configured,
        "ai_provider": settings.ai_provider,
        "paypal_configured": settings.paypal_configured,
        "paypal_environment": settings.paypal_env,
        "guard_policy_version": settings.guard_policy.version,
        "journal_chain_ok": chain_ok,
        "journal_broken_at": broken_at,
    }


@router.post("/search", tags=["discovery"])
def search(
    payload: SearchRequest,
    user_id: str = Depends(get_user_id),
    container: Container = Depends(get_container),
    session: Session = Depends(get_session),
    correlation_id: str = Depends(get_correlation_id),
) -> dict[str, Any]:
    outcome = search_products(
        session,
        user_id=user_id,
        query=ProductQuery(
            text=payload.query,
            limit=payload.limit,
            max_price=payload.max_price,
            currency=payload.currency,
        ),
        source=container.product_source,
        environment=container.settings.env,
        correlation_id=correlation_id,
    )
    return {
        "data_mode": outcome.data_mode.value,
        "source": outcome.source,
        "as_of": outcome.as_of.isoformat() if hasattr(outcome.as_of, "isoformat") else str(outcome.as_of),
        "warnings": list(outcome.warnings),
        "evidence_count": outcome.evidence_count,
        "products": [product_json(product) for product in outcome.products],
    }


@router.get("/offers", tags=["discovery"])
def list_offers(
    product_id: str | None = Query(default=None, max_length=128),
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    offers = ProductRepository(session).list_offers(user_id, product_id=product_id)
    products = ProductRepository(session)
    rows = []
    for offer in offers:
        product = products.get_product(user_id, offer.product_id)
        rows.append(offer_json(offer, title=product.title if product else None))
    return {"offers": rows, "count": len(rows)}


@router.post("/quotes", status_code=201, tags=["quotes"])
def create_quote(
    payload: QuoteRequest,
    user_id: str = Depends(get_user_id),
    container: Container = Depends(get_container),
    session: Session = Depends(get_session),
    correlation_id: str = Depends(get_correlation_id),
) -> dict[str, Any]:
    settings = container.settings
    policy = settings.guard_policy
    requirements: RequirementSet = payload.requirements.to_domain(
        version="req-" + hashlib.sha256(canonical_json(payload.requirements.model_dump(mode="json")).encode("utf-8")).hexdigest()[:12]
    )
    base = limits_from_policy(policy)
    limits = tighten_limits(
        base,
        max_total=payload.user_limits.max_total,
        max_per_item=payload.user_limits.max_per_item,
        max_quantity=payload.user_limits.max_quantity,
    )
    outcome = prepare_quote(
        session,
        user_id=user_id,
        selections=[(item.offer_id, item.quantity) for item in payload.selections],
        requirements=requirements,
        limits=limits,
        policy=policy,
        now=container.clock.now(),
        data_mode=settings.data_mode,
        environment=settings.env,
        correlation_id=correlation_id,
    )
    evidence = evidence_for_quote(session, user_id=user_id, quote=outcome.quote)
    body = quote_json(
        outcome.quote,
        decision=outcome.decision,
        data_mode=outcome.data_mode,
        evidence=evidence,
    )
    body["intent_id"] = outcome.intent_id
    body["next_action"] = (
        "review_evidence_and_approve"
        if outcome.decision.outcome.value == "ALLOW"
        else "resolve_blocking_checks"
    )
    return body


@router.get("/quotes/{quote_id}", tags=["quotes"])
def get_quote(
    quote_id: str,
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    repository = QuoteRepository(session)
    row = repository.get(user_id, quote_id)
    if row is None:
        raise NotFound(f"quote {quote_id} not found")
    quote = repository.to_domain(row)
    evidence = evidence_for_quote(session, user_id=user_id, quote=quote)
    return quote_json(
        quote,
        decision=row.guard_decision_json,
        data_mode=row.data_mode,
        status=row.status,
        evidence=evidence,
    )


@router.post("/quotes/{quote_id}/approve", tags=["approval"])
def approve(
    quote_id: str,
    payload: ApprovalRequest,
    user_id: str = Depends(get_user_id),
    container: Container = Depends(get_container),
    session: Session = Depends(get_session),
    correlation_id: str = Depends(get_correlation_id),
) -> dict[str, Any]:
    outcome = approve_quote(
        session,
        user_id=user_id,
        quote_id=quote_id,
        snapshot_hash=payload.snapshot_hash,
        explicit_consent=payload.explicit_consent,
        acknowledged_risks=payload.acknowledged_risks,
        policy=container.settings.guard_policy,
        now=container.clock.now(),
        environment=container.settings.env,
        correlation_id=correlation_id,
    )
    return {
        "approval": approval_json(outcome.approval, decision=outcome.decision),
        "quote": quote_json(
            outcome.quote,
            decision=outcome.decision,
            data_mode=container.settings.data_mode,
        ),
        "next_action": "revalidate_before_payment",
    }


@router.post("/quotes/{quote_id}/reject", tags=["approval"])
def reject(
    quote_id: str,
    payload: RejectRequest,
    user_id: str = Depends(get_user_id),
    container: Container = Depends(get_container),
    session: Session = Depends(get_session),
    correlation_id: str = Depends(get_correlation_id),
) -> dict[str, Any]:
    rejected = reject_quote(
        session,
        user_id=user_id,
        quote_id=quote_id,
        reason=payload.reason,
        now=container.clock.now(),
        environment=container.settings.env,
        correlation_id=correlation_id,
    )
    return {"quote_id": quote_id, "status": "rejected", "rejected_approvals": rejected}


@router.get("/approvals/{approval_id}", tags=["approval"])
def get_approval(
    approval_id: str,
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    row = ApprovalRepository(session).get(user_id, approval_id)
    if row is None:
        raise NotFound(f"approval {approval_id} not found")
    return approval_json(row)


@router.post("/approvals/{approval_id}/revalidate", tags=["approval"])
def revalidate(
    approval_id: str,
    user_id: str = Depends(get_user_id),
    container: Container = Depends(get_container),
    session: Session = Depends(get_session),
    correlation_id: str = Depends(get_correlation_id),
) -> dict[str, Any]:
    outcome = revalidate_approval(
        session,
        user_id=user_id,
        approval_id=approval_id,
        policy=container.settings.guard_policy,
        now=container.clock.now(),
        environment=container.settings.env,
        correlation_id=correlation_id,
    )
    return {
        "approval": approval_json(outcome.approval),
        "outcome": outcome.outcome.value,
        "reasons": list(outcome.reasons),
        "changes": [
            {"path": change.path, "before": change.before, "after": change.after}
            for change in outcome.changes
        ],
        "guard": decision_json(outcome.decision),
        "quote": quote_json(
            outcome.quote,
            decision=outcome.decision,
            data_mode=container.settings.data_mode,
        ),
    }


@router.post("/payments/orders", status_code=201, tags=["payments"])
def create_payment_order(
    payload: PaymentOrderRequest,
    user_id: str = Depends(get_user_id),
    container: Container = Depends(get_container),
    session: Session = Depends(get_session),
    correlation_id: str = Depends(get_correlation_id),
) -> dict[str, Any]:
    outcome = prepare_payment(
        session,
        user_id=user_id,
        approval_id=payload.approval_id,
        gateway=container.payment_gateway,
        policy=container.settings.guard_policy,
        now=container.clock.now(),
        environment=container.settings.env,
        correlation_id=correlation_id,
    )
    return {
        "payment": payment_json(outcome.payment, replayed=outcome.replayed),
        "quote": quote_json(
            outcome.quote,
            decision=outcome.revalidation.decision,
            data_mode=container.settings.data_mode,
        ),
        "revalidation": {
            "outcome": outcome.revalidation.outcome.value,
            "reasons": list(outcome.revalidation.reasons),
        },
        "sandbox": outcome.payment.environment == "sandbox",
        "disclosure": (
            "This PayPal payment is a demo payment to Smartcart, not a purchase from a retailer. "
            "The retailer purchase remains yours to complete."
        ),
    }


@router.post("/payments/orders/{payment_id}/capture", tags=["payments"])
def capture_payment_order(
    payment_id: str,
    user_id: str = Depends(get_user_id),
    container: Container = Depends(get_container),
    session: Session = Depends(get_session),
    correlation_id: str = Depends(get_correlation_id),
) -> dict[str, Any]:
    outcome = capture_payment(
        session,
        user_id=user_id,
        payment_id=payment_id,
        gateway=container.payment_gateway,
        policy=container.settings.guard_policy,
        now=container.clock.now(),
        environment=container.settings.env,
        correlation_id=correlation_id,
    )
    return {
        "capture": {
            "capture_id": outcome.capture.id,
            "provider_capture_id": outcome.capture.provider_capture_id,
            "status": outcome.capture.status,
            "amount": outcome.capture.amount,
            "currency": outcome.capture.currency,
            "captured_at": outcome.capture.captured_at.isoformat(),
            "retailer_purchase_state": outcome.capture.retailer_purchase_state,
        },
        "payment": payment_json(outcome.payment),
        "quote_id": outcome.quote.quote_id,
        "snapshot_hash": outcome.quote.snapshot_hash,
        "payment_state": "captured",
        "retailer_purchase_state": outcome.retailer_purchase_state,
        "disclaimer": outcome.disclaimer,
        "replayed": outcome.replayed,
    }


@router.get("/journal", tags=["journal"])
def read_journal(
    limit: int = Query(default=100, ge=1, le=500),
    after_seq: int = Query(default=0, ge=0),
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    repository = JournalRepository(session)
    rows = repository.list(user_id=user_id, limit=limit, after_seq=after_seq)
    chain_ok, broken_at = repository.verify_chain()
    return {
        "chain_ok": chain_ok,
        "broken_at": broken_at,
        "events": [
            {
                "seq": row.seq,
                "event_id": row.event_id,
                "occurred_at": row.occurred_at.isoformat(),
                "recorded_at": row.recorded_at.isoformat(),
                "actor": row.actor,
                "event_type": row.event_type,
                "subject_type": row.subject_type,
                "subject_id": row.subject_id,
                "environment": row.environment,
                "payload": row.payload,
                "prev_hash": row.prev_hash,
                "entry_hash": row.entry_hash,
            }
            for row in rows
        ],
    }


@router.get("/evidence/{evidence_id}", tags=["journal"])
def read_evidence(
    evidence_id: str,
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    from app.persistence.repositories import EvidenceRepository

    row = EvidenceRepository(session).get(user_id, evidence_id)
    if row is None:
        raise NotFound(f"evidence {evidence_id} not found")
    payload = evidence_json(row)
    payload["payload"] = row.payload
    return payload
