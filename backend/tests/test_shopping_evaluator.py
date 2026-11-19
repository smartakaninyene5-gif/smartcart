"""Deterministic evidence evaluation and recommendation tests.

These tests exercise the authoritative backend evaluator directly (no LLM,
no network). They pin the fixed contract:

* status semantics (VERIFIED / UNVERIFIED / MISSING / CONFLICTING /
  AI_ASSESSMENT);
* unknown mandatory evidence can never count as verified compliance;
* budget, currency and exclusions are hard restrictions;
* preferences can never disqualify a product;
* offer-level facts come from the single selected purchasable offer;
* recommendations only ever come from COMPLIANT products and are omitted
  rather than manufactured.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.domain.enums import Availability, Condition
from app.domain.models import Offer, Product
from app.shopping.evaluation import (
    _evidence_for,
    attribute_matches,
    evaluate_product,
    recommend,
    select_best_offer,
)
from app.shopping.models import (
    Claim,
    ClaimSource,
    ConfirmedCriteria,
    EvidenceStatus,
    Requirement,
)

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------- helpers


def make_offer(
    offer_id: str = "off_1",
    *,
    price: str = "100.00",
    currency: str = "USD",
    condition: Condition = Condition.NEW,
    availability: Availability = Availability.IN_STOCK,
    merchant_domain: str = "bestbuy.com",
    shipping: str | None = "0.00",
    url: str = "https://www.bestbuy.com/site/product/1",
    product_id: str = "prod_1",
) -> Offer:
    return Offer(
        offer_id=offer_id,
        source="mock",
        source_offer_id=f"src-{offer_id}",
        product_id=product_id,
        offer_product_id=f"op-{offer_id}",
        merchant_name=merchant_domain,
        merchant_domain=merchant_domain,
        price=Decimal(price),
        currency=currency,
        shipping_cost=None if shipping is None else Decimal(shipping),
        availability=availability,
        condition=condition,
        url=url,
        observed_at=NOW,
        as_of=NOW,
    )


def make_product(
    *,
    product_id: str = "prod_1",
    title: str = "Test Headphones",
    brand: str | None = "Sony",
    attributes: dict[str, list[str]] | None = None,
    offers: tuple[Offer, ...] | None = None,
) -> Product:
    return Product(
        product_id=product_id,
        source="mock",
        source_product_id=f"src-{product_id}",
        title=title,
        brand=brand,
        attributes=attributes or {},
        offers=offers if offers is not None else (make_offer(product_id=product_id),),
        observed_at=NOW,
    )


def make_criteria(
    *,
    must_have: tuple[Requirement, ...] = (),
    preferences: tuple[Requirement, ...] = (),
    budget: str | None = None,
    currency: str = "USD",
    excluded_brands: tuple[str, ...] = (),
    excluded_merchants: tuple[str, ...] = (),
) -> ConfirmedCriteria:
    return ConfirmedCriteria(
        request_id="sreq_test",
        category="headphones",
        search_query="wireless noise cancelling headphones",
        must_have=must_have,
        preferences=preferences,
        budget=None if budget is None else Decimal(budget),
        currency=currency,
        excluded_brands=excluded_brands,
        excluded_merchants=excluded_merchants,
        confirmed_at=NOW,
    )


def noise_requirement() -> Requirement:
    return Requirement(attribute="noise_cancellation", comparator="boolean_true")


def battery_requirement(hours: float = 30) -> Requirement:
    return Requirement(
        attribute="battery_life_hours", comparator="at_least", numeric=hours
    )


def verified_noise_product(**overrides) -> Product:
    return make_product(
        attributes={"noise-cancellation-technology": ["Active noise cancellation (ANC)"]},
        **overrides,
    )


# --------------------------------------------------------------- status matrix


def test_structured_channel3_evidence_is_verified_and_can_comply():
    assessment = evaluate_product(
        verified_noise_product(), make_criteria(must_have=(noise_requirement(),))
    )
    noise = assessment.fields["noise_cancellation_technology"]
    assert noise.status is EvidenceStatus.VERIFIED
    assert all(claim.directly_observed for claim in noise.claims)
    assert assessment.decision == "COMPLIANT"
    assert not assessment.reasons


def test_ai_only_claim_is_ai_assessment_and_never_complies():
    product = make_product(attributes={})
    ai_claim = Claim(
        source=ClaimSource.AI,
        value="active noise cancellation",
        trust="AI_UNVERIFIED",
        directly_observed=False,
    )
    assessment = evaluate_product(
        product,
        make_criteria(must_have=(noise_requirement(),)),
        extra_claims={"noise_cancellation": [ai_claim]},
    )
    assert assessment.fields["noise_cancellation"].status is EvidenceStatus.AI_ASSESSMENT
    assert assessment.decision == "INSUFFICIENT_EVIDENCE"
    assert assessment.decision != "COMPLIANT"


def test_indirect_claim_is_unverified_and_blocks_compliance():
    product = make_product(attributes={})
    indirect = Claim(
        source=ClaimSource.KERNEL,
        value="active noise cancellation",
        trust="AI_UNVERIFIED",
        directly_observed=False,
    )
    assessment = evaluate_product(
        product,
        make_criteria(must_have=(noise_requirement(),)),
        extra_claims={"noise_cancellation": [indirect]},
    )
    assert assessment.fields["noise_cancellation"].status is EvidenceStatus.UNVERIFIED
    assert assessment.decision == "INSUFFICIENT_EVIDENCE"


def test_missing_mandatory_evidence_blocks_and_is_listed():
    assessment = evaluate_product(
        make_product(attributes={}), make_criteria(must_have=(noise_requirement(),))
    )
    assert _evidence_for(
        assessment.fields, noise_requirement()
    ).status is EvidenceStatus.MISSING
    assert assessment.decision == "INSUFFICIENT_EVIDENCE"
    assert any("noise_cancellation" in item for item in assessment.missing_information)


def test_conflicting_claims_are_preserved_and_block():
    product = make_product(attributes={"noise_cancellation": ["active"]})
    contradicting = Claim(
        source=ClaimSource.KERNEL,
        value="no",
        trust="EXTERNAL_CONFIRMED",
        directly_observed=True,
    )
    assessment = evaluate_product(
        product,
        make_criteria(must_have=(noise_requirement(),)),
        extra_claims={"noise_cancellation": [contradicting]},
    )
    field = assessment.fields["noise_cancellation"]
    assert field.status is EvidenceStatus.CONFLICTING
    assert {claim.value for claim in field.claims} == {"active", "no"}
    assert assessment.decision == "INSUFFICIENT_EVIDENCE"
    assert assessment.conflicts


def test_verified_numeric_spec_passes_at_least():
    product = make_product(attributes={"battery_life": ["40"]})
    assessment = evaluate_product(
        product, make_criteria(must_have=(battery_requirement(30),))
    )
    assert assessment.fields["battery_life"].status is EvidenceStatus.VERIFIED
    assert assessment.decision == "COMPLIANT"


def test_verified_numeric_spec_below_threshold_disqualifies():
    product = make_product(attributes={"battery_life": ["10"]})
    assessment = evaluate_product(
        product, make_criteria(must_have=(battery_requirement(30),))
    )
    assert assessment.decision == "DISQUALIFIED"
    assert any("battery_life_hours" in reason for reason in assessment.reasons)


# ------------------------------------------------------------- hard constraints


def test_budget_is_a_hard_disqualifier():
    assessment = evaluate_product(
        make_product(offers=(make_offer(price="150.00"),)),
        make_criteria(budget="100"),
    )
    assert assessment.decision == "DISQUALIFIED"
    assert any("budget violation" in reason for reason in assessment.reasons)


def test_currency_mismatch_is_never_silently_relaxed():
    assessment = evaluate_product(
        make_product(offers=(make_offer(currency="EUR"),)),
        make_criteria(currency="USD"),
    )
    assert assessment.decision == "DISQUALIFIED"
    assert any("currency mismatch" in reason for reason in assessment.reasons)


def test_excluded_merchant_disqualifies():
    assessment = evaluate_product(
        make_product(offers=(make_offer(merchant_domain="backmarket.com"),)),
        make_criteria(excluded_merchants=("backmarket.com",)),
    )
    assert assessment.decision == "DISQUALIFIED"
    assert any("excluded merchant" in reason for reason in assessment.reasons)


def test_excluded_brand_disqualifies():
    assessment = evaluate_product(
        make_product(brand="Beats"), make_criteria(excluded_brands=("beats",))
    )
    assert assessment.decision == "DISQUALIFIED"
    assert any("excluded brand" in reason for reason in assessment.reasons)


def test_preference_miss_never_disqualifies_a_compliant_product():
    product = verified_noise_product(brand="JBL")
    criteria = make_criteria(
        must_have=(noise_requirement(),),
        preferences=(Requirement(attribute="brand", comparator="equal", value="Sony"),),
    )
    assessment = evaluate_product(product, criteria)
    assert assessment.decision == "COMPLIANT"
    assert "brand" in assessment.preference_misses


def test_missing_preference_evidence_never_disqualifies():
    product = verified_noise_product()
    criteria = make_criteria(
        must_have=(noise_requirement(),),
        preferences=(battery_requirement(30),),
    )
    assessment = evaluate_product(product, criteria)
    assert assessment.decision == "COMPLIANT"
    assert "battery_life_hours" in assessment.preference_misses


# ------------------------------------------------------- offer-level selection


def test_select_best_offer_prefers_in_stock_over_cheaper_out_of_stock():
    cheaper_out = make_offer(
        "off_out", price="50.00", availability=Availability.OUT_OF_STOCK
    )
    in_stock = make_offer("off_in", price="60.00", availability=Availability.IN_STOCK)
    chosen = select_best_offer((cheaper_out, in_stock))
    assert chosen is not None and chosen.offer_id == "off_in"


def test_select_best_offer_falls_back_to_cheapest_when_all_out_of_stock():
    a = make_offer("a", price="90.00", availability=Availability.OUT_OF_STOCK)
    b = make_offer("b", price="70.00", availability=Availability.OUT_OF_STOCK)
    assert select_best_offer((a, b)).offer_id == "b"
    assert select_best_offer(()) is None


def test_offer_facts_come_from_the_selected_offer_only():
    unknown_condition = make_offer(
        "cheap", price="80.00", condition=Condition.UNKNOWN
    )
    new_condition = make_offer("dear", price="100.00", condition=Condition.NEW)
    product = make_product(offers=(unknown_condition, new_condition))
    assessment = evaluate_product(product, make_criteria(budget="150"))
    assert assessment.best_offer_id == "cheap"
    condition = assessment.fields["condition"]
    assert condition.status is EvidenceStatus.MISSING
    # The other merchant's NEW condition must not leak in.
    assert all(claim.value != "new" for claim in condition.claims)
    assert any("condition is unknown" in item for item in assessment.missing_information)


def test_price_conflict_between_sources_blocks_certification():
    product = verified_noise_product(offers=(make_offer(price="100.00"),))
    conflict = Claim(
        source=ClaimSource.KERNEL,
        value="80.00",
        trust="EXTERNAL_CONFIRMED",
        directly_observed=True,
        url="https://www.bestbuy.com/site/product/1",
    )
    assessment = evaluate_product(
        product,
        make_criteria(must_have=(noise_requirement(),)),
        extra_claims={"price": [conflict]},
    )
    assert assessment.fields["price"].status is EvidenceStatus.CONFLICTING
    assert assessment.decision == "INSUFFICIENT_EVIDENCE"
    assert any("price conflicts" in item for item in assessment.conflicts)


# ------------------------------------------------------------------ ranking


def _compliant_pair():
    noise = noise_requirement()
    sony = make_product(
        product_id="sony",
        brand="Sony",
        attributes={"noise_cancellation": ["active"]},
        offers=(make_offer("sony_offer", price="90.00", product_id="sony"),),
    )
    jbl = make_product(
        product_id="jbl",
        brand="JBL",
        attributes={"noise_cancellation": ["active"]},
        offers=(make_offer("jbl_offer", price="80.00", product_id="jbl"),),
    )
    criteria = make_criteria(
        must_have=(noise,),
        preferences=(Requirement(attribute="brand", comparator="equal", value="Sony"),),
    )
    return [evaluate_product(sony, criteria), evaluate_product(jbl, criteria)], criteria


def test_recommendations_are_ranked_from_compliant_products_only():
    assessments, criteria = _compliant_pair()
    assert all(a.decision == "COMPLIANT" for a in assessments)
    cheapest, best_value, best_match = recommend(assessments, criteria)

    assert cheapest is not None and cheapest.product_id == "jbl"
    assert cheapest.price == "80.00"
    # Preference match puts the Sony product ahead on value and match.
    assert best_value is not None and best_value.product_id == "sony"
    assert best_match is not None and best_match.product_id == "sony"
    assert best_value.value_score is not None


def test_no_qualifying_products_returns_no_recommendations():
    criteria = make_criteria(budget="10")
    assessments = [
        evaluate_product(
            make_product(
                product_id="a", offers=(make_offer("a", price="100.00", product_id="a"),)
            ),
            criteria,
        ),
        evaluate_product(
            make_product(
                product_id="b", offers=(make_offer("b", price="200.00", product_id="b"),)
            ),
            criteria,
        ),
    ]
    assert all(a.decision == "DISQUALIFIED" for a in assessments)
    assert recommend(assessments, criteria) == (None, None, None)


def test_insufficient_evidence_products_are_never_recommended():
    criteria = make_criteria(must_have=(noise_requirement(),))
    assessment = evaluate_product(make_product(attributes={}), criteria)
    assert assessment.decision == "INSUFFICIENT_EVIDENCE"
    assert recommend([assessment], criteria) == (None, None, None)


# ------------------------------------------------------------------ helpers


def test_attribute_matching_is_normalised_and_conservative():
    assert attribute_matches("battery_life_hours", "battery-life")
    assert attribute_matches("noise_cancellation", "noise-cancellation-technology")
    assert not attribute_matches("noise_cancellation", "colour")
    assert not attribute_matches("", "anything")
