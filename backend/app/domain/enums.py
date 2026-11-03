"""Domain enumerations. Pure data: no I/O, no third-party imports."""

from enum import Enum


class Condition(str, Enum):
    NEW = "new"
    USED = "used"
    REFURBISHED = "refurbished"
    OPEN_BOX = "open_box"
    UNKNOWN = "unknown"


class Availability(str, Enum):
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    PREORDER = "preorder"
    BACKORDER = "backorder"
    UNKNOWN = "unknown"


class EvidenceKind(str, Enum):
    """What kind of claim a row of evidence supports."""

    SOURCE_OBSERVATION = "source_observation"
    DERIVED = "derived"
    USER_INPUT = "user_input"
    AI_STATEMENT = "ai_statement"
    EXTERNAL_CONFIRMATION = "external_confirmation"


class TrustLevel(str, Enum):
    """How much authority a piece of evidence carries.

    SOURCE_OBSERVED means "the provider told us this at this time" - it is a
    provenance claim, not a guarantee that the provider is correct. AI_UNVERIFIED
    is never usable as a fact, by construction (see ``is_fact``).
    """

    SOURCE_OBSERVED = "source_observed"
    USER_DECLARED = "user_declared"
    EXTERNAL_CONFIRMED = "external_confirmed"
    AI_UNVERIFIED = "ai_unverified"

    @property
    def is_fact(self) -> bool:
        return self is not TrustLevel.AI_UNVERIFIED


class Actor(str, Enum):
    USER = "user"
    SYSTEM = "system"
    AI = "ai"
    EXTERNAL = "external"


class GuardOutcome(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    NEEDS_INFO = "NEEDS_INFO"


class RuleStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT = "INSUFFICIENT"


class ApprovalStatus(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    STALE = "stale"
    EXPIRED = "expired"
    CONSUMED = "consumed"


class IntentStatus(str, Enum):
    PREPARED = "prepared"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    PAYMENT_PENDING = "payment_pending"
    PAID = "paid"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class PaymentStatus(str, Enum):
    CREATED = "created"
    PAYER_ACTION_REQUIRED = "payer_action_required"
    APPROVED = "approved"
    CAPTURED = "captured"
    VOIDED = "voided"
    FAILED = "failed"


class RetailerPurchaseState(str, Enum):
    """Deliberately separate from payment state.

    A captured PayPal payment proves money moved to the PayPal payee. It does
    NOT prove a retailer shipped anything. Only retailer evidence can promote
    this to EVIDENCE_CONFIRMED.
    """

    UNKNOWN = "unknown"
    USER_REPORTED = "user_reported"
    EVIDENCE_CONFIRMED = "evidence_confirmed"


class DataMode(str, Enum):
    LIVE = "LIVE"
    MOCK = "MOCK"
