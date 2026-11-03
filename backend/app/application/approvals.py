"""Explicit approval and revalidation use cases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.application.common import limits_from_policy, quote_as_of, rebuild_quote
from app.application.errors import (
    ApprovalNotValid,
    ConsentRequired,
    GuardDenied,
    NotFound,
    QuoteChanged,
    QuoteNotApprovable,
    SnapshotMismatch,
)
from app.application.quotes import current_quote
from app.domain.enums import Actor, ApprovalStatus, GuardOutcome
from app.domain.guard import GuardContext, GuardDecision, GuardPolicy, evaluate
from app.domain.models import ApprovalFacts, MaterialChange, Quote
from app.domain.snapshot import compute_snapshot_hash, diff_material
from app.journal import JournalEvents, JournalWriter
from app.persistence.repositories import (
    ApprovalRepository,
    IntentRepository,
    PaymentRepository,
    ProductRepository,
    QuoteRepository,
)


@dataclass
class ApprovalOutcome:
    approval: object
    quote: Quote
    decision: GuardDecision


@dataclass
class RevalidationOutcome:
    approval: object
    quote: Quote
    outcome: GuardOutcome
    decision: GuardDecision | None
    changes: tuple[MaterialChange, ...]
    reasons: tuple[str, ...]

    @property
    def allowed(self) -> bool:
        return self.outcome is GuardOutcome.ALLOW


def _load_quote(session: Session, user_id: str, quote_id: str) -> tuple[object, Quote]:
    repository = QuoteRepository(session)
    row = repository.get(user_id, quote_id)
    if row is None:
        raise NotFound(f"quote {quote_id} not found")
    return row, repository.to_domain(row)


# Quote states that permanently end the approval lifecycle for that basket.
TERMINAL_QUOTE_STATUSES = ("rejected", "superseded")


def _raise_if_quote_terminal(journal: JournalWriter, user_id: str, quote_id: str, row) -> None:
    """Backed-in lifecycle rule: a terminal quote can never be approved.

    Enforced in the use case, not only at the HTTP layer.
    """

    if row.status in TERMINAL_QUOTE_STATUSES:
        journal.failure(
            error_code="QUOTE_NOT_APPROVABLE",
            message="approval attempted on a terminal quote",
            subject_type="quote",
            subject_id=quote_id,
            user_id=user_id,
        )
        raise QuoteNotApprovable(
            f"quote is {row.status} and can no longer be approved; request a fresh quote",
            details={"status": row.status},
        )


def _change_payload(changes: list[MaterialChange]) -> list[dict]:
    return [
        {"path": change.path, "before": change.before, "after": change.after}
        for change in changes[:12]
    ]


def approve_quote(
    session: Session,
    *,
    user_id: str,
    quote_id: str,
    snapshot_hash: str,
    explicit_consent: bool,
    acknowledged_risks: bool,
    policy: GuardPolicy,
    now: datetime,
    environment: str,
    correlation_id: str | None = None,
) -> ApprovalOutcome:
    if explicit_consent is not True:
        raise ConsentRequired("approval requires explicit_consent=true from the user")

    journal = JournalWriter(session, environment=environment)
    row, quote = _load_quote(session, user_id, quote_id)
    _raise_if_quote_terminal(journal, user_id, quote_id, row)

    if not quote.snapshot_hash or not snapshot_hash or row.snapshot_hash != quote.snapshot_hash:
        raise SnapshotMismatch("quote snapshot is inconsistent with the stored record")
    if snapshot_hash != quote.snapshot_hash:
        journal.failure(
            error_code="SNAPSHOT_MISMATCH",
            message="user approved a hash that does not match the quote",
            subject_type="quote",
            subject_id=quote_id,
            user_id=user_id,
            correlation_id=correlation_id,
        )
        raise SnapshotMismatch("approved snapshot hash does not match the quote")
    if compute_snapshot_hash(quote) != quote.snapshot_hash:
        journal.failure(
            error_code="SNAPSHOT_MISMATCH",
            message="quote content no longer matches its snapshot hash",
            subject_type="quote",
            subject_id=quote_id,
            user_id=user_id,
            correlation_id=correlation_id,
        )
        raise SnapshotMismatch("quote content has changed since it was prepared")

    fresh = current_quote(session, user_id=user_id, quote=quote)
    if fresh is None:
        row.status = "superseded"
        journal.record(
            JournalEvents.APPROVAL_INVALIDATED,
            actor=Actor.SYSTEM,
            subject_type="quote",
            subject_id=quote_id,
            user_id=user_id,
            correlation_id=correlation_id,
            payload={"reasons": ["offer_missing"]},
        )
        raise QuoteChanged("one or more offers no longer exist; request a fresh quote")

    changes = diff_material(quote, fresh, ignore_fields=("evidence", "as_of"))
    if changes:
        row.status = "superseded"
        journal.record(
            JournalEvents.APPROVAL_INVALIDATED,
            actor=Actor.SYSTEM,
            subject_type="quote",
            subject_id=quote_id,
            user_id=user_id,
            correlation_id=correlation_id,
            payload={"reasons": ["offer_changed"], "changes": _change_payload(changes)},
        )
        raise QuoteChanged(
            "offer data changed after the quote was prepared; request a fresh quote",
            details={"changes": _change_payload(changes)},
        )

    limits = limits_from_policy(policy)
    decision = evaluate(
        quote,
        GuardContext(
            now=now,
            policy=policy,
            limits=limits,
            requirements=quote.requirements,
            stage="pre_approval",
        ),
    )
    journal.record(
        JournalEvents.GUARD_EVALUATED,
        actor=Actor.SYSTEM,
        subject_type="quote",
        subject_id=quote_id,
        user_id=user_id,
        correlation_id=correlation_id,
        payload={
            "stage": "approval",
            "outcome": decision.outcome.value,
            "decision_id": decision.decision_id,
            "blocking_codes": list(decision.blocking_codes),
        },
    )
    if decision.outcome is not GuardOutcome.ALLOW:
        journal.record(
            JournalEvents.APPROVAL_REJECTED,
            actor=Actor.SYSTEM,
            subject_type="quote",
            subject_id=quote_id,
            user_id=user_id,
            correlation_id=correlation_id,
            payload={"reason": "guard_blocked", "outcome": decision.outcome.value},
        )
        raise GuardDenied("the deterministic Purchase Guard did not allow this basket", decision=decision)

    approvals = ApprovalRepository(session)
    superseded = approvals.supersede_active(user_id, quote_id, "replaced by a newer approval")
    if superseded:
        journal.record(
            JournalEvents.APPROVAL_SUPERSEDED,
            actor=Actor.USER,
            subject_type="quote",
            subject_id=quote_id,
            user_id=user_id,
            correlation_id=correlation_id,
            payload={"superseded_count": superseded},
        )

    expires_at = min(now + policy.approval_ttl, quote.as_of + policy.max_evidence_age)
    if expires_at <= now:
        journal.record(
            JournalEvents.APPROVAL_INVALIDATED,
            actor=Actor.SYSTEM,
            subject_type="quote",
            subject_id=quote_id,
            user_id=user_id,
            correlation_id=correlation_id,
            payload={"reasons": ["price_evidence_expired"]},
        )
        raise ApprovalNotValid("price evidence has expired; request a fresh quote")

    intent = IntentRepository(session).for_quote(user_id, quote_id)
    approval = approvals.add(
        user_id=user_id,
        quote_id=quote_id,
        intent_id=intent.id if intent else None,
        snapshot_hash=quote.snapshot_hash,
        policy_version=policy.version,
        decision=decision,
        approved_at=now,
        expires_at=expires_at,
    )
    if intent is not None:
        intent.status = "approved"
        session.flush()
        journal.record(
            JournalEvents.INTENT_STATUS_CHANGED,
            actor=Actor.USER,
            subject_type="intent",
            subject_id=intent.id,
            user_id=user_id,
            correlation_id=correlation_id,
            payload={"status": "approved", "quote_id": quote_id},
        )

    journal.record(
        JournalEvents.APPROVAL_GRANTED,
        actor=Actor.USER,
        subject_type="approval",
        subject_id=approval.id,
        user_id=user_id,
        correlation_id=correlation_id,
        payload={
            "quote_id": quote_id,
            "snapshot_hash": quote.snapshot_hash,
            "policy_version": policy.version,
            "decision_id": decision.decision_id,
            "expires_at": expires_at.isoformat(),
            "explicit_consent": True,
            "acknowledged_risks": bool(acknowledged_risks),
        },
    )
    return ApprovalOutcome(approval=approval, quote=quote, decision=decision)


def reject_quote(
    session: Session,
    *,
    user_id: str,
    quote_id: str,
    reason: str,
    now: datetime,
    environment: str,
    correlation_id: str | None = None,
) -> int:
    journal = JournalWriter(session, environment=environment)
    row, _quote = _load_quote(session, user_id, quote_id)
    approvals = ApprovalRepository(session)
    active = approvals.active_for_quote(user_id, quote_id)
    for approval in active:
        approval.status = ApprovalStatus.REJECTED.value
        approval.rejected_at = now
        approval.reason = reason
    row.status = "rejected"
    IntentRepository(session).set_status(user_id, quote_id, "rejected")
    session.flush()
    journal.record(
        JournalEvents.QUOTE_REJECTED,
        actor=Actor.USER,
        subject_type="quote",
        subject_id=quote_id,
        user_id=user_id,
        correlation_id=correlation_id,
        payload={"reason": reason, "rejected_approvals": len(active)},
    )
    journal.record(
        JournalEvents.APPROVAL_REJECTED,
        actor=Actor.USER,
        subject_type="quote",
        subject_id=quote_id,
        user_id=user_id,
        correlation_id=correlation_id,
        payload={"reason": reason, "count": len(active)},
    )
    return len(active)


def revalidate_approval(
    session: Session,
    *,
    user_id: str,
    approval_id: str,
    policy: GuardPolicy,
    now: datetime,
    environment: str,
    correlation_id: str | None = None,
) -> RevalidationOutcome:
    journal = JournalWriter(session, environment=environment)
    approvals = ApprovalRepository(session)
    approval = approvals.get(user_id, approval_id)
    if approval is None:
        raise NotFound(f"approval {approval_id} not found")
    _row, quote = _load_quote(session, user_id, approval.quote_id)

    def finish(outcome: GuardOutcome, reasons: tuple[str, ...], decision=None, changes=()) -> RevalidationOutcome:
        return RevalidationOutcome(
            approval=approval,
            quote=quote,
            outcome=outcome,
            decision=decision,
            changes=tuple(changes),
            reasons=reasons,
        )

    if approval.status in (
        ApprovalStatus.CONSUMED.value,
        ApprovalStatus.STALE.value,
        ApprovalStatus.EXPIRED.value,
        ApprovalStatus.SUPERSEDED.value,
        ApprovalStatus.REJECTED.value,
    ):
        return finish(GuardOutcome.DENY, (f"approval_{approval.status}",))

    if approval.consumed_at is not None:
        return finish(GuardOutcome.DENY, ("approval_consumed",))

    if now >= approval.expires_at:
        approvals.set_status(approval, ApprovalStatus.EXPIRED, "approval expired before use")
        journal.record(
            JournalEvents.APPROVAL_EXPIRED,
            actor=Actor.SYSTEM,
            subject_type="approval",
            subject_id=approval.id,
            user_id=user_id,
            correlation_id=correlation_id,
            payload={"quote_id": quote.quote_id, "expires_at": approval.expires_at.isoformat()},
        )
        return finish(GuardOutcome.DENY, ("approval_expired",))

    if compute_snapshot_hash(quote) != quote.snapshot_hash or quote.snapshot_hash != approval.snapshot_hash:
        approvals.set_status(approval, ApprovalStatus.STALE, "snapshot hash mismatch")
        journal.record(
            JournalEvents.APPROVAL_INVALIDATED,
            actor=Actor.SYSTEM,
            subject_type="approval",
            subject_id=approval.id,
            user_id=user_id,
            correlation_id=correlation_id,
            payload={"reasons": ["snapshot_mismatch"]},
        )
        return finish(GuardOutcome.DENY, ("snapshot_mismatch",))

    fresh = current_quote(session, user_id=user_id, quote=quote)
    if fresh is None:
        approvals.set_status(approval, ApprovalStatus.STALE, "offer no longer exists")
        journal.record(
            JournalEvents.APPROVAL_INVALIDATED,
            actor=Actor.SYSTEM,
            subject_type="approval",
            subject_id=approval.id,
            user_id=user_id,
            correlation_id=correlation_id,
            payload={"reasons": ["offer_missing"]},
        )
        return finish(GuardOutcome.DENY, ("offer_missing",))

    changes = diff_material(quote, fresh, ignore_fields=("evidence", "as_of"))
    if changes:
        approvals.set_status(approval, ApprovalStatus.STALE, "offer changed after approval")
        journal.record(
            JournalEvents.APPROVAL_INVALIDATED,
            actor=Actor.SYSTEM,
            subject_type="approval",
            subject_id=approval.id,
            user_id=user_id,
            correlation_id=correlation_id,
            payload={"reasons": ["offer_changed"], "changes": _change_payload(changes)},
        )
        return finish(GuardOutcome.DENY, ("offer_changed",), changes=changes)

    duplicate = PaymentRepository(session).capture_exists(quote.quote_id, approval.snapshot_hash)
    decision = evaluate(
        quote,
        GuardContext(
            now=now,
            policy=policy,
            limits=limits_from_policy(policy),
            requirements=quote.requirements,
            stage="capture",
            approval=approvals.facts(approval),
            duplicate_capture_exists=duplicate,
            expected_environment=None,
        ),
    )
    journal.record(
        JournalEvents.GUARD_EVALUATED,
        actor=Actor.SYSTEM,
        subject_type="approval",
        subject_id=approval.id,
        user_id=user_id,
        correlation_id=correlation_id,
        payload={
            "stage": "capture",
            "outcome": decision.outcome.value,
            "decision_id": decision.decision_id,
            "blocking_codes": list(decision.blocking_codes),
        },
    )
    if decision.outcome is not GuardOutcome.ALLOW:
        approvals.set_status(approval, ApprovalStatus.STALE, "failed revalidation")
        journal.record(
            JournalEvents.APPROVAL_INVALIDATED,
            actor=Actor.SYSTEM,
            subject_type="approval",
            subject_id=approval.id,
            user_id=user_id,
            correlation_id=correlation_id,
            payload={
                "reasons": list(decision.blocking_codes),
                "outcome": decision.outcome.value,
            },
        )
        return finish(decision.outcome, tuple(decision.blocking_codes), decision=decision)

    journal.record(
        JournalEvents.REVALIDATION_PASSED,
        actor=Actor.SYSTEM,
        subject_type="approval",
        subject_id=approval.id,
        user_id=user_id,
        correlation_id=correlation_id,
        payload={
            "quote_id": quote.quote_id,
            "snapshot_hash": quote.snapshot_hash,
            "expires_at": approval.expires_at.isoformat(),
            "decision_id": decision.decision_id,
        },
    )
    return finish(GuardOutcome.ALLOW, (), decision=decision)
