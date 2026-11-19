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


class FakeLlm:
    """Scripted structured-output LLM for deterministic shopping tests.

    ``responses`` is a queue of dicts; when empty the fake fails with
    ``LlmUnavailable`` so advisory fallbacks are exercised deterministically.
    An entry may also be an Exception instance to raise, or a callable taking
    (prompt, schema) and returning a dict.
    """

    name = "gemini"

    def __init__(self, responses: list | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    def structured(self, *, prompt: str, schema: dict) -> dict:
        from app.ports import LlmUnavailable

        self.calls.append({"prompt": prompt, "schema": schema})
        if not self.responses:
            raise LlmUnavailable("no scripted Gemini response")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(prompt, schema)
        return item


class FakeKernel:
    """In-process KERNEL stand-in: one deterministic page, no network.

    Records created/deleted sessions and every visited URL so tests can prove
    cleanup and bounded navigation. Session ids are synthetic; no tokens or
    credentials are ever produced.
    """

    name = "kernel"

    def __init__(
        self,
        *,
        page_text: str = "",
        title: str = "Example Product Page",
        final_url: str | None = None,
        emulate_redirect: bool = False,
        fail_create: bool = False,
        fail_visit: bool = False,
        fail_delete: bool = False,
    ) -> None:
        self.page_text = page_text
        self.title = title
        self.final_url = final_url
        self.emulate_redirect = emulate_redirect
        self.fail_create = fail_create
        self.fail_visit = fail_visit
        self.fail_delete = fail_delete
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.visited: list[str] = []

    def create_session(self):
        from app.ports import KernelBrowserSession, KernelUnavailable

        if self.fail_create:
            raise KernelUnavailable("fake session creation failure")
        session_id = f"sess_fake_{len(self.created) + 1}"
        self.created.append(session_id)
        return KernelBrowserSession(session_id=session_id)

    def visit(self, session_id: str, url: str):
        from app.ports import KernelPage, KernelUnavailable

        if self.fail_visit:
            raise KernelUnavailable("fake navigation failure")
        if session_id not in self.created:
            raise KernelUnavailable("fake session does not exist")
        self.visited.append(url)
        final_url = self.final_url or (url.replace("://", "://www.") if self.emulate_redirect else url)
        return KernelPage(
            url=url,
            final_url=final_url,
            title=self.title,
            text=self.page_text,
            redirected=bool(self.emulate_redirect),
        )

    def delete_session(self, session_id: str) -> None:
        from app.ports import KernelUnavailable

        if self.fail_delete:
            raise KernelUnavailable("fake delete failure")
        self.deleted.append(session_id)
