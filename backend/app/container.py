"""Composition root: engine, session factory, adapters, app factory."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.adapters import (
    build_kernel_client,
    build_llm_client,
    build_payment_gateway,
    build_product_source,
)
from app.adapters.clock import SystemClock
from app.application.errors import AppError
from app.config import Settings, get_settings
from app.persistence.base import create_engine_for, init_db, make_session_factory


class Container:
    def __init__(
        self,
        settings: Settings,
        *,
        clock=None,
        product_source=None,
        payment_gateway=None,
        llm=None,
        kernel_client=None,
    ) -> None:
        self.settings = settings
        self.clock = clock or SystemClock()
        self.engine = create_engine_for(settings.database_url)
        init_db(self.engine)
        self.session_factory = make_session_factory(self.engine)
        self.product_source = product_source or build_product_source(settings, self.clock)
        self.payment_gateway = payment_gateway if payment_gateway is not None else build_payment_gateway(settings)
        self.llm = llm if llm is not None else build_llm_client(settings)
        # KERNEL research client (None when not configured); may be injected
        # for tests. Not exposed as an API: only the shopping workflow uses it.
        self.kernel_client = kernel_client if kernel_client is not None else build_kernel_client(settings)

    def session(self):
        return self.session_factory()


def create_app(
    settings: Settings | None = None,
    *,
    clock=None,
    product_source=None,
    payment_gateway=None,
    llm=None,
    kernel_client=None,
    frontend_dir: Path | None = None,
) -> FastAPI:
    from app.api.routes import router as api_router
    from app.api.webhooks import router as webhook_router

    settings = settings or get_settings()
    app = FastAPI(
        title="Smartcart API",
        version="0.1.0",
        description="Evidence-first purchasing agent. Deterministic guard, explicit approval, no AI authority.",
    )
    app.state.container = Container(
        settings,
        clock=clock,
        product_source=product_source,
        payment_gateway=payment_gateway,
        llm=llm,
        kernel_client=kernel_client,
    )
    app.state.settings = settings

    @app.exception_handler(AppError)
    async def _app_error_handler(_request, exc: AppError) -> JSONResponse:
        body: dict[str, Any] = {
            "error": {"code": exc.code, "message": exc.message, "details": exc.details}
        }
        decision = getattr(exc, "decision", None)
        if decision is not None:
            body["error"]["decision"] = decision.model_dump(mode="json")
        return JSONResponse(status_code=exc.http_status, content=body)

    app.include_router(api_router, prefix="/api/v1")
    app.include_router(webhook_router)

    directory = frontend_dir or (Path(__file__).resolve().parents[2] / "frontend")
    if directory.is_dir():
        app.mount("/", StaticFiles(directory=str(directory), html=True), name="frontend")
    return app
