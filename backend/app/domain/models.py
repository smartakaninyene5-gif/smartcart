"""Pure domain models.

Frozen pydantic models: constructing one either yields a valid fact or raises.
No database, network, clock or LLM imports are allowed here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.enums import Availability, Condition
from app.domain.money import ZERO, currency_code, money, money_sum


def as_utc(value: datetime) -> datetime:
    """Normalise to timezone-aware UTC. Naive input is treated as UTC."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def normalise_domain(value: str | None) -> str:
    """Return a bare lowercase host, or 'unknown'. Never trusts raw input."""

    if not value or not isinstance(value, str):
        return "unknown"
    raw = value.strip().lower()
    if "://" in raw:
        raw = urlsplit(raw).netloc
    raw = raw.split("/")[0].split(":")[0].strip(".")
    if raw.startswith("www."):
        raw = raw[4:]
    if not raw or " " in raw or "." not in raw:
        return "unknown"
    return raw


class DomainModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Product(DomainModel):
    product_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    source_product_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=1000)
    brand: str | None = None
    description: str = ""
    image_url: str | None = None
    category: str | None = None
    attributes: dict[str, list[str]] = Field(default_factory=dict)
    offers: tuple[Offer, ...] = ()
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return as_utc(value)


class Offer(DomainModel):
    """A merchant offer as observed at a point in time.

    Price lives here and only here. ``as_of`` is the provider's freshness
    timestamp (when the price was true); ``observed_at`` is when we saw it.
    """

    offer_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    source_offer_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    offer_product_id: str = Field(min_length=1)
    variant_id: str | None = None
    selected_options: tuple[tuple[str, str], ...] = ()
    merchant_name: str | None = None
    merchant_domain: str
    price: Decimal
    currency: str
    shipping_cost: Decimal | None = None
    availability: Availability = Availability.UNKNOWN
    condition: Condition = Condition.UNKNOWN
    url: str
    redirect_url: str | None = None
    is_redirect: bool = False
    observed_at: datetime
    as_of: datetime
    evidence_id: str | None = None

    @field_validator("price")
    @classmethod
    def _price(cls, value: object) -> Decimal:
        return money(value, "price", allow_zero=False)

    @field_validator("shipping_cost")
    @classmethod
    def _shipping(cls, value: object) -> Decimal | None:
        return None if value is None else money(value, "shipping_cost")

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        return currency_code(value)

    @field_validator("merchant_domain")
    @classmethod
    def _domain(cls, value: str) -> str:
        return normalise_domain(value)

    @field_validator("url", "redirect_url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.startswith(("http://", "https://")):
            raise ValueError("offer url must be absolute http(s)")
        return value

    @field_validator("observed_at", "as_of")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return as_utc(value)


class RequirementSet(DomainModel):
    """User-declared constraints. These tighten policy, never loosen it."""

    version: str = "req-1"
    query: str = ""
    currency: str = "USD"
    budget_total: Decimal | None = None
    budget_per_item: Decimal | None = None
    max_quantity_per_line: int = Field(default=3, ge=1, le=100)
    allowed_conditions: tuple[Condition, ...] = (Condition.NEW,)
    must_have_keywords: tuple[str, ...] = ()

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        return currency_code(value)

    @field_validator("budget_total", "budget_per_item")
    @classmethod
    def _budget(cls, value: object) -> Decimal | None:
        return None if value is None else money(value, "budget", allow_zero=False)


class UserLimits(DomainModel):
    """Per-user hard limits. The Guard uses min(user, policy)."""

    max_total: Decimal
    max_per_item: Decimal
    max_quantity_per_line: int = Field(ge=1, le=100)

    @field_validator("max_total", "max_per_item")
    @classmethod
    def _amount(cls, value: object) -> Decimal:
        return money(value, "limit", allow_zero=False)

    @classmethod
    def demo(cls) -> UserLimits:
        return cls(max_total=Decimal("1000.00"), max_per_item=Decimal("500.00"), max_quantity_per_line=3)


class LineItem(DomainModel):
    offer_id: str
    product_id: str
    offer_product_id: str
    variant_id: str | None = None
    title: str
    merchant_name: str | None = None
    merchant_domain: str
    quantity: int = Field(ge=1, le=100)
    unit_price: Decimal
    shipping_cost: Decimal = ZERO
    currency: str
    condition: Condition
    availability: Availability
    as_of: datetime
    evidence_ids: tuple[str, ...] = ()

    @field_validator("unit_price")
    @classmethod
    def _price(cls, value: object) -> Decimal:
        return money(value, "unit_price", allow_zero=False)

    @field_validator("shipping_cost")
    @classmethod
    def _shipping(cls, value: object) -> Decimal:
        return money(value, "shipping_cost")

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        return currency_code(value)

    @field_validator("merchant_domain")
    @classmethod
    def _domain(cls, value: str) -> str:
        return normalise_domain(value)

    @field_validator("as_of")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return as_utc(value)

    @property
    def subtotal(self) -> Decimal:
        return money(self.unit_price * self.quantity, "line subtotal")

    @classmethod
    def from_offer(
        cls,
        offer: Offer,
        *,
        title: str,
        quantity: int,
        evidence_ids: tuple[str, ...] = (),
    ) -> LineItem:
        return cls(
            offer_id=offer.offer_id,
            product_id=offer.product_id,
            offer_product_id=offer.offer_product_id,
            variant_id=offer.variant_id,
            title=title,
            merchant_name=offer.merchant_name,
            merchant_domain=offer.merchant_domain,
            quantity=quantity,
            unit_price=offer.price,
            shipping_cost=offer.shipping_cost or ZERO,
            currency=offer.currency,
            condition=offer.condition,
            availability=offer.availability,
            as_of=offer.as_of,
            evidence_ids=tuple(dict.fromkeys(evidence_ids)),
        )


class Quote(DomainModel):
    """An immutable priced basket. Content, not row identity, is hashed."""

    quote_id: str
    user_id: str
    currency: str
    lines: tuple[LineItem, ...] = Field(min_length=1)
    subtotal: Decimal
    shipping_total: Decimal
    grand_total: Decimal
    policy_version: str
    requirements: RequirementSet
    evidence_refs: tuple[str, ...] = ()
    as_of: datetime
    created_at: datetime
    snapshot_hash: str = ""

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        return currency_code(value)

    @field_validator("subtotal", "shipping_total", "grand_total")
    @classmethod
    def _amount(cls, value: object) -> Decimal:
        return money(value, "total")

    @field_validator("as_of", "created_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return as_utc(value)

    @model_validator(mode="after")
    def _arithmetic(self) -> Quote:
        line_subtotal = money_sum(tuple(line.subtotal for line in self.lines), self.currency)
        line_shipping = money_sum(tuple(line.shipping_cost for line in self.lines), self.currency)
        if line_subtotal != self.subtotal:
            raise ValueError(f"subtotal mismatch: lines={line_subtotal} field={self.subtotal}")
        if line_shipping != self.shipping_total:
            raise ValueError(f"shipping mismatch: lines={line_shipping} field={self.shipping_total}")
        if money(line_subtotal + line_shipping, "grand_total") != self.grand_total:
            raise ValueError("grand_total must equal subtotal + shipping_total")
        for line in self.lines:
            if line.currency != self.currency:
                raise ValueError("all lines must share the quote currency")
            missing = set(line.evidence_ids) - set(self.evidence_refs)
            if missing:
                raise ValueError(f"line evidence not referenced by quote: {sorted(missing)}")
        return self

    @classmethod
    def build(
        cls,
        *,
        quote_id: str,
        user_id: str,
        lines: tuple[LineItem, ...],
        policy_version: str,
        requirements: RequirementSet,
        as_of: datetime,
        created_at: datetime,
        snapshot_hash: str = "",
    ) -> Quote:
        currency = lines[0].currency
        subtotal = money_sum(tuple(line.subtotal for line in lines), currency)
        shipping = money_sum(tuple(line.shipping_cost for line in lines), currency)
        refs: dict[str, None] = {}
        for line in lines:
            for evidence_id in line.evidence_ids:
                refs[evidence_id] = None
        return cls(
            quote_id=quote_id,
            user_id=user_id,
            currency=currency,
            lines=lines,
            subtotal=subtotal,
            shipping_total=shipping,
            grand_total=money(subtotal + shipping, "grand_total"),
            policy_version=policy_version,
            requirements=requirements,
            evidence_refs=tuple(refs),
            as_of=as_of,
            created_at=created_at,
            snapshot_hash=snapshot_hash,
        )


class ApprovalFacts(DomainModel):
    """Minimal projection of an approval, for the pure Guard."""

    approval_id: str
    quote_id: str
    user_id: str
    snapshot_hash: str
    status: str
    approved_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None

    @field_validator("approved_at", "expires_at", "consumed_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else as_utc(value)


class MaterialChange(DomainModel):
    path: str
    before: Any = None
    after: Any = None


def offer_from_raw(
    *,
    offer_id: str,
    source: str,
    source_offer_id: str,
    product_id: str,
    offer_product_id: str,
    merchant_name: str | None,
    merchant_domain: str,
    price: object,
    currency: str,
    shipping_cost: object | None,
    availability: Availability,
    condition: Condition,
    url: str,
    redirect_url: str | None,
    is_redirect: bool,
    observed_at: datetime,
    as_of: datetime,
    evidence_id: str | None = None,
    variant_id: str | None = None,
    selected_options: tuple[tuple[str, str], ...] = (),
) -> Offer:
    """Typed constructor used by adapters/normalisers."""

    return Offer(
        offer_id=offer_id,
        source=source,
        source_offer_id=source_offer_id,
        product_id=product_id,
        offer_product_id=offer_product_id,
        variant_id=variant_id,
        selected_options=selected_options,
        merchant_name=merchant_name,
        merchant_domain=merchant_domain,
        price=price,
        currency=currency,
        shipping_cost=shipping_cost,
        availability=availability,
        condition=condition,
        url=url,
        redirect_url=redirect_url,
        is_redirect=is_redirect,
        observed_at=observed_at,
        as_of=as_of,
        evidence_id=evidence_id,
    )
