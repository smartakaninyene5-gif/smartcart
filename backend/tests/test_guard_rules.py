"""Purchase Guard matrix: every rule must deny at the correct moment.

G-001 currency cohesion … G-017 tamper evidence, plus the security invariant
that the evaluate() surface has no override latch.
"""
from __future__ import annotations

import inspect
from datetime import timedelta
from decimal import Decimal

from app.domain.enums import Availability, Condition, GuardOutcome
from app.domain.guard import GuardPolicy, evaluate

from tests.guard_helpers import blocking, ctx, facts, limits, make_offer, make_quote


def test_baseline_pre_approval_allow():
    quote = make_quote()
    outcome, codes, _ = blocking(quote, ctx(quote))
    assert outcome is GuardOutcome.ALLOW
    assert not codes


def test_baseline_capture_with_approval_allow():
    quote = make_quote()
    outcome, codes, _ = blocking(quote, ctx(quote, stage="capture", approval=facts(quote)))
    assert outcome is GuardOutcome.ALLOW
    assert not codes


def test_g001_mixed_currency():
    quote = make_quote()
    mixed = quote.model_copy(update={"lines": (quote.lines[0].model_copy(update={"currency": "EUR"}),)})
    outcome, codes, _ = blocking(mixed, ctx(mixed))
    assert "G-001_CURRENCY" in codes and outcome is GuardOutcome.DENY


def test_g002_user_item_ceiling():
    quote = make_quote()
    outcome, codes, _ = blocking(quote, ctx(quote, user_limits=limits(max_item="90.00")))
    assert "G-002_ITEM_CEILING" in codes and outcome is GuardOutcome.DENY


def test_g003_user_total_ceiling():
    quote = make_quote()
    outcome, codes, _ = blocking(quote, ctx(quote, user_limits=limits(max_total="50.00")))
    assert outcome is GuardOutcome.DENY and "G-003_TOTAL_CEILING" in codes


def test_g004_quantity_ceiling():
    quote = make_quote(quantity=6)
    outcome, codes, _ = blocking(quote, ctx(quote))
    assert "G-004_QUANTITY" in codes and outcome is GuardOutcome.DENY


def test_g005_out_of_stock_denied():
    quote = make_quote(offers=make_offer(availability=Availability.OUT_OF_STOCK))
    outcome, codes, _ = blocking(quote, ctx(quote))
    assert "G-005_AVAILABILITY" in codes and outcome is GuardOutcome.DENY


def test_g005_unknown_stock_needs_info():
    quote = make_quote(offers=make_offer(availability=Availability.UNKNOWN))
    outcome, codes, _ = blocking(quote, ctx(quote))
    assert outcome is GuardOutcome.NEEDS_INFO and "G-005_AVAILABILITY" in codes


def test_g006_stale_evidence_denied():
    from datetime import UTC, datetime
    stale = make_quote(as_of=datetime(2026, 10, 31, 11, 40, 0, tzinfo=UTC))  # 20 min old
    outcome, codes, _ = blocking(stale, ctx(stale))
    assert "G-006_FRESHNESS" in codes and outcome is GuardOutcome.DENY


def test_g006_future_timestamp_denied():
    from datetime import UTC, datetime
    future = make_quote(as_of=datetime(2026, 10, 31, 12, 10, 0, tzinfo=UTC))  # 10 min ahead
    outcome, codes, _ = blocking(future, ctx(future))
    assert "G-006_FRESHNESS" in codes and outcome is GuardOutcome.DENY


def test_g007_used_condition_denied():
    quote = make_quote(offers=make_offer(condition=Condition.USED))
    outcome, codes, _ = blocking(quote, ctx(quote))
    assert "G-007_CONDITION" in codes and outcome is GuardOutcome.DENY


def test_g007_unknown_condition_needs_info():
    quote = make_quote(offers=make_offer(condition=Condition.UNKNOWN))
    outcome, codes, _ = blocking(quote, ctx(quote))
    assert outcome is GuardOutcome.NEEDS_INFO and "G-007_CONDITION" in codes


def test_g008_merchant_denylist():
    quote = make_quote()
    policy = GuardPolicy(merchant_denylist=("bestbuy.com",))
    outcome, codes, _ = blocking(quote, ctx(quote, policy=policy))
    assert "G-008_MERCHANT_POLICY" in codes and outcome is GuardOutcome.DENY


def test_g008_merchant_allowlist_excludes():
    quote = make_quote()
    policy = GuardPolicy(merchant_allowlist=("amazon.com",))
    outcome, codes, _ = blocking(quote, ctx(quote, policy=policy))
    assert "G-008_MERCHANT_POLICY" in codes and outcome is GuardOutcome.DENY


def test_g009_missing_evidence_needs_info():
    quote = make_quote(evidence=False)
    outcome, codes, _ = blocking(quote, ctx(quote))
    assert outcome is GuardOutcome.NEEDS_INFO and "G-009_EVIDENCE_COMPLETE" in codes


def test_g010_variant_identity_mismatch():
    quote = make_quote()
    mismatched = quote.model_copy(update={
        "lines": (quote.lines[0].model_copy(update={"offer_product_id": "mock:other"}),)
    })
    outcome, codes, _ = blocking(mismatched, ctx(mismatched))
    assert "G-010_VARIANT_MATCH" in codes and outcome is GuardOutcome.DENY


def test_g010_duplicate_lines_denied():
    line = make_quote().lines[0]
    duplicate = make_quote().model_copy(update={"lines": (line, line)})
    outcome, codes, _ = blocking(duplicate, ctx(duplicate))
    assert "G-010_VARIANT_MATCH" in codes and outcome is GuardOutcome.DENY


def test_g011_snapshot_tampering_denied():
    quote = make_quote()
    tampered = quote.model_copy(update={"snapshot_hash": "sha256:" + "0" * 64})
    outcome, codes, _ = blocking(tampered, ctx(tampered))
    assert "G-011_SNAPSHOT_MATCH" in codes and outcome is GuardOutcome.DENY


def test_g011_approval_bound_to_other_quote_denied():
    quote = make_quote()
    wrong = facts(quote, snapshot_hash="sha256:" + "1" * 64)
    outcome, codes, _ = blocking(quote, ctx(quote, stage="capture", approval=wrong))
    assert "G-011_SNAPSHOT_MATCH" in codes and outcome is GuardOutcome.DENY


def test_g012_duplicate_capture_denied():
    quote = make_quote()
    outcome, codes, _ = blocking(
        quote,
        ctx(quote, stage="capture", approval=facts(quote), duplicate_capture_exists=True),
    )
    assert "G-012_NO_DUPLICATE" in codes and outcome is GuardOutcome.DENY


def test_g013_payment_amount_mismatch_denied():
    quote = make_quote()
    outcome, codes, _ = blocking(
        quote,
        ctx(
            quote,
            stage="capture",
            approval=facts(quote),
            payment_amount=Decimal("1.00"),
            payment_currency="USD",
        ),
    )
    assert "G-013_AMOUNT_MATCH" in codes and outcome is GuardOutcome.DENY


def test_g014_environment_mismatch_denied():
    quote = make_quote()
    outcome, codes, _ = blocking(
        quote,
        ctx(
            quote,
            stage="capture",
            approval=facts(quote),
            order_environment="live",
            expected_environment="sandbox",
        ),
    )
    assert "G-014_ENV_CONSISTENCY" in codes and outcome is GuardOutcome.DENY


def test_g015_policy_version_mismatch_denied():
    quote = make_quote(policy_version="0.9.0")
    outcome, codes, _ = blocking(quote, ctx(quote))
    assert "G-015_POLICY_VERSION" in codes and outcome is GuardOutcome.DENY


def test_g016_capture_without_approval_needs_info():
    quote = make_quote()
    outcome, codes, _ = blocking(quote, ctx(quote, stage="capture", approval=None))
    assert "G-016_APPROVAL_VALID" in codes and outcome is GuardOutcome.NEEDS_INFO


def test_g016_consumed_approval_denied():
    from datetime import UTC, datetime
    quote = make_quote()
    consumed = facts(quote, status="consumed", consumed_at=datetime(2026, 10, 31, 12, 0, tzinfo=UTC))
    outcome, codes, _ = blocking(quote, ctx(quote, stage="capture", approval=consumed))
    assert "G-016_APPROVAL_VALID" in codes and outcome is GuardOutcome.DENY


def test_g016_expired_approval_denied():
    from datetime import UTC, datetime
    quote = make_quote()
    expired = facts(quote, expires_at=datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC))
    outcome, codes, _ = blocking(quote, ctx(quote, stage="capture", approval=expired))
    assert "G-016_APPROVAL_VALID" in codes and outcome is GuardOutcome.DENY


def test_g016_other_users_approval_denied():
    quote = make_quote()
    foreign = facts(quote, user_id="someone_else")
    outcome, codes, _ = blocking(quote, ctx(quote, stage="capture", approval=foreign))
    assert "G-016_APPROVAL_VALID" in codes and outcome is GuardOutcome.DENY


def test_g017_arithmetic_tamper_denied():
    quote = make_quote()
    tampered_total = quote.model_copy(update={"grand_total": Decimal("1.00")})
    outcome, codes, _ = blocking(tampered_total, ctx(tampered_total))
    assert "G-017_ARITHMETIC" in codes and outcome is GuardOutcome.DENY


def test_guard_evaluation_surface_has_no_override_latch():
    """Security invariant: no parameter anywhere in the evaluate() signature
    can force an outcome."""
    params = list(inspect.signature(evaluate).parameters)
    assert params == ["quote", "context"]
    forbidden = {"outcome", "decision", "allow", "override", "force", "verbose"}
    assert forbidden.isdisjoint(params)


def test_user_limits_can_only_tighten():
    """API-level invariant: user-supplied limits only lower ceilings."""
    from decimal import Decimal

    from app.application.common import limits_from_policy, tighten_limits

    base = limits_from_policy(GuardPolicy())
    tightened = tighten_limits(base, max_total=Decimal("50.00"), max_quantity=2)
    assert tightened.max_total == Decimal("50.00")
    assert tightened.max_quantity_per_line == 2
    # a wider request cannot loosen the policy ceiling
    loosened = tighten_limits(base, max_total=Decimal("999999.00"))
    assert loosened.max_total == base.max_total
