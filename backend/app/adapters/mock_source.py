"""Deterministic mock product source.

This exists so the whole deterministic core can be exercised without an API
key. It is *always* labelled ``data_mode=MOCK`` and ``source=mock``; nothing
downstream may present it as live data.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from app.adapters.normalize import normalise_product
from app.domain.enums import DataMode
from app.domain.models import Product, as_utc
from app.ports import ProductQuery, SourceMalformed, SourceSearchResult

_FIXTURE = Path(__file__).with_name("fixtures") / "channel3_search.json"


class MockProductSource:
    name = "mock"
    data_mode = DataMode.MOCK

    def __init__(self, clock, fixture_path: Path | None = None) -> None:
        self._clock = clock
        self._fixture_path = fixture_path or _FIXTURE

    def _load(self) -> dict:
        try:
            payload = json.loads(self._fixture_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SourceMalformed(f"mock fixture unreadable: {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
            raise SourceMalformed("mock fixture has no products array")
        return payload

    def search(self, query: ProductQuery) -> SourceSearchResult:
        payload = self._load()
        now = as_utc(self._clock.now())
        products: list[Product] = []
        raw_offers: dict[str, dict] = {}
        raw_products: dict[str, dict] = {}
        warnings: list[str] = []

        for raw in payload["products"]:
            try:
                normalised = normalise_product(
                    raw,
                    source=self.name,
                    observed_at=now,
                    as_of_default=now,
                    default_currency=query.currency,
                )
            except SourceMalformed as exc:
                warnings.append(str(exc))
                continue
            warnings.extend(normalised.warnings)
            offers = normalised.product.offers
            if query.max_price is not None:
                offers = tuple(offer for offer in offers if offer.price <= query.max_price)
            if not offers:
                continue
            product = normalised.product.model_copy(update={"offers": offers})
            products.append(product)
            raw_products[product.product_id] = normalised.raw_product
            for offer in offers:
                raw_offers[offer.offer_id] = normalised.raw_offers.get(offer.offer_id, {})
            if len(products) >= query.limit:
                break

        if not products:
            warnings.append("mock source returned no products matching the query")

        return SourceSearchResult(
            source=self.name,
            data_mode=DataMode.MOCK,
            as_of=now,
            products=tuple(products),
            raw_offer_payloads=raw_offers,
            raw_product_payloads=raw_products,
            warnings=tuple(warnings),
        )
