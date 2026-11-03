"""Regression: SQLite write transactions must begin with BEGIN IMMEDIATE.

Root cause fixed by this test (Milestone 1.1 live verification):
with deferred BEGIN, a session whose first read happens while a concurrent
commit is still being written to the WAL takes a stale snapshot, and its
first write fails immediately with
``sqlite3.OperationalError: database is locked`` (SQLITE_BUSY_SNAPSHOT does
not honor busy_timeout). Under uvicorn this hit every POST /api/v1/quotes
that followed a POST /api/v1/search, because the search session commits in
dependency teardown after its response was already sent.

The engine factory therefore begins every transaction with BEGIN IMMEDIATE,
which serialises transaction starts on the WAL write lock and lets
busy_timeout apply.
"""
from __future__ import annotations

import os

from sqlalchemy import text

from app.persistence.base import Base, create_engine_for, make_session_factory
from app.persistence import tables


def _driver_connection(connection):
    fairy = connection.connection
    for attr in ("driver_connection", "dbapi_connection", "connection"):
        value = getattr(fairy, attr, None)
        if value is not None:
            return value
    raise AssertionError("cannot reach raw sqlite3 connection")


def test_sqlite_transactions_begin_immediate(tmp_path) -> None:
    """The begin listener emits BEGIN IMMEDIATE for every transaction."""

    engine = create_engine_for(f"sqlite+pysqlite:///{tmp_path / 'reg.db'}")
    try:
        connection = engine.connect()
        # Attach a raw statement trace to the underlying sqlite3 connection
        # before any transaction starts.
        dbapi = _driver_connection(connection)
        statements: list[str] = []
        dbapi.set_trace_callback(statements.append)

        connection.execute(text("SELECT 1"))
        connection.rollback()
        connection.execute(text("SELECT 1"))

        begins = [s.strip().upper() for s in statements if s.strip().upper().startswith("BEGIN")]
        assert begins, f"no BEGIN captured: {statements!r}"
        for begin in begins:
            assert begin == "BEGIN IMMEDIATE", f"expected BEGIN IMMEDIATE, got {begin!r}"
        connection.close()
    finally:
        engine.dispose()


def test_orm_round_trip_still_works(tmp_path) -> None:
    """Normal ORM write/read round-trip still works with BEGIN IMMEDIATE."""

    engine = create_engine_for(f"sqlite+pysqlite:///{tmp_path / 'rw.db'}")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)

    with session_factory() as session:
        session.add(tables.User(id="reg_user", display_name="reg_user"))
        session.commit()

    with session_factory() as session:
        row = session.get(tables.User, "reg_user")
        assert row is not None
        assert row.display_name == "reg_user"

    assert os.path.getsize(tmp_path / "rw.db") > 0
    engine.dispose()
