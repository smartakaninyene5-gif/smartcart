"""Milestone 1.1 fix 3: duplicate capture must not roll back unrelated work.

The unique constraint ``(quote_id, snapshot_hash)`` on ``captures`` must
produce a deterministic, mapped application error while the surrounding
transaction (journal entries, payment state, approval consumption) stays
intact, and must never poison the session for commit.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.application.errors import DuplicateCapture
from app.domain.enums import Actor
from app.persistence import tables as T
from app.persistence.repositories import (
    JournalRepository,
    PaymentRepository,
    QuoteRepository,
    StoreError,
)
from tests.guard_helpers import make_quote

NOW = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)


def _seed_chain(container) -> object:
    """user -> quote -> approval -> payment order (satisfies all FKs).

    The QuoteRecord row is hydrated exactly like QuoteRepository.add would
    (valid canonical quote_json), so to_domain() succeeds for the
    _mark_captured mapping test. Returns the domain Quote.
    """
    domain = make_quote(quote_id="qte_x", user_id="user_demo")
    with container.session() as session:
        session.add(T.User(id="user_demo"))
        session.flush()
        session.add(T.QuoteRecord(
            id=domain.quote_id,
            user_id=domain.user_id,
            status="prepared",
            currency=domain.currency,
            subtotal=domain.subtotal,
            shipping_total=domain.shipping_total,
            grand_total=domain.grand_total,
            snapshot_hash=domain.snapshot_hash,
            policy_version=domain.policy_version,
            quote_json=domain.model_dump(mode="json"),
            requirements_json=domain.requirements.model_dump(mode="json"),
            evidence_refs=list(domain.evidence_refs),
            guard_outcome="ALLOW",
            guard_decision_json={},
            data_mode="MOCK",
            as_of=domain.as_of,
        ))
        session.flush()
        session.add(T.Approval(
            id="apr_x", user_id=domain.user_id, quote_id=domain.quote_id,
            snapshot_hash=domain.snapshot_hash,
            status="approved", policy_version=domain.policy_version, guard_decision_json={},
            approved_at=NOW, expires_at=NOW, created_at=NOW,
        ))
        session.flush()
        session.add(T.PaymentOrder(
            id="po_x", user_id=domain.user_id, approval_id="apr_x", quote_id=domain.quote_id,
            provider="paypal", provider_order_id="ord_x", status="approved",
            amount=domain.grand_total, currency=domain.currency, environment="test",
            raw_json={},
        ))
        session.commit()
    return domain


def test_duplicate_capture_savepoint_keeps_transaction(container):
    domain = _seed_chain(container)
    capture_args = dict(
        user_id="user_demo",
        quote_id="qte_x",
        snapshot_hash=domain.snapshot_hash,
        provider_capture_id="cap_dedup_1",
        status="captured",
        amount=domain.grand_total,
        currency=domain.currency,
    )
    with container.session() as session:
        # unrelated pending work first: a journal entry
        journal_row = JournalRepository(session).append(
            actor=Actor.SYSTEM,
            event_type="test.before",
            subject_type="x",
            subject_id="1",
            user_id="user_demo",
            payload={"note": "must survive"},
            environment="test",
        )
        payments = PaymentRepository(session)
        first = payments.add_capture(payment_order_id="po_x", **capture_args)
        session.flush()
        with pytest.raises(StoreError):
            payments.add_capture(payment_order_id="po_x", **capture_args)
        # the journal entry written before the duplicate is still visible in
        # the SAME transaction (no rollback happened)
        rows = list(session.scalars(select(T.JournalEvent)).all())
        assert any(r.event_id == journal_row.event_id for r in rows)
        # the original capture object is intact
        assert session.get(T.Capture, first.id) is first
        # must not raise: commit succeeds without re-flushing the dead row
        session.commit()

    with container.session() as session:
        captures = list(session.scalars(select(T.Capture)).all())
        assert len(captures) == 1  # the duplicate insert never landed
        assert len(list(session.scalars(select(T.JournalEvent)).all())) == 1


def test_mark_captured_maps_storeerror_to_duplicate_capture(container):
    """``payments._mark_captured`` maps StoreError onto the stable app error."""
    domain = _seed_chain(container)
    from app.application.payments import _mark_captured

    capture_args = dict(
        amount=domain.grand_total,
        currency=domain.currency,
    )
    with container.session() as session:
        payments_row = session.get(T.PaymentOrder, "po_x")
        domain_quote = QuoteRepository(session).to_domain(
            QuoteRepository(session).get("user_demo", "qte_x")
        )
        common = dict(
            payment=payments_row,
            quote=domain_quote,
            approval_id="apr_x",
            now=NOW,
            environment="test",
            correlation_id="corr_1",
            source="api_response",
        )
        outcome = _mark_captured(
            session,
            provider_capture_id="cap_first",
            **capture_args,
            **common,
        )
        assert outcome.capture.provider_capture_id == "cap_first"

        with pytest.raises(DuplicateCapture) as excinfo:
            _mark_captured(
                session,
                provider_capture_id="cap_second",  # same quote + snapshot
                **capture_args,
                **common,
            )
        assert excinfo.value.code == "DUPLICATE_CAPTURE"
        assert excinfo.value.http_status == 409
        # prior state survives the failed second capture
        assert session.get(T.PaymentOrder, "po_x").status == "captured"
        session.commit()

    with container.session() as session:
        # one capture row only, and the first capture's journal event exists
        assert len(list(session.scalars(select(T.Capture)).all())) == 1
        events = [e.event_type for e in session.scalars(select(T.JournalEvent)).all()]
        assert "payment.captured" in events
