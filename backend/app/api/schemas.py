"""HTTP request schemas and response serialisers.

Clients may submit offer ids, quantities and their own limits. They may not
submit prices, totals, guard outcomes or payment amounts.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.enums import Availability, Condition, DataMode, GuardOutcome
from app.domain.guard import GuardDecision
from app.domain.models import Offer, Product, Quote, RequirementSet
from app.domain.money import currency_code, money
from app.shopping.models import ShoppingUnderstanding


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=300)
    max_price: Decimal | None = None
    limit: int = Field(default=8, ge=1, le=20)
    currency: str = "USD"

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        return currency_code(value)

    @field_validator("max_price")
    @classmethod
    def _max_price(cls, value: object) -> Decimal | None:
        return None if value is None else money(value, "max_price", allow_zero=False)


class RequirementsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(default="", max_length=300)
    currency: str = "USD"
    budget_total: Decimal | None = None
    budget_per_item: Decimal | None = None
    max_quantity_per_line: int = Field(default=3, ge=1, le=100)
    allowed_conditions: list[Condition] = Field(default_factory=lambda: [Condition.NEW])
    must_have_keywords: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        return currency_code(value)

    def to_domain(self, *, version: str) -> RequirementSet:
        conditions = tuple(self.allowed_conditions) or (Condition.NEW,)
        if Condition.UNKNOWN in conditions:
            # Unknown condition can never be treated as acceptable-by-default.
            conditions = tuple(c for c in conditions if c is not Condition.UNKNOWN)
        return RequirementSet(
            version=version,
            query=self.query,
            currency=self.currency,
            budget_total=self.budget_total,
            budget_per_item=self.budget_per_item,
            max_quantity_per_line=self.max_quantity_per_line,
            allowed_conditions=conditions or (Condition.NEW,),
            must_have_keywords=tuple(self.must_have_keywords),
        )


class SelectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offer_id: str = Field(min_length=1, max_length=180)
    quantity: int = Field(default=1, ge=1, le=100)


class UserLimitsIn(BaseModel):
    """User limits can only tighten the policy ceiling."""

    model_config = ConfigDict(extra="forbid")

    max_total: Decimal | None = None
    max_per_item: Decimal | None = None
    max_quantity: int | None = Field(default=None, ge=1, le=100)


class QuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selections: list[SelectionIn] = Field(min_length=1, max_length=20)
    requirements: RequirementsIn = Field(default_factory=RequirementsIn)
    user_limits: UserLimitsIn = Field(default_factory=UserLimitsIn)


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_hash: str = Field(min_length=1, max_length=80)
    explicit_consent: bool = False
    acknowledged_risks: bool = False


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="user_rejected", max_length=500)


class PaymentOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str = Field(min_length=1, max_length=64)


class ShoppingUnderstandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=3, max_length=4000)


class ShoppingConfirmRequest(BaseModel):
    """The user's corrected/confirmed reading of their request."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=64)
    understanding: ShoppingUnderstanding


class ShoppingSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=64)
    research: bool = True


class ShoppingResearchRequest(BaseModel):
    """Internal targeted-research trigger; bounded to this search's products."""

    model_config = ConfigDict(extra="forbid")

    product_ids: list[str] | None = Field(default=None, max_length=20)


def offer_json(offer: Offer, *, title: str | None = None) -> dict[str, Any]:
    return {
        "offer_id": offer.offer_id,
        "product_id": offer.product_id,
        "title": title,
        "merchant_name": offer.merchant_name,
        "merchant_domain": offer.merchant_domain,
        "price": offer.price,
        "currency": offer.currency,
        "shipping_cost": offer.shipping_cost,
        "availability": offer.availability.value,
        "condition": offer.condition.value,
        "url": offer.url,
        "redirect_url": offer.redirect_url,
        "is_redirect": offer.is_redirect,
        "as_of": offer.as_of.isoformat(),
        "observed_at": offer.observed_at.isoformat(),
        "evidence_id": offer.evidence_id,
        "source": offer.source,
        "variant_id": offer.variant_id,
    }


def product_json(product: Product) -> dict[str, Any]:
    return {
        "product_id": product.product_id,
        "title": product.title,
        "brand": product.brand,
        "description": product.description,
        "image_url": product.image_url,
        "category": product.category,
        "attributes": product.attributes,
        "observed_at": product.observed_at.isoformat(),
        "offers": [offer_json(offer, title=product.title) for offer in product.offers],
    }


def decision_json(decision: GuardDecision | dict[str, Any] | None) -> dict[str, Any] | None:
    if decision is None:
        return None
    payload = decision.model_dump(mode="json") if isinstance(decision, GuardDecision) else decision
    return {
        "decision_id": payload.get("decision_id"),
        "outcome": payload.get("outcome"),
        "stage": payload.get("stage"),
        "policy_version": payload.get("policy_version"),
        "snapshot_hash": payload.get("snapshot_hash"),
        "evaluated_at": payload.get("evaluated_at"),
        "blocking_codes": payload.get("blocking_codes", []),
        "summary": payload.get("summary", ""),
        "rules": [
            {
                "code": rule.get("code"),
                "status": rule.get("status"),
                "reason": rule.get("reason"),
                "evidence_refs": rule.get("evidence_refs", []),
            }
            for rule in payload.get("rules", [])
        ],
    }


def evidence_json(row) -> dict[str, Any]:
    return {
        "evidence_id": row.id,
        "kind": row.kind,
        "trust": row.trust,
        "source": row.source,
        "source_ref": row.source_ref,
        "subject_type": row.subject_type,
        "subject_id": row.subject_id,
        "observed_at": row.observed_at.isoformat(),
        "as_of": row.as_of.isoformat() if row.as_of else None,
        "payload_sha256": row.payload_sha256,
    }


def quote_json(
    quote: Quote,
    *,
    decision: GuardDecision | dict[str, Any] | None,
    data_mode: DataMode | str,
    status: str = "prepared",
    evidence: list | None = None,
) -> dict[str, Any]:
    mode = data_mode.value if isinstance(data_mode, DataMode) else str(data_mode)
    return {
        "quote_id": quote.quote_id,
        "user_id": quote.user_id,
        "status": status,
        "currency": quote.currency,
        "subtotal": quote.subtotal,
        "shipping_total": quote.shipping_total,
        "grand_total": quote.grand_total,
        "snapshot_hash": quote.snapshot_hash,
        "policy_version": quote.policy_version,
        "as_of": quote.as_of.isoformat(),
        "created_at": quote.created_at.isoformat(),
        "data_mode": mode,
        "requirements": quote.requirements.model_dump(mode="json"),
        "lines": [
            {
                "offer_id": line.offer_id,
                "product_id": line.product_id,
                "title": line.title,
                "merchant_name": line.merchant_name,
                "merchant_domain": line.merchant_domain,
                "quantity": line.quantity,
                "unit_price": line.unit_price,
                "shipping_cost": line.shipping_cost,
                "subtotal": line.subtotal,
                "currency": line.currency,
                "condition": line.condition.value,
                "availability": line.availability.value,
                "as_of": line.as_of.isoformat(),
                "evidence_ids": list(line.evidence_ids),
            }
            for line in quote.lines
        ],
        "evidence_refs": list(quote.evidence_refs),
        "evidence": [evidence_json(row) for row in (evidence or [])],
        "guard": decision_json(decision),
        "guard_outcome": (
            decision.outcome.value
            if isinstance(decision, GuardDecision)
            else (decision or {}).get("outcome")
        ),
    }


def approval_json(row, *, decision: GuardDecision | dict[str, Any] | None = None) -> dict[str, Any]:
    stored = decision if decision is not None else row.guard_decision_json
    return {
        "approval_id": row.id,
        "quote_id": row.quote_id,
        "snapshot_hash": row.snapshot_hash,
        "status": row.status,
        "policy_version": row.policy_version,
        "approved_at": row.approved_at.isoformat(),
        "expires_at": row.expires_at.isoformat(),
        "consumed_at": row.consumed_at.isoformat() if row.consumed_at else None,
        "rejected_at": row.rejected_at.isoformat() if row.rejected_at else None,
        "reason": row.reason,
        "guard": decision_json(stored),
    }


def payment_json(row, *, replayed: bool = False) -> dict[str, Any]:
    return {
        "payment_id": row.id,
        "provider": row.provider,
        "provider_order_id": row.provider_order_id,
        "status": row.status,
        "amount": row.amount,
        "currency": row.currency,
        "approve_url": row.approve_url,
        "environment": row.environment,
        "quote_id": row.quote_id,
        "approval_id": row.approval_id,
        "created_at": row.created_at.isoformat(),
        "replayed": replayed,
    }


def guard_outcome_value(value: GuardOutcome | str) -> str:
    return value.value if isinstance(value, GuardOutcome) else str(value)


def availability_enum(value: str) -> Availability:
    try:
        return Availability(value)
    except ValueError:
        return Availability.UNKNOWN
