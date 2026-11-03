"""Purchase Guard engine: deterministic aggregation of rule results."""

from __future__ import annotations

import hashlib

from app.domain.enums import GuardOutcome, RuleStatus
from app.domain.guard.rules import RULES, GuardContext, GuardDecision, RuleResult
from app.domain.models import Quote
from app.domain.snapshot import canonical_json


def _decision_id(quote: Quote, context: GuardContext, results: tuple[RuleResult, ...]) -> str:
    material = {
        "quote": quote.quote_id,
        "snapshot": quote.snapshot_hash,
        "policy": context.policy.version,
        "stage": context.stage,
        "at": context.now,
        "rules": [(r.code, r.status.value) for r in results],
        "approval": context.approval.approval_id if context.approval else None,
        "payment": context.payment_amount,
        "duplicate": context.duplicate_capture_exists,
    }
    digest = hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()
    return f"gd_{digest[:24]}"


def evaluate(quote: Quote, context: GuardContext) -> GuardDecision:
    """Evaluate every rule and aggregate.

    Any FAIL denies. Otherwise any INSUFFICIENT needs more information.
    Only a clean run allows. There is no override parameter and no way for an
    unpaid or AI-controlled caller to change the outcome.
    """

    results = tuple(rule(quote, context) for rule in RULES)
    failures = tuple(r for r in results if r.status is RuleStatus.FAIL)
    insufficient = tuple(r for r in results if r.status is RuleStatus.INSUFFICIENT)

    if failures:
        outcome = GuardOutcome.DENY
    elif insufficient:
        outcome = GuardOutcome.NEEDS_INFO
    else:
        outcome = GuardOutcome.ALLOW

    blocking = tuple(r.code for r in failures + insufficient)
    if outcome is GuardOutcome.ALLOW:
        summary = "All deterministic checks passed; the user may be asked to approve."
    else:
        summary = "; ".join(f"{r.code}: {r.reason}" for r in failures + insufficient)

    return GuardDecision(
        decision_id=_decision_id(quote, context, results),
        quote_id=quote.quote_id,
        user_id=quote.user_id,
        outcome=outcome,
        stage=context.stage,
        policy_version=context.policy.version,
        snapshot_hash=quote.snapshot_hash,
        evaluated_at=context.now,
        rules=results,
        blocking_codes=blocking,
        summary=summary,
    )
