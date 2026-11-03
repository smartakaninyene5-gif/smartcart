"""API contract: validation, authorization boundaries, error codes, evidence.

These run against the mock source with no live credentials.
"""
from __future__ import annotations

from tests.conftest import approve_default, find_offer, make_quote


def _search(client, query="wireless headphones", max_price="150"):
    response = client.post("/api/v1/search", json={"query": query, "max_price": max_price, "limit": 8})
    assert response.status_code == 200, response.text
    return response.json()


def test_health_contract(client):
    body = client.get("/api/v1/health").json()
    assert body.get("data_mode") == "MOCK"
    assert body.get("channel3_configured") is False
    assert body.get("journal_chain_ok") is True


def test_search_contract(client):
    payload = _search(client)
    assert payload.get("data_mode") == "MOCK"
    assert len(payload.get("products", [])) >= 3
    assert payload.get("evidence_count", 0) > 0
    # the mock corpus contains a malformed offer; search must surface a
    # warning rather than crash
    assert any("no usable price" in w for w in payload.get("warnings", [])), payload.get("warnings")


def test_search_validation_rejects_bad_input(client):
    missing_query = client.post("/api/v1/search", json={"query": ""})
    assert missing_query.status_code == 422
    negative_price = client.post("/api/v1/search", json={"query": "x", "max_price": "-5"})
    assert negative_price.status_code == 422
    guestcredential = client.post("/api/v1/search", json={"query": "x", "extra": True})
    assert guestcredential.status_code == 422  # extra fields forbidden


def test_quote_contract_and_snapshot_determinism(client):
    sony = find_offer(_search(client), "bestbuy.com")
    quote1 = make_quote(client, sony["offer_id"])
    quote2 = make_quote(client, sony["offer_id"])
    assert quote1.get("guard_outcome") == "ALLOW"
    assert str(quote1.get("grand_total")) in ("128.00", "128.0", "128.000")
    assert str(quote1.get("snapshot_hash", "")).startswith("sha256:")
    assert len(quote1.get("evidence", [])) >= 1
    assert quote1["intent_id"]
    # deterministic core: identical inputs, identical hash
    assert quote1["snapshot_hash"] == quote2["snapshot_hash"]


def test_client_supplied_price_is_rejected(client):
    sony = find_offer(_search(client), "bestbuy.com")
    response = client.post("/api/v1/quotes", json={
        "selections": [{"offer_id": sony["offer_id"], "quantity": 1}],
        "requirements": {"currency": "USD", "budget_total": "200", "allowed_conditions": ["new"]},
        "price": "0.01",
    })
    assert response.status_code == 422


def test_user_limits_tighten_only_and_block(client):
    sony = find_offer(_search(client), "bestbuy.com")
    limited = client.post("/api/v1/quotes", json={
        "selections": [{"offer_id": sony["offer_id"], "quantity": 1}],
        "requirements": {"currency": "USD", "budget_total": "200", "allowed_conditions": ["new"]},
        "user_limits": {"max_total": "50"},
    }).json()
    assert limited.get("guard_outcome") in ("DENY", "NEEDS_INFO")
    assert "G-003_TOTAL_CEILING" in (limited.get("guard", {}).get("blocking_codes") or [])


def test_approval_gates(client):
    sony = find_offer(_search(client), "bestbuy.com")
    quote = make_quote(client, sony["offer_id"])

    no_consent = client.post(f"/api/v1/quotes/{quote['quote_id']}/approve", json={
        "snapshot_hash": quote["snapshot_hash"], "explicit_consent": False,
    })
    assert no_consent.status_code == 400
    assert no_consent.json()["error"]["code"] == "EXPLICIT_CONSENT_REQUIRED"

    wrong_snapshot = client.post(f"/api/v1/quotes/{quote['quote_id']}/approve", json={
        "snapshot_hash": "sha256:" + "0" * 64, "explicit_consent": True,
    })
    assert wrong_snapshot.status_code == 409
    assert wrong_snapshot.json()["error"]["code"] == "SNAPSHOT_MISMATCH"

    approval = approve_default(client, quote).json()
    assert approval["approval"]["snapshot_hash"] == quote["snapshot_hash"]
    assert approval["approval"]["expires_at"]

    reval = client.post(f"/api/v1/approvals/{approval['approval']['approval_id']}/revalidate").json()
    assert reval.get("outcome") == "ALLOW"


def test_cross_user_access_is_denied_filled(client):
    sony = find_offer(_search(client), "bestbuy.com")
    quote = make_quote(client, sony["offer_id"])
    approval = approve_default(client, quote).json()["approval"]

    other = {"X-User-Id": "someone_else"}
    assert client.get(f"/api/v1/quotes/{quote['quote_id']}", headers=other).status_code == 404
    assert client.get(
        f"/api/v1/approvals/{approval['approval_id']}", headers=other
    ).status_code == 404
    assert client.post(
        f"/api/v1/approvals/{approval['approval_id']}/revalidate", headers=other
    ).status_code == 404


def test_unknown_quote_404(client):
    assert client.get("/api/v1/quotes/qte_none").status_code == 404


def test_payment_without_credentials_contract(client):
    sony = find_offer(_search(client), "bestbuy.com")
    quote = make_quote(client, sony["offer_id"])
    approval = approve_default(client, quote).json()["approval"]

    order = client.post("/api/v1/payments/orders", json={"approval_id": approval["approval_id"]})
    assert order.status_code == 503
    assert order.json()["error"]["code"] == "PAYPAL_NOT_CONFIGURED"

    webhook = client.post(
        "/webhooks/paypal",
        json={"id": "evt_1", "event_type": "PAYMENT.CAPTURE.COMPLETED"},
    )
    assert webhook.status_code == 503


def test_journal_endpoint_contract(client):
    sony = find_offer(_search(client), "bestbuy.com")
    quote = make_quote(client, sony["offer_id"])
    approve_default(client, quote)

    journal = client.get("/api/v1/journal?limit=500").json()
    assert journal.get("chain_ok") is True
    types = {e["event_type"] for e in journal["events"]}
    assert "discovery.searched" in types
    assert "quote.prepared" in types
    assert "approval.granted" in types


def test_evidence_endpoint_contract(client):
    search = _search(client)
    sony = find_offer(search, "bestbuy.com")
    response = client.get(f"/api/v1/evidence/{sony['evidence_id']}")
    assert response.status_code == 200
    assert response.json().get("payload")


def test_unconfigured_channel3_fails_fast():
    """Adapter factory refuses live REST without a key (no silent fallback)."""
    from app.adapters import build_product_source
    from app.config import Settings
    from app.ports import SourceNotConfigured
    from tests.conftest import NOW

    settings = Settings(
        _env_file=None, product_source="channel3", channel3_api_key=None,
    )
    try:
        build_product_source(settings, NOW)
        raised = False
    except SourceNotConfigured:
        raised = True
    assert raised


def test_missing_gemini_key_disables_ai():
    from app.adapters import build_llm_client
    from app.config import Settings

    assert build_llm_client(Settings(_env_file=None, ai_provider="gemini", gemini_api_key=None)) is None
