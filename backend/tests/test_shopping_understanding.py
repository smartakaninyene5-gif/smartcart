"""Shopping request understanding: prompt rules, parsing and clarification."""
from __future__ import annotations

import pytest

from app.shopping.understanding import (
    UNDERSTANDING_SCHEMA,
    build_understanding_prompt,
    criteria_from_payload,
    deterministic_clarifications,
    parse_understanding,
    parse_understanding_text,
    understanding_to_criteria,
)

NOW = __import__("datetime").datetime(2026, 10, 31, 12, 0, tzinfo=__import__("datetime").timezone.utc)

FULL = {
    "category": "headphones",
    "search_query": "wireless noise cancelling headphones",
    "must_have": [
        {"attribute": "noise_cancellation", "comparator": "boolean_true"},
        {"attribute": "battery_life_hours", "comparator": "at_least", "numeric": 30},
    ],
    "preferences": [
        {"attribute": "brand", "comparator": "equal", "value": "Sony"},
    ],
    "budget": 100,
    "currency": "USD",
    "quantity": 1,
    "preferred_brands": ["Sony", "JBL"],
    "excluded_brands": ["Beats"],
    "excluded_merchants": ["backmarket.com"],
    "clarifications_needed": [],
    "notes": ["noise cancellation is compulsory"],
}


def test_prompt_states_must_preference_and_no_invention_rules():
    prompt = build_understanding_prompt("I need headphones under $100")
    assert "MUST HAVE" in prompt
    assert "PREFERENCE" in prompt
    assert "Never invent" in prompt
    assert "I need headphones under $100" in prompt
    assert "JSON only" in prompt


def test_empty_prompt_text_is_rejected():
    with pytest.raises(ValueError):
        build_understanding_prompt("   ")


def test_schema_requires_the_essential_fields():
    assert set(UNDERSTANDING_SCHEMA["required"]) >= {
        "category", "search_query", "must_have", "preferences", "currency"
    }


def test_parse_separates_mandatory_from_preferences():
    understanding = parse_understanding(FULL)
    assert [r.attribute for r in understanding.must_have] == [
        "noise_cancellation", "battery_life_hours"
    ]
    assert [r.attribute for r in understanding.preferences] == ["brand"]
    assert understanding.must_have[1].numeric == 30
    assert understanding.budget == 100
    # Understanding keeps display casing; confirmed criteria normalise it.
    assert understanding.excluded_brands == ["Beats"]
    assert understanding_to_criteria(
        understanding, request_id="sreq_1", confirmed_at=NOW
    ).excluded_brands == ("beats",)


def test_parse_from_raw_text():
    import json

    understanding = parse_understanding_text(json.dumps(FULL))
    assert understanding.search_query.startswith("wireless")


def test_unparseable_text_raises():
    with pytest.raises(ValueError):
        parse_understanding_text("{not json")


def test_unknown_fields_are_ignored_but_nothing_is_invented():
    payload = dict(FULL)
    payload["invented_requirement"] = "must be gold plated"
    understanding = parse_understanding(payload)
    assert "gold" not in understanding.search_query
    assert len(understanding.must_have) == 2


def test_at_least_without_numeric_is_rejected():
    payload = dict(FULL)
    payload["must_have"] = [{"attribute": "battery_life_hours", "comparator": "at_least"}]
    with pytest.raises(ValueError):
        parse_understanding(payload)


def test_money_with_more_than_two_decimals_is_rejected():
    payload = dict(FULL)
    payload["budget"] = "99.999"
    with pytest.raises(ValueError):
        parse_understanding(payload)


def test_deterministic_clarifications_for_incomplete_request():
    incomplete = parse_understanding({"category": "", "search_query": "", "must_have": [], "preferences": []})
    clarifications = deterministic_clarifications(incomplete)
    assert any("search_query" in item for item in clarifications)
    assert any("category" in item for item in clarifications)
    assert any("budget" in item for item in clarifications)
    assert any("requirements" in item for item in clarifications)


def test_complete_request_needs_no_backend_clarifications():
    assert deterministic_clarifications(parse_understanding(FULL)) == []


def test_understanding_to_criteria_freezes_the_authoritative_criteria():
    understanding = parse_understanding(FULL)
    criteria = understanding_to_criteria(understanding, request_id="sreq_1", confirmed_at=NOW)
    assert criteria.request_id == "sreq_1"
    assert criteria.budget == 100
    assert criteria.currency == "USD"
    assert len(criteria.must_have) == 2
    assert criteria.excluded_merchants == ("backmarket.com",)
    assert criteria.confirmed_at == NOW


def test_criteria_require_a_query():
    understanding = parse_understanding(
        {"category": "", "search_query": "", "must_have": [], "preferences": []}
    )
    with pytest.raises(ValueError):
        understanding_to_criteria(understanding, request_id="sreq_1", confirmed_at=NOW)


def test_criteria_fall_back_to_category_when_query_is_empty():
    understanding = parse_understanding(
        {"category": "headphones", "search_query": "", "must_have": [], "preferences": []}
    )
    criteria = understanding_to_criteria(understanding, request_id="sreq_1", confirmed_at=NOW)
    assert criteria.search_query == "headphones"


def test_confirm_payload_rejects_unusable_criteria():
    with pytest.raises(ValueError):
        criteria_from_payload(
            {"category": "", "search_query": "", "must_have": [], "preferences": []},
            request_id="sreq_1",
            confirmed_at=NOW,
        )


def test_confirm_payload_rejects_non_positive_budget():
    with pytest.raises(ValueError):
        criteria_from_payload(
            {**FULL, "budget": 0},
            request_id="sreq_1",
            confirmed_at=NOW,
        )
