"""PayPal webhook endpoint. Signature is verified before anything is trusted."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import get_container, get_correlation_id, get_session
from app.application.payments import handle_paypal_webhook
from app.container import Container

router = APIRouter()


@router.post("/webhooks/paypal", tags=["webhooks"])
async def paypal_webhook(
    request: Request,
    container: Container = Depends(get_container),
    session: Session = Depends(get_session),
    correlation_id: str = Depends(get_correlation_id),
) -> dict[str, Any]:
    body = await request.body()
    headers = {key.lower(): value for key, value in request.headers.items()}
    return handle_paypal_webhook(
        session,
        gateway=container.payment_gateway,
        headers=headers,
        body=body,
        environment=container.settings.env,
        correlation_id=correlation_id,
    )
