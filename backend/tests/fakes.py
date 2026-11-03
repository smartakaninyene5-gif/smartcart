"""A scripted in-process PayPal gateway for payment-flow tests.

Mirrors the audit's FakeGateway. No network, no credentials, deterministic
amounts so the guard's G-013 check can pass or fail on demand.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.ports import CaptureResult, OrderState, PreparedOrder, VerifiedEvent


class FakeGateway:
    name = "paypal"
    environment = "sandbox"
    configured = True

    def __init__(self) -> None:
        self.capture_calls = 0
        self.force_amount: Decimal | None = None
        # The order/capture amount the gateway reports for get_order/capture.
        # Tests set this to the quote grand total before driving the flow.
        self.amount: Decimal = Decimal("0.00")

    def create_order(self, quote: Any, idempotency_key: str) -> PreparedOrder:
        amount = self.force_amount or quote.grand_total
        return PreparedOrder(
            provider_order_id="PAY-TEST-1",
            status="payer_action_required",
            approve_url="https://www.sandbox.paypal.com/checkoutnow?token=secret-token",
            amount=amount,
            currency=quote.currency,
            environment="sandbox",
            raw={"id": "PAY-TEST-1", "status": "PAYER_ACTION_REQUIRED"},
        )

    def get_order(self, provider_order_id: str) -> OrderState:
        amount = self.force_amount or self.amount
        return OrderState(
            provider_order_id=provider_order_id,
            status="approved",
            amount=amount,
            currency="USD",
            approve_url=None,
            capture_id=None,
            environment="sandbox",
            raw={},
        )

    def capture(self, provider_order_id: str, idempotency_key: str) -> CaptureResult:
        self.capture_calls += 1
        amount = self.force_amount or self.amount
        return CaptureResult(
            provider_capture_id="CAP-TEST-1",
            status="captured",
            amount=amount,
            currency="USD",
            environment="sandbox",
            raw={},
        )

    def verify_webhook(self, headers: dict, body: bytes | str) -> VerifiedEvent:
        import json

        event = json.loads(body)
        return VerifiedEvent(
            provider_event_id=event.get("id", "evt"),
            event_type=event.get("event_type", "unknown"),
            signature_verified=True,
            payload=event,
        )
