"""Milestone 1.1 fix 1: UTC datetime persistence and revalidation safety.

Regression tests for the naive/aware crash that used to occur in
``revalidate_approval`` when SQLite returned naive datetimes.
"""
from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import text

from app.adapters.clock import FixedClock
from app.config import Settings
from app.container import create_app
from app.persistence.base import Base, create_engine_for
from app.persistence import tables as T

NOW = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)


def _is_utc(value: datetime) -> bool:
    return (
        value.tzinfo is not None
        and value.tzinfo is not None
        and value.utcoffset() == timedelta(0)
        and value.astimezone(UTC) == value
    )


def test_column_compiles_with_utc_type(tmp_path):
    """Every datetime column is the UTCDateTime TypeDecorator."""
    from app.persistence.base import UTCDateTime

    expected = {
        T.User.created_at,
        T.OfferRecord.observed_at,
        T.OfferRecord.as_of,
        T.Evidence.recorded_at if hasattr(T.Evidence, "recorded_at") else None,
        T.QuoteRecord.as_of,
        T.Approval.approved_at,
        T.Approval.expires_at,
        T.PaymentOrder.created_at,
        T.Capture.captured_at,
        T.JournalEvent.occurred_at,
    }
    for column in expected:
        if column is None:
            continue
        assert isinstance(column.type, UTCDateTime), f"{column} is not UTCDateTime"


def test_aware_datetimes_round_trip_unchanged(tmp_path):
    """Aware UTC values survive a bind/flush/commit/retrieve cycle unchanged."""
    engine = create_engine_for(f"sqlite+pysqlite:///{tmp_path / 'rt.db'}")
    Base.metadata.create_all(engine)
    from sqlalchemy.orm import Session

    stamp = datetime(2026, 6, 1, 9, 30, 0, 123456, tzinfo=UTC)
    with Session(engine) as session:
        session.add(T.User(id="u_rt", created_at=stamp))
        session.commit()

    with Session(engine) as session:
        user = session.get(T.User, "u_rt")
        assert user.created_at == stamp
        assert _is_utc(user.created_at)


def test_non_utc_aware_input_normalised_to_same_instant(tmp_path):
    """A non-UTC aware value is byte-equivalent to its UTC instant on read."""
    engine = create_engine_for(f"sqlite+pysqlite:///{tmp_path / 'tz.db'}")
    Base.metadata.create_all(engine)
    from sqlalchemy.orm import Session

    berlin = datetime(2026, 6, 1, 11, 30, 0, tzinfo=timezone(timedelta(hours=2)))  # == 09:30Z
    with Session(engine) as session:
        session.add(T.User(id="u_tz", created_at=berlin))
        session.commit()

    with Session(engine) as session:
        user = session.get(T.User, "u_tz")
        assert user.created_at == datetime(2026, 6, 1, 9, 30, 0, tzinfo=UTC)


def test_legacy_naive_database_row_reads_as_utc(tmp_path):
    """Rows written by pre-fix code (naive timestamps) are read as UTC, safely."""
    engine = create_engine_for(f"sqlite+pysqlite:///{tmp_path / 'legacy.db'}")
    Base.metadata.create_all(engine)
    from sqlalchemy.orm import Session

    # Deliberately bypass the ORM type to simulate a legacy raw row.
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO users (id, created_at) VALUES ('u_legacy', '2026-05-01 08:00:00.000000')"),
        )
    with Session(engine) as session:
        user = session.get(T.User, "u_legacy")
        assert _is_utc(user.created_at)
        assert user.created_at == datetime(2026, 5, 1, 8, 0, 0, tzinfo=UTC)


def test_approval_datetimes_aware_after_api_flow(tmp_path):
    """Full API flow: approval facts survive persistence as aware UTC, and the
    expiry comparison (the original crash site) works without errors."""
    settings = Settings(
        _env_file=None,
        env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'api.db'}",
        product_source="mock",
        ai_provider="none",
        paypal_client_id=None,
        paypal_client_secret=None,
    )
    app = create_app(settings, clock=FixedClock(NOW))
    from fastapi.testclient import TestClient

    client = TestClient(app)
    search = client.post("/api/v1/search", json={"query": "headphones"}).json()
    for product in search["products"]:
        for offer in product["offers"]:
            if offer["merchant_domain"] == "bestbuy.com":
                sony = offer
    quote = client.post("/api/v1/quotes", json={
        "selections": [{"offer_id": sony["offer_id"], "quantity": 1}],
        "requirements": {"currency": "USD", "budget_total": "200", "allowed_conditions": ["new"]},
    }).json()
    response = client.post(f"/api/v1/quotes/{quote['quote_id']}/approve", json={
        "snapshot_hash": quote["snapshot_hash"],
        "explicit_consent": True,
    })
    assert response.status_code == 200, response.text
    approval = response.json()["approval"]

    from app.persistence.repositories import ApprovalRepository

    with app.state.container.session() as session:
        row = ApprovalRepository(session).get("user_demo", approval["approval_id"])
        assert row is not None
        assert _is_utc(row.approved_at)
        assert _is_utc(row.expires_at)
        assert _is_utc(row.created_at)

    # Revalidation at the same clock instant exercises the comparison path.
    reval = client.post(f"/api/v1/approvals/{approval['approval_id']}/revalidate").json()
    assert reval.get("outcome") == "ALLOW", reval
