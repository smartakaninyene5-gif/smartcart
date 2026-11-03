"""Adapter factories. This is the only place that decides live vs mock."""

from __future__ import annotations

from app.adapters.channel3_rest import Channel3RestSource
from app.adapters.gemini import GeminiLlmClient
from app.adapters.mock_source import MockProductSource
from app.adapters.paypal import PayPalGateway
from app.config import Settings
from app.ports import LlmClient, PaymentGateway, ProductSource, SourceNotConfigured


def build_product_source(settings: Settings, clock) -> ProductSource:
    if settings.product_source == "channel3":
        if not settings.channel3_configured:
            raise SourceNotConfigured(
                "SMARTCART_PRODUCT_SOURCE=channel3 requires SMARTCART_CHANNEL3_API_KEY"
            )
        return Channel3RestSource(
            settings.channel3_api_key.get_secret_value(),
            base_url=settings.channel3_base_url,
            clock=clock,
            timeout=settings.channel3_timeout_seconds,
        )
    return MockProductSource(clock=clock)


def build_llm_client(settings: Settings) -> LlmClient | None:
    if settings.ai_provider == "none" or not settings.gemini_configured:
        return None
    return GeminiLlmClient(
        settings.gemini_api_key.get_secret_value(),
        model=settings.gemini_model,
        base_url=settings.gemini_base_url,
    )


def build_payment_gateway(settings: Settings) -> PaymentGateway | None:
    if not settings.paypal_configured:
        return None
    return PayPalGateway(
        client_id=settings.paypal_client_id.get_secret_value(),
        client_secret=settings.paypal_client_secret.get_secret_value(),
        environment=settings.paypal_env,
        base_url=settings.paypal_base_url,
        return_url=settings.paypal_return_url,
        cancel_url=settings.paypal_cancel_url,
        webhook_id=settings.paypal_webhook_id,
    )
