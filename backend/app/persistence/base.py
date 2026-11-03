"""SQLAlchemy base, engine setup, and append-only trigger installation."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Engine, DateTime, TypeDecorator, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.domain.models import as_utc


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator):
    """Single UTC datetime convention at the persistence boundary.

    All stored datetimes are timezone-aware UTC. SQLite (and other dialects)
    return plain values, so every read is normalised back to aware UTC, and
    naive write input is interpreted as UTC rather than crashing later
    comparisons. Never silently strips timezone information.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    @property
    def python_type(self) -> type[datetime]:  # noqa: D102
        return datetime

    def process_bind_param(self, value, dialect):  # type: ignore[no-untyped-def]
        if value is None:
            return None
        return as_utc(value)

    def process_result_value(self, value, dialect):  # type: ignore[no-untyped-def]
        if value is None:
            return None
        return as_utc(value)


def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def _sqlite_connect_for_savepoints(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
    # Documented pysqlite workaround (SQLAlchemy: "Serializable isolation /
    # Savepoints / Transactional DDL"). pysqlite swallows transactional
    # statements, which breaks SAVEPOINT semantics; disable its implicit
    # transaction handling so SQLAlchemy emits BEGIN explicitly.
    try:
        dbapi_connection.isolation_level = None
    except AttributeError:
        pass


def _sqlite_emit_begin(connection) -> None:  # type: ignore[no-untyped-def]
    # BEGIN IMMEDIATE, not deferred BEGIN: a request that will write must
    # acquire the WAL write lock at transaction start. With deferred BEGIN the
    # snapshot is taken at the first read, so a transaction opened while a
    # concurrent commit is still being fsynced to the WAL reads a stale
    # snapshot and its first write fails immediately with
    # "database is locked" (SQLITE_BUSY_SNAPSHOT bypasses busy_timeout,
    # because waiting would deadlock). With BEGIN IMMEDIATE the busy_timeout
    # applies and the snapshot is taken only after any in-flight commit has
    # landed. Savepoint/nested-transaction semantics are unaffected.
    connection.exec_driver_sql("BEGIN IMMEDIATE")


def create_engine_for(database_url: str) -> Engine:
    is_sqlite = database_url.startswith("sqlite")
    engine = create_engine(
        database_url,
        future=True,
        pool_pre_ping=True,
        connect_args={"check_same_thread": False} if is_sqlite else {},
    )
    if is_sqlite:
        event.listen(engine, "connect", _sqlite_pragmas)
        event.listen(engine, "connect", _sqlite_connect_for_savepoints)
        event.listen(engine, "begin", _sqlite_emit_begin)
    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


_SQLITE_TRIGGERS = {
    "journal_events": "journal_events is append-only",
    "evidence": "evidence is append-only",
    "transaction_events": "transaction_events is append-only",
    "captures": "captures is append-only",
}


def install_append_only_triggers(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as connection:
        for table, message in _SQLITE_TRIGGERS.items():
            connection.execute(
                text(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_no_update "
                    f"BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT, '{message}'); END"
                )
            )
            connection.execute(
                text(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_no_delete "
                    f"BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT, '{message}'); END"
                )
            )


def init_db(engine: Engine) -> None:
    from app.persistence import tables  # noqa: F401  (register mappers)

    Base.metadata.create_all(engine)
    install_append_only_triggers(engine)
