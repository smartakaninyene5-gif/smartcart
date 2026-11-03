"""Quote preparation use case. Server prices everything; clients never do."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.application.common import line_from_offer, merge_limits, quote_as_of
from app.application.errors import NotFound, ValidationFailure
from app.domain.enums import Actor, DataMode, GuardOutcome
from app.domain.guard import GuardContext, GuardDecision, GuardPolicy, evaluate
from app.domain.models import ApprovalFacts, Quote, RequirementSet, UserLimits
from app.domain.snapshot import seal
from app.journal import JournalEvents, JournalWriter
from app.persistence.base import new_id
from app.persistence.repositories import (
    EvidenceRepository,
    IntentRepository,
    ProductRepository,
    QuoteRepository,
    UserRepository,
)


@dataclass
class QuoteOutcome:
    quote: Quote
    decision: GuardDecision
    data_mode: DataMode
    intent_id: str


def prepare_quote(
    session: Session,
    *,
    user_id: str,
    selections: list[tuple[str, int]],
    requirements: RequirementSet,
    limits: UserLimits,
    policy: GuardPolicy,
    now,
    data_mode: DataMode,
    environment: str,
    correlation_id: str | None = None,
) -> QuoteOutcome:
    if not selections:
        raise ValidationFailure("at least one offer must be selected")
    offer_ids = [offer_id for offer_id, _ in selections]
    if len(set(offer_ids)) != len(offer_ids):
        raise ValidationFailure("duplicate offer in selection")
    if any(quantity < 1 for _, quantity in selections):
        raise ValidationFailure("quantity must be at least 1")

    UserRepository(session).ensure(user_id)
    catalog = ProductRepository(session)
    offers = {offer.offer_id: offer for offer in catalog.list_offers(user_id, offer_ids=offer_ids)}
    missing = [offer_id for offer_id in offer_ids if offer_id not in offers]
    if missing:
        raise NotFound(f"offer(s) not found: {', '.join(missing)}")

    requirements = merge_limits(requirements, limits)

    lines = []
    for offer_id, quantity in selections:
        offer = offers[offer_id]
        product = catalog.get_product(user_id, offer.product_id)
        title = product.title if product else offer.product_id
        lines.append(line_from_offer(offer, title=title, quantity=quantity))

    currencies = {line.currency for line in lines}
    if len(currencies) > 1:
        raise ValidationFailure(f"cannot combine offers in different currencies: {sorted(currencies)}")

    quote = seal(
        Quote.build(
            quote_id=new_id("qte"),
            user_id=user_id,
            lines=tuple(lines),
            policy_version=policy.version,
            requirements=requirements,
            as_of=quote_as_of(tuple(lines)),
            created_at=now,
        )
    )
    decision = evaluate(
        quote,
        GuardContext(
            now=now,
            policy=policy,
            limits=limits,
            requirements=requirements,
            stage="pre_approval",
        ),
    )

    journal = JournalWriter(session, environment=environment)
    quote_row = QuoteRepository(session).add(quote, decision, data_mode=data_mode)
    intent = IntentRepository(session).create(user_id, quote.quote_id, note=requirements.query or None)
    intent.status = "awaiting_approval" if decision.outcome is GuardOutcome.ALLOW else "prepared"
    session.flush()

    journal.record(
        JournalEvents.QUOTE_PREPARED,
        actor=Actor.SYSTEM,
        subject_type="quote",
        subject_id=quote.quote_id,
        user_id=user_id,
        correlation_id=correlation_id,
        payload={
            "snapshot_hash": quote.snapshot_hash,
            "policy_version": quote.policy_version,
            "grand_total": str(quote.grand_total),
            "currency": quote.currency,
            "line_count": len(quote.lines),
            "data_mode": data_mode.value,
            "guard_outcome": decision.outcome.value,
            "guard_decision_id": decision.decision_id,
        },
    )
    journal.record(
        JournalEvents.GUARD_EVALUATED,
        actor=Actor.SYSTEM,
        subject_type="quote",
        subject_id=quote.quote_id,
        user_id=user_id,
        correlation_id=correlation_id,
        payload={
            "stage": decision.stage,
            "outcome": decision.outcome.value,
            "policy_version": decision.policy_version,
            "decision_id": decision.decision_id,
            "blocking_codes": list(decision.blocking_codes),
        },
    )
    journal.record(
        JournalEvents.INTENT_CREATED,
        actor=Actor.SYSTEM,
        subject_type="intent",
        subject_id=intent.id,
        user_id=user_id,
        correlation_id=correlation_id,
        payload={"quote_id": quote.quote_id, "status": intent.status},
    )

    assert quote_row.snapshot_hash == quote.snapshot_hash
    return QuoteOutcome(quote=quote, decision=decision, data_mode=data_mode, intent_id=intent.id)


def current_quote(session: Session, *, user_id: str, quote: Quote) -> Quote | None:
    """Rebuild the same basket from the offers as currently stored."""

    from app.application.common import rebuild_quote

    offers = ProductRepository(session).list_offers(user_id, offer_ids=[line.offer_id for line in quote.lines])
    return rebuild_quote(quote, {offer.offer_id: offer for offer in offers})


def evidence_for_quote(session: Session, *, user_id: str, quote: Quote):
    repository = EvidenceRepository(session)
    rows = []
    for evidence_id in quote.evidence_refs:
        row = repository.get(user_id, evidence_id)
        if row is not None:
            rows.append(row)
    return rows
