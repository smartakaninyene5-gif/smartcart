"""API dependencies: session lifecycle, identity, correlation id."""

from __future__ import annotations

import re
from collections.abc import Iterator

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from app.application.errors import ValidationFailure
from app.container import Container
from app.persistence.base import new_id

_USER_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def get_container(request: Request) -> Container:
    return request.app.state.container


def get_session(request: Request) -> Iterator[Session]:
    """One transaction per request.

    Business-rule failures (``AppError``) are committed, because their journal
    entries and state transitions are evidence. Unexpected failures roll back.
    """

    session = request.app.state.container.session()
    try:
        yield session
        session.commit()
    except ValidationFailure:
        session.commit()
        raise
    except Exception as exc:
        from app.application.errors import AppError

        if isinstance(exc, AppError):
            session.commit()
        else:
            session.rollback()
        raise
    finally:
        session.close()


def get_user_id(
    container: Container = Depends(get_container),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> str:
    user_id = (x_user_id or container.settings.demo_user_id).strip()
    if not _USER_RE.match(user_id):
        raise ValidationFailure("X-User-Id must match [A-Za-z0-9_-]{1,64}")
    return user_id


def get_correlation_id(
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
) -> str:
    if x_request_id and _USER_RE.match(x_request_id):
        return x_request_id
    return new_id("req")
