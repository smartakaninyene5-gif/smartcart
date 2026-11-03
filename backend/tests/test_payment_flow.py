"""Payment workflows against a scripted gateway: amounts, idempotency, dedupe.

No real PayPal calls; the FakeGateway is deterministic and observationally
equivalent for the deterministic core's contract.
"""
from __future__ import annotations

from decimal import Decimal

from tests.conftest import find_offer, make_quote


def _quote_and_approval(client, fake):
    search = client.post("/api/v1/search", json={"query": "headphones"}).json()
    sony = find_offer(search, "bestbuy.com")
    quote = make_quote(client, sony["offer_id"])
    response = client.post(f"/api/v1/quotes/{quote['quote_id']}/approve", json={
        "snapshot_hash": quote["snapshot_hash"],
        "explicit_consent": True,
    })
    assert response.status_code == 200, response.text
    approval_id = response.json()["approval"]["approval_id"]
    fake.amount = Decimal(str(quote["grand_total"]))
    return quote, approval_id


def _fresh_and_order(client, fake):
    _, approval_id = _quote_and_approval(client, fake)
    response = client.post("/api/v1/payments/orders", json={"approval_id": approval_id})
    return response, approval_id


def test_payment_order_created_with_fake_gateway(client, container):
    from tests.fakes import FakeGateway

    fake = FakeGateway()
    container.payment_gateway = fake
    response, _ = _fresh_and_order(client, fake)
    assert response.status_code == 201, response.text
    body = response.json()
    assert str(body["payment"]["amount"]) in ("128.00", "128.0")
    assert body["sandbox"] is True
    assert "container" and "not" in body.get("disclosure", "").lower()
    container.payment_gateway = None


def test_payment_order_idempotent_replay(client, container):
    from tests.fakes import FakeGateway

    fake = FakeGateway()
    container.payment_gateway = fake
    first, approval_id = _fresh_and_order(client, fake)
    first_payment = first.json()["payment"]
    second = client.post("/api/v1/payments/orders", json={"approval_id": approval_id}).json()
    second_payment = second["payment"]
    assert second_payment["payment_id"] == first_payment["payment_id"]
    assert second_payment["replayed"] is True
    container.payment_gateway = None


def test_capture_success_contract_and_replay(client, container):
    from tests.fakes import FakeGateway

    fake = FakeGateway()
    container.payment_gateway = fake
    order, _ = _fresh_and_order(client, fake)
    payment_id = order.json()["payment"]["payment_id"]

    captured = client.post(f"/api/v1/payments/orders/{payment_id}/capture")
    assert captured.status_code == 200, captured.text
    body = captured.json()
    assert str(body["capture"]["amount"]) in ("128.00", "128.0")
    assert body["retailer_purchase_state"] == "unknown"
    assert "retailer" in body.get("disclaimer", "").lower()

    replay = client.post(f"/api/v1/payments/orders/{payment_id}/capture")
    assert replay.status_code == 200 and replay.json().get("replayed") is True
    container.payment_gateway = None


def test_webhook_accepted_once_then_deduped(client, container):
    from tests.fakes import FakeGateway

    fake = FakeGateway()
    container.payment_gateway = fake
    order, _ = _fresh_and_order(client, fake)
    payment_id = order.json()["payment"]["payment_id"]
    captured = client.post(f"/api/v1/payments/orders/{payment_id}/capture")
    assert captured.status_code == 200

    event = {
        "id": "CAP-TEST-1",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {
            "id": "CAP-TEST-1",
            "supplementary_data": {"related_ids": {"order_id": "PAY-TEST-1"}},
            "amount": {"value": "128.00", "currency_code": "USD"},
        },
    }
    first = client.post("/webhooks/paypal", json=event)
    assert first.status_code == 200
    second = client.post("/webhooks/paypal", json=event)
    assert second.status_code == 200 and second.json().get("duplicate") is True
    container.payment_gateway = None


def test_provider_amount_mismatch_at_order_creation_rejected(client, container):
    from tests.fakes import FakeGateway

    fake = FakeGateway()
    container.payment_gateway = fake
    fake.force_amount = Decimal("1.00")
    response, _ = _fresh_and_order(client, fake)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "PAYMENT_PROVIDER_ERROR"
    container.payment_gateway = None


def test_amount_mismatch_at_capture_refused(client, container):
    from tests.fakes import FakeGateway

    fake = FakeGateway()
    container.payment_gateway = fake
    order, _ = _fresh_and_order(client, fake)  # force_amount=None here
    payment_id = order.json()["payment"]["payment_id"]

    fake.force_amount = Decimal("1.00")  # provider now reports a different amount
    captured = client.post(f"/api/v1/payments/orders/{payment_id}/capture")
    assert captured.status_code in (409, 502)
    container.payment_gateway = None


def test_gateway_failure_is_opaque_provider_error(client, container):
    from tests.fakes import FakeGateway

    class ExplodingGateway(FakeGateway):
        def create_order(self, quote, idempotency_key):
            raise TimeoutError("boom")

    fake = ExplodingGateway()
    container.payment_gateway = fake
    response, _ = _fresh_and_order(client, fake)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "PAYMENT_PROVIDER_ERROR"
    assert "TimeoutError" in str(response.json()["error"]["message"])
    container.payment_gateway = None


def test_expired_approval_cannot_create_order(app, client, container):
    from tests.fakes import FakeGateway

    fake = FakeGateway()
    container.payment_gateway = fake
    _, approval_id = _quote_and_approval(client, fake)
    app.state.container.clock.advance(minutes=16)  # past evidence age

    response = client.post("/api/v1/payments/orders", json={"approval_id": approval_id})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "APPROVAL_NOT_VALID"
    container.payment_gateway = None
