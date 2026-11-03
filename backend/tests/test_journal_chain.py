"""Journal hash-chain integrity and append-only enforcement."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.domain.enums import Actor
from app.domain.snapshot import chain_hash
from app.persistence import tables as T
from app.persistence.repositories import JournalRepository

NOW = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)


def _append(session, event_type: str, subject: str, payload: dict | None = None):
    return JournalRepository(session).append(
        actor=Actor.SYSTEM,
        event_type=event_type,
        subject_type="test",
        subject_id=subject,
        user_id="user_demo",
        payload=payload or {},
        environment="test",
    )


def _seed(container):
    """Commit three chained journal events and one evidence row."""
    with container.session() as session:
        session.add(T.User(id="user_demo"))
        session.flush()
        first = _append(session, "t.a", "a")
        _append(session, "t.b", "b", {"k": "v"})
        _append(session, "t.c", "c")
        from app.persistence.tables import Evidence

        from app.persistence.tables import Evidence

        session.add(
            Evidence(
                id="evd_trig",
                user_id="user_demo",
                kind="source_observation",
                trust="source_observed",
                source="test",
                source_ref="https://example.test/x",
                subject_type="offer",
                subject_id="mock:o1",
                observed_at=NOW,
                as_of=None,
                payload={"price": "1.00"},
                payload_sha256="sha256:" + "0" * 64,
            )
        )
        session.commit()
        return first


def test_chain_links_and_hashes(container):
    _seed(container)
    with container.session() as session:
        rows = list(JournalRepository(session).list(limit=10))
        ok, broken = JournalRepository(session).verify_chain()
    # sequential chaining
    assert rows[1].prev_hash == rows[0].entry_hash
    assert rows[2].prev_hash == rows[1].entry_hash
    # recomputed hashes match stored hashes
    for row in rows:
        assert chain_hash(row.prev_hash, row.entry_payload) == row.entry_hash
    assert ok and broken is None


def test_tamper_detection_logic_and_real_world_protection(container):
    """Payload changes are detectable: recomputing the chain hash over a
    modified payload must not match the stored hash — without mutating any
    live row (append-only would reject ORM autoflush anyway; the trigger
    blocking real UPDATEs is asserted in the mutation test below)."""
    _seed(container)
    with container.session() as session:
        rows = list(JournalRepository(session).list(limit=10))
        ok, broken = JournalRepository(session).verify_chain()
    first = rows[0]
    # untouched payload verifies
    assert chain_hash(first.prev_hash, first.entry_payload) == first.entry_hash
    assert ok and broken is None
    # any byte-meaningful change to the entry payload breaks verification
    for tampered in (
        first.entry_payload.replace('"event_type":"t.a"', '"event_type":"t.b"'),
        first.entry_payload.replace('"environment":"test"', '"environment":"dev"'),
        first.entry_payload[:-1],  # truncation
    ):
        assert chain_hash(first.prev_hash, tampered) != first.entry_hash


def test_append_only_triggers_block_mutation(container):
    """SQLite triggers make journal_events and evidence append-only."""
    _seed(container)
    statements = [
        "UPDATE journal_events SET payload = '{}'",
        "DELETE FROM journal_events",
        "UPDATE evidence SET payload = '{}'",
        "DELETE FROM evidence",
    ]
    for statement in statements:
        with container.session() as session:
            # the append-only trigger fires as a constraint violation
            with pytest.raises(IntegrityError):
                session.execute(text(statement))
                session.commit()
        # failed statements left no collateral: the chain still verifies
        with container.session() as session:
            ok, _ = JournalRepository(session).verify_chain()
            assert ok
