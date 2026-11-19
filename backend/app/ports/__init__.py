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


class LlmAuthError(LlmError):
    pass


class LlmRateLimited(LlmError):
    pass


class LlmTimeout(LlmError):
    pass


class LlmUnavailable(LlmError):
    pass


class LlmModelUnavailable(LlmError):
    pass


class LlmMalformed(LlmError):
    pass


class KernelError(Exception):
    """Base class for KERNEL browser-research failures."""


class KernelNotConfigured(KernelError):
    pass


class KernelAuthError(KernelError):
    pass


class KernelRateLimited(KernelError):
    pass


class KernelTimeout(KernelError):
    pass


class KernelUnavailable(KernelError):
    pass


class KernelMalformed(KernelError):
    pass


class KernelUnsafeUrl(KernelError):
    """The requested URL failed safety validation (never navigated)."""


@dataclass(frozen=True)
class KernelBrowserSession:
    session_id: str
    deleted: bool = False


@dataclass(frozen=True)
class KernelPage:
    """What the browser observed on one page. Content is untrusted data."""

    url: str
    final_url: str
    title: str
    text: str
    redirected: bool


@dataclass
class KernelResearchResult:
    session_id: str
    pages: list[KernelPage] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.pages)


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


class KernelBrowserClient(Protocol):
    name: str

    def create_session(self) -> KernelBrowserSession: ...

    def visit(self, session_id: str, url: str) -> KernelPage:
        """Navigate to a validated public URL and read title + visible text.

        Read-only: implementations must never generate actions (form fill,
        clicks, typing); page content is returned, never executed.
        """
        ...

    def delete_session(self, session_id: str) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
