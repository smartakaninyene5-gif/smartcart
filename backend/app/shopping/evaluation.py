"""Deterministic evidence evaluation and recommendation ranking.

This module is the authoritative backend evaluation. It never consults the
LLM. Rules implemented (fixed contract):

* VERIFIED   - every claim for the attribute is directly observed structured
               data (Channel3 source observation or a literal-substring match
               on a visited Kernel page).
* UNVERIFIED - a statement exists but was not directly observed on a source
               page, or direct and AI claims are mixed.
* AI_ASSESSMENT - all claims are AI statements; never usable as a fact.
* CONFLICTING - claims disagree; both are preserved and disclosed.
* MISSING    - no evidence at all.

Unknown or unverifiable mandatory specifications can never count as verified
compliance: they block the check instead. Budget is a hard restriction and a
currency mismatch is never silently relaxed.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Iterable, Sequence

from app.domain.enums import Availability
from app.domain.models import Offer, Product
from app.shopping.models import (
    CheckResult,
    Claim,
    ClaimSource,
    ConfirmedCriteria,
    EvidenceStatus,
    FieldEvidence,
    ProductAssessment,
    Recommendation,
    Requirement,
)

_TRUE_WORDS = {"yes", "true", "active", "enabled", "included", "with", "1"}
_FALSE_WORDS = {"no", "false", "none", "without", "excluded", "not included"}


def _canonical_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")


def _canonical_value(value: str | None) -> str:
    return (value or "").strip().lower()


def select_best_offer(offers: Sequence[Offer]) -> Offer | None:
    """The deterministic offer used for price/compliance decisions.

    In-stock offers are preferred (a cheaper out-of-stock listing cannot be
    recommended); among the preferred pool the cheapest wins, with the offer
    id as a stable tie-break.
    """
    if not offers:
        return None
    in_stock = [offer for offer in offers if offer.availability is Availability.IN_STOCK]
    pool = in_stock or list(offers)
    return min(pool, key=lambda offer: (offer.price, offer.offer_id))


def attribute_matches(required: str, available: str) -> bool:
    """Deterministic attribute matching: normalised equality or containment."""
    req = _canonical_key(required)
    key = _canonical_key(available)
    if not req or not key:
        return False
    return req == key or req in key or key in req


def _looks_numeric(text: str) -> float | None:
    match = re.fullmatch(r"\d+(?:\.\d+)?", text.strip())
    return float(match.group(0)) if match else None


def claim(value: str | None, *, source: ClaimSource, trust: str, url: str | None = None,
          page_title: str | None = None, observed_at=None, directly_observed: bool = False,
          snippet: str | None = None, note: str | None = None) -> Claim:
    numeric = None
    if value is not None:
        numeric = _looks_numeric(value)
    return Claim(
        source=source,
        value=value,
        numeric=numeric,
        trust=trust,
        url=url,
        page_title=page_title,
        observed_at=observed_at,
        directly_observed=directly_observed,
        snippet=snippet,
        note=note,
    )


def build_field_claims(
    product: Product,
    *,
    criteria: ConfirmedCriteria,
    extra_claims: dict[str, list[Claim]] | None = None,
) -> dict[str, list[Claim]]:
    """Assemble provenance-carrying claims from Channel3 data (plus extras)."""
    claims: dict[str, list[Claim]] = {}

    def add(attribute: str, new: Claim) -> None:
        claims.setdefault(_canonical_key(attribute), []).append(new)

    for attribute, values in (product.attributes or {}).items():
        for value in values:
            add(attribute, claim(str(value), source=ClaimSource.CHANNEL3, trust="SOURCE_OBSERVED",
                                 directly_observed=True))
    if product.brand:
        add("brand", claim(product.brand, source=ClaimSource.CHANNEL3, trust="SOURCE_OBSERVED",
                           directly_observed=True))
    best_offer = select_best_offer(product.offers)
    if best_offer is not None:
        # Offer facts are evaluated for the offer that would actually be
        # recommended, never mixed across merchants (a different merchant's
        # condition/availability is not evidence about this offer).
        offer = best_offer
        add("price", claim(_money_text(offer.price), source=ClaimSource.CHANNEL3,
                           trust="SOURCE_OBSERVED", directly_observed=True, note="selected offer price"))
        add(
            "condition",
            claim(
                None if offer.condition.value == "unknown" else offer.condition.value,
                source=ClaimSource.CHANNEL3,
                trust="SOURCE_OBSERVED",
                directly_observed=offer.condition.value != "unknown",
                note=None if offer.condition.value != "unknown" else "condition reported as unknown",
            ),
        )
        add(
            "availability",
            claim(
                None if offer.availability.value == "unknown" else offer.availability.value,
                source=ClaimSource.CHANNEL3,
                trust="SOURCE_OBSERVED",
                directly_observed=offer.availability.value != "unknown",
                note=None if offer.availability.value != "unknown" else "availability reported as unknown",
            ),
        )
        if offer.shipping_cost is not None:
            add("shipping_cost", claim(_money_text(offer.shipping_cost), source=ClaimSource.CHANNEL3,
                                       trust="SOURCE_OBSERVED", directly_observed=True))
        add("merchant_domain", claim(offer.merchant_domain, source=ClaimSource.CHANNEL3,
                                     trust="SOURCE_OBSERVED", directly_observed=True))

    for attribute, extra in (extra_claims or {}).items():
        for item in extra:
            add(attribute, item)
    return claims


def _money_text(amount: Decimal | None) -> str:
    return format(amount.quantize(Decimal("0.01")), "f") if amount is not None else "unknown"


def merge_claims(attribute: str, claims: Sequence[Claim]) -> FieldEvidence:
    """Collapse claims for one attribute into one status (deterministic)."""
    if not claims:
        return FieldEvidence(attribute=attribute, status=EvidenceStatus.MISSING, claims=())
    real = [c for c in claims if c.value is not None]
    if not real:
        # Only "we looked and it was unknown" marker claims.
        return FieldEvidence(attribute=attribute, status=EvidenceStatus.MISSING, claims=tuple(claims))
    values = {_canonical_value(c.value) for c in real}
    if len(values) > 1:
        return FieldEvidence(attribute=attribute, status=EvidenceStatus.CONFLICTING, claims=tuple(claims))
    sources = {c.source for c in real}
    if sources == {ClaimSource.AI}:
        status = EvidenceStatus.AI_ASSESSMENT
    elif all(c.directly_observed and c.trust in ("SOURCE_OBSERVED", "EXTERNAL_CONFIRMED") for c in real):
        status = EvidenceStatus.VERIFIED
    else:
        status = EvidenceStatus.UNVERIFIED
    return FieldEvidence(attribute=attribute, status=status, claims=tuple(claims))


def evaluate_field(requirement: Requirement, evidence: FieldEvidence) -> CheckResult:
    status_hint = [evidence.status.value]
    mandatory = True

    def result(outcome: str, detail: str) -> CheckResult:
        return CheckResult(
            attribute=_canonical_key(requirement.attribute),
            mandatory=mandatory,
            outcome=outcome,  # type: ignore[arg-type]
            status=evidence.status,
            detail=detail,
            evidence_statuses=status_hint,
        )

    if evidence.status in (EvidenceStatus.MISSING,):
        return result("blocked", f"no evidence for required attribute '{evidence.attribute}'")
    if evidence.status is EvidenceStatus.CONFLICTING:
        values = sorted({_canonical_value(c.value) for c in evidence.claims if c.value})
        return result(
            "blocked",
            f"conflicting claims for '{evidence.attribute}' "
            f"(both preserved: {', '.join(values)}); cannot certify compliance",
        )
    if evidence.status in (EvidenceStatus.UNVERIFIED, EvidenceStatus.AI_ASSESSMENT):
        return result(
            "blocked",
            f"'{evidence.attribute}' is {evidence.status.value}: an unverified "
            f"claim cannot count as verified compliance",
        )

    value = _canonical_value(evidence.value)

    def numeric() -> float | None:
        for c in evidence.claims:
            if c.numeric is not None:
                return c.numeric
        return None

    comparator = requirement.comparator
    if comparator == "boolean_true":
        if value in _TRUE_WORDS or any(
            re.search(rf"\b{re.escape(word)}\b", value) for word in _TRUE_WORDS
        ):
            return result("pass", f"verified: '{evidence.value}'")
        if value in _FALSE_WORDS or any(
            re.search(rf"\b{re.escape(word)}\b", value) for word in _FALSE_WORDS
        ):
            return result("fail", f"verified non-compliance: '{evidence.value}'")
        if requirement.value and requirement.value.lower() in value:
            return result("pass", f"verified: '{evidence.value}'")
        return result("blocked", f"verified value '{evidence.value}' could not be matched "
                                 "against the requirement")
    if comparator == "avoid":
        if requirement.value and requirement.value.lower() in value:
            return result("fail", f"verified exclusion violation: '{evidence.value}'")
        if evidence.attribute in ("brand", "merchant_domain") and requirement.value:
            return result("fail", f"verified exclusion violation: '{evidence.value}'")
        return result("pass", f"verified absence of '{requirement.value}'")
    if comparator == "equal":
        if requirement.value and (value == requirement.value.lower()
                                  or requirement.value.lower() in value):
            return result("pass", f"verified match: '{evidence.value}'")
        return result("fail", f"verified mismatch: '{evidence.value}'")
    number = numeric()
    if number is None:
        return result("blocked", "verified value had no parsable numeric figure")
    if comparator == "at_least":
        if number >= (requirement.numeric or 0):
            return result("pass", f"verified {number} >= {requirement.numeric}")
        return result("fail", f"verified {number} < {requirement.numeric}")
    if comparator == "at_most":
        if number <= (requirement.numeric or 0):
            return result("pass", f"verified {number} <= {requirement.numeric}")
        return result("fail", f"verified {number} > {requirement.numeric}")
    return result("blocked", "unknown comparator")  # unreachable


def evaluate_product(
    product: Product,
    criteria: ConfirmedCriteria,
    *,
    extra_claims: dict[str, list[Claim]] | None = None,
) -> ProductAssessment:
    claims = build_field_claims(product, criteria=criteria, extra_claims=extra_claims)
    fields = {key: merge_claims(key, items) for key, items in claims.items()}

    checks: list[CheckResult] = []
    reasons: list[str] = []
    missing_information: list[str] = []
    conflicts: list[str] = []

    def run_check(requirement: Requirement, mandatory: bool) -> CheckResult:
        evidence = _evidence_for(fields, requirement)
        check = evaluate_field(requirement, evidence)
        if not mandatory:
            # Non-mandatory checks are recorded but can only ever help.
            check = check.model_copy(update={"mandatory": False})
        return check

    mandatory_checks: list[CheckResult] = []
    for requirement in criteria.must_have:
        check = run_check(requirement, True)
        mandatory_checks.append(check)

    preference_checks: list[CheckResult] = []
    for preference in criteria.preferences:
        preference_checks.append(run_check(preference, False))

    # Brand / merchant exclusions are mandatory by definition.
    for check in preference_checks:
        checks.append(check)

    # Hard checks with offers: budget, currency, exclusions, decision facts.
    offers = product.offers
    budget_ok = True
    currency_ok = True
    if offers:
        best = select_best_offer(offers)
        if criteria.currency and best.currency != criteria.currency:
            currency_ok = False
            reasons.append(
                f"currency mismatch: offer currency {best.currency} != required "
                f"{criteria.currency}; currency is never silently relaxed"
            )
        if criteria.budget is not None and best.price > criteria.budget:
            budget_ok = False
            reasons.append(
                f"budget violation: best price {_money_text(best.price)} {best.currency} "
                f"> budget {_money_text(criteria.budget)} {criteria.currency}"
            )
        if criteria.excluded_merchants:
            for offer in offers:
                if offer.merchant_domain in criteria.excluded_merchants:
                    reasons.append(f"excluded merchant: {offer.merchant_domain}")
    if criteria.excluded_brands and product.brand:
        brand = product.brand.strip().lower()
        for excluded in criteria.excluded_brands:
            if brand == excluded or excluded in brand:
                reasons.append(f"excluded brand: {product.brand}")

    checks.extend(mandatory_checks)
    for check in mandatory_checks:
        if check.outcome == "fail":
            reasons.append(f"mandatory requirement failed: {check.attribute} - {check.detail}")
        elif check.outcome == "blocked":
            missing_information.append(
                f"mandatory attribute '{check.attribute}' unresolved ({check.status.value})"
            )
            if check.status is EvidenceStatus.CONFLICTING:
                conflicts.append(check.detail)

    condition_field = _evidence_for(fields, Requirement(attribute="condition"))
    availability_field = _evidence_for(fields, Requirement(attribute="availability"))
    if condition_field.status is EvidenceStatus.MISSING:
        missing_information.append("product condition is unknown")
    if availability_field.status is EvidenceStatus.MISSING:
        missing_information.append("availability/stock status is unknown")

    # A price conflict between Channel3 and a researched page means the price
    # itself cannot be certified - disclose both claims and block.
    price_field = _evidence_for(fields, Requirement(attribute="price"))
    if price_field.status is EvidenceStatus.CONFLICTING:
        price_values = sorted({_canonical_value(c.value) for c in price_field.claims if c.value})
        message = (
            "price conflicts between sources (both claims preserved: "
            f"{', '.join(price_values)})"
        )
        missing_information.append(message)
        conflicts.append(message)

    disqualified = bool(reasons) or any(c.outcome == "fail" for c in mandatory_checks)
    blocked = any(c.outcome == "blocked" for c in mandatory_checks) or (
        price_field.status is EvidenceStatus.CONFLICTING
    )
    if disqualified:
        decision = "DISQUALIFIED"
    elif blocked:
        decision = "INSUFFICIENT_EVIDENCE"
    else:
        decision = "COMPLIANT"

    best = select_best_offer(offers)
    best_price = _money_text(best.price) if best else None
    best_offer = best

    preference_matches: list[str] = []
    preference_misses: list[str] = []
    for preference, check in zip(criteria.preferences, preference_checks):
        if check.outcome == "pass":
            preference_matches.append(check.attribute)
        else:
            preference_misses.append(check.attribute)

    return ProductAssessment(
        product_id=product.product_id,
        title=product.title,
        brand=product.brand,
        decision=decision,  # type: ignore[arg-type]
        fields=fields,
        checks=checks,
        reasons=reasons,
        missing_information=missing_information,
        conflicts=conflicts,
        best_price=best_price,
        currency=best.currency if best else None,
        best_offer_id=best_offer.offer_id if best_offer else None,
        best_offer_url=best_offer.url if best_offer else None,
        preference_matches=preference_matches,
        preference_misses=preference_misses,
    )


def _evidence_for(fields: dict[str, FieldEvidence], requirement: Requirement) -> FieldEvidence:
    if requirement.attribute in fields:
        return fields[requirement.attribute]
    for key, evidence in fields.items():
        if attribute_matches(requirement.attribute, key):
            return evidence
    return FieldEvidence(attribute=_canonical_key(requirement.attribute),
                         status=EvidenceStatus.MISSING, claims=())


def value_score(assessment: ProductAssessment) -> float:
    """Transparent, documented ranking score.

    score = 3 * mandatory_satisfied + 2 * preferences_matched
            + verified_spec_count - 2 * blocked_important - 2 * conflicts
    """
    mandatory_pass = sum(1 for c in assessment.checks if c.mandatory and c.outcome == "pass")
    blocked = len(assessment.missing_information) + len(assessment.conflicts)
    verified_specs = sum(
        1 for field in assessment.fields.values()
        if field.status is EvidenceStatus.VERIFIED and field.attribute
        not in ("price", "merchant_domain", "brand")
    )
    score = 3 * mandatory_pass + 2 * len(assessment.preference_matches) + verified_specs
    score -= 2 * blocked
    return float(score)


def recommend(
    assessments: Iterable[ProductAssessment],
    criteria: ConfirmedCriteria,
) -> tuple[Recommendation | None, Recommendation | None, Recommendation | None]:
    """Produce (cheapest, best_value, best_match) from COMPLIANT assessments.

    Categories are independently omitted when no qualifying product or
    insufficient distinct evidence exists; nothing is manufactured.
    """
    eligible = [a for a in assessments if a.decision == "COMPLIANT" and a.best_price is not None]
    if not eligible:
        return None, None, None

    def price_key(a: ProductAssessment) -> tuple[Decimal, str]:
        return (Decimal(str(a.best_price)), a.product_id)

    def score_key(a: ProductAssessment) -> tuple[float, tuple[Decimal, str]]:
        return (-value_score(a), price_key(a))

    def match_key(a: ProductAssessment) -> tuple[int, tuple[Decimal, str]]:
        brand_bonus = 1 if a.brand and a.brand.strip().lower() in criteria.preferred_brands else 0
        return (-(len(a.preference_matches) + brand_bonus), price_key(a))

    cheapest = min(eligible, key=price_key)
    best_value = min(eligible, key=score_key)
    best_match = min(eligible, key=match_key)
    return (
        _recommendation("cheapest_compliant", cheapest, criteria,
                        "lowest price among products with verified compliance"),
        _recommendation("best_value", best_value, criteria,
                        "strongest transparent price-to-evidence balance"),
        _recommendation("best_match", best_match, criteria,
                        "closest match to stated preferences among compliant products"),
    )


def _recommendation(
    category: str,
    assessment: ProductAssessment,
    criteria: ConfirmedCriteria,
    reason: str,
) -> Recommendation:
    supporting: list[str] = []
    for check in assessment.checks:
        if check.outcome == "pass":
            supporting.append(f"{check.attribute}: {check.status.value}")
    mandatory_satisfied = [c.attribute for c in assessment.checks if c.mandatory and c.outcome == "pass"]
    return Recommendation(
        category=category,  # type: ignore[arg-type]
        product_id=assessment.product_id,
        title=assessment.title,
        best_offer_id=assessment.best_offer_id or "",
        price=assessment.best_price or "unknown",
        currency=assessment.currency or criteria.currency,
        url=assessment.best_offer_url,
        reason=reason,
        mandatory_satisfied=mandatory_satisfied,
        preferences_matched=list(assessment.preference_matches),
        supporting_evidence=supporting,
        missing_or_uncertain=list(assessment.missing_information),
        tradeoffs=list(assessment.conflicts),
        value_score=value_score(assessment) if category == "best_value" else None,
    )
