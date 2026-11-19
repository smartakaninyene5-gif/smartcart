"""Pydantic models for the shopping-intelligence pipeline.

Everything here is data. Contracts:
* MUST vs PREFERENCE is explicit; confirm happens before any discovery.
* Evidence statuses are deterministic enums; the backend, never the LLM,
  assigns them.
* Money is Decimal-derived; floats never enter money comparisons through
  these models (numeric spec values are unit counts like hours, not money).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.money import currency_code, money


def as_utc(value: datetime) -> datetime:
    from datetime import timezone

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class ShoppingModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


# ----------------------------------------------------------------- understanding


class Requirement(ShoppingModel):
    """One extracted requirement. ``mandatory`` distinguishes MUST / PREFERENCE."""

    attribute: str = Field(min_length=1, max_length=120)
    comparator: Literal["at_least", "at_most", "equal", "boolean_true", "avoid"] = "boolean_true"
    value: str | None = Field(default=None, max_length=300)
    numeric: float | None = None
    detail: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _needs_value(self) -> "Requirement":
        if self.comparator in ("equal",) and not self.value:
            raise ValueError("comparator 'equal' requires a value")
        if self.comparator in ("at_least", "at_most") and self.numeric is None:
            raise ValueError(f"comparator '{self.comparator}' requires a numeric value")
        if self.numeric is not None and self.comparator in ("boolean_true", "avoid"):
            raise ValueError("numeric value requires comparator at_least/at_most")
        return self


class ShoppingUnderstanding(ShoppingModel):
    """Gemini's structured reading of the user's request. Advisory input.

    Nothing here is authoritative until the user confirms it. The backend
    re-checks essentials deterministically and adds clarifications.
    """

    category: str = Field(default="", max_length=300)
    search_query: str = Field(default="", max_length=300)
    must_have: list[Requirement] = Field(default_factory=list, max_length=20)
    preferences: list[Requirement] = Field(default_factory=list, max_length=20)
    budget: Decimal | None = None
    budget_total: Decimal | None = None
    currency: str = "USD"
    quantity: int = Field(default=1, ge=1, le=100)
    preferred_brands: list[str] = Field(default_factory=list, max_length=10)
    excluded_brands: list[str] = Field(default_factory=list, max_length=10)
    excluded_merchants: list[str] = Field(default_factory=list, max_length=20)
    clarifications_needed: list[str] = Field(default_factory=list, max_length=10)
    notes: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        try:
            return currency_code(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("budget", "budget_total")
    @classmethod
    def _amount(cls, value: object) -> Decimal | None:
        if value is None or isinstance(value, str) and not str(value).strip():
            return None
        return money(value, "budget", allow_zero=False)


# ------------------------------------------------------------------- criteria


class ConfirmedCriteria(ShoppingModel):
    """The authoritative search criteria, produced only by user confirmation."""

    request_id: str = Field(min_length=1, max_length=64)
    category: str = Field(default="", max_length=300)
    search_query: str = Field(min_length=1, max_length=300)
    must_have: tuple[Requirement, ...] = Field(max_length=20)
    preferences: tuple[Requirement, ...] = Field(default=(), max_length=20)
    budget: Decimal | None = None
    budget_total: Decimal | None = None
    currency: str = "USD"
    quantity: int = Field(default=1, ge=1, le=100)
    preferred_brands: tuple[str, ...] = ()
    excluded_brands: tuple[str, ...] = ()
    excluded_merchants: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    confirmed_at: datetime

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        return currency_code(value)

    @field_validator("budget", "budget_total")
    @classmethod
    def _amount(cls, value: object) -> Decimal | None:
        if value is None:
            return None
        return money(value, "budget", allow_zero=False)

    @field_validator("confirmed_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return as_utc(value)

    def mandatory_attributes(self) -> tuple[str, ...]:
        return tuple(req.attribute for req in self.must_have)


# ------------------------------------------------------------------- evidence


class EvidenceStatus(str, Enum):
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    MISSING = "MISSING"
    CONFLICTING = "CONFLICTING"
    AI_ASSESSMENT = "AI_ASSESSMENT"


class ClaimSource(str, Enum):
    CHANNEL3 = "channel3"
    KERNEL = "kernel"
    AI = "ai"


class Claim(ShoppingModel):
    """One provenance-carrying statement about one attribute."""

    source: ClaimSource
    value: str | None = None
    numeric: float | None = None
    trust: str = "SOURCE_OBSERVED"
    url: str | None = None
    page_title: str | None = None
    observed_at: datetime | None = None
    directly_observed: bool = False
    snippet: str | None = None
    note: str | None = None

    @field_validator("observed_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else as_utc(value)


class FieldEvidence(ShoppingModel):
    attribute: str
    status: EvidenceStatus
    claims: tuple[Claim, ...] = ()

    @property
    def value(self) -> str | None:
        for claim in self.claims:
            if claim.value:
                return claim.value
        return None


class CheckResult(ShoppingModel):
    attribute: str
    mandatory: bool
    outcome: Literal["pass", "fail", "blocked"]
    status: EvidenceStatus
    detail: str = ""
    evidence_statuses: list[str] = Field(default_factory=list)


class ProductAssessment(ShoppingModel):
    """Deterministic backend verdict. This is the authoritative evaluation."""

    product_id: str
    title: str
    brand: str | None = None
    decision: Literal["COMPLIANT", "DISQUALIFIED", "INSUFFICIENT_EVIDENCE"]
    fields: dict[str, FieldEvidence] = Field(default_factory=dict)
    checks: list[CheckResult] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    best_price: str | None = None
    currency: str | None = None
    best_offer_id: str | None = None
    best_offer_url: str | None = None
    preference_matches: list[str] = Field(default_factory=list)
    preference_misses: list[str] = Field(default_factory=list)


class ResearchDecision(ShoppingModel):
    product_id: str
    offer_id: str
    url: str
    merchant_domain: str
    fields: list[str]
    reasons: list[str]


class ResearchFinding(ShoppingModel):
    attribute: str
    value: str | None = None
    numeric: float | None = None
    source: str = "kernel"
    url: str
    page_title: str | None = None
    observed_at: datetime
    directly_observed: bool = False
    snippet: str | None = None
    notes: list[str] = Field(default_factory=list)

    @field_validator("observed_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return as_utc(value)


class ProductExplanation(ShoppingModel):
    """Interpretation combining deterministic evaluation + optional Gemini prose."""

    product_id: str
    summary: str = ""
    why_selected: list[str] = Field(default_factory=list)
    tradeoffs: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)
    ai_participated: bool = False
    ai_was_available: bool = False


# ------------------------------------------------------------- recommendations


class Recommendation(ShoppingModel):
    category: Literal["cheapest_compliant", "best_value", "best_match"]
    product_id: str
    title: str
    best_offer_id: str
    price: str
    currency: str
    url: str | None = None
    reason: str = ""
    mandatory_satisfied: list[str] = Field(default_factory=list)
    preferences_matched: list[str] = Field(default_factory=list)
    supporting_evidence: list[str] = Field(default_factory=list)
    missing_or_uncertain: list[str] = Field(default_factory=list)
    tradeoffs: list[str] = Field(default_factory=list)
    value_score: float | None = None
    explanation: ProductExplanation | None = None


class RecommendationSet(ShoppingModel):
    cheapest_compliant: Recommendation | None = None
    best_value: Recommendation | None = None
    best_match: Recommendation | None = None

    @property
    def non_empty(self) -> list[Recommendation]:
        return [r for r in (self.cheapest_compliant, self.best_value, self.best_match) if r]


def evidence_status_dict(mapping: dict[str, FieldEvidence]) -> dict[str, Any]:
    """Serialise a status mapping for API output."""
    return {
        attribute: {
            "status": field.status.value,
            "value": field.value,
            "claims": [
                {
                    "source": claim.source.value,
                    "value": claim.value,
                    "trust": claim.trust,
                    "url": claim.url,
                    "directly_observed": claim.directly_observed,
                    "snippet": claim.snippet,
                }
                for claim in field.claims
            ],
        }
        for attribute, field in mapping.items()
    }
