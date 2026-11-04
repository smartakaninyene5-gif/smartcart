"""Typed application settings.

Secrets are ``SecretStr`` and are never logged. Empty environment values become
``None`` so "configured" checks cannot be fooled by an empty string.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.enums import Condition, DataMode
from app.domain.guard.policy import GuardPolicy


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_prefix="SMARTCART_",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    env: Literal["dev", "test", "sandbox", "production"] = "dev"
    database_url: str = "sqlite+pysqlite:///./smartcart.db"
    demo_user_id: str = "user_demo"

    product_source: Literal["mock", "channel3"] = "mock"
    # Accepts SMARTCART_CHANNEL3_API_KEY (documented) and the bare
    # CHANNEL3_API_KEY name actually used in the deployment .env file.
    channel3_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "smartcart_channel3_api_key",
            "channel3_api_key",
        ),
    )
    channel3_base_url: str = "https://api.trychannel3.com"
    channel3_timeout_seconds: float = 20.0

    ai_provider: Literal["none", "gemini"] = "none"
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-2.5-flash"
    gemini_base_url: str = "https://generativelanguage.googleapis.com"

    paypal_env: Literal["sandbox", "live"] = "sandbox"
    paypal_client_id: SecretStr | None = None
    paypal_client_secret: SecretStr | None = None
    paypal_base_url: str = "https://api-m.sandbox.paypal.com"
    paypal_return_url: str = "http://localhost:8000/?paypal=return"
    paypal_cancel_url: str = "http://localhost:8000/?paypal=cancel"
    paypal_webhook_id: str | None = None

    guard_policy_version: str = "1.0.0"
    guard_max_total: Decimal = Decimal("1000.00")
    guard_max_item: Decimal = Decimal("500.00")
    guard_max_quantity: int = 5
    guard_max_evidence_age_seconds: int = 900
    guard_approval_ttl_seconds: int = 1800

    guard_merchant_allowlist: str = ""
    guard_merchant_denylist: str = ""

    @field_validator(
        "channel3_api_key",
        "gemini_api_key",
        "paypal_client_id",
        "paypal_client_secret",
        mode="before",
    )
    @classmethod
    def _empty_secret_is_none(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, SecretStr):
            return value if value.get_secret_value().strip() else None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def channel3_configured(self) -> bool:
        return self.channel3_api_key is not None

    @property
    def gemini_configured(self) -> bool:
        return self.ai_provider == "gemini" and self.gemini_api_key is not None

    @property
    def paypal_configured(self) -> bool:
        return self.paypal_client_id is not None and self.paypal_client_secret is not None

    @property
    def data_mode(self) -> DataMode:
        if self.product_source == "channel3" and self.channel3_configured:
            return DataMode.LIVE
        return DataMode.MOCK

    def _split_domains(self, raw: str) -> tuple[str, ...]:
        return tuple(part.strip().lower() for part in raw.split(",") if part.strip())

    @property
    def guard_policy(self) -> GuardPolicy:
        allowlist = self._split_domains(self.guard_merchant_allowlist)
        return GuardPolicy(
            version=self.guard_policy_version,
            currency="USD",
            max_total=self.guard_max_total,
            max_item_price=self.guard_max_item,
            max_quantity_per_line=self.guard_max_quantity,
            max_evidence_age=timedelta(seconds=self.guard_max_evidence_age_seconds),
            allowed_conditions=(Condition.NEW,),
            merchant_allowlist=allowlist or None,
            merchant_denylist=self._split_domains(self.guard_merchant_denylist),
            approval_ttl=timedelta(seconds=self.guard_approval_ttl_seconds),
        )


def get_settings() -> Settings:
    return Settings()
