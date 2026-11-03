"""Pytest fixtures: isolated SQLite app instance, fixed clock, API client.

Every test gets its own database file and app container. All external
services are disabled by default (mock product source, no AI, no PayPal
credentials); a FakeGateway can be swapped into ``container.payment_gateway``
when the payment flow is under test. No live credentials are required.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.adapters.clock import FixedClock
from app.config import Settings
from app.container import create_app

NOW = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def app(tmp_path):
    """Fresh FastAPI app with an isolated SQLite file and a fixed clock."""
    settings = Settings(
        _env_file=None,
        env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'test.db'}",
        product_source="mock",
        ai_provider="none",
        paypal_client_id=None,
        paypal_client_secret=None,
        guard_max_evidence_age_seconds=900,
        guard_approval_ttl_seconds=1800,
    )
    return create_app(settings, clock=FixedClock(NOW))


@pytest.fixture()
def container(app):
    return app.state.container


@pytest.fixture()
def client(app):
    return TestClient(app)


@pytest.fixture()
def now():
    return NOW


def find_offer(search_payload: dict, merchant_domain: str) -> dict:
    """Locate one offer by merchant in a /search response."""
    for product in search_payload["products"]:
        for offer in product["offers"]:
            if offer["merchant_domain"] == merchant_domain:
                return offer
    raise AssertionError(f"no offer for {merchant_domain}")


def make_quote(client, offer_id: str, *, quantity: int = 1, user_limits: dict | None = None):
    """Standard ALLOW quote request through the API."""
    body: dict = {
        "selections": [{"offer_id": offer_id, "quantity": quantity}],
        "requirements": {
            "currency": "USD",
            "budget_total": "200",
            "max_quantity_per_line": 3,
            "allowed_conditions": ["new"],
        },
    }
    if user_limits:
        body["user_limits"] = user_limits
    response = client.post("/api/v1/quotes", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def approve_default(client, quote: dict, **overrides) -> dict:
    """Approve a quote with the minimum consent payload."""
    payload = {"snapshot_hash": quote["snapshot_hash"], "explicit_consent": True, **overrides}
    return client.post(f"/api/v1/quotes/{quote['quote_id']}/approve", json=payload)
