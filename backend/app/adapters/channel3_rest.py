"""Channel3 REST adapter (preferred production source).

Live verification is BLOCKED: no API key exists in this environment and
unauthenticated calls return 403 (verified in Milestone 0). The documented
request/response shape is implemented and contract-tested with a mock
transport; live response parsing remains UNVERIFIED.
"""

from __future__ import annotations

import httpx

from app.adapters.normalize import normalise_product, parse_datetime
from app.domain.enums import DataMode
from app.domain.models import Product, as_utc
from app.ports import (
    ProductQuery,
    ProductSource,
    SourceAuthError,
    SourceMalformed,
    SourceNotConfigured,
    SourceRateLimited,
    SourceSearchResult,
    SourceUnavailable,
)


class Channel3RestSource:
    name = "channel3"
    data_mode = DataMode.LIVE

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        clock,
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise SourceNotConfigured("Channel3 API key is not configured")
        self._api_key = api_key
        self._clock = clock
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json", "x-api-key": api_key},
        )

    def close(self) -> None:
        self._client.close()

    def _extract_products(self, payload: object) -> list[dict]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ("products", "results", "data"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []

    def search(self, query: ProductQuery) -> SourceSearchResult:
        try:
            response = self._client.post(
                "/v1/search",
                json={"query": query.text, "limit": query.limit},
            )
        except httpx.TimeoutException as exc:
            raise SourceUnavailable("Channel3 search timed out") from exc
        except httpx.HTTPError as exc:
            raise SourceUnavailable("Channel3 search transport error") from exc

        if response.status_code in (401, 403):
            raise SourceAuthError("Channel3 rejected the API key (HTTP %s)" % response.status_code)
        if response.status_code == 429:
            raise SourceRateLimited("Channel3 rate limit reached (HTTP 429)")
        if response.status_code >= 500:
            raise SourceUnavailable(f"Channel3 is unavailable (HTTP {response.status_code})")
        if response.status_code >= 400:
            raise SourceMalformed(f"Channel3 rejected the search (HTTP {response.status_code})")

        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceMalformed("Channel3 returned non-JSON body") from exc

        now = as_utc(self._clock.now())
        as_of_raw = payload.get("as_of") if isinstance(payload, dict) else None
        as_of = parse_datetime(as_of_raw, now)
        products: list[Product] = []
        raw_offers: dict[str, dict] = {}
        raw_products: dict[str, dict] = {}
        warnings: list[str] = []

        for raw in self._extract_products(payload):
            try:
                normalised = normalise_product(
                    raw,
                    source=self.name,
                    observed_at=now,
                    as_of_default=as_of,
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
            warnings.append("Channel3 returned no usable products")
        if as_of_raw is None:
            warnings.append("Channel3 response had no as_of timestamp; using request time")

        return SourceSearchResult(
            source=self.name,
            data_mode=DataMode.LIVE,
            as_of=as_of,
            products=tuple(products),
            raw_offer_payloads=raw_offers,
            raw_product_payloads=raw_products,
            warnings=tuple(warnings),
        )
