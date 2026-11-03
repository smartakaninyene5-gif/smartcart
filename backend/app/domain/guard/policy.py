"""Versioned, immutable Purchase Guard policy."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.enums import Condition
from app.domain.models import RequirementSet, UserLimits, normalise_domain
from app.domain.money import money


class GuardPolicy(BaseModel):
    """Global purchasing policy. Changing a value requires a new version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(default="1.0.0", min_length=1)
    currency: str = "USD"
    max_total: Decimal = Decimal("1000.00")
    max_item_price: Decimal = Decimal("500.00")
    max_quantity_per_line: int = Field(default=5, ge=1, le=100)
    max_evidence_age: timedelta = timedelta(minutes=15)
    max_future_skew: timedelta = timedelta(seconds=120)
    allowed_conditions: tuple[Condition, ...] = (Condition.NEW,)
    merchant_allowlist: tuple[str, ...] | None = None
    merchant_denylist: tuple[str, ...] = ()
    approval_ttl: timedelta = timedelta(minutes=30)

    @field_validator("max_total", "max_item_price")
    @classmethod
    def _amount(cls, value: object) -> Decimal:
        return money(value, "policy limit", allow_zero=False)

    @field_validator("merchant_allowlist", "merchant_denylist")
    @classmethod
    def _domains(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        cleaned = tuple(dict.fromkeys(normalise_domain(item) for item in value))
        if "unknown" in cleaned:
            raise ValueError("merchant lists cannot contain an unknown domain")
        return cleaned

    def effective_allowed_conditions(self, requirements: RequirementSet) -> tuple[Condition, ...]:
        allowed = set(self.allowed_conditions) & set(requirements.allowed_conditions)
        return tuple(sorted(allowed, key=lambda c: c.value))

    def effective_limits(self, limits: UserLimits, requirements: RequirementSet) -> tuple[Decimal, Decimal, int]:
        max_total = min(self.max_total, limits.max_total)
        max_item = min(self.max_item_price, limits.max_per_item)
        max_qty = min(self.max_quantity_per_line, limits.max_quantity_per_line, requirements.max_quantity_per_line)
        if requirements.budget_total is not None:
            max_total = min(max_total, requirements.budget_total)
        if requirements.budget_per_item is not None:
            max_item = min(max_item, requirements.budget_per_item)
        return max_total, max_item, max_qty
