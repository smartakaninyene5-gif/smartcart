"""Secret redaction for journal payloads and log-bound data.

Allowlist thinking: this module redacts by key *and* scrubs credential-shaped
substrings in values, so a secret cannot reach the journal just because it was
nested under an unexpected key.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

_SECRET_KEY_MARKERS = (
    "password",
    "passwd",
    "secret",
    "client_secret",
    "api_key",
    "apikey",
    "authorization",
    "auth_token",
    "access_token",
    "refresh_token",
    "id_token",
    "bearer",
    "session_cookie",
    "passkey",
    "otp",
    "pin",
    "cvv",
    "card_number",
    "security_code",
    "private_key",
    "signature",
)

_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)\bBasic\s+[A-Za-z0-9+/=]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"\bA21[A-Za-z0-9_\-]{20,}"),  # PayPal client-id shape
)


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _SECRET_KEY_MARKERS)


def redact_text(value: str) -> str:
    for pattern in _SECRET_VALUE_PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (REDACTED if _is_secret_key(str(key)) else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def contains_secret(value: Any, secrets: list[str]) -> bool:
    """Test helper: assert a serialised structure contains none of these."""

    haystack = str(value)
    return any(secret and secret in haystack for secret in secrets)
