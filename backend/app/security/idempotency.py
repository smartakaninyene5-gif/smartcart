"""Idempotency helpers shared by application services."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

from app.domain.snapshot import canonical_json


def fingerprint(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def create_order_key(user_id: str, approval_id: str, snapshot_hash: str) -> str:
    return f"create_order:{user_id}:{approval_id}:{snapshot_hash}"


def capture_key(provider_order_id: str) -> str:
    return f"capture:{provider_order_id}"


def expiry(now: datetime, hours: int = 24) -> datetime:
    # Longer than PayPal's 6h (72h on request) idempotency window.
    return now + timedelta(hours=hours)
