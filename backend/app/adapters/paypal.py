"""PayPal Orders v2 + webhook verification adapter.

No fake checkout. Without client credentials the application returns
``PAYPAL_NOT_CONFIGURED``; this adapter never simulates a payment.

A captured PayPal order proves that money moved to the PayPal payee. It does
not prove a retailer purchase, and the application keeps those two states
separate (see ``RetailerPurchaseState``).
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

import httpx

from app.domain.models import Quote
from app.domain.money import money
from app.ports import CaptureResult, OrderState, PreparedOrder, VerifiedEvent


class PayPalError(Exception):
    """Base PayPal failure. Messages never contain credentials or tokens."""


class PayPalNotConfigured(PayPalError):
    pass


class PayPalAuthError(PayPalError):
    pass


class PayPalRejected(PayPalError):
    def __init__(self, message: str, *, status_code: int | None = None, issue: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.issue = issue


class PayPalUnavailable(PayPalError):
    pass


class PayPalAmbiguous(PayPalError):
    """Transport failure after a non-idempotent request: must be reconciled."""


_STATUS_MAP = {
    "CREATED": "created",
    "SAVED": "created",
    "PAYER_ACTION_REQUIRED": "payer_action_required",
    "APPROVED": "approved",
    "COMPLETED": "captured",
    "VOIDED": "voided",
    "DECLINED": "failed",
    "FAILED": "failed",
}


def _map_status(raw: Any) -> str:
    return _STATUS_MAP.get(str(raw or "").upper(), "failed")


def _amount(payload: dict[str, Any]) -> tuple[Decimal | None, str | None]:
    units = payload.get("purchase_units")
    if not isinstance(units, list) or not units:
        return None, None
    amount = units[0].get("amount") if isinstance(units[0], dict) else None
    if not isinstance(amount, dict):
        return None, None
    value = amount.get("value")
    currency = amount.get("currency_code")
    if value is None or not currency:
        return None, None
    try:
        return money(value, "paypal amount", allow_zero=False), str(currency).upper()
    except ValueError:
        return None, None


def _link(payload: dict[str, Any], *rels: str) -> str | None:
    links = payload.get("links")
    if not isinstance(links, list):
        return None
    for link in links:
        if isinstance(link, dict) and link.get("rel") in rels and link.get("href"):
            return str(link["href"])
    return None


def _capture_id(payload: dict[str, Any]) -> str | None:
    units = payload.get("purchase_units")
    if not isinstance(units, list) or not units or not isinstance(units[0], dict):
        return None
    payments = units[0].get("payments")
    if not isinstance(payments, dict):
        return None
    captures = payments.get("captures")
    if isinstance(captures, list) and captures and isinstance(captures[0], dict):
        return captures[0].get("id")
    return None


class PayPalGateway:
    name = "paypal"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        environment: str,
        base_url: str,
        return_url: str,
        cancel_url: str,
        webhook_id: str | None = None,
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not client_id or not client_secret:
            raise PayPalNotConfigured("PayPal client credentials are not configured")
        self.environment = environment
        self.configured = True
        self._client_id = client_id
        self._client_secret = client_secret
        self._return_url = return_url
        self._cancel_url = cancel_url
        self._webhook_id = webhook_id
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport)

    # -- auth -------------------------------------------------------------
    def _access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        try:
            response = self._client.post(
                "/v1/oauth2/token",
                data={"grant_type": "client_credentials"},
                auth=(self._client_id, self._client_secret),
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise PayPalUnavailable("PayPal token endpoint is unreachable") from exc
        if response.status_code in (401, 403):
            raise PayPalAuthError("PayPal rejected the client credentials")
        if response.status_code >= 500:
            raise PayPalUnavailable("PayPal token endpoint is unavailable")
        if response.status_code >= 400:
            raise PayPalAuthError("PayPal token request failed")
        try:
            payload = response.json()
            token = payload["access_token"]
            expires_in = float(payload.get("expires_in", 300))
        except (ValueError, KeyError, TypeError) as exc:
            raise PayPalAuthError("PayPal token response was malformed") from exc
        self._token = str(token)
        # Refresh early to avoid using an expired token mid-flight.
        self._token_expires_at = time.monotonic() + max(30.0, expires_in - 60.0)
        return self._token

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if idempotency_key:
            headers["PayPal-Request-Id"] = idempotency_key
        return headers

    def _error(self, response: httpx.Response, action: str) -> PayPalRejected:
        issue = None
        try:
            body = response.json()
            details = body.get("details") if isinstance(body, dict) else None
            if isinstance(details, list) and details and isinstance(details[0], dict):
                issue = details[0].get("issue")
        except ValueError:
            issue = None
        return PayPalRejected(f"PayPal {action} failed (HTTP {response.status_code})", status_code=response.status_code, issue=issue)

    # -- orders -----------------------------------------------------------
    def create_order(self, quote: Quote, idempotency_key: str) -> PreparedOrder:
        body = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": quote.quote_id,
                    "custom_id": quote.quote_id,
                    "invoice_id": f"SC-{quote.quote_id[-20:]}",
                    "description": "Smartcart demo payment for an approved basket (not a retailer purchase)",
                    "amount": {
                        "currency_code": quote.currency,
                        "value": f"{quote.grand_total:.2f}",
                    },
                }
            ],
            "application_context": {
                "brand_name": "Smartcart (demo)",
                "user_action": "PAY_NOW",
                "shipping_preference": "NO_SHIPPING",
                "return_url": self._return_url,
                "cancel_url": self._cancel_url,
            },
        }
        try:
            response = self._client.post("/v2/checkout/orders", json=body, headers=self._headers(idempotency_key))
        except httpx.HTTPError as exc:
            raise PayPalAmbiguous("PayPal create-order transport failure; reconcile before retrying") from exc
        if response.status_code >= 400:
            raise self._error(response, "create-order")
        try:
            payload = response.json()
            order_id = payload["id"]
        except (ValueError, KeyError, TypeError) as exc:
            raise PayPalRejected("PayPal create-order response was malformed") from exc
        amount, currency = _amount(payload)
        return PreparedOrder(
            provider_order_id=str(order_id),
            status=_map_status(payload.get("status")),
            approve_url=_link(payload, "approve", "payer-action"),
            amount=amount or quote.grand_total,
            currency=currency or quote.currency,
            environment=self.environment,
            raw=payload,
        )

    def get_order(self, provider_order_id: str) -> OrderState:
        try:
            response = self._client.get(f"/v2/checkout/orders/{provider_order_id}", headers=self._headers())
        except httpx.HTTPError as exc:
            raise PayPalUnavailable("PayPal order lookup failed") from exc
        if response.status_code >= 400:
            raise self._error(response, "get-order")
        try:
            payload = response.json()
        except ValueError as exc:
            raise PayPalRejected("PayPal order response was malformed") from exc
        amount, currency = _amount(payload)
        return OrderState(
            provider_order_id=str(payload.get("id") or provider_order_id),
            status=_map_status(payload.get("status")),
            amount=amount,
            currency=currency,
            approve_url=_link(payload, "approve", "payer-action"),
            capture_id=_capture_id(payload),
            environment=self.environment,
            raw=payload,
        )

    def capture(self, provider_order_id: str, idempotency_key: str) -> CaptureResult:
        try:
            response = self._client.post(
                f"/v2/checkout/orders/{provider_order_id}/capture",
                headers=self._headers(idempotency_key),
                json={},
            )
        except httpx.HTTPError as exc:
            raise PayPalAmbiguous("PayPal capture transport failure; reconcile before retrying") from exc
        if response.status_code >= 400:
            raise self._error(response, "capture")
        try:
            payload = response.json()
        except ValueError as exc:
            raise PayPalRejected("PayPal capture response was malformed") from exc
        amount, currency = _amount(payload)
        capture_id = _capture_id(payload)
        if not capture_id or amount is None or currency is None:
            raise PayPalRejected("PayPal capture response lacked capture id or amount")
        return CaptureResult(
            provider_capture_id=str(capture_id),
            status=_map_status(payload.get("status")),
            amount=amount,
            currency=currency,
            environment=self.environment,
            raw=payload,
        )

    # -- webhooks ---------------------------------------------------------
    def verify_webhook(self, headers: dict[str, str], body: bytes) -> VerifiedEvent:
        if not self._webhook_id:
            raise PayPalNotConfigured("PayPal webhook id is not configured")
        lowered = {key.lower(): value for key, value in headers.items()}
        required = {
            "paypal-transmission-id": lowered.get("paypal-transmission-id"),
            "paypal-transmission-time": lowered.get("paypal-transmission-time"),
            "paypal-cert-url": lowered.get("paypal-cert-url"),
            "paypal-auth-algo": lowered.get("paypal-auth-algo"),
            "paypal-transmission-sig": lowered.get("paypal-transmission-sig"),
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise PayPalRejected(f"webhook verification headers missing: {', '.join(missing)}")
        try:
            event = __import__("json").loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise PayPalRejected("webhook body is not valid JSON") from exc
        if not isinstance(event, dict):
            raise PayPalRejected("webhook body is not an object")

        verification_body = {
            "auth_algo": required["paypal-auth-algo"],
            "cert_url": required["paypal-cert-url"],
            "transmission_id": required["paypal-transmission-id"],
            "transmission_sig": required["paypal-transmission-sig"],
            "transmission_time": required["paypal-transmission-time"],
            "webhook_id": self._webhook_id,
            "webhook_event": event,
        }
        try:
            response = self._client.post(
                "/v1/notifications/verify-webhook-signature",
                json=verification_body,
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise PayPalUnavailable("PayPal webhook verification is unreachable") from exc
        if response.status_code >= 400:
            raise self._error(response, "webhook-verification")
        try:
            status = response.json().get("verification_status")
        except (ValueError, AttributeError) as exc:
            raise PayPalRejected("webhook verification response was malformed") from exc
        if status != "SUCCESS":
            raise PayPalRejected("webhook signature verification failed")
        return VerifiedEvent(
            provider_event_id=str(event.get("id") or ""),
            event_type=str(event.get("event_type") or ""),
            signature_verified=True,
            payload=event,
        )
