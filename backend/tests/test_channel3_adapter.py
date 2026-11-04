"""Channel3RestSource contract tests over an httpx mock transport.

These tests verify the adapter's HTTP contract, error mapping and response
parsing exactly as documented (https://channel3.ai/docs/guides/make-a-search,
POST /v1/search with ``x-api-key``). No live credentials or network is used:
every response is synthesised by ``httpx.MockTransport``.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.adapters.channel3_rest import Channel3RestSource
from app.adapters.clock import FixedClock
from app.ports import (
    ProductQuery,
    SourceAuthError,
    SourceMalformed,
    SourceRateLimited,
    SourceUnavailable,
)
from tests.conftest import NOW

FIXTURE = Path(__file__).parents[1] / "app" / "adapters" / "fixtures" / "channel3_search.json"


def make_source(handler) -> Channel3RestSource:
    transport = httpx.MockTransport(handler)
    source = Channel3RestSource(
        "test-api-key",  # synthetic; never a real credential
        base_url="https://api.testchannel3",
        clock=FixedClock(NOW),
        transport=transport,
    )
    return source


def respond(handler) -> Channel3RestSource:
    return make_source(handler)


def _fixed_body(status_code: int, body: object | None = None, *, text: str | None = None):
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        if text is not None:
            return httpx.Response(status_code, text=text)
        if body is None:
            return httpx.Response(status_code)
        return httpx.Response(status_code, json=body)

    return handler


def _raise(exc: Exception):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


def _query() -> ProductQuery:
    return ProductQuery(text="wireless headphones", limit=8)


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------

def test_request_shape_matches_documented_api():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["auth_header"] = request.headers.get("x-api-key")
        captured["json"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"products": [], "as_of": "2026-10-31T12:00:00Z"})

    source = make_source(handler)
    source.search(_query())

    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.testchannel3/v1/search"
    assert captured["auth_header"] == "test-api-key"
    assert captured["json"] == {"query": "wireless headphones", "limit": 8}


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_maps_to_source_auth_error(status):
    source = respond(_fixed_body(status, {"message": "Forbidden"}))
    with pytest.raises(SourceAuthError):
        source.search(_query())


def test_rate_limit_maps_to_source_rate_limited():
    source = respond(_fixed_body(429, {"message": "rate limited"}))
    with pytest.raises(SourceRateLimited):
        source.search(_query())


def test_server_error_maps_to_source_unavailable():
    source = respond(_fixed_body(500, {"message": "boom"}))
    with pytest.raises(SourceUnavailable):
        source.search(_query())


def test_client_error_maps_to_source_malformed():
    source = respond(_fixed_body(400, {"message": "bad query"}))
    with pytest.raises(SourceMalformed):
        source.search(_query())


def test_timeout_maps_to_source_unavailable():
    source = respond(_raise(httpx.ReadTimeout("timed out")))
    with pytest.raises(SourceUnavailable, match="timed out"):
        source.search(_query())


def test_transport_error_maps_to_source_unavailable():
    source = respond(_raise(httpx.ConnectError("connection refused")))
    with pytest.raises(SourceUnavailable, match="transport error"):
        source.search(_query())


def test_non_json_body_maps_to_source_malformed():
    source = respond(_fixed_body(200, None, text="<html>gateway error</html>"))
    with pytest.raises(SourceMalformed, match="non-JSON"):
        source.search(_query())


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def test_documented_payload_shape_parses_into_domain_objects():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    result = make_source(handler).search(_query())

    assert result.source == "channel3"
    assert result.data_mode.value == "LIVE"
    assert len(result.products) == 4
    first = result.products[0]
    assert first.product_id == "channel3:mock-sony-ch730"
    assert first.title.startswith("Sony WH-CH730N")
    assert first.brand == "Sony"
    offer = first.offers[0]
    assert offer.merchant_domain == "bestbuy.com"
    assert offer.price == Decimal("128.0")
    assert offer.is_redirect is True  # buy./cdn.trychannel3.com redirector
    # Raw payloads retained for evidence storage (round-tripped through the
    # mock transport, so equality rather than identity).
    assert result.raw_product_payloads[first.product_id] == payload["products"][0]
    # The fixture's deliberately price-less offer is skipped with a warning.
    assert result.warnings == ("mock-anker-q30: offer #1 has no usable price; skipped",)


def test_broken_offers_are_skipped_with_warning_not_invented():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    result = make_source(handler).search(_query())
    anker = next(p for p in result.products if p.source_product_id == "mock-anker-q30")
    assert [o.merchant_domain for o in anker.offers] == ["amazon.com"]
    # The price-less offer was skipped, not silently dropped or invented.
    assert "mock-anker-q30: offer #1 has no usable price; skipped" in result.warnings


@pytest.mark.parametrize("wrapper", ["list", "results", "data"])
def test_payload_container_variants_are_supported(wrapper):
    product = {
        "id": "p1",
        "title": "A product",
        "offers": [
            {
                "url": "https://shop.example/p1",
                "domain": "shop.example",
                "price": {"price": 10.0, "currency": "USD"},
            }
        ],
    }
    body: object | None = {
        "results": [product],
        "data": [product],
    }
    if wrapper == "list":
        body = [product]
    else:
        body = {wrapper: [product], "as_of": "2026-10-31T12:00:00Z"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    result = make_source(handler).search(_query())
    assert len(result.products) == 1
    assert result.products[0].product_id == "channel3:p1"


def test_empty_result_set_reports_warning():
    source = respond(_fixed_body(200, {"products": [], "as_of": "2026-10-31T12:00:00Z"}))
    result = source.search(_query())
    assert result.products == ()
    assert any("no usable products" in w for w in result.warnings)


def test_response_without_as_of_uses_request_time():
    source = respond(_fixed_body(200, {"products": []}))
    result = source.search(_query())
    assert result.as_of == NOW
    assert any("no as_of" in w for w in result.warnings)


def test_max_price_filter_drops_dearer_offers():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    query = ProductQuery(text="wireless headphones", limit=8, max_price=Decimal("130"))
    result = make_source(handler).search(query)

    for product in result.products:
        for offer in product.offers:
            assert offer.price <= Decimal("130")
    # The Sony product keeps only its Best Buy offer (128.00) and drops 132.50.
    sony = next(p for p in result.products if p.source_product_id == "mock-sony-ch730")
    assert [o.price for o in sony.offers] == [Decimal("128.0")]


def test_constructor_refuses_empty_key_without_calling_network():
    with pytest.raises(Exception) as excinfo:  # SourceNotConfigured
        Channel3RestSource(
            "   ",
            base_url="https://api.testchannel3",
            clock=FixedClock(NOW),
            transport=httpx.MockTransport(_fixed_body(200, {})),
        )
    assert "not configured" in str(excinfo.value)
