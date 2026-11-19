"""End-to-end shopping workflow tests through the HTTP API.

External systems are faked in-process: ``FakeLlm`` scripts Gemini's structured
outputs and ``FakeKernel`` returns one deterministic product page. Nothing
here touches the network or a real credential.

Covered: understand -> confirm -> search, evidence rows + journal chain,
targeted internal research, research disabled, no-qualifying-products,
ownership 404s, out-of-order state, malformed/missing AI, KERNEL cleanup on
failure, and the Purchase Guard regression (shopping never grants payment
authority).
"""
from __future__ import annotations

from tests.fakes import FakeKernel, FakeLlm

UNDERSTANDING = {
    "category": "headphones",
    "search_query": "wireless noise cancelling headphones",
    "must_have": [
        {"attribute": "noise_cancellation", "comparator": "boolean_true"},
    ],
    "preferences": [
        {"attribute": "brand", "comparator": "equal", "value": "Beats"},
    ],
    "budget": 100,
    "currency": "USD",
    "quantity": 1,
    "preferred_brands": ["Beats"],
    "excluded_brands": [],
    "excluded_merchants": [],
    "clarifications_needed": [],
    "notes": [],
}

INTERPRETATION = {
    "summary": "Deterministic evidence supports this product.",
    "why_selected": ["mandatory requirement is VERIFIED"],
    "tradeoffs": ["prices can change"],
    "risk_notes": ["re-check the merchant page before purchase"],
}

PAGE_TEXT = (
    "Active noise cancellation. Battery life: 40 hours. "
    "In stock. Free shipping. Condition: new."
)

ANKER_ID = "mock:mock-anker-q30"
BEATS_ID = "mock:mock-beats-studio3"
JBL_ID = "mock:mock-jbl-tune770"

def install(container, *, llm=None, kernel=None):
    """Inject fakes into the live container used by the API routes."""
    container.llm = llm if llm is not None else FakeLlm(
        [UNDERSTANDING, INTERPRETATION, INTERPRETATION, INTERPRETATION]
    )
    container.kernel_client = kernel
    return container


def understand(client) -> dict:
    response = client.post(
        "/api/v1/shopping/understand",
        json={"text": "I must have wireless noise cancelling headphones under $100"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def confirm(client, understood: dict) -> dict:
    response = client.post(
        "/api/v1/shopping/confirm",
        json={
            "request_id": understood["request_id"],
            "understanding": understood["understanding"],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def search(client, request_id: str, *, research: bool = True) -> dict:
    response = client.post(
        "/api/v1/shopping/search",
        json={"request_id": request_id, "research": research},
    )
    assert response.status_code == 200, response.text
    return response.json()


# ------------------------------------------------------------------ full flow


def test_complete_shopping_workflow(client, container):
    kernel = FakeKernel(page_text=PAGE_TEXT)
    install(container, kernel=kernel)

    understood = understand(client)
    assert understood["next_action"] in ("confirm", "confirm_or_clarify")
    assert understood["understanding"]["budget"] == "100.00"
    assert [r["attribute"] for r in understood["understanding"]["must_have"]] == [
        "noise_cancellation"
    ]

    confirmed = confirm(client, understood)
    assert confirmed["status"] == "confirmed"
    assert confirmed["criteria"]["search_query"] == "wireless noise cancelling headphones"
    assert confirmed["criteria"]["currency"] == "USD"

    result = search(client, understood["request_id"])
    assert result["source"] == "mock"
    assert result["data_mode"] == "MOCK"
    assert result["no_qualifying_products"] is False
    assert result["ai_participated"] is True

    assessments = {item["product_id"]: item for item in result["assessments"]}
    # Sony is filtered out by the per-unit budget (its offers are > $100).
    assert set(assessments) == {BEATS_ID, JBL_ID, ANKER_ID}
    assert all(item["decision"] == "COMPLIANT" for item in assessments.values())

    # Missing evidence was resolved by targeted browsing, and the combined
    # evidence is tagged: direct page matches are VERIFIED, not AI claims.
    for product_id, attribute in (
        (BEATS_ID, "condition"),
        (JBL_ID, "noise_cancellation"),
        (ANKER_ID, "noise_cancellation"),
    ):
        field = assessments[product_id]["fields"][attribute]
        assert field["status"] == "VERIFIED"
        assert any(c["source"] == "kernel" for c in field["claims"])
        assert any(c["directly_observed"] is True for c in field["claims"])

    research = result["research"]
    assert len(research["planned"]) == 3
    assert len(research["performed"]) == 3
    assert all(item["success"] for item in research["performed"])
    assert all(item["url"].startswith("https://") for item in research["performed"])
    assert all(item["session_id"] for item in research["performed"])

    # One bounded browser session per product, all cleaned up.
    assert len(kernel.created) == 3
    assert len(kernel.deleted) == 3
    assert set(kernel.created) == set(kernel.deleted)
    assert {url for url in kernel.visited} == {
        item["url"] for item in research["performed"]
    }

    # Deterministic ranking from COMPLIANT products only.
    recommendations = result["recommendations"]
    assert recommendations["cheapest_compliant"]["product_id"] == ANKER_ID
    assert recommendations["cheapest_compliant"]["price"] == "79.99"
    assert recommendations["best_value"]["product_id"] == BEATS_ID
    assert recommendations["best_match"]["product_id"] == BEATS_ID
    assert result["explanations"]

    # Stored search is retrievable with provenance-preserving research rows.
    stored = client.get(f"/api/v1/shopping/searches/{result['search_id']}")
    assert stored.status_code == 200, stored.text
    stored_body = stored.json()
    assert stored_body["search_id"] == result["search_id"]
    assert len(stored_body["research"]["records"]) == 3
    record = stored_body["research"]["records"][0]
    assert record["url"].startswith("https://")
    assert record["fields"]
    assert record["reasons"]
    assert record["findings"]
    assert record["limitations"] == []

    # Journal: ordered, hash-chained, with the full shopping/research chain.
    journal = client.get("/api/v1/journal?limit=500").json()
    assert journal["chain_ok"] is True
    event_types = [event["event_type"] for event in journal["events"]]
    for required in (
        "shopping.requested",
        "shopping.confirmed",
        "discovery.searched",
        "research.started",
        "research.completed",
        "shopping.searched",
    ):
        assert required in event_types, required

    # Every direct KERNEL finding is a real evidence row with a source URL.
    completed = next(
        event for event in journal["events"] if event["event_type"] == "research.completed"
    )
    evidence_id = completed["payload"]["evidence_ids"][0]
    evidence = client.get(f"/api/v1/evidence/{evidence_id}")
    assert evidence.status_code == 200, evidence.text
    evidence_body = evidence.json()
    assert evidence_body["source"] == "kernel"
    assert evidence_body["trust"] == "external_confirmed"
    assert evidence_body["payload"]["directly_observed"] is True
    assert evidence_body["payload"]["url"].startswith("https://")

    # The LLM was used for understanding + interpretation only.
    assert len(container.llm.calls) == 4


def test_search_result_is_private_to_its_owner(client, container):
    install(container, kernel=FakeKernel(page_text=PAGE_TEXT))
    understood = understand(client)
    confirm(client, understood)
    result = search(client, understood["request_id"])

    other = client.get(
        f"/api/v1/shopping/searches/{result['search_id']}",
        headers={"X-User-Id": "other_user"},
    )
    assert other.status_code == 404
    assert other.json()["error"]["code"] == "NOT_FOUND"

    research = client.post(
        f"/api/v1/shopping/searches/{result['search_id']}/research",
        json={},
        headers={"X-User-Id": "other_user"},
    )
    assert research.status_code == 404


# -------------------------------------------------------------- state machine


def test_search_before_confirm_is_rejected(client, container):
    install(container, llm=FakeLlm([UNDERSTANDING]))
    understood = understand(client)
    response = client.post(
        "/api/v1/shopping/search", json={"request_id": understood["request_id"]}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SHOPPING_STATE_INVALID"


def test_confirm_unknown_request_is_not_found(client):
    response = client.post(
        "/api/v1/shopping/confirm",
        json={"request_id": "sreq_missing", "understanding": UNDERSTANDING},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


# ------------------------------------------------------------- research modes


def test_research_can_be_disabled_and_then_run_via_internal_endpoint(client, container):
    kernel = FakeKernel(page_text=PAGE_TEXT)
    install(container, llm=FakeLlm([UNDERSTANDING]), kernel=kernel)
    understood = understand(client)
    confirm(client, understood)

    without = search(client, understood["request_id"], research=False)
    assert without["research"]["planned"] == []
    assert without["research"]["performed"] == []
    # Beats still has verified mandatory evidence (noise cancellation); the
    # unknown condition is disclosed rather than assumed, so it may still be
    # recommended. JBL and Anker remain INSUFFICIENT without research.
    assert without["no_qualifying_products"] is False
    assert without["recommendations"]["cheapest_compliant"]["product_id"] == BEATS_ID
    assessments = {item["product_id"]: item for item in without["assessments"]}
    assert assessments[BEATS_ID]["decision"] == "COMPLIANT"
    assert assessments[BEATS_ID]["fields"]["condition"]["status"] == "MISSING"
    assert assessments[JBL_ID]["decision"] == "INSUFFICIENT_EVIDENCE"
    assert kernel.created == []

    # Targeted internal research: bounded to this search's own products.
    response = client.post(
        f"/api/v1/shopping/searches/{without['search_id']}/research",
        json={"product_ids": [ANKER_ID]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["research"]["records"]) == 1
    assert body["research"]["records"][0]["product_id"] == ANKER_ID
    assessments = {item["product_id"]: item for item in body["assessments"]}
    assert assessments[ANKER_ID]["decision"] == "COMPLIANT"
    assert assessments[JBL_ID]["decision"] == "INSUFFICIENT_EVIDENCE"
    assert body["no_qualifying_products"] is False
    assert body["recommendations"]["cheapest_compliant"]["product_id"] == ANKER_ID


def test_targeted_research_rejects_foreign_product_ids(client, container):
    install(container, llm=FakeLlm([UNDERSTANDING]), kernel=FakeKernel(page_text=PAGE_TEXT))
    understood = understand(client)
    confirm(client, understood)
    result = search(client, understood["request_id"], research=False)

    response = client.post(
        f"/api/v1/shopping/searches/{result['search_id']}/research",
        json={"product_ids": ["not:in-this-search"]},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_targeted_research_unknown_search_is_not_found(client, container):
    install(container, kernel=FakeKernel(page_text=PAGE_TEXT))
    response = client.post(
        "/api/v1/shopping/searches/ssea_missing/research", json={}
    )
    assert response.status_code == 404


def test_no_qualifying_products_is_reported_without_recommendations(client, container):
    tight = dict(UNDERSTANDING, budget=50)
    install(container, llm=FakeLlm([tight]), kernel=FakeKernel(page_text=PAGE_TEXT))
    understood = understand(client)
    confirm(client, understood)
    result = search(client, understood["request_id"])

    assert result["assessments"] == []
    assert result["product_count"] == 0
    assert result["no_qualifying_products"] is True
    assert result["research"]["planned"] == []
    assert result["research"]["performed"] == []
    assert result["recommendations"] == {
        "cheapest_compliant": None,
        "best_value": None,
        "best_match": None,
    }


def test_missing_kernel_configuration_degrades_without_research(client, container):
    install(container, llm=FakeLlm([UNDERSTANDING]), kernel=None)
    understood = understand(client)
    confirm(client, understood)
    result = search(client, understood["request_id"])

    assert result["research"]["planned"]
    assert result["research"]["performed"] == []
    assert result["no_qualifying_products"] is False
    assert result["recommendations"]["cheapest_compliant"]["product_id"] == BEATS_ID
    assert result["ai_participated"] is False
    journal = client.get("/api/v1/journal?limit=500").json()
    assert "research.failed" in [event["event_type"] for event in journal["events"]]


def test_kernel_visit_failure_is_recorded_and_session_cleaned_up(client, container):
    kernel = FakeKernel(fail_visit=True)
    install(container, llm=FakeLlm([UNDERSTANDING]), kernel=kernel)
    understood = understand(client)
    confirm(client, understood)
    result = search(client, understood["request_id"])

    assert len(result["research"]["performed"]) == 3
    assert all(item["success"] is False for item in result["research"]["performed"])
    assert all(
        any("page visit failed" in limit for limit in item["limitations"])
        for item in result["research"]["performed"]
    )
    assert len(kernel.deleted) == 3
    assert result["no_qualifying_products"] is False
    cheapest = result["recommendations"]["cheapest_compliant"]
    assert cheapest["product_id"] == BEATS_ID
    assert any("condition" in item for item in cheapest["missing_or_uncertain"])


def test_kernel_creation_failure_is_recorded_without_deleting(client, container):
    kernel = FakeKernel(fail_create=True)
    install(container, llm=FakeLlm([UNDERSTANDING]), kernel=kernel)
    understood = understand(client)
    confirm(client, understood)
    result = search(client, understood["request_id"])

    assert len(result["research"]["performed"]) == 3
    assert all(item["session_id"] == "" for item in result["research"]["performed"])
    assert all(
        any("could not be created" in limit for limit in item["limitations"])
        for item in result["research"]["performed"]
    )
    assert kernel.deleted == []


# ------------------------------------------------------------ AI failure modes


def test_missing_ai_configuration_returns_503(client):
    response = client.post(
        "/api/v1/shopping/understand", json={"text": "headphones under $100"}
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "AI_NOT_CONFIGURED"


def test_malformed_ai_understanding_returns_502(client, container):
    broken = {
        "category": "headphones",
        "search_query": "headphones",
        "must_have": [{"attribute": "battery_life_hours", "comparator": "at_least"}],
        "preferences": [],
        "currency": "USD",
    }
    install(container, llm=FakeLlm([broken]))
    response = client.post(
        "/api/v1/shopping/understand", json={"text": "headphones with great battery"}
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "AI_UNAVAILABLE"


def test_understand_validates_request_body(client):
    response = client.post("/api/v1/shopping/understand", json={"text": "x"})
    assert response.status_code == 422


# ------------------------------------------------------- Purchase Guard check


def test_shopping_workflow_grants_no_payment_authority(client, container):
    from tests.fakes import FakeGateway

    install(container, kernel=FakeKernel(page_text=PAGE_TEXT))
    container.payment_gateway = FakeGateway()
    understood = understand(client)
    confirm(client, understood)
    result = search(client, understood["request_id"])

    # A recommendation is not an approval or a payment.
    for key in ("approval_id", "snapshot_hash", "payment", "capture"):
        assert key not in result
    for recommendation in result["recommendations"].values():
        if recommendation is not None:
            assert "approval" not in recommendation
            assert "payment" not in recommendation

    # The payment path still requires a genuine approval; shopping cannot mint one.
    payment = client.post(
        "/api/v1/payments/orders", json={"approval_id": "appr_from_shopping"}
    )
    assert payment.status_code in (404, 409)
    assert "error" in payment.json()


# --------------------------------------------------------------------- health


def test_health_reports_ai_and_kernel_configuration_without_secrets(client):
    body = client.get("/api/v1/health").json()
    assert body["ai_provider"] == "none"
    assert body["ai_configured"] is False
    assert body["kernel_configured"] is False
    assert "api_key" not in str(body).lower()
    assert body["journal_chain_ok"] is True
