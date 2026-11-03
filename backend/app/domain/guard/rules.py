"""Purchase Guard rules.

Each rule is a pure function ``(quote, context) -> RuleResult``. Rules never
perform I/O and never consult the wall clock; ``context.now`` is injected.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.enums import Availability, GuardOutcome, RuleStatus
from app.domain.models import ApprovalFacts, Quote, RequirementSet, UserLimits, as_utc
from app.domain.money import CENT, money, money_sum
from app.domain.guard.policy import GuardPolicy


class RuleResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    status: RuleStatus
    reason: str
    evidence_refs: tuple[str, ...] = ()

    @property
    def blocking(self) -> bool:
        return self.status is not RuleStatus.PASS


class GuardContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    now: datetime
    policy: GuardPolicy
    limits: UserLimits
    requirements: RequirementSet
    stage: Literal["pre_approval", "capture"] = "pre_approval"
    approval: ApprovalFacts | None = None
    duplicate_capture_exists: bool = False
    payment_amount: Decimal | None = None
    payment_currency: str | None = None
    order_environment: str | None = None
    expected_environment: str | None = None

    @field_validator("now")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return as_utc(value)

    @field_validator("payment_amount")
    @classmethod
    def _amount(cls, value: object) -> Decimal | None:
        return None if value is None else money(value, "payment_amount", allow_zero=False)


class GuardDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: str
    quote_id: str
    user_id: str
    outcome: GuardOutcome
    stage: str
    policy_version: str
    snapshot_hash: str
    evaluated_at: datetime
    rules: tuple[RuleResult, ...]
    blocking_codes: tuple[str, ...] = ()
    summary: str = ""


def _ok(code: str, reason: str, evidence: tuple[str, ...] = ()) -> RuleResult:
    return RuleResult(code=code, status=RuleStatus.PASS, reason=reason, evidence_refs=evidence)


def _fail(code: str, reason: str, evidence: tuple[str, ...] = ()) -> RuleResult:
    return RuleResult(code=code, status=RuleStatus.FAIL, reason=reason, evidence_refs=evidence)


def _insufficient(code: str, reason: str, evidence: tuple[str, ...] = ()) -> RuleResult:
    return RuleResult(code=code, status=RuleStatus.INSUFFICIENT, reason=reason, evidence_refs=evidence)


def _evidence(quote: Quote) -> tuple[str, ...]:
    return tuple(dict.fromkeys(quote.evidence_refs))


def rule_currency(quote: Quote, ctx: GuardContext) -> RuleResult:
    currencies = {line.currency for line in quote.lines} | {quote.currency}
    if len(currencies) != 1:
        return _fail("G-001_CURRENCY", f"mixed currencies in basket: {sorted(currencies)}", _evidence(quote))
    if quote.currency != ctx.policy.currency:
        return _fail(
            "G-001_CURRENCY",
            f"quote currency {quote.currency} does not match policy currency {ctx.policy.currency}",
            _evidence(quote),
        )
    if quote.currency != ctx.requirements.currency:
        return _fail(
            "G-001_CURRENCY",
            f"quote currency {quote.currency} does not match requirement currency {ctx.requirements.currency}",
            _evidence(quote),
        )
    return _ok("G-001_CURRENCY", f"single currency {quote.currency}")


def rule_item_ceiling(quote: Quote, ctx: GuardContext) -> RuleResult:
    _, max_item, _ = ctx.policy.effective_limits(ctx.limits, ctx.requirements)
    offending = [line for line in quote.lines if line.unit_price > max_item]
    if offending:
        return _fail(
            "G-002_ITEM_CEILING",
            f"unit price exceeds {max_item} for {len(offending)} line(s): "
            + ", ".join(f"{line.offer_id}={line.unit_price}" for line in offending),
            tuple(ref for line in offending for ref in line.evidence_ids),
        )
    return _ok("G-002_ITEM_CEILING", f"all unit prices within {max_item}")


def rule_total_ceiling(quote: Quote, ctx: GuardContext) -> RuleResult:
    max_total, _, _ = ctx.policy.effective_limits(ctx.limits, ctx.requirements)
    if quote.grand_total > max_total:
        return _fail(
            "G-003_TOTAL_CEILING",
            f"basket total {quote.grand_total} exceeds ceiling {max_total}",
            _evidence(quote),
        )
    return _ok("G-003_TOTAL_CEILING", f"basket total {quote.grand_total} within {max_total}")


def rule_quantity(quote: Quote, ctx: GuardContext) -> RuleResult:
    _, _, max_qty = ctx.policy.effective_limits(ctx.limits, ctx.requirements)
    bad = [line for line in quote.lines if not (1 <= line.quantity <= max_qty)]
    if bad:
        return _fail(
            "G-004_QUANTITY",
            f"quantity outside 1..{max_qty} for: " + ", ".join(f"{line.offer_id}={line.quantity}" for line in bad),
        )
    return _ok("G-004_QUANTITY", f"all quantities within 1..{max_qty}")


def rule_availability(quote: Quote, ctx: GuardContext) -> RuleResult:
    unknown = [line for line in quote.lines if line.availability is Availability.UNKNOWN]
    if unknown:
        return _insufficient(
            "G-005_AVAILABILITY",
            "availability unknown for: " + ", ".join(line.offer_id for line in unknown),
            tuple(ref for line in unknown for ref in line.evidence_ids),
        )
    unavailable = [line for line in quote.lines if line.availability is not Availability.IN_STOCK]
    if unavailable:
        return _fail(
            "G-005_AVAILABILITY",
            "offer not in stock: " + ", ".join(f"{line.offer_id}={line.availability.value}" for line in unavailable),
            tuple(ref for line in unavailable for ref in line.evidence_ids),
        )
    return _ok("G-005_AVAILABILITY", "all offers in stock")


def rule_freshness(quote: Quote, ctx: GuardContext) -> RuleResult:
    stale = []
    future = []
    for line in quote.lines:
        age = ctx.now - line.as_of
        if line.as_of > ctx.now + ctx.policy.max_future_skew:
            future.append(line)
        elif age > ctx.policy.max_evidence_age:
            stale.append(line)
    if future:
        return _fail(
            "G-006_FRESHNESS",
            "provider timestamp is in the future: " + ", ".join(line.offer_id for line in future),
            tuple(ref for line in future for ref in line.evidence_ids),
        )
    if stale:
        oldest = max(quote.lines, key=lambda line: ctx.now - line.as_of)
        return _fail(
            "G-006_FRESHNESS",
            f"price evidence older than {ctx.policy.max_evidence_age} (oldest {oldest.as_of.isoformat()})",
            tuple(ref for line in stale for ref in line.evidence_ids),
        )
    return _ok("G-006_FRESHNESS", "price evidence is fresh")


def rule_condition(quote: Quote, ctx: GuardContext) -> RuleResult:
    unknown = [line for line in quote.lines if line.condition.value == "unknown"]
    if unknown:
        return _insufficient(
            "G-007_CONDITION",
            "condition unknown (not assumed new): " + ", ".join(line.offer_id for line in unknown),
            tuple(ref for line in unknown for ref in line.evidence_ids),
        )
    allowed = ctx.policy.effective_allowed_conditions(ctx.requirements)
    if not allowed:
        return _fail("G-007_CONDITION", "no condition is acceptable under current policy")
    rejected = [line for line in quote.lines if line.condition not in allowed]
    if rejected:
        return _fail(
            "G-007_CONDITION",
            "condition not allowed: "
            + ", ".join(f"{line.offer_id}={line.condition.value}" for line in rejected)
            + f" (allowed: {[c.value for c in allowed]})",
            tuple(ref for line in rejected for ref in line.evidence_ids),
        )
    return _ok("G-007_CONDITION", f"conditions allowed: {[c.value for c in allowed]}")


def rule_merchant(quote: Quote, ctx: GuardContext) -> RuleResult:
    unknown = [line for line in quote.lines if line.merchant_domain == "unknown"]
    if unknown:
        return _insufficient(
            "G-008_MERCHANT_POLICY",
            "merchant domain could not be established: " + ", ".join(line.offer_id for line in unknown),
        )
    denied = [line for line in quote.lines if line.merchant_domain in ctx.policy.merchant_denylist]
    if denied:
        return _fail(
            "G-008_MERCHANT_POLICY",
            "merchant is on the denylist: " + ", ".join(sorted({line.merchant_domain for line in denied})),
        )
    if ctx.policy.merchant_allowlist is not None:
        outside = [line for line in quote.lines if line.merchant_domain not in (ctx.policy.merchant_allowlist or ())]
        if outside:
            return _fail(
                "G-008_MERCHANT_POLICY",
                "merchant is not on the allowlist: " + ", ".join(sorted({line.merchant_domain for line in outside})),
            )
    return _ok("G-008_MERCHANT_POLICY", "merchants satisfy policy")


def rule_evidence(quote: Quote, ctx: GuardContext) -> RuleResult:
    missing = [line.offer_id for line in quote.lines if not line.evidence_ids]
    if missing:
        return _insufficient(
            "G-009_EVIDENCE_COMPLETE",
            "no evidence recorded for: " + ", ".join(missing),
        )
    if not quote.evidence_refs:
        return _insufficient("G-009_EVIDENCE_COMPLETE", "quote has no evidence references")
    empty = [ref for ref in quote.evidence_refs if not isinstance(ref, str) or not ref.strip()]
    if empty:
        return _insufficient("G-009_EVIDENCE_COMPLETE", "quote contains blank evidence references")
    return _ok("G-009_EVIDENCE_COMPLETE", f"{len(quote.evidence_refs)} evidence reference(s) present")


def rule_variant(quote: Quote, ctx: GuardContext) -> RuleResult:
    bad = [
        line.offer_id
        for line in quote.lines
        if line.product_id != line.offer_product_id or not line.product_id or not line.offer_product_id
    ]
    if bad:
        return _fail("G-010_VARIANT_MATCH", "line product identity does not match observed offer: " + ", ".join(bad))
    blank_variant = [line.offer_id for line in quote.lines if line.variant_id is not None and not line.variant_id.strip()]
    if blank_variant:
        return _fail("G-010_VARIANT_MATCH", "blank variant identifier: " + ", ".join(blank_variant))
    duplicated = {line.offer_id for line in quote.lines if [x.offer_id for x in quote.lines].count(line.offer_id) > 1}
    if duplicated:
        return _fail("G-010_VARIANT_MATCH", "duplicate offer lines: " + ", ".join(sorted(duplicated)))
    return _ok("G-010_VARIANT_MATCH", "product and variant identity consistent")


def rule_snapshot(quote: Quote, ctx: GuardContext) -> RuleResult:
    from app.domain.snapshot import compute_snapshot_hash

    recomputed = compute_snapshot_hash(quote)
    if quote.snapshot_hash != recomputed:
        return _fail(
            "G-011_SNAPSHOT_MATCH",
            f"stored snapshot hash does not match recomputed content ({quote.snapshot_hash[:19]}... != {recomputed[:19]}...)",
        )
    if ctx.approval is not None:
        if ctx.approval.snapshot_hash != quote.snapshot_hash:
            return _fail("G-011_SNAPSHOT_MATCH", "approval is bound to a different snapshot")
        if ctx.approval.quote_id != quote.quote_id:
            return _fail("G-011_SNAPSHOT_MATCH", "approval is bound to a different quote")
    if ctx.stage == "capture" and ctx.approval is None:
        return _insufficient("G-011_SNAPSHOT_MATCH", "capture requires an approval bound to this snapshot")
    return _ok("G-011_SNAPSHOT_MATCH", "snapshot hash verified")


def rule_duplicate(quote: Quote, ctx: GuardContext) -> RuleResult:
    if ctx.duplicate_capture_exists:
        return _fail("G-012_NO_DUPLICATE", "a capture already exists for this quote and snapshot")
    return _ok("G-012_NO_DUPLICATE", "no previous capture for this quote and snapshot")


def rule_amount_match(quote: Quote, ctx: GuardContext) -> RuleResult:
    if ctx.payment_amount is None:
        return _ok("G-013_AMOUNT_MATCH", "no payment amount supplied at this stage")
    if ctx.payment_currency is not None and ctx.payment_currency != quote.currency:
        return _fail(
            "G-013_AMOUNT_MATCH",
            f"payment currency {ctx.payment_currency} does not match quote currency {quote.currency}",
        )
    if ctx.payment_amount != quote.grand_total:
        return _fail(
            "G-013_AMOUNT_MATCH",
            f"payment amount {ctx.payment_amount} does not match quote total {quote.grand_total}",
        )
    return _ok("G-013_AMOUNT_MATCH", f"payment amount matches {quote.grand_total}")


def rule_environment(quote: Quote, ctx: GuardContext) -> RuleResult:
    if not ctx.order_environment or not ctx.expected_environment:
        return _ok("G-014_ENV_CONSISTENCY", "environment check not applicable")
    if ctx.order_environment != ctx.expected_environment:
        return _fail(
            "G-014_ENV_CONSISTENCY",
            f"order environment {ctx.order_environment} does not match expected {ctx.expected_environment}",
        )
    return _ok("G-014_ENV_CONSISTENCY", f"environment {ctx.order_environment} consistent")


def rule_policy_version(quote: Quote, ctx: GuardContext) -> RuleResult:
    if quote.policy_version != ctx.policy.version:
        return _fail(
            "G-015_POLICY_VERSION",
            f"quote was evaluated under policy {quote.policy_version}, active policy is {ctx.policy.version}",
        )
    return _ok("G-015_POLICY_VERSION", f"policy {ctx.policy.version} active")


def rule_approval(quote: Quote, ctx: GuardContext) -> RuleResult:
    if ctx.stage != "capture":
        return _ok("G-016_APPROVAL_VALID", "approval not required before approval")
    approval = ctx.approval
    if approval is None:
        return _insufficient("G-016_APPROVAL_VALID", "no approval for capture")
    if approval.user_id != quote.user_id:
        return _fail("G-016_APPROVAL_VALID", "approval belongs to a different user")
    if approval.status != "approved":
        return _fail("G-016_APPROVAL_VALID", f"approval status is {approval.status}, not approved")
    if approval.consumed_at is not None:
        return _fail("G-016_APPROVAL_VALID", "approval has already been consumed")
    if approval.expires_at <= ctx.now:
        return _fail("G-016_APPROVAL_VALID", f"approval expired at {approval.expires_at.isoformat()}")
    if approval.approved_at > ctx.now + ctx.policy.max_future_skew:
        return _fail("G-016_APPROVAL_VALID", "approval timestamp is in the future")
    return _ok("G-016_APPROVAL_VALID", f"approval valid until {approval.expires_at.isoformat()}")


def rule_arithmetic(quote: Quote, ctx: GuardContext) -> RuleResult:
    try:
        subtotal = sum((line.unit_price * line.quantity for line in quote.lines), Decimal("0.00"))
        shipping = sum((line.shipping_cost for line in quote.lines), Decimal("0.00"))
        expected_subtotal = money(subtotal, "subtotal")
        expected_shipping = money(shipping, "shipping")
        expected_total = money(expected_subtotal + expected_shipping, "total")
    except (TypeError, ValueError) as exc:
        return _fail("G-017_ARITHMETIC", f"basket arithmetic is invalid: {exc}")
    if (
        expected_subtotal != quote.subtotal
        or expected_shipping != quote.shipping_total
        or expected_total != quote.grand_total
    ):
        return _fail(
            "G-017_ARITHMETIC",
            "stored totals do not match line arithmetic "
            f"(lines={expected_total}, stored={quote.grand_total})",
        )
    return _ok("G-017_ARITHMETIC", "totals recomputed exactly")


RULES: tuple[Callable[[Quote, GuardContext], RuleResult], ...] = (
    rule_currency,
    rule_item_ceiling,
    rule_total_ceiling,
    rule_quantity,
    rule_availability,
    rule_freshness,
    rule_condition,
    rule_merchant,
    rule_evidence,
    rule_variant,
    rule_snapshot,
    rule_duplicate,
    rule_amount_match,
    rule_environment,
    rule_policy_version,
    rule_approval,
    rule_arithmetic,
)
