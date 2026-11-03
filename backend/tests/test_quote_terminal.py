"""Milestone 1.1 fix 4: terminal quotes can never be approved again.

Backend-enforced (use case level), journaled, and still enforced end-to-end
through the HTTP layer.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.persistence import tables as T
from app.persistence.repositories import QuoteRepository

from tests.conftest import NOW, approve_default, find_offer, make_quote


def test_reject_then_approve_returns_409_not_approvable(client):
    search = client.post("/api/v1/search", json={"query": "headphones"}).json()
    sony = find_offer(search, "bestbuy.com")
    quote = make_quote(client, sony["offer_id"])
    quote_id, snapshot_hash = quote["quote_id"], quote["snapshot_hash"]

    rejected = client.post(f"/api/v1/quotes/{quote_id}/reject", json={"reason": "changed my mind"})
    assert rejected.status_code == 200, rejected.text

    response = approve_default(client, quote)
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error"]["code"] == "QUOTE_NOT_APPROVABLE", body
    assert body["error"]["details"]["status"] == "rejected"


def test_rejected_quote_stays_rejected_with_journalled_failure(container, client):
    search = client.post("/api/v1/search", json={"query": "headphones"}).json()
    sony = find_offer(search, "bestbuy.com")
    quote = make_quote(client, sony["offer_id"])
    client.post(f"/api/v1/quotes/{quote['quote_id']}/reject", json={"reason": "too expensive"})
    response = approve_default(client, quote)  # 409, journaled
    assert response.status_code == 409

    with container.session() as session:
        row = QuoteRepository(session).get("user_demo", quote["quote_id"])
        status = row.status
        failure_events = list(
            session.scalars(
                select(T.JournalEvent).where(T.JournalEvent.event_type == "system.failure")
            ).all()
        )
    assert status == "rejected"
    failures = [e for e in failure_events if "QUOTE_NOT_APPROVABLE" in str(e.payload)]
    assert failures, failure_events
    assert all(f.subject_id == quote["quote_id"] for f in failures)


def test_superseded_quote_cannot_be_approved(client, container):
    search = client.post("/api/v1/search", json={"query": "headphones"}).json()
    sony = find_offer(search, "bestbuy.com")
    quote = make_quote(client, sony["offer_id"])

    with container.session() as session:
        row = session.get(T.QuoteRecord, quote["quote_id"])
        row.status = "superseded"
        session.commit()

    response = approve_default(client, quote)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "QUOTE_NOT_APPROVABLE"
    assert response.json()["error"]["details"]["status"] == "superseded"


def test_approval_before_reject_is_superseded(client):
    """Rejecting a quote deactivates any already-granted approvals."""
    search = client.post("/api/v1/search", json={"query": "headphones"}).json()
    sony = find_offer(search, "bestbuy.com")
    quote = make_quote(client, sony["offer_id"])
    approval = approve_default(client, quote).json()["approval"]

    rejected = client.post(f"/api/v1/quotes/{quote['quote_id']}/reject", json={"reason": "user rejected"})
    assert rejected.status_code == 200
    assert rejected.json()["rejected_approvals"] >= 1

    with client.app.state.container.session() as session:
        statuses = list(
            session.scalars(
                select(T.Approval.status).where(T.Approval.id == approval["approval_id"])
            ).all()
        )
    # The application's terminal state for a user-rejected approval is
    # "rejected" (reject_quote sets status=ApprovalStatus.REJECTED, not
    # superseded); both are terminal for the approval lifecycle.
    assert statuses == ["rejected"]


def test_use_case_guard_is_backend_enforced(client, container):
    """The rule lives in the use case, not the HTTP layer: calling
    ``approve_quote`` directly on a rejected quote raises the same error."""
    from app.application.approvals import approve_quote
    from app.application.errors import QuoteNotApprovable

    search = client.post("/api/v1/search", json={"query": "headphones"}).json()
    sony = find_offer(search, "bestbuy.com")
    quote = make_quote(client, sony["offer_id"])
    client.post(
        f"/api/v1/quotes/{quote['quote_id']}/reject",
        json={"reason": "rejected for the use-case test"},
    )

    with container.session() as session:
        with pytest.raises(QuoteNotApprovable) as excinfo:
            approve_quote(
                session,
                user_id="user_demo",
                quote_id=quote["quote_id"],
                snapshot_hash=quote["snapshot_hash"],
                explicit_consent=True,
                acknowledged_risks=True,
                policy=container.settings.guard_policy,
                now=NOW,
                environment="test",
            )
    assert excinfo.value.code == "QUOTE_NOT_APPROVABLE"


def test_approve_unknown_quote_404(client):
    response = client.post(
        "/api/v1/quotes/qte_none/approve",
        json={"snapshot_hash": "sha256:x", "explicit_consent": True},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
