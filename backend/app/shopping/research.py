"""Deterministic research decisioning and Kernel page-text extraction.

Two jobs, both pure functions and fully unit-testable:

1. :func:`research_decision` - decides, from the deterministic assessment,
   whether targeted KERNEL research is warranted for a product, which fields
   to investigate, which exact offer URL to visit, and why. Browsing is never
   triggered by missing *preferences* alone, and never for products already
   disqualified by verified evidence.

2. :func:`extract_findings` - extracts spec facts from an already-fetched
   page's visible text using literal substring/regex matching only. There is
   no LLM in the extraction loop, and page text can never become
   instructions: it is only ever searched as data. Only literal matches may
   be tagged directly observed (VERIFIED-worthy); anything fuzzier stays
   UNVERIFIED at most.
"""

from __future__ import annotations

import re
from typing import Sequence

from app.domain.models import Product
from app.shopping.models import (
    ConfirmedCriteria,
    EvidenceStatus,
    ProductAssessment,
    ResearchDecision,
    ResearchFinding,
    Requirement,
)
from app.shopping.evaluation import _evidence_for, select_best_offer


# --------------------------------------------------------------------- decide

_BLOCKING_STATUSES = {EvidenceStatus.MISSING, EvidenceStatus.UNVERIFIED, EvidenceStatus.CONFLICTING}


def research_decision(
    assessment: ProductAssessment,
    product: Product,
    criteria: ConfirmedCriteria,
) -> ResearchDecision | None:
    """Return the deterministic decision, or None when research is not needed.

    Reasons name: which required fields are missing/uncertain/conflicting,
    which purchase-decision facts are unknown, why browsing is necessary, and
    the exact product URL to investigate.
    """
    if assessment.decision == "DISQUALIFIED":
        # Verified hard evidence already excludes the product; browsing
        # cannot help and must not be used to soften the verdict.
        return None
    offers = [offer for offer in product.offers if offer.url]
    if not offers:
        return None
    target = select_best_offer(offers)
    if target is None:
        return None
    url = target.url
    fields: list[str] = []
    reasons: list[str] = []

    for requirement in criteria.must_have:
        evidence = _evidence_for(assessment.fields, requirement)
        if evidence.status in _BLOCKING_STATUSES:
            label = _STATUS_WORDS[evidence.status]
            reasons.append(
                f"mandatory attribute '{requirement.attribute}' is {label} in "
                f"Channel3 evidence; browsing the product page can resolve it"
            )
            fields.append(_canonical(requirement.attribute))

    condition = _evidence_for(assessment.fields, Requirement(attribute="condition"))
    if condition.status is EvidenceStatus.MISSING:
        reasons.append("condition is unknown on all offers; purchase-decision information gap")
        fields.append("condition")

    availability = _evidence_for(assessment.fields, Requirement(attribute="availability"))
    if availability.status is EvidenceStatus.MISSING:
        reasons.append("availability/stock status is unknown; needed before considering a purchase")
        fields.append("availability")

    if product.offers and all(offer.shipping_cost is None for offer in product.offers):
        reasons.append("shipping cost missing on every offer; important for the true total")
        fields.append("shipping_cost")

    merchant = _evidence_for(assessment.fields, Requirement(attribute="merchant_domain"))
    if merchant.status is EvidenceStatus.MISSING or (merchant.value or "") == "unknown":
        reasons.append("merchant identity unclear; the relationship between listing and seller is uncertain")
        fields.append("merchant_identity")

    if _variant_ambiguity(product):
        reasons.append(
            "multiple offers for this product differ substantially in price with no "
            "attribute evidence explaining the difference (variant ambiguity)"
        )
        fields.append("variants")

    if not reasons:
        # Rule: evidence is already sufficient for a useful recommendation -
        # do NOT browse for merely-missing preferences.
        return None

    return ResearchDecision(
        product_id=product.product_id,
        offer_id=target.offer_id,
        url=url,
        merchant_domain=target.merchant_domain,
        fields=_dedupe(fields)[:10],
        reasons=reasons[:10],
    )


_STATUS_WORDS = {
    EvidenceStatus.MISSING: "missing",
    EvidenceStatus.UNVERIFIED: "unverified",
    EvidenceStatus.CONFLICTING: "conflicting",
}


def _canonical(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def _dedupe(items: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for item in items:
        seen.setdefault(item, None)
    return list(seen)


def _variant_ambiguity(product: Product) -> bool:
    if len(product.offers) < 2:
        return False
    prices = sorted(offer.price for offer in product.offers)
    cheapest, dearest = prices[0], prices[-1]
    if cheapest <= 0:
        return False
    spread = (dearest - cheapest) / cheapest
    explained = bool(product.attributes)
    return spread > 0.25 and not explained


# -------------------------------------------------------------------- extract

def extract_findings(
    page_url: str,
    page_title: str | None,
    page_text: str,
    observed_at,
    fields: Sequence[str],
    *,
    redirected: bool = False,
) -> list[ResearchFinding]:
    """Extract facts for the requested fields from raw page text (data only).

    A finding's ``directly_observed`` is True only when a literal substring
    or a structural regex matched. Everything else is left un-extracted: we
    never pretend information was verified when the page does not contain it.
    """
    findings: list[ResearchFinding] = []
    text = page_text or ""
    for field in fields:
        key = _canonical(field)
        extractor = _EXTRACTORS.get(key)
        if extractor is None:
            continue
        found = extractor(text)
        for value, numeric, snippet, direct, notes in found:
            if redirected:
                notes = [*notes, "page was reached through a redirect; final URL differs from the requested one"]
            findings.append(
                ResearchFinding(
                    attribute=key,
                    value=value,
                    numeric=numeric,
                    url=page_url,
                    page_title=page_title,
                    observed_at=observed_at,
                    directly_observed=direct,
                    snippet=snippet,
                    notes=notes,
                )
            )
    return findings


def _window(text: str, start: int, end: int) -> str:
    return text[max(0, start - 60): min(len(text), end + 80)]


def _snippet(text: str, start: int, end: int) -> str:
    snippet = text[max(0, start - 40): min(len(text), end + 40)].replace("\n", " ")
    return re.sub(r"\s+", " ", snippet)[:160]


def _battery_lifes(text: str) -> list[tuple[str, float | None, str, bool, list[str]]]:
    results = []
    for match in re.finditer(r"battery", text, re.IGNORECASE):
        window = _window(text, match.start(), match.end())
        m = re.search(r"(\d{1,3})\s*(?:hours|hrs|hr)\b", window, re.IGNORECASE)
        if m:
            results.append((
                m.group(1),
                float(m.group(1)),
                _snippet(text, m.start(), m.end()),
                True,
                ["battery-life figure found next to a 'battery' mention"],
            ))
    return results[:3]


def _find_phrase(text: str, phrase: str) -> tuple[int, int] | None:
    """Literal phrase lookup with word boundaries (no 'anc' inside 'ancient')."""
    match = re.search(rf"\b{re.escape(phrase)}\b", text, re.IGNORECASE)
    return (match.start(), match.end()) if match else None


def _phrase_matches(phrases: list[str]):
    def extract(text: str) -> list[tuple[str, float | None, str, bool, list[str]]]:
        results = []
        for phrase in phrases:
            bounds = _find_phrase(text, phrase)
            if bounds is not None:
                start, end = bounds
                results.append((
                    phrase,
                    None,
                    _snippet(text, start, end),
                    True,
                    ["explicit phrase match"],
                ))
        return results[:3]

    return extract


def _availability(text: str) -> list[tuple[str, float | None, str, bool, list[str]]]:
    results = []
    for phrase in _AVAILABILITY_PHRASES:
        bounds = _find_phrase(text, phrase)
        if bounds is not None:
            start, end = bounds
            value = "out_of_stock" if phrase in (
                "out of stock", "sold out", "currently unavailable") else "in_stock"
            results.append((value, None, _snippet(text, start, end), True, []))
    if len(results) > 1:
        results = [
            (value, numeric, snippet, direct, [*notes,
             "conflicting availability phrases on the same page; both claims preserved"])
            for value, numeric, snippet, direct, notes in results
        ]
    return results


_AVAILABILITY_PHRASES = (
    "in stock",
    "out of stock",
    "sold out",
    "pre-order",
    "preorder",
    "backorder",
    "currently unavailable",
)


def _condition(text: str) -> list[tuple[str, float | None, str, bool, list[str]]]:
    for phrase in ("refurbished", "open-box", "open box", "pre-owned", "second-hand"):
        bounds = _find_phrase(text, phrase)
        if bounds is not None:
            start, end = bounds
            value = "open_box" if phrase in ("open-box", "open box") else (
                "refurbished" if phrase == "refurbished" else "used")
            return [(value, None, _snippet(text, start, end), True, [])]
    for phrase in ("brand new", "condition: new", "condition new", "new condition"):
        bounds = _find_phrase(text, phrase)
        if bounds is not None:
            start, end = bounds
            return [("new", None, _snippet(text, start, end), True, [])]
    return []


def _shipping(text: str) -> list[tuple[str, float | None, str, bool, list[str]]]:
    bounds = _find_phrase(text, "free shipping")
    if bounds is not None:
        start, end = bounds
        return [("0.00", 0.0, _snippet(text, start, end), True, [])]
    m = re.search(
        r"(?:shipping|delivery)(?:\s*(?:cost|fee))?\s*(?:is|:)?\s*\$\s?(\d{1,4}(?:\.\d{2})?)",
        text,
        re.IGNORECASE,
    )
    if m:
        value = m.group(1)
        return [(value, float(value), _snippet(text, m.start(), m.end()), True, [])]
    return []


def _price(text: str, *, observed_at=None) -> list[tuple[str, float | None, str, bool, list[str]]]:
    tokens = re.findall(r"\$\s?(\d{1,4}(?:,\d{3})*(?:\.\d{2})?)", text)
    distinct: dict[str, int] = {}
    for token in tokens:
        distinct[token] = distinct.get(token, 0) + 1
    if not distinct:
        return []
    if len(distinct) == 1:
        value = next(iter(distinct))
        return [(value, float(value.replace(",", "")), "price figure on page", True, [])]
    # Several figures: cannot reliably attribute one to this product; report
    # the most frequent one but never count it as directly observed.
    best = max(distinct.items(), key=lambda kv: (kv[1], kv[0]))
    return [
        (
            best[0],
            float(best[0].replace(",", "")),
            _snippet(text, 0, min(len(text), 120)),
            False,
            [f"{len(distinct)} distinct price figures on the page; attribution is unreliable"],
        )
    ]


def _generic_numeric(label_words: list[str], unit_hint: str | None = None):
    def extract(text: str) -> list[tuple[str, float | None, str, bool, list[str]]]:
        lowered = text.lower()
        for word in label_words:
            index = lowered.find(word)
            if index < 0:
                continue
            window = text[index: min(len(text), index + 120)]
            number = re.search(r"\d+(?:[.,]\d+)?", window)
            if not number:
                continue
            value = number.group(0).replace(",", ".")
            unit = None
            if unit_hint:
                unit_match = re.search(unit_hint, window, re.IGNORECASE)
                if unit_match:
                    unit = unit_match.group(0)
            return [(
                value if unit is None else f"{value} {unit}",
                float(value),
                _snippet(text, index + number.start(), index + number.end()),
                False,  # indirect: label near a number; not a spec-table literal
                ["generic numeric near label; kept UNVERIFIED"],
            )]
        return []

    return extract


_EXTRACTORS = {
    "battery_life": _battery_lifes,
    "battery_life_hours": _battery_lifes,
    "noise_cancellation": _phrase_matches([
        "active noise cancellation",
        "active noise cancelling",
        "noise cancelling",
        "noise canceling",
        "adaptive noise cancellation",
        "anc",
    ]),
    "wireless": _phrase_matches(["wireless", "bluetooth"]),
    "water_resistance_rating": _generic_numeric(["water resistance", "waterproof"], r"ipx?\d*"),
    "condition": _condition,
    "availability": _availability,
    "shipping_cost": _shipping,
    "price": _price,
}
