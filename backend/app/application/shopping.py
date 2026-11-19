"""Shopping-intelligence orchestration.

Workflow (all steps journaled, all evaluation deterministic):

    understand (Gemini, advisory) -> user confirm -> Channel3 discovery
    -> deterministic evidence evaluation -> targeted KERNEL research for
    products with evidence gaps -> provenance-preserving evidence storage
    -> deterministic re-evaluation -> deterministic recommendations with
    optional Gemini interpretation (advisory prose only).

Purchase Guard and payments are untouched here: this module can recommend,
never approve, pay, or override a DENY.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.application.discovery import search_products
from app.application.errors import (
    AiNotConfigured,
    AiUnavailable,
    NotFound,
    ShoppingState,
    ValidationFailure,
)
from app.config import Settings
from app.domain.enums import Actor, EvidenceKind, TrustLevel
from app.domain.models import Product, as_utc
from app.journal import JournalEvents, JournalWriter
from app.persistence.repositories import (
    EvidenceRepository,
    ProductRepository,
    ShoppingRepository,
    UserRepository,
)
from app.ports import KernelBrowserClient, LlmClient, LlmError, ProductQuery, ProductSource
from app.shopping import evaluation
from app.shopping.interpretation import (
    INTERPRETATION_SCHEMA,
    build_interpretation_prompt,
    deterministic_explanation,
    merge_explanation,
    parse_interpretation,
)
from app.shopping.models import (
    Claim,
    ClaimSource,
    ConfirmedCriteria,
    ProductAssessment,
    RecommendationSet,
    ResearchDecision,
    ResearchFinding,
    ShoppingUnderstanding,
    evidence_status_dict,
)
from app.shopping.research import extract_findings, research_decision
from app.shopping.understanding import (
    UNDERSTANDING_SCHEMA,
    build_understanding_prompt,
    criteria_from_payload,
    deterministic_clarifications,
    parse_understanding,
)


@dataclass
class UnderstandOutcome:
    request_id: str
    status: str
    understanding: ShoppingUnderstanding
    clarifications: list[str]


@dataclass
class ConfirmOutcome:
    request_id: str
    status: str
    criteria: ConfirmedCriteria


@dataclass
class ResearchOutcome:
    product_id: str
    offer_id: str
    url: str
    session_id: str
    fields: list[str]
    reasons: list[str]
    findings: list[dict[str, Any]]
    limitations: list[str]
    success: bool


@dataclass
class ShoppingSearchOutcome:
    search_id: str
    request_id: str
    source: str
    data_mode: str
    as_of: object
    warnings: tuple[str, ...]
    criteria: ConfirmedCriteria
    assessments: list[ProductAssessment]
    product_count: int
    offer_count: int
    evidence_count: int
    planned_research: list[ResearchDecision]
    result: dict[str, Any] = field(default_factory=dict)
    performed_research: list[ResearchOutcome] = field(default_factory=list)


# ------------------------------------------------------------------- helpers


def _llm_structured(llm: LlmClient | None, *, prompt: str, schema: dict) -> dict:
    if llm is None:
        raise AiNotConfigured("no AI provider is configured")
    try:
        payload = llm.structured(prompt=prompt, schema=schema)
    except LlmError as exc:
        raise AiUnavailable(f"Gemini request failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise AiUnavailable("Gemini returned an unexpected payload type")
    return payload


def findings_to_claims(findings: list[ResearchFinding]) -> dict[str, list[Claim]]:
    claims: dict[str, list[Claim]] = {}
    for finding in findings:
        item = Claim(
            source=ClaimSource.KERNEL,
            value=finding.value,
            numeric=finding.numeric,
            trust="EXTERNAL_CONFIRMED" if finding.directly_observed else "AI_UNVERIFIED",
            url=finding.url,
            page_title=finding.page_title,
            observed_at=finding.observed_at,
            directly_observed=finding.directly_observed,
            snippet=finding.snippet,
            note="; ".join(finding.notes) if finding.notes else None,
        )
        claims.setdefault(finding.attribute, []).append(item)
    return claims


def _persist_finding(
    evidence_repo: EvidenceRepository,
    finding: ResearchFinding,
    user_id: str,
    product_id: str,
) -> str:
    directly = finding.directly_observed
    row = evidence_repo.add(
        user_id=user_id,
        kind=EvidenceKind.EXTERNAL_CONFIRMATION if directly else EvidenceKind.DERIVED,
        trust=TrustLevel.EXTERNAL_CONFIRMED if directly else TrustLevel.AI_UNVERIFIED,
        source="kernel",
        source_ref=finding.url[:300],
        subject_type="product_spec",
        subject_id=product_id,
        observed_at=finding.observed_at,
        as_of=finding.observed_at,
        payload={
            "field": finding.attribute,
            "value": finding.value,
            "numeric": finding.numeric,
            "directly_observed": directly,
            "url": finding.url[:500],
            "page_title": finding.page_title,
            "snippet": finding.snippet,
            "notes": list(finding.notes),
        },
    )
    return row.id


# ---------------------------------------------------------------- understand


def understand_request(
    session: Session,
    *,
    user_id: str,
    text: str,
    llm: LlmClient | None,
    clock,
    environment: str,
    correlation_id: str | None = None,
) -> UnderstandOutcome:
    users = UserRepository(session)
    users.ensure(user_id)
    journal = JournalWriter(session, environment=environment)

    try:
        payload = _llm_structured(
            llm,
            prompt=build_understanding_prompt(text),
            schema=UNDERSTANDING_SCHEMA,
        )
    except AiNotConfigured as exc:
        journal.failure(
            error_code="AI_NOT_CONFIGURED",
            message=str(exc),
            subject_type="shopping_request",
            subject_id="understanding",
            user_id=user_id,
            correlation_id=correlation_id,
        )
        raise
    except AiUnavailable as exc:
        journal.failure(
            error_code="AI_UNAVAILABLE",
            message=str(exc),
            subject_type="shopping_request",
            subject_id="understanding",
            user_id=user_id,
            correlation_id=correlation_id,
        )
        raise

    try:
        understanding = parse_understanding(payload)
    except ValueError as exc:
        journal.failure(
            error_code="AI_UNDERSTANDING_MALFORMED",
            message=str(exc),
            subject_type="shopping_request",
            subject_id="understanding",
            user_id=user_id,
            correlation_id=correlation_id,
        )
        raise AiUnavailable(f"Gemini returned an unusable understanding: {exc}") from exc

    clarifications = deterministic_clarifications(understanding)
    all_clarifications = list(
        dict.fromkeys(understanding.clarifications_needed + clarifications)
    )[:10]
    repository = ShoppingRepository(session)
    row = repository.create_request(
        user_id,
        text,
        understanding.model_dump(mode="json"),
        all_clarifications,
    )
    journal.record(
        JournalEvents.SHOPPING_REQUESTED,
        actor=Actor.AI,
        subject_type="shopping_request",
        subject_id=row.id,
        user_id=user_id,
        correlation_id=correlation_id,
        payload={
            "category": understanding.category[:120],
            "search_query": understanding.search_query[:120] if understanding.search_query else "",
            "must_have_count": len(understanding.must_have),
            "preference_count": len(understanding.preferences),
            "clarification_count": len(all_clarifications),
        },
    )
    return UnderstandOutcome(
        request_id=row.id,
        status=row.status,
        understanding=understanding,
        clarifications=all_clarifications,
    )


# -------------------------------------------------------------------- confirm


def confirm_request(
    session: Session,
    *,
    user_id: str,
    request_id: str,
    payload: dict,
    clock,
    environment: str,
    correlation_id: str | None = None,
) -> ConfirmOutcome:
    repository = ShoppingRepository(session)
    row = repository.get_request(user_id, request_id)
    if row is None:
        raise NotFound(f"shopping request {request_id} not found")
    journal = JournalWriter(session, environment=environment)
    try:
        criteria = criteria_from_payload(
            payload,
            request_id=request_id,
            confirmed_at=as_utc(clock.now()),
        )
    except ValueError as exc:
        raise ValidationFailure(f"confirmed criteria are not usable: {exc}") from exc
    repository.confirm_request(row, criteria.model_dump(mode="json"), as_utc(clock.now()))
    journal.record(
        JournalEvents.SHOPPING_CONFIRMED,
        actor=Actor.USER,
        subject_type="shopping_request",
        subject_id=request_id,
        user_id=user_id,
        correlation_id=correlation_id,
        payload={
            "search_query": criteria.search_query[:120],
            "must_have": [requirement.attribute for requirement in criteria.must_have],
            "preferences": [requirement.attribute for requirement in criteria.preferences],
            "budget": str(criteria.budget) if criteria.budget is not None else None,
            "currency": criteria.currency,
            "quantity": criteria.quantity,
        },
    )
    return ConfirmOutcome(request_id=request_id, status="confirmed", criteria=criteria)


# -------------------------------------------------------------------- search


def run_shopping_search(
    session: Session,
    *,
    user_id: str,
    request_id: str,
    source: ProductSource,
    llm: LlmClient | None,
    kernel_client: KernelBrowserClient | None,
    settings: Settings,
    clock,
    environment: str,
    correlation_id: str | None = None,
    do_research: bool = True,
) -> ShoppingSearchOutcome:
    repository = ShoppingRepository(session)
    request_row = repository.get_request(user_id, request_id)
    if request_row is None:
        raise NotFound(f"shopping request {request_id} not found")
    if request_row.status != "confirmed" or not request_row.confirmed_json:
        raise ShoppingState(
            "shopping request is not confirmed yet; run /shopping/confirm first"
        )
    criteria = ConfirmedCriteria.model_validate(request_row.confirmed_json)
    now = as_utc(clock.now())

    users = UserRepository(session)
    users.ensure(user_id)
    journal = JournalWriter(session, environment=environment)
    products_repo = ProductRepository(session)
    evidence_repo = EvidenceRepository(session)

    query = ProductQuery(
        text=criteria.search_query,
        limit=8,
        max_price=criteria.budget,
        currency=criteria.currency,
    )
    discovery = search_products(
        session,
        user_id=user_id,
        query=query,
        source=source,
        environment=environment,
        correlation_id=correlation_id,
    )
    products: list[Product] = list(discovery.products)
    if not products:
        # Re-read any previously stored products matching the same query id
        # is not meaningful without a search contract; an empty result is a
        # valid answer with no qualifying products.
        products = []

    assessments = [evaluation.evaluate_product(product, criteria) for product in products]

    # ---- deterministic research planning ----
    planned: list[ResearchDecision] = []
    if do_research:
        for product, assessment in zip(products, assessments):
            decision = research_decision(assessment, product, criteria)
            if decision is not None:
                planned.append(decision)
        planned.sort(key=lambda d: (_price_of(products, d.product_id), d.product_id))
        planned = planned[: max(0, settings.max_research_products)]

    search_row = repository.create_search(
        user_id=user_id,
        request_id=request_id,
        query_text=criteria.search_query,
        criteria_json=criteria.model_dump(mode="json"),
        result_json={},
        data_mode=discovery.data_mode.value,
        source=discovery.source,
        research_performed=0,
        as_of=discovery.as_of,
    )

    # ---- targeted KERNEL research ----
    performed: list[ResearchOutcome] = []
    extra_claims: dict[str, dict[str, list[Claim]]] = {}
    if planned and kernel_client is None:
        journal.record(
            JournalEvents.RESEARCH_FAILED,
            actor=Actor.SYSTEM,
            subject_type="shopping_search",
            subject_id=search_row.id,
            user_id=user_id,
            correlation_id=correlation_id,
            payload={"planned": len(planned), "reason": "KERNEL research client is not configured"},
        )
    elif planned and kernel_client is not None:
        for decision in planned:
            product = next(p for p in products if p.product_id == decision.product_id)
            journal.record(
                JournalEvents.RESEARCH_STARTED,
                actor=Actor.SYSTEM,
                subject_type="shopping_search",
                subject_id=search_row.id,
                user_id=user_id,
                correlation_id=correlation_id,
                payload={
                    "product_id": decision.product_id,
                    "url": decision.url[:300],
                    "fields": decision.fields,
                    "reasons": decision.reasons,
                },
            )
            outcome, findings = _research_product(
                kernel_client,
                criteria=criteria,
                decision=decision,
                now=now,
            )
            performed.append(outcome)
            evidence_ids = [
                _persist_finding(evidence_repo, finding, user_id, decision.product_id)
                for finding in findings
            ]
            repository.create_research(
                user_id=user_id,
                search_id=search_row.id,
                product_id=decision.product_id,
                offer_id=decision.offer_id,
                url=decision.url,
                kernel_session_id=outcome.session_id,
                fields=decision.fields,
                reasons=decision.reasons,
                findings=outcome.findings,
                limitations=outcome.limitations,
                success=outcome.success,
            )
            claims = findings_to_claims(findings)
            if claims:
                extra_claims[decision.product_id] = claims
            journal.record(
                JournalEvents.RESEARCH_COMPLETED if outcome.success else JournalEvents.RESEARCH_FAILED,
                actor=Actor.SYSTEM,
                subject_type="shopping_search",
                subject_id=search_row.id,
                user_id=user_id,
                correlation_id=correlation_id,
                payload={
                    "product_id": decision.product_id,
                    "findings": len(findings),
                    "evidence_ids": evidence_ids,
                    "limitations": outcome.limitations,
                },
            )

    assessments = _evaluate_with_claims(products, criteria, extra_claims)
    recommendations = _recommendations(assessments, criteria)
    no_qualifying = not recommendations.non_empty

    # ---- Gemini interpretation (advisory prose; statuses stay fixed) ----
    recommendations, ai_participated = _interpret_recommendations(
        recommendations,
        assessments,
        criteria,
        performed,
        llm=llm,
    )

    result_payload = shopping_result_json(
        search_id=search_row.id,
        request_id=request_id,
        as_of=discovery.as_of,
        data_mode=discovery.data_mode.value,
        source=discovery.source,
        warnings=list(discovery.warnings),
        criteria=criteria,
        assessments=assessments,
        planned=planned,
        performed=performed,
        recommendations=recommendations,
        no_qualifying=no_qualifying,
        ai_participated=ai_participated,
    )
    repository.update_search_result(
        search_row,
        result_payload,
        research_performed=len(performed),
    )

    journal.record(
        JournalEvents.SHOPPING_SEARCHED,
        actor=Actor.SYSTEM,
        subject_type="shopping_search",
        subject_id=search_row.id,
        user_id=user_id,
        correlation_id=correlation_id,
        payload={
            "request_id": request_id,
            "query": criteria.search_query[:120],
            "product_count": len(products),
            "offer_count": sum(len(p.offers) for p in products),
            "assessments": {a.product_id: a.decision for a in assessments},
            "research_planned": len(planned),
            "research_performed": len(performed),
            "recommendations": {
                key: getattr(recommendations, key).product_id
                for key in ("cheapest_compliant", "best_value", "best_match")
                if getattr(recommendations, key) is not None
            },
            "no_qualifying_products": no_qualifying,
        },
    )

    return ShoppingSearchOutcome(
        search_id=search_row.id,
        request_id=request_id,
        source=discovery.source,
        data_mode=discovery.data_mode.value,
        as_of=discovery.as_of,
        warnings=discovery.warnings,
        criteria=criteria,
        assessments=assessments,
        product_count=len(products),
        offer_count=sum(len(p.offers) for p in products),
        evidence_count=discovery.evidence_count
        + sum(len(outcome.findings) for outcome in performed),
        planned_research=planned,
        result=result_payload,
        performed_research=performed,
    )


def _evaluate_with_claims(
    products: list[Product],
    criteria: ConfirmedCriteria,
    extra_claims: dict[str, dict[str, list[Claim]]],
) -> list[ProductAssessment]:
    return [
        evaluation.evaluate_product(product, criteria, extra_claims=extra_claims.get(product.product_id))
        for product in products
    ]


def _recommendations(
    assessments: list[ProductAssessment],
    criteria: ConfirmedCriteria,
) -> RecommendationSet:
    cheapest, best_value, best_match = evaluation.recommend(assessments, criteria)
    return RecommendationSet(
        cheapest_compliant=cheapest,
        best_value=best_value,
        best_match=best_match,
    )


def _interpret_recommendations(
    recommendations: RecommendationSet,
    assessments: list[ProductAssessment],
    criteria: ConfirmedCriteria,
    performed: list[ResearchOutcome],
    *,
    llm: LlmClient | None,
) -> tuple[RecommendationSet, bool]:
    """Attach explanations. Gemini prose is advisory; failures fall back.

    Recommendation models are frozen, so the annotated set is rebuilt rather
    than mutated in place.
    """
    used = False
    updated: dict[str, Any] = {}
    for recommendation in recommendations.non_empty:
        assessment = _assessment_for(assessments, recommendation.product_id)
        interpretation = None
        try:
            raw = _llm_structured(
                llm,
                prompt=build_interpretation_prompt(
                    criteria,
                    assessment,
                    research_summary=_research_summary(performed, recommendation.product_id),
                ),
                schema=INTERPRETATION_SCHEMA,
            )
            interpretation = parse_interpretation(raw)
            used = True
        except (AiNotConfigured, AiUnavailable, ValueError):
            interpretation = None
        base = deterministic_explanation(
            assessment,
            ai_participated=interpretation is not None,
            ai_was_available=llm is not None,
        )
        updated[recommendation.category] = recommendation.model_copy(
            update={"explanation": merge_explanation(base, interpretation)}
        )
    return recommendations.model_copy(update=updated), used


# ---------------------------------------------------------------- internal API


def get_search_result(
    session: Session,
    *,
    user_id: str,
    search_id: str,
) -> dict[str, Any]:
    repository = ShoppingRepository(session)
    row = repository.get_search(user_id, search_id)
    if row is None:
        raise NotFound(f"shopping search {search_id} not found")
    payload = dict(row.result_json or {})
    payload["search_id"] = row.id
    payload["research"] = dict(payload.get("research") or {})
    payload["research"]["records"] = [
        {
            "id": r.id,
            "product_id": r.product_id,
            "offer_id": r.offer_id,
            "url": r.url,
            "fields": r.fields,
            "reasons": r.reasons,
            "findings": r.findings,
            "limitations": r.limitations,
            "success": r.success,
        }
        for r in repository.list_research(user_id, search_id)
    ]
    return payload


def targeted_research(
    session: Session,
    *,
    user_id: str,
    search_id: str,
    product_ids: list[str] | None,
    kernel_client: KernelBrowserClient | None,
    settings: Settings,
    clock,
    environment: str,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Internal workflow: run additional KERNEL research on a stored search.

    Bounded to a deterministic subset of the search's own products; no raw
    browser control is exposed. Findings are persisted with provenance and
    the stored evaluation is recomputed with the combined evidence.
    """
    repository = ShoppingRepository(session)
    row = repository.get_search(user_id, search_id)
    if row is None:
        raise NotFound(f"shopping search {search_id} not found")
    criteria = ConfirmedCriteria.model_validate(row.criteria_json)
    stored = (row.result_json or {}).get("assessments", [])
    known_ids = [item["product_id"] for item in stored]
    requested = product_ids or known_ids
    unknown = [pid for pid in requested if pid not in known_ids]
    if unknown:
        raise ValidationFailure(f"product ids not part of this search: {unknown[:5]}")
    requested = requested[: max(0, settings.max_research_products)]

    products_repo = ProductRepository(session)
    products = [
        product
        for product in (products_repo.get_product(user_id, pid) for pid in requested)
        if product is not None
    ]
    if not products:
        raise NotFound(f"no stored products for shopping search {search_id}")
    if kernel_client is None:
        raise ShoppingState("KERNEL research client is not configured")

    journal = JournalWriter(session, environment=environment)
    evidence_repo = EvidenceRepository(session)
    now = as_utc(clock.now())

    research_rows = repository.list_research(user_id, search_id)
    extra_claims = research_claims_from_records(research_rows)

    performed: list[ResearchOutcome] = []
    for product in products:
        assessment = evaluation.evaluate_product(
            product, criteria, extra_claims=extra_claims.get(product.product_id)
        )
        decision = research_decision(assessment, product, criteria)
        if decision is None:
            continue
        journal.record(
            JournalEvents.RESEARCH_STARTED,
            actor=Actor.SYSTEM,
            subject_type="shopping_search",
            subject_id=search_id,
            user_id=user_id,
            correlation_id=correlation_id,
            payload={
                "product_id": decision.product_id,
                "url": decision.url[:300],
                "fields": decision.fields,
                "reasons": decision.reasons,
            },
        )
        outcome, findings = _research_product(
            kernel_client, criteria=criteria, decision=decision, now=now
        )
        performed.append(outcome)
        evidence_ids = [
            _persist_finding(evidence_repo, finding, user_id, decision.product_id)
            for finding in findings
        ]
        repository.create_research(
            user_id=user_id,
            search_id=search_id,
            product_id=decision.product_id,
            offer_id=decision.offer_id,
            url=decision.url,
            kernel_session_id=outcome.session_id,
            fields=decision.fields,
            reasons=decision.reasons,
            findings=outcome.findings,
            limitations=outcome.limitations,
            success=outcome.success,
        )
        claims = findings_to_claims(findings)
        if claims:
            extra_claims.setdefault(decision.product_id, {}).update(claims)
        journal.record(
            JournalEvents.RESEARCH_COMPLETED if outcome.success else JournalEvents.RESEARCH_FAILED,
            actor=Actor.SYSTEM,
            subject_type="shopping_search",
            subject_id=search_id,
            user_id=user_id,
            correlation_id=correlation_id,
            payload={
                "product_id": decision.product_id,
                "findings": len(findings),
                "evidence_ids": evidence_ids,
                "limitations": outcome.limitations,
            },
        )

    # Re-evaluate every product in the search with all accumulated research.
    all_products = [
        product
        for product in (products_repo.get_product(user_id, pid) for pid in known_ids)
        if product is not None
    ]
    assessments = _evaluate_with_claims(all_products, criteria, extra_claims)
    recommendations = _recommendations(assessments, criteria)
    no_qualifying = not recommendations.non_empty
    previous_research = [
        ResearchOutcome(
            product_id=item["product_id"],
            offer_id=item["offer_id"],
            url=item["url"],
            session_id="",
            fields=list(item.get("fields") or []),
            reasons=list(item.get("reasons") or []),
            findings=list(item.get("findings") or []),
            limitations=list(item.get("limitations") or []),
            success=bool(item.get("success")),
        )
        for item in ((row.result_json or {}).get("research") or {}).get("performed", [])
    ]

    result_payload = shopping_result_json(
        search_id=search_id,
        request_id=row.request_id,
        as_of=row.as_of,
        data_mode=row.data_mode,
        source=row.source,
        warnings=list((row.result_json or {}).get("warnings") or []),
        criteria=criteria,
        assessments=assessments,
        planned=[],
        performed=previous_research + performed,
        recommendations=recommendations,
        no_qualifying=no_qualifying,
        ai_participated=False,
    )
    repository.update_search_result(
        row,
        result_payload,
        research_performed=len(previous_research) + len(performed),
    )
    return get_search_result(session, user_id=user_id, search_id=search_id)


def research_claims_from_records(records) -> dict[str, dict[str, list[Claim]]]:
    """Rebuild provenance-checked claims from stored KERNEL research rows."""
    claims: dict[str, dict[str, list[Claim]]] = {}
    for record in records:
        findings: list[ResearchFinding] = []
        for item in record.findings or []:
            try:
                findings.append(ResearchFinding.model_validate(item))
            except ValueError:
                continue
        for attribute, items in findings_to_claims(findings).items():
            claims.setdefault(record.product_id, {}).setdefault(attribute, []).extend(items)
    return claims


# ------------------------------------------------------------ research helpers


def _price_of(products: list[Product], product_id: str) -> Decimal:
    for product in products:
        if product.product_id == product_id:
            return min((offer.price for offer in product.offers), default=Decimal("999999999"))
    return Decimal("999999999")


def _assessment_for(
    assessments: list[ProductAssessment], product_id: str
) -> ProductAssessment:
    for assessment in assessments:
        if assessment.product_id == product_id:
            return assessment
    raise NotFound(f"assessment for {product_id} not found")


def _research_product(
    kernel_client: KernelBrowserClient,
    *,
    criteria: ConfirmedCriteria,
    decision: ResearchDecision,
    now: datetime,
) -> tuple[ResearchOutcome, list[ResearchFinding]]:
    """Create session -> visit page -> extract -> delete session (finally)."""
    limitations: list[str] = []
    findings: list[ResearchFinding] = []
    session_id = ""
    try:
        session = kernel_client.create_session()
        session_id = session.session_id
    except Exception as exc:  # KernelError family; adapter redacts messages
        limitations.append(f"browser session could not be created: {exc}")
        return (
            ResearchOutcome(
                product_id=decision.product_id,
                offer_id=decision.offer_id,
                url=decision.url,
                session_id="",
                fields=list(decision.fields),
                reasons=list(decision.reasons),
                findings=[],
                limitations=limitations,
                success=False,
            ),
            [],
        )

    try:
        page = kernel_client.visit(session_id, decision.url)
        if page.redirected:
            limitations.append(
                f"final URL after redirect was {page.final_url} (requested: {decision.url})"
            )
        if not page.text.strip():
            limitations.append(
                "page returned no visible text (may require JS rendering or be blocked)"
            )
        findings = extract_findings(
            page.final_url,
            page.title,
            page.text,
            now,
            decision.fields,
            redirected=page.redirected,
        )
        if not findings:
            limitations.append(
                "none of the requested fields could be extracted from the page "
                "(left UNKNOWN rather than assumed)"
            )
    except Exception as exc:  # KernelError family
        limitations.append(f"page visit failed: {exc}")
    finally:
        try:
            kernel_client.delete_session(session_id)
        except Exception as exc:
            limitations.append(f"browser cleanup issue: {exc}")

    outcome = ResearchOutcome(
        product_id=decision.product_id,
        offer_id=decision.offer_id,
        url=decision.url,
        session_id=session_id,
        fields=list(decision.fields),
        reasons=list(decision.reasons),
        findings=[
            {
                "attribute": finding.attribute,
                "value": finding.value,
                "numeric": finding.numeric,
                "directly_observed": finding.directly_observed,
                "url": finding.url,
                "page_title": finding.page_title,
                "snippet": finding.snippet,
                "notes": list(finding.notes),
            }
            for finding in findings
        ],
        limitations=limitations,
        success=bool(findings),
    )
    return outcome, findings


def _research_summary(performed: list[ResearchOutcome], product_id: str) -> str:
    lines: list[str] = []
    for outcome in performed:
        if outcome.product_id != product_id:
            continue
        lines.append(
            f"Kernel research visited {outcome.url} and found {len(outcome.findings)} field(s)."
        )
        for finding in outcome.findings[:8]:
            notes = "; ".join(finding.get("notes") or [])[:120]
            lines.append(
                f"- field {finding['attribute']}: value={finding.get('value')!r} "
                f"directly_observed={finding.get('directly_observed')} {notes}"
            )
        for limitation in outcome.limitations[:5]:
            lines.append(f"- limitation: {limitation}")
    return "\n".join(lines[:40])


def shopping_result_json(
    *,
    search_id: str,
    request_id: str,
    as_of,
    data_mode: str,
    source: str,
    warnings: list[str],
    criteria: ConfirmedCriteria,
    assessments: list[ProductAssessment],
    planned: list[ResearchDecision],
    performed: list[ResearchOutcome],
    recommendations: RecommendationSet,
    no_qualifying: bool,
    ai_participated: bool,
) -> dict[str, Any]:
    def assessment_json(assessment: ProductAssessment) -> dict[str, Any]:
        payload = assessment.model_dump(mode="json", exclude={"fields"})
        payload["fields"] = evidence_status_dict(assessment.fields)
        return payload

    return {
        "search_id": search_id,
        "request_id": request_id,
        "as_of": as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of),
        "data_mode": data_mode,
        "source": source,
        "warnings": warnings,
        "criteria": criteria.model_dump(mode="json"),
        "product_count": len(assessments),
        "assessments": [assessment_json(assessment) for assessment in assessments],
        "research": {
            "planned": [decision.model_dump(mode="json") for decision in planned],
            "performed": [
                {
                    "product_id": outcome.product_id,
                    "offer_id": outcome.offer_id,
                    "url": outcome.url,
                    "session_id": outcome.session_id,
                    "fields": outcome.fields,
                    "reasons": outcome.reasons,
                    "findings": outcome.findings,
                    "limitations": outcome.limitations,
                    "success": outcome.success,
                }
                for outcome in performed
            ],
        },
        "recommendations": {
            "cheapest_compliant": _rec_json(recommendations.cheapest_compliant),
            "best_value": _rec_json(recommendations.best_value),
            "best_match": _rec_json(recommendations.best_match),
        },
        "explanations": {
            rec.category: rec.explanation.model_dump(mode="json")
            for rec in recommendations.non_empty
            if rec.explanation is not None
        },
        "no_qualifying_products": no_qualifying,
        "ai_participated": ai_participated,
    }


def _rec_json(rec) -> dict[str, Any] | None:
    if rec is None:
        return None
    return rec.model_dump(mode="json", exclude={"explanation"})
