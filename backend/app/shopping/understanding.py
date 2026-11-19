"""Request understanding: Gemini extracts structure, the backend checks it.

Contract:
* The prompt forbids inventing requirements and demands MUST/PREFERENCE
  separation and explicit clarifications.
* Output is parsed into :class:`ShoppingUnderstanding` with Pydantic rules
  (unknown keys dropped, budgets/money validated, enums/limits enforced).
* The backend independently appends deterministic clarifications when
  essentials are missing; Gemini statements are never silently trusted.
"""

from __future__ import annotations

import json
from decimal import Decimal

from app.shopping.models import (
    ConfirmedCriteria,
    Requirement,
    ShoppingUnderstanding,
)

UNDERSTANDING_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "category": {"type": "string"},
        "search_query": {"type": "string"},
        "must_have": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "attribute": {"type": "string"},
                    "comparator": {
                        "type": "string",
                        "enum": ["at_least", "at_most", "equal", "boolean_true", "avoid"],
                    },
                    "value": {"type": "string"},
                    "numeric": {"type": "number"},
                    "detail": {"type": "string"},
                },
                "required": ["attribute", "comparator"],
            },
        },
        "preferences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "attribute": {"type": "string"},
                    "comparator": {
                        "type": "string",
                        "enum": ["at_least", "at_most", "equal", "boolean_true", "avoid"],
                    },
                    "value": {"type": "string"},
                    "numeric": {"type": "number"},
                    "detail": {"type": "string"},
                },
                "required": ["attribute", "comparator"],
            },
        },
        "budget": {"type": "number"},
        "budget_total": {"type": "number"},
        "currency": {"type": "string"},
        "quantity": {"type": "integer"},
        "preferred_brands": {"type": "array", "items": {"type": "string"}},
        "excluded_brands": {"type": "array", "items": {"type": "string"}},
        "excluded_merchants": {"type": "array", "items": {"type": "string"}},
        "clarifications_needed": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["category", "search_query", "must_have", "preferences", "currency"],
}

UNDERSTANDING_PROMPT_TEMPLATE = """You are the request-understanding component of Smartcart, \
an evidence-first shopping agent. Parse the shopping request below.

STRICT RULES:
1. MUST HAVE vs PREFERENCE: only the user's explicit compulsory language (must, \
compulsory, required, has to, need, mandatory, "under $X" budgets) is a MUST. \
Everything softer (prefer, would like, ideally, nice to have) is a PREFERENCE. \
Never upgrade a preference into a must.
2. Never invent requirements that the user did not state.
3. Budget: the maximum acceptable price for ONE unit unless the user's words \
clearly mean a total ("in total", "all together"), which goes to budget_total. \
The currency must be the user's stated currency (or USD if none given).
4. Requirements use attribute names like: noise_cancellation, \
battery_life_hours, wireless, price, condition, water_resistance_rating, etc.
   - comparators: at_least/at_most need "numeric"; equal needs "value"; \
boolean_true for presence; avoid for exclusions.
5. If an essential detail is ambiguous or missing (e.g. budget, category, \
what product), list it in clarifications_needed. Do not guess.
6. Respond with JSON only, matching the given schema. No commentary.

SHOPPING REQUEST:
\"\"\"{text}\"\"\"
"""


def build_understanding_prompt(text: str) -> str:
    if not text or not text.strip():
        raise ValueError("shopping request text is empty")
    return UNDERSTANDING_PROMPT_TEMPLATE.format(text=text.strip()[:4000])


def parse_understanding(payload: dict) -> ShoppingUnderstanding:
    """Validate Gemini's structured answer into a ShoppingUnderstanding."""
    if not isinstance(payload, dict):
        raise ValueError("understanding payload was not an object")
    return ShoppingUnderstanding.model_validate(payload)


def parse_understanding_text(raw_text: str) -> ShoppingUnderstanding:
    """Parse the raw JSON text returned by the model."""
    try:
        payload = json.loads(raw_text)
    except ValueError as exc:
        raise ValueError(f"understanding response was not valid JSON: {exc}") from exc
    return parse_understanding(payload)


def deterministic_clarifications(understanding: ShoppingUnderstanding) -> list[str]:
    """Backend checks Gemini cannot be trusted with (never silently relaxed)."""
    issues: list[str] = []
    if not understanding.search_query.strip():
        issues.append("search_query is empty: what product should be searched for?")
    if not understanding.category.strip():
        issues.append("category is missing: confirm the product category")
    if understanding.budget is None and understanding.budget_total is None:
        issues.append("no budget was extracted: confirm the maximum price per item")
    if not understanding.must_have and not understanding.preferences:
        issues.append("no requirements were extracted: confirm what matters to you")
    if understanding.quantity > 1 and understanding.budget_total is None:
        issues.append(
            f"quantity is {understanding.quantity} but no total budget was extracted; "
            "budget will be applied per unit"
        )
    return issues


def understanding_to_criteria(
    understanding: ShoppingUnderstanding,
    *,
    request_id: str,
    confirmed_at,
) -> ConfirmedCriteria:
    """Freeze a confirmed understanding into authoritative criteria."""
    query = understanding.search_query.strip() or understanding.category.strip()
    if not query:
        raise ValueError("confirmed criteria need a non-empty search query")
    return ConfirmedCriteria(
        request_id=request_id,
        category=understanding.category.strip(),
        search_query=query[:300],
        must_have=tuple(understanding.must_have),
        preferences=tuple(understanding.preferences),
        budget=understanding.budget,
        budget_total=understanding.budget_total,
        currency=understanding.currency,
        quantity=understanding.quantity,
        preferred_brands=tuple(b.strip().lower() for b in understanding.preferred_brands if b.strip())[:10],
        excluded_brands=tuple(b.strip().lower() for b in understanding.excluded_brands if b.strip())[:10],
        excluded_merchants=tuple(m.strip().lower() for m in understanding.excluded_merchants if m.strip())[:20],
        notes=tuple(n.strip() for n in understanding.notes if n.strip())[:10],
        confirmed_at=confirmed_at,
    )


def criteria_from_payload(
    payload: dict,
    *,
    request_id: str,
    confirmed_at,
) -> ConfirmedCriteria:
    """Build confirmed criteria from the confirm-endpoint payload.

    The payload is a corrected ShoppingUnderstanding: money rules, comparator
    rules and bounds all re-validated at the boundary. A confirm payload with
    no usable query is rejected (422 at the API layer).
    """
    if not isinstance(payload, dict):
        raise ValueError("confirm payload was not an object")
    # The price constraint can only come from an explicit budget field.
    understanding = ShoppingUnderstanding.model_validate(payload)
    if understanding.budget is not None and understanding.budget <= Decimal("0"):
        raise ValueError("budget must be greater than zero")
    return understanding_to_criteria(
        understanding,
        request_id=request_id,
        confirmed_at=confirmed_at,
    )


__all__ = [
    "UNDERSTANDING_SCHEMA",
    "build_understanding_prompt",
    "parse_understanding",
    "parse_understanding_text",
    "deterministic_clarifications",
    "understanding_to_criteria",
    "criteria_from_payload",
    "Requirement",
]
