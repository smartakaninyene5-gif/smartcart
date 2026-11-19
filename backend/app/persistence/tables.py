"""Relational tables. Portable across SQLite and PostgreSQL."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.persistence.base import Base, UTCDateTime, utcnow


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("user_id", "source", "source_product_id", name="uq_products_user_source"),)

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), index=True)
    source: Mapped[str] = mapped_column(String(32))
    source_product_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(1000))
    brand: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(200), nullable=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class OfferRecord(Base):
    __tablename__ = "offers"
    __table_args__ = (
        UniqueConstraint("user_id", "source", "source_offer_id", name="uq_offers_user_source"),
        Index("ix_offers_user_product", "user_id", "product_id"),
    )

    id: Mapped[str] = mapped_column(String(180), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), index=True)
    product_id: Mapped[str] = mapped_column(String(128), ForeignKey("products.id"), index=True)
    source: Mapped[str] = mapped_column(String(32))
    source_offer_id: Mapped[str] = mapped_column(String(180))
    offer_product_id: Mapped[str] = mapped_column(String(128))
    variant_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    merchant_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    merchant_domain: Mapped[str] = mapped_column(String(200))
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3))
    shipping_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    availability: Mapped[str] = mapped_column(String(32))
    condition: Mapped[str] = mapped_column(String(32))
    url: Mapped[str] = mapped_column(Text)
    redirect_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_redirect: Mapped[bool] = mapped_column(Boolean, default=False)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime())
    as_of: Mapped[datetime] = mapped_column(UTCDateTime())
    evidence_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class Evidence(Base):
    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    trust: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(64))
    source_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    subject_type: Mapped[str] = mapped_column(String(40))
    subject_id: Mapped[str] = mapped_column(String(180), index=True)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime())
    as_of: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    payload_sha256: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class QuoteRecord(Base):
    __tablename__ = "quotes"
    __table_args__ = (Index("ix_quotes_user_created", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="prepared")
    currency: Mapped[str] = mapped_column(String(3))
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    shipping_total: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    grand_total: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    snapshot_hash: Mapped[str] = mapped_column(String(80), index=True)
    policy_version: Mapped[str] = mapped_column(String(32))
    quote_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    requirements_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    evidence_refs: Mapped[list[str]] = mapped_column(JSON)
    guard_outcome: Mapped[str] = mapped_column(String(16))
    guard_decision_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    data_mode: Mapped[str] = mapped_column(String(8), default="MOCK")
    as_of: Mapped[datetime] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class PurchaseIntent(Base):
    __tablename__ = "purchase_intents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), index=True)
    quote_id: Mapped[str] = mapped_column(String(64), ForeignKey("quotes.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="prepared")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (Index("ix_approvals_quote_status", "quote_id", "status"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), index=True)
    quote_id: Mapped[str] = mapped_column(String(64), ForeignKey("quotes.id"), index=True)
    intent_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("purchase_intents.id"), nullable=True)
    snapshot_hash: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(32), default="approved")
    policy_version: Mapped[str] = mapped_column(String(32))
    guard_decision_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    approved_at: Mapped[datetime] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    rejected_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class PaymentOrder(Base):
    __tablename__ = "payment_orders"
    __table_args__ = (UniqueConstraint("provider", "provider_order_id", name="uq_payment_provider_order"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), index=True)
    approval_id: Mapped[str] = mapped_column(String(64), ForeignKey("approvals.id"), index=True)
    quote_id: Mapped[str] = mapped_column(String(64), ForeignKey("quotes.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32), default="paypal")
    provider_order_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3))
    approve_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    environment: Mapped[str] = mapped_column(String(16))
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class Capture(Base):
    __tablename__ = "captures"
    __table_args__ = (UniqueConstraint("quote_id", "snapshot_hash", name="uq_capture_quote_snapshot"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), index=True)
    payment_order_id: Mapped[str] = mapped_column(String(64), ForeignKey("payment_orders.id"), index=True)
    quote_id: Mapped[str] = mapped_column(String(64), ForeignKey("quotes.id"), index=True)
    snapshot_hash: Mapped[str] = mapped_column(String(80))
    provider_capture_id: Mapped[str] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(32))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3))
    retailer_purchase_state: Mapped[str] = mapped_column(String(32), default="unknown")
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class TransactionEvent(Base):
    __tablename__ = "transaction_events"
    __table_args__ = (UniqueConstraint("provider", "provider_event_id", name="uq_transaction_provider_event"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("users.id"), nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(32))
    event_type: Mapped[str] = mapped_column(String(80))
    provider_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payment_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    signature_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    retailer_purchase_state: Mapped[str] = mapped_column(String(32), default="unknown")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    scope: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="in_progress")
    response_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())


class JournalEvent(Base):
    __tablename__ = "journal_events"
    __table_args__ = (Index("ix_journal_user_seq", "user_id", "seq"),)

    seq: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    actor: Mapped[str] = mapped_column(String(16))
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    subject_type: Mapped[str] = mapped_column(String(40))
    subject_id: Mapped[str] = mapped_column(String(180), index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    environment: Mapped[str] = mapped_column(String(16), default="dev")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    entry_payload: Mapped[str] = mapped_column(Text)
    prev_hash: Mapped[str] = mapped_column(String(64))
    entry_hash: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime())
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class ShoppingRequest(Base):
    """A user's natural-language shopping request and its lifecycle."""

    __tablename__ = "shopping_requests"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), index=True)
    raw_text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="understood")
    understanding_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    clarifications: Mapped[list[str]] = mapped_column(JSON, default=list)
    confirmed_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    understood_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class ShoppingSearch(Base):
    """One executed shopping search with its full deterministic result."""

    __tablename__ = "shopping_searches"
    __table_args__ = (Index("ix_shopping_searches_user", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), index=True)
    request_id: Mapped[str] = mapped_column(String(64), ForeignKey("shopping_requests.id"), index=True)
    query_text: Mapped[str] = mapped_column(String(300))
    criteria_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    data_mode: Mapped[str] = mapped_column(String(8), default="MOCK")
    source: Mapped[str] = mapped_column(String(32), default="mock")
    research_performed: Mapped[int] = mapped_column(Integer, default=0)
    as_of: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class ShoppingResearch(Base):
    """One targeted KERNEL research action and what it found."""

    __tablename__ = "shopping_research"
    __table_args__ = (Index("ix_shopping_research_search", "search_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), index=True)
    search_id: Mapped[str] = mapped_column(String(64), ForeignKey("shopping_searches.id"), index=True)
    product_id: Mapped[str] = mapped_column(String(128))
    offer_id: Mapped[str] = mapped_column(String(180))
    url: Mapped[str] = mapped_column(Text)
    kernel_session_id: Mapped[str] = mapped_column(String(128))
    fields: Mapped[list[str]] = mapped_column(JSON, default=list)
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    limitations: Mapped[list[str]] = mapped_column(JSON, default=list)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


__all__ = [
    "User",
    "Product",
    "OfferRecord",
    "Evidence",
    "QuoteRecord",
    "PurchaseIntent",
    "Approval",
    "PaymentOrder",
    "Capture",
    "TransactionEvent",
    "IdempotencyKey",
    "JournalEvent",
    "ShoppingRequest",
    "ShoppingSearch",
    "ShoppingResearch",
]
