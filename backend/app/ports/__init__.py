"""Outbound port definitions. Implementations live in ``app.adapters``."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from app.domain.enums import DataMode
from app.domain.models import Product, Quote


class SourceError(Exception):
    """Base class for product-source failures."""


class SourceNotConfigured(SourceError):
    pass


class SourceAuthError(SourceError):
    pass


class SourceRateLimited(SourceError):
    pass


class SourceUnavailable(SourceError):
    pass


class SourceMalformed(SourceError):
    pass


@dataclass(frozen=True)
class ProductQuery:
    text: str
    limit: int = 8
    max_price: Decimal | None = None
    currency: str = "USD"


@dataclass
class SourceSearchResult:
    source: str
    data_mode: DataMode
    as_of: datetime
    products: tuple[Product, ...]
    raw_offer_payloads: dict[str, Any] = field(default_factory=dict)
    raw_product_payloads: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


class ProductSource(Protocol):
    name: str
    data_mode: DataMode

    def search(self, query: ProductQuery) -> SourceSearchResult: ...


class LlmError(Exception):
    pass


class LlmNotConfigured(LlmError):
    pass


class LlmClient(Protocol):
    name: str

    def structured(self, *, prompt: str, schema: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class PreparedOrder:
    provider_order_id: str
    status: str
    approve_url: str | None
    amount: Decimal
    currency: str
    environment: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class OrderState:
    provider_order_id: str
    status: str
    amount: Decimal | None
    currency: str | None
    approve_url: str | None
    capture_id: str | None
    environment: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class CaptureResult:
    provider_capture_id: str
    status: str
    amount: Decimal
    currency: str
    environment: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class VerifiedEvent:
    provider_event_id: str
    event_type: str
    signature_verified: bool
    payload: dict[str, Any]


class PaymentGateway(Protocol):
    name: str
    environment: str
    configured: bool

    def create_order(self, quote: Quote, idempotency_key: str) -> PreparedOrder: ...

    def get_order(self, provider_order_id: str) -> OrderState: ...

    def capture(self, provider_order_id: str, idempotency_key: str) -> CaptureResult: ...

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> VerifiedEvent: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
