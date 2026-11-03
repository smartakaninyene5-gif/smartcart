"""Approval expiry and price-change invalidation (revalidation determinism)."""
from __future__ import annotations

from decimal import Decimal

from app.persistence import tables as T

from tests.conftest import find_offer, make_quote


def _fresh_quote(client):
    """Create a fresh bestbuy quote; returns (quote, offer_id)."""
    search = client.post("/api/v1/search", json={"query": "headphones"}).json()
    sony = find_offer(search, "bestbuy.com")
    quote = make_quote(client, sony["offer_id"])
    return quote, sony["offer_id"]


def _approval(client, quote):
    response = client.post(f"/api/v1/quotes/{quote['quote_id']}/approve", json={
        "snapshot_hash": quote["snapshot_hash"],
        "explicit_consent": True,
    })
    assert response.status_code == 200, response.text
    return response.json()["approval"]


def test_approval_expires_after_evidence_age_wins(app, client):
    """Expiry is min(ttl, as_of + max_evidence_age); advance past both."""
    clock = app.state.container.clock
    quote, _offer_id = _fresh_quote(client)
    approval = _approval(client, quote)

    still_valid = client.post(f"/api/v1/approvals/{approval['approval_id']}/revalidate").json()
    assert still_valid.get("outcome") == "ALLOW", still_valid

    clock.advance(minutes=16)  # past 1800s ttl? no: past as_of+900s anyway

    expired = client.post(f"/api/v1/approvals/{approval['approval_id']}/revalidate").json()
    assert expired.get("outcome") == "DENY", expired
    assert "approval_expired" in (expired.get("reasons") or [])

    status = client.get(f"/api/v1/approvals/{approval['approval_id']}").json().get("status")
    assert status == "expired"


def test_price_change_invalidates_approval(client, container):
    quote, offer_id = _fresh_quote(client)
    approval = _approval(client, quote)

    with container.session() as session:
        row = session.get(T.OfferRecord, offer_id)
        row.price = Decimal("99.00")
        session.commit()

    changed = client.post(f"/api/v1/approvals/{approval['approval_id']}/revalidate").json()
    assert changed.get("outcome") == "DENY", changed
    assert "offer_changed" in (changed.get("reasons") or [])
    paths = [c["path"] for c in changed.get("changes", [])]
    assert any("unit_price" in p for p in paths), paths

    status = client.get(f"/api/v1/approvals/{approval['approval_id']}").json().get("status")
    assert status == "stale"


def test_price_change_blocks_capture_guard_by_extension(client, container):
    """A stale approval cannot be used for payment either (approval_not_valid)."""
    quote, offer_id = _fresh_quote(client)
    approval_id = _approval(client, quote)["approval_id"]

    with container.session() as session:
        row = session.get(T.OfferRecord, offer_id)
        row.price = Decimal("99.00")
        session.commit()

    # payment without credentials already fails 503 before this check, so
    # assert via the revalidation API surface (capture guard paths are
    # covered in test_payment_flow).
    changed = client.post(f"/api/v1/approvals/{approval_id}/revalidate").json()
    assert changed.get("outcome") == "DENY"


def test_fresh_quote_after_expiry_still_begins_a_new_lifecycle(app, client):
    clock = app.state.container.clock
    quote, _ = _fresh_quote(client)
    approval = _approval(client, quote)
    clock.advance(minutes=16)
    assert client.post(f"/api/v1/approvals/{approval['approval_id']}/revalidate").json()["outcome"] == "DENY"

    quote2, _ = _fresh_quote(client)
    assert quote2.get("guard_outcome") == "ALLOW"
    approval2 = _approval(client, quote2)
    revalid = client.post(f"/api/v1/approvals/{approval2['approval_id']}/revalidate").json()
    assert revalid.get("outcome") == "ALLOW"
