"""FastAPI application entry point."""

from __future__ import annotations

from app.container import create_app

app = create_app()
