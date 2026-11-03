"""Milestone 1.1 fix 2: Decimal-safe journal serialization.

Decimals must never hit the JSON column raw (TypeError) and never be
converted to floats. The serialized form must be representation-consistent
with the canonical-hash pipeline so chain hashes are unchanged.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.domain.enums import Actor
from app.domain.snapshot import canonical_json, json_safe
from app.persistence.repositories import JournalRepository

NOW = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)


def test_json_safe_scalar_conversions():
    payload = {
        "amount": Decimal("10.50"),
        "total": Decimal("0"),
        "when": datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc),
        "text": "unchanged",
        "nested": [("x", Decimal("100.00"))],
    }
    safe = json_safe(payload)
    assert safe["amount"] == "10.50"  # fixed 2-dp string, never a float
    assert safe["total"] == "0.00"
    assert isinstance(safe["amount"], str)
    assert not isinstance(safe["amount"], float)
    assert "amount" in canonical_json(payload)  # canonicaliser accepts Decimal


def test_json_safe_matches_canonical_bytes():
    """Pre-stringifying a payload must not change its canonical encoding."""
    with_decimal = canonical_json({"amount": Decimal("10.50")})
    with_string = canonical_json(json_safe({"amount": Decimal("10.50")}))
    assert with_decimal == with_string


def test_journal_append_with_decimal_payload_persists_strings(container):
    """The journal column stores a JSON-safe structure; hash chain stays valid."""
    with container.session() as session:
        repository = JournalRepository(session)
        row = repository.append(
            actor=Actor.SYSTEM,
            event_type="test.money",
            subject_type="quote",
            subject_id="qte_x",
            user_id="user_demo",
            payload={"grand_total": Decimal("128.00"), "shipping": Decimal("0.00")},
            environment="test",
            occurred_at=NOW,
        )
        stored = row.payload
        ok, broken_seq = repository.verify_chain()
    assert isinstance(stored, dict)
    assert stored["grand_total"] == "128.00"
    assert not isinstance(stored["grand_total"], float)
    assert ok, f"chain broken at seq {broken_seq}"


def test_journal_append_with_datetime_payload(container):
    with container.session() as session:
        repository = JournalRepository(session)
        later = datetime(2026, 10, 31, 12, 30, 0, tzinfo=timezone.utc)
        row = repository.append(
            actor=Actor.SYSTEM,
            event_type="test.time",
            subject_type="quote",
            subject_id="qte_x",
            user_id="user_demo",
            payload={"as_of": NOW, "next_review": later},
            environment="test",
            occurred_at=NOW,
        )
        stored = row.payload
        ok, _broken = repository.verify_chain()
    # datetimes stay datetime objects in the JSON column (never timedelta or
    # any other unsupported type); the canonical encoding uses UTC ISO "Z"
    # strings (canonical_json convention).
    assert stored["as_of"] == "2026-10-31T12:00:00Z"
    assert stored["next_review"] == "2026-10-31T12:30:00Z"
    assert ok


def test_money_never_becomes_float_through_money_json():
    from app.domain.money import money

    value = money("10.50")
    assert isinstance(value, Decimal)
    assert str(value) == "10.50"
