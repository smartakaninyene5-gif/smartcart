"""Product discovery use case: observe, normalise, evidence, journal."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.application.errors import SourceFailure
from app.domain.enums import Actor, DataMode, EvidenceKind, TrustLevel
from app.domain.models import Product
from app.journal import JournalEvents, JournalWriter
from app.persistence.repositories import EvidenceRepository, ProductRepository, UserRepository
from app.ports import ProductQuery, ProductSource, SourceError


@dataclass
class SearchOutcome:
    source: str
    data_mode: DataMode
    as_of: object
    products: tuple[Product, ...]
    warnings: tuple[str, ...]
    evidence_count: int


def search_products(
    session: Session,
    *,
    user_id: str,
    query: ProductQuery,
    source: ProductSource,
    environment: str,
    correlation_id: str | None = None,
) -> SearchOutcome:
    users = UserRepository(session)
    users.ensure(user_id)
    journal = JournalWriter(session, environment=environment)
    products = ProductRepository(session)
    evidence = EvidenceRepository(session)

    try:
        result = source.search(query)
    except SourceError as exc:
        journal.failure(
            error_code="SOURCE_FAILURE",
            message=str(exc),
            subject_type="search",
            subject_id=query.text[:180],
            user_id=user_id,
            correlation_id=correlation_id,
        )
        raise SourceFailure(str(exc)) from exc

    evidence_ids: list[str] = []
    for product in result.products:
        raw_product = result.raw_product_payloads.get(product.product_id)
        product_evidence = evidence.add(
            user_id=user_id,
            kind=EvidenceKind.SOURCE_OBSERVATION,
            trust=TrustLevel.SOURCE_OBSERVED,
            source=result.source,
            source_ref=product.source_product_id,
            subject_type="product",
            subject_id=product.product_id,
            observed_at=product.observed_at,
            as_of=result.as_of,
            payload={"raw": raw_product} if raw_product is not None else {"product_id": product.product_id},
        )
        evidence_ids.append(product_evidence.id)
        products.upsert_product(user_id, product)

        for offer in product.offers:
            raw_offer = result.raw_offer_payloads.get(offer.offer_id, {})
            row = evidence.add(
                user_id=user_id,
                kind=EvidenceKind.SOURCE_OBSERVATION,
                trust=TrustLevel.SOURCE_OBSERVED,
                source=result.source,
                source_ref=offer.source_offer_id,
                subject_type="offer",
                subject_id=offer.offer_id,
                observed_at=offer.observed_at,
                as_of=offer.as_of,
                payload={
                    "product_id": offer.product_id,
                    "merchant_domain": offer.merchant_domain,
                    "price": str(offer.price),
                    "currency": offer.currency,
                    "offer": raw_offer,
                },
            )
            evidence_ids.append(row.id)
            observed_offer = offer.model_copy(update={"evidence_id": row.id})
            products.upsert_offer(user_id, observed_offer)
            journal.record(
                JournalEvents.OFFER_OBSERVED,
                actor=Actor.SYSTEM,
                subject_type="offer",
                subject_id=offer.offer_id,
                user_id=user_id,
                correlation_id=correlation_id,
                payload={
                    "price": str(offer.price),
                    "currency": offer.currency,
                    "merchant_domain": offer.merchant_domain,
                    "availability": offer.availability.value,
                    "condition": offer.condition.value,
                    "as_of": offer.as_of.isoformat(),
                    "is_redirect": offer.is_redirect,
                    "evidence_id": row.id,
                },
            )

    journal.record(
        JournalEvents.DISCOVERY_SEARCHED,
        actor=Actor.SYSTEM,
        subject_type="search",
        subject_id=query.text[:180],
        user_id=user_id,
        correlation_id=correlation_id,
        payload={
            "source": result.source,
            "data_mode": result.data_mode.value,
            "query": query.text,
            "product_count": len(result.products),
            "offer_count": sum(len(p.offers) for p in result.products),
            "as_of": result.as_of.isoformat() if hasattr(result.as_of, "isoformat") else str(result.as_of),
            "warnings": list(result.warnings),
        },
    )

    stored_products: list[Product] = []
    for product in result.products:
        stored = products.get_product(user_id, product.product_id)
        if stored is not None:
            stored_products.append(stored)

    return SearchOutcome(
        source=result.source,
        data_mode=result.data_mode,
        as_of=result.as_of,
        products=tuple(stored_products),
        warnings=result.warnings,
        evidence_count=len(evidence_ids),
    )
