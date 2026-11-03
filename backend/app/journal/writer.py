"""Journal writer: redaction + append-only persistence."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.domain.enums import Actor
from app.journal.events import JournalEvents
from app.persistence.repositories import JournalRepository
from app.persistence.tables import JournalEvent
from app.security.redaction import redact


class JournalWriter:
    def __init__(self, session: Session, *, environment: str = "dev") -> None:
        self._repository = JournalRepository(session)
        self._environment = environment

    def record(
        self,
        event_type: str,
        *,
        actor: Actor,
        subject_type: str,
        subject_id: str,
        payload: dict[str, Any] | None = None,
        user_id: str | None = None,
        correlation_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> JournalEvent:
        return self._repository.append(
            actor=actor,
            event_type=event_type,
            subject_type=subject_type,
            subject_id=subject_id,
            payload=redact(payload or {}),
            user_id=user_id,
            correlation_id=correlation_id,
            environment=self._environment,
            occurred_at=occurred_at,
        )

    def failure(
        self,
        *,
        error_code: str,
        message: str,
        subject_type: str,
        subject_id: str,
        user_id: str | None = None,
        correlation_id: str | None = None,
    ) -> JournalEvent:
        return self.record(
            JournalEvents.FAILURE,
            actor=Actor.SYSTEM,
            subject_type=subject_type,
            subject_id=subject_id,
            payload={"error_code": error_code, "message": redact(message)},
            user_id=user_id,
            correlation_id=correlation_id,
        )
