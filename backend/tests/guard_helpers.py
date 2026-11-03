"""Domain-level helpers to build sealed quotes for the Guard matrix tests.

Replicates the audit harness (defaults nobody overrides by accident) so each
rule can be tested in isolation without the HTTP layer.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from app.domain.enums import Availability, Condition
from app.domain.guard import GuardContext, GuardPolicy, GuardDecision, evaluate
from app.domain.models import ApprovalFacts, LineItem, Offer, Quote, RequirementSet, UserLimits
from app.domain.snapshot import seal


def make_offer(**kw) -> Offer:
    from datetime import timezone

    NOW = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)
    defaults = dict(
        offer_id="mock:o1",
        source="mock",
        source_offer_id="o1",
        product_id="mock:p1",
        offer_product_id="mock:p1",
        merchant_domain="bestbuy.com",
        merchant_name="Best Buy",
        price=Decimal("100.00"),
        currency="USD",
        availability=Availability.IN_STOCK,
        condition=Condition.NEW,
        url="https://buy.trychannel3.com/x",
        is_redirect=True,
        observed_at=NOW,
        as_of=NOW,
        evidence_id="evd_1",
    )
    defaults.update(kw)
    return Offer(**defaults)


def make_quote(*, offers=None, quantity: int = 1, policy_version: str = "1.0.0", as_of=None,
               evidence: bool = True, quote_id: str = "qte_test", user_id: str = "user_demo") -> Quote:
    from datetime import timezone

    NOW = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)
    offer = offers or make_offer(**({"as_of": as_of, "observed_at": as_of} if as_of else {}))
    line = LineItem.from_offer(
        offer,
        title="Test product",
        quantity=quantity,
        evidence_ids=(offer.evidence_id,) if evidence and offer.evidence_id else (),
    )
    quote = Quote.build(
        quote_id=quote_id,
        user_id=user_id,
        lines=(line,),
        policy_version=policy_version,
        requirements=RequirementSet(budget_total=Decimal("500.00")),
        as_of=as_of or offer.as_of,
        created_at=NOW,
    )
    return seal(quote)


def limits(max_total="1000.00", max_item="500.00", max_qty=5) -> UserLimits:
    return UserLimits(
        max_total=Decimal(max_total),
        max_per_item=Decimal(max_item),
        max_quantity_per_line=max_qty,
    )


def facts(quote: Quote, **kw) -> ApprovalFacts:
    from datetime import timezone

    NOW = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)
    base = dict(
        approval_id="apr_1",
        quote_id=quote.quote_id,
        user_id=quote.user_id,
        snapshot_hash=quote.snapshot_hash,
        status="approved",
        approved_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    )
    base.update(kw)
    return ApprovalFacts(**base)


def ctx(quote: Quote, *, stage: str = "pre_approval", approval=None, policy=None,
        user_limits=None, **kw) -> GuardContext:
    from datetime import timezone

    NOW = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)
    return GuardContext(
        now=kw.pop("now", NOW),
        policy=policy or GuardPolicy(),
        limits=user_limits or limits(),
        requirements=quote.requirements,
        stage=stage,
        approval=approval,
        **kw,
    )


def blocking(quote: Quote, context: GuardContext):
    decision: GuardDecision = evaluate(quote, context)
    return decision.outcome, set(decision.blocking_codes), decision
