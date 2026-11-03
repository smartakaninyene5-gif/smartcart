"""Shared application helpers."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.domain.models import LineItem, Offer, Quote, RequirementSet, UserLimits, as_utc
from app.domain.money import money
from app.persistence.repositories import ProductRepository


def default_limits(max_total: Decimal, max_item: Decimal, max_qty: int) -> UserLimits:
    return UserLimits(max_total=max_total, max_per_item=max_item, max_quantity_per_line=max_qty)


def limits_from_policy(policy) -> UserLimits:  # type: ignore[no-untyped-def]
    return UserLimits(
        max_total=policy.max_total,
        max_per_item=policy.max_item_price,
        max_quantity_per_line=policy.max_quantity_per_line,
    )


def merge_limits(requirements: RequirementSet, limits: UserLimits) -> RequirementSet:
    """Fold a user's tightened limits into the requirements that are hashed.

    This keeps revalidation reproducible: the snapshot itself carries the
    limits that were in force, rather than relying on request state.
    """

    budget_total = requirements.budget_total
    if limits.max_total is not None:
        budget_total = limits.max_total if budget_total is None else min(budget_total, limits.max_total)
    budget_item = requirements.budget_per_item
    if limits.max_per_item is not None:
        budget_item = limits.max_per_item if budget_item is None else min(budget_item, limits.max_per_item)
    return requirements.model_copy(
        update={
            "budget_total": budget_total,
            "budget_per_item": budget_item,
            "max_quantity_per_line": min(requirements.max_quantity_per_line, limits.max_quantity_per_line),
        }
    )


def tighten_limits(
    base: UserLimits,
    *,
    max_total: Decimal | None = None,
    max_per_item: Decimal | None = None,
    max_quantity: int | None = None,
) -> UserLimits:
    """User-supplied limits may only make the ceiling lower."""

    return UserLimits(
        max_total=min(base.max_total, money(max_total, "max_total")) if max_total is not None else base.max_total,
        max_per_item=min(base.max_per_item, money(max_per_item, "max_per_item"))
        if max_per_item is not None
        else base.max_per_item,
        max_quantity_per_line=min(base.max_quantity_per_line, max_quantity)
        if max_quantity is not None
        else base.max_quantity_per_line,
    )


def line_from_offer(offer: Offer, *, title: str, quantity: int) -> LineItem:
    evidence = (offer.evidence_id,) if offer.evidence_id else ()
    return LineItem.from_offer(offer, title=title or offer.product_id, quantity=quantity, evidence_ids=evidence)


def quote_as_of(lines: tuple[LineItem, ...]) -> datetime:
    """Oldest price evidence in the basket: the conservative freshness clock."""

    return min(as_utc(line.as_of) for line in lines)


def rebuild_quote(quote: Quote, offers_by_offer_id: dict[str, Offer]) -> Quote | None:
    """Reconstruct the same basket from currently stored offers.

    Returns ``None`` when any line's offer no longer exists, which revalidation
    treats as a material change.
    """

    lines: list[LineItem] = []
    for line in quote.lines:
        offer = offers_by_offer_id.get(line.offer_id)
        if offer is None:
            return None
        lines.append(line_from_offer(offer, title=line.title, quantity=line.quantity))
    if not lines:
        return None
    return Quote.build(
        quote_id=quote.quote_id,
        user_id=quote.user_id,
        lines=tuple(lines),
        policy_version=quote.policy_version,
        requirements=quote.requirements,
        as_of=quote_as_of(tuple(lines)),
        created_at=quote.created_at,
        snapshot_hash="",
    )
