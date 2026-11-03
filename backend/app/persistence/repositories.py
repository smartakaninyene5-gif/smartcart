"""Repositories: the only code that translates domain objects to rows."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.enums import (
    Actor,
    ApprovalStatus,
    Availability,
    Condition,
    DataMode,
    EvidenceKind,
    GuardOutcome,
    TrustLevel,
)
from app.domain.guard import GuardDecision
from app.domain.models import ApprovalFacts, Offer, Product, Quote, as_utc
from app.domain.snapshot import canonical_json, chain_hash, json_safe, payload_digest
from app.persistence.base import new_id, utcnow
from app.persistence.tables import (
    Approval,
    Capture,
    Evidence,
    IdempotencyKey,
    JournalEvent,
    OfferRecord,
    PaymentOrder,
    Product as ProductRow,
    PurchaseIntent,
    QuoteRecord,
    TransactionEvent,
    User,
)


class StoreError(Exception):
    pass


class CorruptRecord(StoreError):
    pass


class IdempotencyConflict(StoreError):
    pass


def _enum(value: type, raw: str, default):  # type: ignore[no-untyped-def]
    try:
        return value(raw)
    except ValueError:
        return default


def _offer_to_row(user_id: str, offer: Offer) -> OfferRecord:
    return OfferRecord(
        id=offer.offer_id,
        user_id=user_id,
        product_id=offer.product_id,
        source=offer.source,
        source_offer_id=offer.source_offer_id,
        offer_product_id=offer.offer_product_id,
        variant_id=offer.variant_id,
        merchant_name=offer.merchant_name,
        merchant_domain=offer.merchant_domain,
        price=offer.price,
        currency=offer.currency,
        shipping_cost=offer.shipping_cost,
        availability=offer.availability.value,
        condition=offer.condition.value,
        url=offer.url,
        redirect_url=offer.redirect_url,
        is_redirect=offer.is_redirect,
        observed_at=offer.observed_at,
        as_of=offer.as_of,
        evidence_id=offer.evidence_id,
    )


def _row_to_offer(row: OfferRecord) -> Offer:
    try:
        return Offer(
            offer_id=row.id,
            source=row.source,
            source_offer_id=row.source_offer_id,
            product_id=row.product_id,
            offer_product_id=row.offer_product_id,
            variant_id=row.variant_id,
            merchant_name=row.merchant_name,
            merchant_domain=row.merchant_domain,
            price=row.price,
            currency=row.currency,
            shipping_cost=row.shipping_cost,
            availability=_enum(Availability, row.availability, Availability.UNKNOWN),
            condition=_enum(Condition, row.condition, Condition.UNKNOWN),
            url=row.url,
            redirect_url=row.redirect_url,
            is_redirect=row.is_redirect,
            observed_at=as_utc(row.observed_at),
            as_of=as_utc(row.as_of),
            evidence_id=row.evidence_id,
        )
    except (ValueError, TypeError) as exc:
        raise CorruptRecord(f"offer row {row.id} is invalid: {exc}") from exc


class UserRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def ensure(self, user_id: str, display_name: str | None = None) -> User:
        row = self.session.get(User, user_id)
        if row is None:
            row = User(id=user_id, display_name=display_name or user_id)
            self.session.add(row)
            self.session.flush()
        return row


class ProductRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_product(self, user_id: str, product: Product) -> None:
        row = self.session.scalar(
            select(ProductRow).where(
                ProductRow.user_id == user_id,
                ProductRow.source == product.source,
                ProductRow.source_product_id == product.source_product_id,
            )
        )
        if row is None:
            row = ProductRow(
                id=product.product_id,
                user_id=user_id,
                source=product.source,
                source_product_id=product.source_product_id,
                title=product.title,
            )
            self.session.add(row)
        row.title = product.title
        row.brand = product.brand
        row.description = product.description
        row.image_url = product.image_url
        row.category = product.category
        row.attributes = dict(product.attributes)
        row.updated_at = utcnow()
        self.session.flush()

    def upsert_offer(self, user_id: str, offer: Offer) -> None:
        row = self.session.scalar(
            select(OfferRecord).where(
                OfferRecord.user_id == user_id,
                OfferRecord.source == offer.source,
                OfferRecord.source_offer_id == offer.source_offer_id,
            )
        )
        if row is None:
            self.session.add(_offer_to_row(user_id, offer))
        else:
            row.product_id = offer.product_id
            row.offer_product_id = offer.offer_product_id
            row.variant_id = offer.variant_id
            row.merchant_name = offer.merchant_name
            row.merchant_domain = offer.merchant_domain
            row.price = offer.price
            row.currency = offer.currency
            row.shipping_cost = offer.shipping_cost
            row.availability = offer.availability.value
            row.condition = offer.condition.value
            row.url = offer.url
            row.redirect_url = offer.redirect_url
            row.is_redirect = offer.is_redirect
            row.observed_at = offer.observed_at
            row.as_of = offer.as_of
            row.evidence_id = offer.evidence_id
            row.updated_at = utcnow()
        self.session.flush()

    def attach_evidence(self, user_id: str, offer_id: str, evidence_id: str) -> None:
        row = self.session.get(OfferRecord, offer_id)
        if row is None or row.user_id != user_id:
            raise CorruptRecord(f"offer {offer_id} not found for user")
        row.evidence_id = evidence_id
        self.session.flush()

    def get_offer(self, user_id: str, offer_id: str) -> Offer | None:
        row = self.session.get(OfferRecord, offer_id)
        if row is None or row.user_id != user_id:
            return None
        return _row_to_offer(row)

    def list_offers(
        self,
        user_id: str,
        *,
        product_id: str | None = None,
        offer_ids: Sequence[str] | None = None,
    ) -> list[Offer]:
        stmt = select(OfferRecord).where(OfferRecord.user_id == user_id)
        if product_id:
            stmt = stmt.where(OfferRecord.product_id == product_id)
        if offer_ids is not None:
            stmt = stmt.where(OfferRecord.id.in_(list(offer_ids)))
        rows = self.session.scalars(stmt.order_by(OfferRecord.price, OfferRecord.id)).all()
        return [_row_to_offer(row) for row in rows]

    def get_product(self, user_id: str, product_id: str) -> Product | None:
        row = self.session.get(ProductRow, product_id)
        if row is None or row.user_id != user_id:
            return None
        offers = self.list_offers(user_id, product_id=product_id)
        return Product(
            product_id=row.id,
            source=row.source,
            source_product_id=row.source_product_id,
            title=row.title,
            brand=row.brand,
            description=row.description,
            image_url=row.image_url,
            category=row.category,
            attributes=row.attributes or {},
            offers=tuple(offers),
            observed_at=as_utc(row.updated_at),
        )

    def list_products(self, user_id: str, *, limit: int = 20) -> list[Product]:
        rows = self.session.scalars(
            select(ProductRow).where(ProductRow.user_id == user_id).order_by(ProductRow.created_at.desc()).limit(limit)
        ).all()
        products: list[Product] = []
        for row in rows:
            product = self.get_product(user_id, row.id)
            if product is not None:
                products.append(product)
        return products


class EvidenceRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(
        self,
        *,
        user_id: str,
        kind: EvidenceKind,
        trust: TrustLevel,
        source: str,
        source_ref: str | None,
        subject_type: str,
        subject_id: str,
        observed_at: datetime,
        as_of: datetime | None,
        payload: dict[str, Any],
    ) -> Evidence:
        row = Evidence(
            id=new_id("evd"),
            user_id=user_id,
            kind=kind.value,
            trust=trust.value,
            source=source,
            source_ref=source_ref,
            subject_type=subject_type,
            subject_id=subject_id,
            observed_at=as_utc(observed_at),
            as_of=as_utc(as_of) if as_of else None,
            payload=payload,
            payload_sha256=payload_digest(payload),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get(self, user_id: str, evidence_id: str) -> Evidence | None:
        row = self.session.get(Evidence, evidence_id)
        if row is None or row.user_id != user_id:
            return None
        return row

    def list_for(self, user_id: str, subject_type: str, subject_id: str) -> list[Evidence]:
        return list(
            self.session.scalars(
                select(Evidence)
                .where(
                    Evidence.user_id == user_id,
                    Evidence.subject_type == subject_type,
                    Evidence.subject_id == subject_id,
                )
                .order_by(Evidence.created_at)
            ).all()
        )


class QuoteRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, quote: Quote, decision: GuardDecision, *, data_mode: DataMode = DataMode.MOCK) -> QuoteRecord:
        row = QuoteRecord(
            id=quote.quote_id,
            user_id=quote.user_id,
            status="prepared",
            currency=quote.currency,
            subtotal=quote.subtotal,
            shipping_total=quote.shipping_total,
            grand_total=quote.grand_total,
            snapshot_hash=quote.snapshot_hash,
            policy_version=quote.policy_version,
            quote_json=quote.model_dump(mode="json"),
            requirements_json=quote.requirements.model_dump(mode="json"),
            evidence_refs=list(quote.evidence_refs),
            guard_outcome=decision.outcome.value,
            guard_decision_json=decision.model_dump(mode="json"),
            data_mode=data_mode.value,
            as_of=quote.as_of,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get(self, user_id: str, quote_id: str) -> QuoteRecord | None:
        row = self.session.get(QuoteRecord, quote_id)
        if row is None or row.user_id != user_id:
            return None
        return row

    def to_domain(self, row: QuoteRecord) -> Quote:
        try:
            return Quote.model_validate(row.quote_json)
        except ValueError as exc:
            raise CorruptRecord(f"quote {row.id} failed validation: {exc}") from exc


class IntentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, user_id: str, quote_id: str, note: str | None = None) -> PurchaseIntent:
        row = PurchaseIntent(id=new_id("int"), user_id=user_id, quote_id=quote_id, status="prepared", note=note)
        self.session.add(row)
        self.session.flush()
        return row

    def for_quote(self, user_id: str, quote_id: str) -> PurchaseIntent | None:
        return self.session.scalar(
            select(PurchaseIntent)
            .where(PurchaseIntent.user_id == user_id, PurchaseIntent.quote_id == quote_id)
            .order_by(PurchaseIntent.created_at.desc())
        )

    def set_status(self, user_id: str, quote_id: str, status: str) -> None:
        row = self.for_quote(user_id, quote_id)
        if row is not None:
            row.status = status
            row.updated_at = utcnow()
            self.session.flush()


class ApprovalRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(
        self,
        *,
        user_id: str,
        quote_id: str,
        intent_id: str | None,
        snapshot_hash: str,
        policy_version: str,
        decision: GuardDecision,
        approved_at: datetime,
        expires_at: datetime,
    ) -> Approval:
        row = Approval(
            id=new_id("apr"),
            user_id=user_id,
            quote_id=quote_id,
            intent_id=intent_id,
            snapshot_hash=snapshot_hash,
            status=ApprovalStatus.APPROVED.value,
            policy_version=policy_version,
            guard_decision_json=decision.model_dump(mode="json"),
            approved_at=as_utc(approved_at),
            expires_at=as_utc(expires_at),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get(self, user_id: str, approval_id: str) -> Approval | None:
        row = self.session.get(Approval, approval_id)
        if row is None or row.user_id != user_id:
            return None
        return row

    def active_for_quote(self, user_id: str, quote_id: str) -> list[Approval]:
        return list(
            self.session.scalars(
                select(Approval).where(
                    Approval.user_id == user_id,
                    Approval.quote_id == quote_id,
                    Approval.status == ApprovalStatus.APPROVED.value,
                    Approval.consumed_at.is_(None),
                )
            ).all()
        )

    def supersede_active(self, user_id: str, quote_id: str, reason: str) -> int:
        rows = self.active_for_quote(user_id, quote_id)
        for row in rows:
            row.status = ApprovalStatus.SUPERSEDED.value
            row.reason = reason
        self.session.flush()
        return len(rows)

    def set_status(self, row: Approval, status: ApprovalStatus, reason: str | None = None) -> None:
        row.status = status.value
        if reason:
            row.reason = reason
        self.session.flush()

    def facts(self, row: Approval) -> ApprovalFacts:
        return ApprovalFacts(
            approval_id=row.id,
            quote_id=row.quote_id,
            user_id=row.user_id,
            snapshot_hash=row.snapshot_hash,
            status=row.status,
            approved_at=as_utc(row.approved_at),
            expires_at=as_utc(row.expires_at),
            consumed_at=as_utc(row.consumed_at) if row.consumed_at else None,
        )


class PaymentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add_order(
        self,
        *,
        user_id: str,
        approval_id: str,
        quote_id: str,
        provider_order_id: str,
        status: str,
        amount: Decimal,
        currency: str,
        approve_url: str | None,
        environment: str,
        raw: dict[str, Any],
    ) -> PaymentOrder:
        row = PaymentOrder(
            id=new_id("pay"),
            user_id=user_id,
            approval_id=approval_id,
            quote_id=quote_id,
            provider="paypal",
            provider_order_id=provider_order_id,
            status=status,
            amount=amount,
            currency=currency,
            approve_url=approve_url,
            environment=environment,
            raw_json=raw,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get(self, user_id: str, payment_id: str) -> PaymentOrder | None:
        row = self.session.get(PaymentOrder, payment_id)
        if row is None or row.user_id != user_id:
            return None
        return row

    def by_provider_order(self, provider_order_id: str) -> PaymentOrder | None:
        return self.session.scalar(
            select(PaymentOrder).where(
                PaymentOrder.provider == "paypal",
                PaymentOrder.provider_order_id == provider_order_id,
            )
        )

    def set_status(self, row: PaymentOrder, status: str) -> None:
        row.status = status
        row.updated_at = utcnow()
        self.session.flush()

    def capture_exists(self, quote_id: str, snapshot_hash: str) -> bool:
        return (
            self.session.scalar(
                select(Capture).where(Capture.quote_id == quote_id, Capture.snapshot_hash == snapshot_hash)
            )
            is not None
        )

    def capture_for_order(self, payment_order_id: str) -> Capture | None:
        return self.session.scalar(select(Capture).where(Capture.payment_order_id == payment_order_id))

    def add_capture(
        self,
        *,
        user_id: str,
        payment_order_id: str,
        quote_id: str,
        snapshot_hash: str,
        provider_capture_id: str,
        status: str,
        amount: Decimal,
        currency: str,
    ) -> Capture:
        row = Capture(
            id=new_id("cap"),
            user_id=user_id,
            payment_order_id=payment_order_id,
            quote_id=quote_id,
            snapshot_hash=snapshot_hash,
            provider_capture_id=provider_capture_id,
            status=status,
            amount=amount,
            currency=currency,
            retailer_purchase_state="unknown",
        )
        try:
            # Flush inside a savepoint: an IntegrityError (duplicate capture)
            # rolls back only this insert, not the surrounding request
            # transaction (journal entries, payment state, approval updates).
            # The row is added inside the savepoint: pending state is flushed
            # when the savepoint starts, so adding it before would move the
            # duplicate's IntegrityError outside the savepoint's scope.
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError as exc:
            raise StoreError("duplicate capture for this quote and snapshot") from exc
        return row


class TransactionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add_verified_event(
        self,
        *,
        user_id: str | None,
        provider: str,
        event_type: str,
        provider_event_id: str,
        payment_order_id: str | None,
        signature_verified: bool,
        payload: dict[str, Any],
        retailer_purchase_state: str = "unknown",
        note: str | None = None,
    ) -> tuple[TransactionEvent, bool]:
        if provider_event_id:
            existing = self.session.scalar(
                select(TransactionEvent).where(
                    TransactionEvent.provider == provider,
                    TransactionEvent.provider_event_id == provider_event_id,
                )
            )
            if existing is not None:
                return existing, False
        row = TransactionEvent(
            id=new_id("txe"),
            user_id=user_id,
            provider=provider,
            event_type=event_type,
            provider_event_id=provider_event_id or None,
            payment_order_id=payment_order_id,
            signature_verified=signature_verified,
            retailer_purchase_state=retailer_purchase_state,
            payload=payload,
            note=note,
        )
        self.session.add(row)
        self.session.flush()
        return row, True


class IdempotencyRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def begin(
        self, *, key: str, scope: str, fingerprint: str, expires_at: datetime
    ) -> tuple[IdempotencyKey, bool]:
        existing = self.session.get(IdempotencyKey, key)
        if existing is not None:
            if existing.fingerprint != fingerprint:
                raise IdempotencyConflict("same idempotency key used with a different request")
            return existing, False
        row = IdempotencyKey(
            key=key,
            scope=scope,
            fingerprint=fingerprint,
            status="in_progress",
            expires_at=as_utc(expires_at),
        )
        try:
            with self.session.begin_nested():
                self.session.add(row)
        except IntegrityError:
            existing = self.session.get(IdempotencyKey, key)
            if existing is None:
                raise StoreError("idempotency key race could not be resolved")
            if existing.fingerprint != fingerprint:
                raise IdempotencyConflict("same idempotency key used with a different request")
            return existing, False
        return row, True

    def complete(self, key: str, response_ref: str) -> None:
        row = self.session.get(IdempotencyKey, key)
        if row is None:
            raise StoreError(f"idempotency key {key} not found")
        row.status = "completed"
        row.response_ref = response_ref
        self.session.flush()

    def fail(self, key: str, reason: str) -> None:
        row = self.session.get(IdempotencyKey, key)
        if row is None:
            return
        row.status = "failed"
        row.response_ref = reason[:200]
        self.session.flush()


class JournalRepository:
    """Append-only hash chain. Rows are never updated or deleted."""

    GENESIS = "0" * 64

    def __init__(self, session: Session) -> None:
        self.session = session

    def append(
        self,
        *,
        actor: Actor,
        event_type: str,
        subject_type: str,
        subject_id: str,
        payload: dict[str, Any],
        user_id: str | None = None,
        correlation_id: str | None = None,
        environment: str = "dev",
        occurred_at: datetime | None = None,
    ) -> JournalEvent:
        previous = self.session.scalar(select(JournalEvent).order_by(JournalEvent.seq.desc()).limit(1))
        prev_hash = previous.entry_hash if previous else self.GENESIS
        occurred = as_utc(occurred_at or utcnow())
        event_id = new_id("jrn")
        safe_payload = json_safe(payload)
        body = {
            "event_id": event_id,
            "user_id": user_id,
            "actor": actor.value,
            "event_type": event_type,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "correlation_id": correlation_id,
            "environment": environment,
            "occurred_at": occurred,
            "payload": safe_payload,
        }
        entry_payload = canonical_json(body)
        row = JournalEvent(
            event_id=event_id,
            user_id=user_id,
            actor=actor.value,
            event_type=event_type,
            subject_type=subject_type,
            subject_id=subject_id,
            correlation_id=correlation_id,
            environment=environment,
            payload=safe_payload,
            entry_payload=entry_payload,
            prev_hash=prev_hash,
            entry_hash=chain_hash(prev_hash, entry_payload),
            occurred_at=occurred,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def list(self, *, user_id: str | None = None, limit: int = 100, after_seq: int = 0) -> list[JournalEvent]:
        stmt = select(JournalEvent).where(JournalEvent.seq > after_seq)
        if user_id:
            stmt = stmt.where((JournalEvent.user_id == user_id) | (JournalEvent.user_id.is_(None)))
        rows = self.session.scalars(stmt.order_by(JournalEvent.seq.desc()).limit(min(limit, 500))).all()
        return list(reversed(rows))

    def verify_chain(self) -> tuple[bool, int | None]:
        rows = self.session.scalars(select(JournalEvent).order_by(JournalEvent.seq)).all()
        previous = self.GENESIS
        for row in rows:
            if row.prev_hash != previous:
                return False, row.seq
            if chain_hash(row.prev_hash, row.entry_payload) != row.entry_hash:
                return False, row.seq
            previous = row.entry_hash
        return True, None


def evidence_is_fact(kind: str, trust: str) -> bool:
    """AI statements are evidence of what was said, never evidence of a fact."""

    if kind == EvidenceKind.AI_STATEMENT.value:
        return False
    try:
        return TrustLevel(trust).is_fact
    except ValueError:
        return False
