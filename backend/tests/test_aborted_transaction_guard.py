"""A deadlock victim's request must fail, never answer 200 for a write Postgres rolled back.

The defect: ``AuditService.log`` took the global audit-chain advisory lock inside an ``except
Exception`` that swallowed everything. When Postgres picked that wait as a deadlock victim
(40P01), the transaction was left aborted, SQLAlchemy still called the session ACTIVE, and
``commit()`` sent a COMMIT that Postgres turned into a ROLLBACK without psycopg2 raising. The
route answered 200; nothing it wrote was saved, and it has no audit row.

Two fixes, both covered here:

* ``AuditService.log`` re-raises a database error no savepoint contained (``log_required``
  reports it as ``AuditWriteError``); ``app/main.py`` answers a lost lock race with the app-wide
  409 (``STALE_WRITE_CONFLICT_DETAIL``).
* The COMMIT guard in ``app/db/database.py`` refuses to COMMIT a transaction the driver reports
  aborted, whatever swallowed the error, and keeps the SQLSTATE that aborted it.

SQLite has no aborted-transaction state, so the route-level tests run on ``_LibpqLikeConnection``.
It wraps sqlite3 and keeps libpq's transaction status with Postgres's semantics: after a failed
statement, everything but a rollback is refused (25P02), and COMMIT silently rolls back. A control
test proves the wrapper reproduces the defect when the guard is off, so the other tests are not
vacuous. ``scripts/verify_audit_deadlock_postgres.py`` proves the same on real PostgreSQL.
"""

import itertools
import logging
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import psycopg2.extensions
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError, PendingRollbackError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.security import create_access_token
from app.db import database
from app.db.database import (
    LIBPQ_TRANSACTION_INERROR,
    AbortedTransactionCommitError,
    Base,
    TransactionAlreadyAborted,
    get_db,
    install_aborted_transaction_guard,
)
from app.db.locks import is_lost_lock_race
from app.db.mixins import STALE_WRITE_CONFLICT_DETAIL
from app.main import app
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.quality import NonConformanceReport
from app.models.shipping import Shipment
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder
from app.services import audit_service as audit_module
from app.services.audit_service import AuditService, AuditWriteError

_ids = itertools.count(1)

# libpq PGTransactionStatusType values the fake connection reports.
IDLE, INTRANS, INERROR = 0, 2, LIBPQ_TRANSACTION_INERROR

# The statement the fake backend fails, standing in for the chain-lock wait that loses a deadlock.
DEADLOCK_SQL = "SELECT 'werco-test-lose-a-lock-race'"


class _FakePgError(sqlite3.OperationalError):
    """A driver error carrying a SQLSTATE, as psycopg2's do."""

    pgcode = None


class FakeDeadlock(_FakePgError):
    pgcode = "40P01"


class FakeQueryCanceled(_FakePgError):
    pgcode = "57014"


class FakeInFailedSqlTransaction(sqlite3.InternalError):
    pgcode = "25P02"


class _LibpqLikeCursor:
    def __init__(self, connection: "_LibpqLikeConnection", raw: sqlite3.Cursor) -> None:
        self._connection = connection
        self._raw = raw

    def _run(self, method, sql, *args):
        info = self._connection.info
        rolls_back_to_savepoint = sql.lstrip().upper().startswith("ROLLBACK TO SAVEPOINT")
        if info.transaction_status == INERROR and not rolls_back_to_savepoint:
            raise FakeInFailedSqlTransaction(
                "current transaction is aborted, commands ignored until end of transaction block"
            )
        if sql.strip() == DEADLOCK_SQL:
            info.transaction_status = INERROR
            raise self._connection.next_failure("deadlock detected")
        try:
            result = method(sql, *args)
        except sqlite3.Error:
            info.transaction_status = INERROR
            raise
        info.transaction_status = INTRANS
        return result

    def execute(self, sql, *args):
        return self._run(self._raw.execute, sql, *args)

    def executemany(self, sql, *args):
        return self._run(self._raw.executemany, sql, *args)

    def __getattr__(self, name):
        return getattr(self._raw, name)


class _LibpqLikeConnection:
    """sqlite3 behind libpq's transaction status and PostgreSQL's aborted-transaction rules."""

    def __init__(self, raw: sqlite3.Connection) -> None:
        self._raw = raw
        self.info = SimpleNamespace(transaction_status=IDLE)
        self.next_failure = FakeDeadlock

    def cursor(self, *args, **kwargs):
        return _LibpqLikeCursor(self, self._raw.cursor(*args, **kwargs))

    def commit(self) -> None:
        if self.info.transaction_status == INERROR:
            # What PostgreSQL does with a COMMIT of an aborted transaction: roll it back, and
            # (through psycopg2) raise nothing.
            self._raw.rollback()
        else:
            self._raw.commit()
        self.info.transaction_status = IDLE

    def rollback(self) -> None:
        self._raw.rollback()
        self.info.transaction_status = IDLE

    def __getattr__(self, name):
        return getattr(self._raw, name)


@pytest.fixture
def libpq_like():
    """A guarded engine on the libpq-like fake, its one DBAPI connection, and a sessionmaker."""
    raw = _LibpqLikeConnection(sqlite3.connect(":memory:", check_same_thread=False))
    engine = create_engine("sqlite://", creator=lambda: raw, poolclass=StaticPool)
    install_aborted_transaction_guard(engine)
    try:
        yield SimpleNamespace(engine=engine, dbapi=raw, sessions=sessionmaker(bind=engine, autoflush=False))
    finally:
        engine.dispose()


def _make_table(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)"))


def _count_notes(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM notes")).scalar()


# --------------------------------------------------------------------------------------------
# The guard itself
# --------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_libpq_inerror_constant_matches_psycopg2():
    assert LIBPQ_TRANSACTION_INERROR == psycopg2.extensions.TRANSACTION_STATUS_INERROR


@pytest.mark.unit
@pytest.mark.parametrize(
    "dbapi_connection, expected",
    [
        (object(), False),  # sqlite3-like: no ``info`` at all
        (SimpleNamespace(info={}), False),  # an ``info`` without a status is never "aborted"
        (SimpleNamespace(info=SimpleNamespace(transaction_status=INTRANS)), False),
        (SimpleNamespace(info=SimpleNamespace(transaction_status=IDLE)), False),
        (SimpleNamespace(info=SimpleNamespace(transaction_status="garbage")), False),
        (SimpleNamespace(info=SimpleNamespace(transaction_status=INERROR)), True),
    ],
)
def test_transaction_status_reader(dbapi_connection, expected):
    assert database._dbapi_transaction_in_error(dbapi_connection) is expected


@pytest.mark.unit
def test_app_engine_carries_the_guard():
    for identifier, listener in database._GUARD_LISTENERS:
        assert event.contains(database.engine, identifier, listener), identifier


@pytest.mark.unit
def test_install_is_idempotent(libpq_like):
    install_aborted_transaction_guard(libpq_like.engine)
    install_aborted_transaction_guard(libpq_like.engine)
    # One removal clears each listener, so each was registered exactly once.
    for identifier, listener in database._GUARD_LISTENERS:
        event.remove(libpq_like.engine, identifier, listener)
        assert not event.contains(libpq_like.engine, identifier, listener), identifier


@pytest.mark.unit
def test_control_without_the_guard_commit_silently_loses_the_write(libpq_like):
    """The fake reproduces the defect: an aborted transaction's COMMIT returns normally."""
    for identifier, listener in database._GUARD_LISTENERS:
        event.remove(libpq_like.engine, identifier, listener)
    _make_table(libpq_like.engine)
    with libpq_like.sessions() as session:
        session.execute(text("INSERT INTO notes (body) VALUES ('lost')"))
        with pytest.raises(OperationalError):
            session.execute(text(DEADLOCK_SQL))
        session.commit()  # no exception: this is the silent 200
    assert _count_notes(libpq_like.engine) == 0


@pytest.mark.unit
def test_commit_of_an_aborted_transaction_is_refused_with_its_cause(libpq_like):
    _make_table(libpq_like.engine)
    with libpq_like.sessions() as session:
        session.execute(text("INSERT INTO notes (body) VALUES ('lost')"))
        try:
            session.execute(text(DEADLOCK_SQL))
        except DBAPIError:
            pass  # swallowed, as AuditService.log used to
        with pytest.raises(AbortedTransactionCommitError) as refused:
            session.commit()
        session.rollback()
    assert isinstance(refused.value.orig, TransactionAlreadyAborted)
    assert refused.value.orig.pgcode == "40P01"
    assert "FakeDeadlock" in refused.value.orig.cause
    assert is_lost_lock_race(refused.value)
    assert _count_notes(libpq_like.engine) == 0


@pytest.mark.unit
def test_a_core_connection_commit_is_refused_too(libpq_like):
    """The guard is on the engine, not the Session: an ``engine.begin()`` block cannot silently lose a write."""
    _make_table(libpq_like.engine)
    with pytest.raises(AbortedTransactionCommitError) as refused:
        with libpq_like.engine.begin() as conn:
            conn.execute(text("INSERT INTO notes (body) VALUES ('lost')"))
            try:
                conn.execute(text(DEADLOCK_SQL))
            except DBAPIError:
                pass  # swallowed; the block then exits normally and commits
    assert refused.value.orig.pgcode == "40P01"
    assert _count_notes(libpq_like.engine) == 0


@pytest.mark.unit
def test_the_first_error_is_the_cause_not_its_25p02_echoes(libpq_like):
    _make_table(libpq_like.engine)
    with libpq_like.sessions() as session:
        with pytest.raises(OperationalError):
            session.execute(text(DEADLOCK_SQL))
        with pytest.raises(DBAPIError) as echo:
            session.execute(text("SELECT 1"))
        assert echo.value.orig.pgcode == "25P02"
        with pytest.raises(AbortedTransactionCommitError) as refused:
            session.commit()
        session.rollback()
    assert refused.value.orig.pgcode == "40P01"


@pytest.mark.unit
def test_a_non_race_cause_is_refused_but_not_called_a_lost_race(libpq_like):
    _make_table(libpq_like.engine)
    libpq_like.dbapi.next_failure = FakeQueryCanceled
    with libpq_like.sessions() as session:
        with pytest.raises(OperationalError):
            session.execute(text(DEADLOCK_SQL))
        with pytest.raises(AbortedTransactionCommitError) as refused:
            session.commit()
        session.rollback()
    assert refused.value.orig.pgcode == "57014"
    assert not is_lost_lock_race(refused.value)


@pytest.mark.unit
def test_the_connection_is_usable_after_the_refusal_and_the_cause_does_not_leak(libpq_like):
    """The rollback after a refusal ends the transaction; the next one commits, uncontaminated."""
    _make_table(libpq_like.engine)
    with libpq_like.sessions() as session:
        with pytest.raises(OperationalError):
            session.execute(text(DEADLOCK_SQL))
        with pytest.raises(AbortedTransactionCommitError):
            session.commit()
        session.rollback()
        session.execute(text("INSERT INTO notes (body) VALUES ('kept')"))
        session.commit()
    assert _count_notes(libpq_like.engine) == 1
    with libpq_like.engine.connect() as conn:
        assert database._ABORT_CAUSE_KEY not in conn.connection.info


@pytest.mark.unit
def test_a_session_only_closed_after_the_refusal_returns_a_clean_connection(libpq_like):
    """Close-only cleanup (``with SessionLocal() as db:``, ``finally: db.close()``) sends no ROLLBACK.

    SQLAlchemy marks the transaction inactive when the COMMIT raises, and the pool then skips its
    reset-on-return. The guard rolls the DBAPI connection back itself, so the next checkout --
    possibly another tenant's job -- does not inherit an aborted transaction (25P02).
    """
    _make_table(libpq_like.engine)
    session = libpq_like.sessions()
    session.execute(text("INSERT INTO notes (body) VALUES ('lost')"))
    with pytest.raises(OperationalError):
        session.execute(text(DEADLOCK_SQL))
    with pytest.raises(AbortedTransactionCommitError):
        session.commit()
    session.close()  # no rollback() first
    assert libpq_like.dbapi.info.transaction_status != INERROR
    with libpq_like.sessions() as next_session:
        next_session.execute(text("INSERT INTO notes (body) VALUES ('kept')"))
        next_session.commit()
    assert _count_notes(libpq_like.engine) == 1


@pytest.mark.unit
def test_a_savepoint_rollback_un_aborts_the_transaction(libpq_like):
    """An error a savepoint contains is not an aborted transaction: the commit goes through."""
    _make_table(libpq_like.engine)
    with libpq_like.sessions() as session:
        session.execute(text("INSERT INTO notes (body) VALUES ('kept')"))
        nested = session.begin_nested()
        with pytest.raises(OperationalError):
            session.execute(text(DEADLOCK_SQL))
        nested.rollback()
        session.commit()
    assert _count_notes(libpq_like.engine) == 1


@pytest.mark.unit
def test_a_cause_recorded_inside_a_rolled_back_savepoint_is_forgotten(libpq_like):
    """After recovering from one error, a later abort reports ITS cause, not the recovered one."""
    _make_table(libpq_like.engine)
    with libpq_like.sessions() as session:
        nested = session.begin_nested()
        with pytest.raises(OperationalError):
            session.execute(text(DEADLOCK_SQL))
        nested.rollback()
        libpq_like.dbapi.next_failure = FakeQueryCanceled
        with pytest.raises(OperationalError):
            session.execute(text(DEADLOCK_SQL))
        with pytest.raises(AbortedTransactionCommitError) as refused:
            session.commit()
        session.rollback()
    assert refused.value.orig.pgcode == "57014"


@pytest.mark.unit
def test_an_unrecorded_cause_is_still_refused():
    """An abort that never passed through SQLAlchemy has no SQLSTATE; the COMMIT is refused anyway."""
    error = AbortedTransactionCommitError("COMMIT", None, TransactionAlreadyAborted(None, None))
    assert "unrecorded" in str(error)
    assert not is_lost_lock_race(error)


@pytest.mark.unit
@pytest.mark.parametrize("pgcode, expected", [("40P01", True), ("40001", True), ("57014", False), (None, False)])
def test_lost_lock_race_classification(pgcode, expected):
    driver_error = type("DriverError", (Exception,), {"pgcode": pgcode})("boom")
    assert is_lost_lock_race(OperationalError("SELECT 1", {}, driver_error)) is expected
    refused = AbortedTransactionCommitError("COMMIT", None, TransactionAlreadyAborted(pgcode, "DriverError: boom"))
    assert is_lost_lock_race(refused) is expected
    assert is_lost_lock_race(RuntimeError("not a database error")) is False


# --------------------------------------------------------------------------------------------
# AuditService.log: database errors that abort the transaction propagate
# --------------------------------------------------------------------------------------------


def _driver_error(pgcode: str) -> OperationalError:
    return OperationalError(
        "SELECT pg_advisory_xact_lock(%(k)s)", {}, type("DriverError", (Exception,), {"pgcode": pgcode})("boom")
    )


def _chain_lock_that_loses(pgcode: str):
    def lose(self):
        raise _driver_error(pgcode)

    return lose


@pytest.mark.unit
@pytest.mark.parametrize("pgcode", ["40P01", "57014"])
def test_log_reraises_a_chain_lock_error(db_session: Session, monkeypatch, pgcode):
    monkeypatch.setattr(settings, "AUDIT_HASH_CHAIN_ENABLED", True)
    monkeypatch.setattr(AuditService, "_acquire_chain_lock", _chain_lock_that_loses(pgcode))
    with pytest.raises(OperationalError) as raised:
        AuditService(db_session, company_id=1).log(action="UPDATE", resource_type="part", resource_id=1)
    assert raised.value.orig.pgcode == pgcode
    assert is_lost_lock_race(raised.value) is (pgcode == "40P01")


@pytest.mark.unit
def test_log_reraises_a_tail_read_error(db_session: Session, monkeypatch):
    monkeypatch.setattr(settings, "AUDIT_HASH_CHAIN_ENABLED", True)

    def tail_read_fails(self):
        raise _driver_error("57014")

    monkeypatch.setattr(AuditService, "_get_next_sequence_and_previous_hash", tail_read_fails)
    with pytest.raises(OperationalError):
        AuditService(db_session, company_id=1).log(action="UPDATE", resource_type="part", resource_id=1)


@pytest.mark.unit
def test_log_reraises_the_callers_own_failed_flush(db_session: Session):
    """``begin_nested`` flushes the caller's pending rows; that failure is the caller's, not the audit's.

    Swallowing it used to turn a duplicate-key error into a later ``PendingRollbackError`` at
    COMMIT; now the caller sees the ``IntegrityError`` itself.
    """
    number = f"ABORT-GUARD-{next(_ids)}"
    for name in ("first", "duplicate"):
        db_session.add(
            Part(
                part_number=number,
                name=name,
                part_type="manufactured",
                unit_of_measure="each",
                company_id=1,
            )
        )
    with pytest.raises(IntegrityError):
        AuditService(db_session, company_id=1).log(action="CREATE", resource_type="part", resource_id=1)
    db_session.rollback()


@pytest.mark.unit
def test_a_python_side_audit_failure_stays_best_effort(db_session: Session, monkeypatch):
    """A failure that never reaches the database leaves the transaction intact: log, return None."""

    def hash_fails(**_kwargs):
        raise TypeError("not JSON serializable")

    monkeypatch.setattr(settings, "AUDIT_HASH_CHAIN_ENABLED", True)
    monkeypatch.setattr(audit_module, "compute_audit_hash", hash_fails)
    number = f"ABORT-GUARD-{next(_ids)}"
    db_session.add(
        Part(part_number=number, name="kept", part_type="manufactured", unit_of_measure="each", company_id=1)
    )
    db_session.flush()
    assert AuditService(db_session, company_id=1).log(action="CREATE", resource_type="part", resource_id=1) is None
    db_session.commit()
    assert db_session.query(Part).filter(Part.part_number == number).count() == 1


@pytest.mark.unit
@pytest.mark.parametrize("unbindable", [Decimal("1.50"), date(2026, 11, 8)], ids=["decimal", "date"])
@pytest.mark.parametrize("chain_enabled", [True, False], ids=["chain", "paused"])
def test_an_unbindable_audit_value_stays_best_effort_and_keeps_the_callers_write(
    db_session: Session, monkeypatch, unbindable, chain_enabled
):
    """A Python-side failure INSIDE the INSERT savepoint: the JSON column cannot bind the value.

    The integrity hash serializes with ``default=str`` and never notices; the bind does, during the
    savepoint's flush, which deactivates the savepoint. ``log`` must roll the savepoint back so the
    caller's transaction survives -- its write commits -- and no audit row is written.
    """
    monkeypatch.setattr(settings, "AUDIT_HASH_CHAIN_ENABLED", chain_enabled)
    number = f"ABORT-GUARD-{next(_ids)}"
    db_session.add(
        Part(part_number=number, name="kept", part_type="manufactured", unit_of_measure="each", company_id=1)
    )
    db_session.flush()
    audit_rows_before = db_session.query(AuditLog).count()

    entry = AuditService(db_session, company_id=1).log(
        action="CREATE", resource_type="part", resource_id=1, new_values={"quantity": unbindable}
    )

    assert entry is None
    db_session.commit()
    assert db_session.query(Part).filter(Part.part_number == number).count() == 1
    assert db_session.query(AuditLog).count() == audit_rows_before


@pytest.mark.unit
def test_an_unbindable_value_in_the_callers_own_pending_row_still_fails_the_caller(db_session: Session, monkeypatch):
    """The documented edge: ``begin_nested`` flushes the CALLER's pending rows outside any savepoint.

    A Python-side failure there is the caller's own; ``log`` returns None, and the caller's commit
    raises, exactly as its own flush would have. Nothing is silently half-saved.
    """
    monkeypatch.setattr(settings, "AUDIT_HASH_CHAIN_ENABLED", True)
    db_session.add(
        OperationalEvent(
            company_id=1, event_type="abort_guard_probe", source_module="tests", event_payload={"q": Decimal("1")}
        )
    )
    assert AuditService(db_session, company_id=1).log(action="UPDATE", resource_type="part", resource_id=1) is None
    with pytest.raises(PendingRollbackError):
        db_session.commit()
    db_session.rollback()


@pytest.mark.unit
def test_paused_chain_takes_no_lock_so_its_failure_cannot_reach_log(db_session: Session, monkeypatch):
    monkeypatch.setattr(settings, "AUDIT_HASH_CHAIN_ENABLED", False)
    monkeypatch.setattr(AuditService, "_acquire_chain_lock", _chain_lock_that_loses("40P01"))
    entry = AuditService(db_session, company_id=1).log(action="UPDATE", resource_type="part", resource_id=1)
    assert entry is not None
    db_session.commit()


@pytest.mark.unit
def test_log_required_reports_the_database_error_as_audit_write_error(db_session: Session, monkeypatch):
    """Every ``log_required`` caller maps ``AuditWriteError`` to rollback + 503; that contract holds."""
    monkeypatch.setattr(settings, "AUDIT_HASH_CHAIN_ENABLED", True)
    monkeypatch.setattr(AuditService, "_acquire_chain_lock", _chain_lock_that_loses("40P01"))
    with pytest.raises(AuditWriteError) as raised:
        AuditService(db_session, company_id=1).log_required(action="UPDATE", resource_type="part", resource_id=1)
    assert isinstance(raised.value.__cause__, OperationalError)
    assert raised.value.__cause__.orig.pgcode == "40P01"


# --------------------------------------------------------------------------------------------
# Through the HTTP stack, on the libpq-like backend
# --------------------------------------------------------------------------------------------


@pytest.fixture
def routed(libpq_like, monkeypatch):
    """The app wired to the libpq-like engine, with one admin and one work order (priority 5)."""
    engine = libpq_like.engine
    Base.metadata.create_all(engine)
    with libpq_like.sessions() as session:
        session.add(Company(id=1, name="Guard Co", slug="guard-co", is_active=True))
        session.flush()
        admin = User(
            company_id=1,
            email="guard-admin@example.test",
            employee_id="GUARD-1",
            first_name="Guard",
            last_name="Admin",
            hashed_password="unused",
            role=UserRole.ADMIN,
            is_active=True,
        )
        work_order = WorkOrder(
            company_id=1,
            work_order_number="WO-GUARD-1",
            quantity_ordered=5,
            priority=5,
            work_order_type="laser_cutting",
        )
        session.add_all([admin, work_order])
        session.commit()
        admin_id, work_order_id = admin.id, work_order.id

    def guarded_db():
        session = libpq_like.sessions()
        try:
            yield session
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # The request's chain-lock wait is the deadlock victim (the libpq-like backend fails it).
    def chain_lock_wait_loses(self):
        self.db.execute(text(DEADLOCK_SQL))

    monkeypatch.setattr(settings, "AUDIT_HASH_CHAIN_ENABLED", True)
    monkeypatch.setattr(AuditService, "_acquire_chain_lock", chain_lock_wait_loses)
    app.dependency_overrides[get_db] = guarded_db
    try:
        yield SimpleNamespace(
            engine=engine,
            dbapi=libpq_like.dbapi,
            work_order_id=work_order_id,
            headers={
                "Authorization": f"Bearer {create_access_token(subject=admin_id, company_id=1)}",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
    finally:
        app.dependency_overrides.pop(get_db, None)


def _priority(routed) -> int:
    with routed.engine.connect() as conn:
        return conn.execute(
            text("SELECT priority FROM work_orders WHERE id = :i"), {"i": routed.work_order_id}
        ).scalar()


def _work_order_audit_rows(routed) -> int:
    with Session(routed.engine) as session:
        return (
            session.query(AuditLog)
            .filter(AuditLog.resource_type == "work_order", AuditLog.resource_id == routed.work_order_id)
            .count()
        )


def _put_priority(routed, client: TestClient):
    return client.put(
        f"/api/v1/work-orders/{routed.work_order_id}/priority",
        json={"priority": 1, "reason": "rush"},
        headers=routed.headers,
    )


def _swallowing_log(monkeypatch) -> None:
    """Wrap ``AuditService.log`` the way a legacy best-effort caller does, to isolate the guard."""
    real_log = AuditService.log

    def swallowing(self, *args, **kwargs):
        try:
            return real_log(self, *args, **kwargs)
        except Exception:
            return None

    monkeypatch.setattr(AuditService, "log", swallowing)


def _guard_refusals(caplog) -> int:
    return sum("Refused COMMIT" in record.getMessage() for record in caplog.records)


@pytest.mark.api
def test_route_victim_answers_409_and_saves_nothing(routed, caplog):
    """The audit write itself raises (``log`` re-raises the 40P01); the guard never has to act."""
    response = _put_priority(routed, TestClient(app))
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == STALE_WRITE_CONFLICT_DETAIL
    assert _priority(routed) == 5
    assert _work_order_audit_rows(routed) == 0
    assert _guard_refusals(caplog) == 0


@pytest.mark.api
def test_route_victim_behind_a_swallowing_caller_is_refused_at_commit(routed, monkeypatch, caplog):
    """Only the COMMIT guard stands between this request and a silent 200."""
    _swallowing_log(monkeypatch)
    response = _put_priority(routed, TestClient(app))
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == STALE_WRITE_CONFLICT_DETAIL
    assert _priority(routed) == 5
    assert _work_order_audit_rows(routed) == 0
    assert _guard_refusals(caplog) == 1


@pytest.mark.api
def test_route_control_without_the_guard_answers_a_silent_200(routed, monkeypatch):
    """The defect, reproduced on the fake: 200, yet neither the priority nor an audit row was saved."""
    _swallowing_log(monkeypatch)
    for identifier, listener in database._GUARD_LISTENERS:
        event.remove(routed.engine, identifier, listener)
    response = _put_priority(routed, TestClient(app))
    assert response.status_code == 200, response.text
    assert _priority(routed) == 5
    assert _work_order_audit_rows(routed) == 0


@pytest.mark.api
def test_route_non_race_abort_is_a_500_not_a_409(routed, monkeypatch):
    routed.dbapi.next_failure = FakeQueryCanceled
    _swallowing_log(monkeypatch)
    response = _put_priority(routed, TestClient(app, raise_server_exceptions=False))
    assert response.status_code == 500
    assert "Internal server error" in response.json()["detail"]
    assert _priority(routed) == 5


@pytest.mark.api
def test_non_race_database_errors_keep_their_500_path(routed, monkeypatch):
    """The lost-race handler re-raises everything else, so the error still reaches the 500 handler."""
    routed.dbapi.next_failure = FakeQueryCanceled
    with pytest.raises(OperationalError) as raised:
        _put_priority(routed, TestClient(app))
    assert raised.value.orig.pgcode == "57014"
    assert _priority(routed) == 5


@pytest.mark.api
def test_route_retry_after_the_lost_race_succeeds_with_its_audit_row(routed, monkeypatch):
    client = TestClient(app)
    assert _put_priority(routed, client).status_code == 409
    monkeypatch.setattr(AuditService, "_acquire_chain_lock", lambda self: None)
    response = _put_priority(routed, client)
    assert response.status_code == 200, response.text
    assert _priority(routed) == 1
    assert _work_order_audit_rows(routed) == 1


class _SentrySpy:
    def __init__(self) -> None:
        self.captured: list = []

    def capture_exception(self, exc, **scope_kwargs) -> None:
        self.captured.append((exc, scope_kwargs))


@pytest.fixture
def sentry_spy(monkeypatch):
    """Pretend Sentry is configured and record what the SDK would be handed (no network, no init)."""
    import sentry_sdk

    spy = _SentrySpy()
    monkeypatch.setattr(settings, "SENTRY_DSN", "https://public@sentry.invalid/1")
    monkeypatch.setattr(sentry_sdk, "set_tag", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sentry_sdk, "capture_exception", spy.capture_exception)
    return spy


@pytest.mark.api
def test_a_lost_lock_race_is_reported_to_sentry_grouped_by_route_and_sqlstate(routed, sentry_spy):
    """A 409 must not hide a lock-order bug: before the 409 handler, the 500 handler reported it."""
    response = TestClient(app).put(
        f"/api/v1/work-orders/{routed.work_order_id}/priority",
        json={"priority": 1, "reason": "rush"},
        headers={**routed.headers, "X-Correlation-ID": "race-ref-1"},
    )
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": STALE_WRITE_CONFLICT_DETAIL}

    [(exc, scope_kwargs)] = sentry_spy.captured
    assert is_lost_lock_race(exc)
    route = "/api/v1/work-orders/{work_order_id}/priority"
    assert scope_kwargs["fingerprint"] == ["lost-lock-race", "PUT", route, "40P01"]
    assert scope_kwargs["tags"] == {
        "lost_lock_race": "True",
        "pgcode": "40P01",
        "method": "PUT",
        "route": route,
        "path": f"/api/v1/work-orders/{routed.work_order_id}/priority",
        "correlation_id": "race-ref-1",
    }


@pytest.mark.api
def test_a_failing_sentry_report_never_changes_the_409(routed, monkeypatch):
    import app.main as app_main

    attempts = []

    def capture_breaks(exc, **_kwargs):
        attempts.append(exc)
        raise RuntimeError("sentry is down")

    monkeypatch.setattr(app_main, "capture_exception_to_sentry", capture_breaks)
    response = _put_priority(routed, TestClient(app))
    assert len(attempts) == 1 and is_lost_lock_race(attempts[0])  # the report was attempted, and failed
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": STALE_WRITE_CONFLICT_DETAIL}
    assert _priority(routed) == 5


@pytest.mark.api
def test_a_non_race_database_error_is_not_reported_as_a_lost_race(routed, monkeypatch, sentry_spy):
    """Only the 500 handler reports it (once), with its own grouping -- never the race fingerprint."""
    routed.dbapi.next_failure = FakeQueryCanceled
    _swallowing_log(monkeypatch)
    response = _put_priority(routed, TestClient(app, raise_server_exceptions=False))
    assert response.status_code == 500
    assert all("fingerprint" not in scope_kwargs for _exc, scope_kwargs in sentry_spy.captured)


# --------------------------------------------------------------------------------------------
# POST /work-orders/: the creation audit commits WITH the work order, so a 409 saved nothing
# --------------------------------------------------------------------------------------------


def _new_part_id(routed) -> int:
    with Session(routed.engine) as session:
        part = Part(
            part_number=f"ABORT-GUARD-WO-{next(_ids)}",
            name="Guard part",
            part_type="manufactured",
            unit_of_measure="each",
            company_id=1,
        )
        session.add(part)
        session.commit()
        return part.id


def _created_work_orders(routed, part_id: int) -> list:
    """(work order id, number) for every work order on ``part_id``, and every CREATE audit row's."""
    with Session(routed.engine) as session:
        orders = [(wo.id, wo.work_order_number) for wo in session.query(WorkOrder).filter(WorkOrder.part_id == part_id)]
        creates = [
            (row.resource_id, row.resource_identifier, row.new_values)
            for row in session.query(AuditLog).filter(
                AuditLog.resource_type == "work_order", AuditLog.action == "CREATE"
            )
        ]
    return orders, creates


@pytest.mark.api
def test_create_work_order_victim_answers_409_and_saves_no_work_order(routed, caplog, monkeypatch):
    """The 409 is true: the work order rolls back with its lost audit write, so a retry is not a duplicate.

    The creation audit used to be written in a transaction AFTER the work order had committed; a
    lost race there answered "refresh and try again" for a work order that existed.
    """
    part_id = _new_part_id(routed)
    client = TestClient(app)
    body = {"part_id": part_id, "quantity_ordered": 3}

    response = client.post("/api/v1/work-orders/", json=body, headers=routed.headers)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == STALE_WRITE_CONFLICT_DETAIL
    assert _created_work_orders(routed, part_id) == ([], [])
    assert _guard_refusals(caplog) == 0

    monkeypatch.setattr(AuditService, "_acquire_chain_lock", lambda self: None)
    retry = client.post("/api/v1/work-orders/", json=body, headers=routed.headers)
    assert retry.status_code == 201, retry.text
    orders, creates = _created_work_orders(routed, part_id)
    assert orders == [(retry.json()["id"], retry.json()["work_order_number"])]
    [(resource_id, identifier, new_values)] = creates
    assert (resource_id, identifier) == orders[0]
    # The snapshot is the stored row, as when it was taken after the commit.
    assert new_values["id"] == resource_id and new_values["status"] == "draft"
    assert new_values["version"] == 1 and new_values["quantity_ordered"] == 3.0


@pytest.mark.api
def test_create_work_order_reconcile_race_after_commit_still_answers_201(
    client: TestClient, db_session: Session, auth_headers: dict, test_part: Part, monkeypatch
):
    """The defensive reconcile runs after the work order and its audit row committed.

    A lost race there must not answer the "nothing was saved" 409: the work order exists, and a
    retry would duplicate it. It rolls back only the reconcile and the creation answers 201.
    """
    from app.api.endpoints import work_orders as work_orders_endpoint

    def reconcile_loses(*_args, **_kwargs):
        raise _driver_error("40P01")

    monkeypatch.setattr(work_orders_endpoint, "reconcile_work_orders_from_completion_evidence", reconcile_loses)
    response = client.post(
        "/api/v1/work-orders/", json={"part_id": test_part.id, "quantity_ordered": 2}, headers=auth_headers
    )
    assert response.status_code == 201, response.text
    work_order_id = response.json()["id"]
    db_session.rollback()
    db_session.expire_all()
    assert db_session.get(WorkOrder, work_order_id) is not None
    creates = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.resource_type == "work_order", AuditLog.resource_id == work_order_id, AuditLog.action == "CREATE"
        )
        .count()
    )
    assert creates == 1


# --------------------------------------------------------------------------------------------
# Required-audit doors: required evidence lost to the race is audit loss, so rollback + 503
# --------------------------------------------------------------------------------------------


def _create_ncr(routed, client: TestClient):
    """A ``log_required`` caller: ``POST /quality/ncr`` (``quality_record_transaction``)."""
    response = client.post(
        "/api/v1/quality/ncr",
        json={
            "source": "in_process",
            "title": "Lost a lock race",
            "description": "The required audit row's chain-lock wait was the deadlock victim.",
        },
        headers=routed.headers,
    )
    with Session(routed.engine) as session:
        saved = session.query(NonConformanceReport).count() > 0
    return response, saved


def _unlock_a_locked_user(routed, client: TestClient):
    """A ``RequiredSecurityAudit`` caller: ``POST /users/{id}/unlock`` (``atomic_security_write``)."""
    with Session(routed.engine) as session:
        target = User(
            company_id=1,
            email="guard-locked@example.test",
            employee_id="GUARD-2",
            first_name="Locked",
            last_name="Out",
            hashed_password="unused",
            role=UserRole.OPERATOR,
            is_active=True,
            failed_login_attempts=5,
            locked_until=datetime.utcnow() + timedelta(minutes=30),
        )
        session.add(target)
        session.commit()
        target_id = target.id
    response = client.post(f"/api/v1/users/{target_id}/unlock", headers=routed.headers)
    with Session(routed.engine) as session:
        saved = session.get(User, target_id).failed_login_attempts != 5
    return response, saved


@pytest.mark.api
@pytest.mark.parametrize(
    "door", [_create_ncr, _unlock_a_locked_user], ids=["log_required", "RequiredSecurityAudit.log"]
)
def test_required_audit_doors_answer_503_on_a_lost_chain_lock_race(routed, caplog, door):
    """Both doors promise "503 on audit loss" (CLAUDE.md invariant 2; the unlock route's docstring).

    ``test_account_provisioning_boundary`` pins that promise only for ``log`` returning None. Since
    ``log`` now re-raises an un-contained ``DBAPIError``, each door must turn THAT into
    ``AuditWriteError`` as well, or a lost race leaks out as the generic 409 (and a non-race error
    as a 500) on the paths that documented 503.
    """
    response, saved = door(routed, TestClient(app))
    assert response.status_code == 503, response.text
    assert response.json()["detail"] == "Unable to save audit record"
    assert not saved
    with Session(routed.engine) as session:
        assert session.query(AuditLog).count() == 0
    assert _guard_refusals(caplog) == 0


def _create_shipment(routed, client: TestClient):
    """A ``log_required`` caller with NO local translation: ``POST /shipping/`` (``create_shipment_command``)."""
    with Session(routed.engine) as session:
        shipped = WorkOrder(
            company_id=1,
            work_order_number=f"WO-GUARD-SHIP-{next(_ids)}",
            quantity_ordered=5,
            quantity_complete=5,
            status="complete",
            work_order_type="laser_cutting",
        )
        session.add(shipped)
        session.commit()
        work_order_id = shipped.id
    response = client.post(
        "/api/v1/shipping/", json={"work_order_id": work_order_id, "quantity_shipped": 2}, headers=routed.headers
    )
    with Session(routed.engine) as session:
        saved = session.query(Shipment).filter(Shipment.work_order_id == work_order_id).count() > 0
    return response, saved


def _audit_row_not_written(monkeypatch) -> None:
    """``log`` returns None (it could not write the row) -- the other way ``log_required`` raises."""
    monkeypatch.setattr(AuditService, "_acquire_chain_lock", lambda self: None)
    monkeypatch.setattr(AuditService, "log", lambda self, *args, **kwargs: None)


@pytest.mark.api
@pytest.mark.parametrize("failure", ["lost_chain_lock_race", "audit_row_not_written"])
def test_an_untranslated_audit_write_error_answers_503_and_saves_nothing(routed, monkeypatch, failure):
    """Every ``log_required`` door answers 503 (invariant 2), not only the ones that translate it.

    ``POST /shipping/`` lets ``AuditWriteError`` escape; it used to reach the global handler as a
    generic 500. ``app.main``'s handler now answers the standard 503, after ``get_db`` rolled back.
    """
    if failure == "audit_row_not_written":
        _audit_row_not_written(monkeypatch)
    response, saved = _create_shipment(routed, TestClient(app, raise_server_exceptions=False))
    assert response.status_code == 503, response.text
    assert response.json() == {"detail": "Unable to save audit record"}
    assert not saved
    with Session(routed.engine) as session:
        assert session.query(AuditLog).filter(AuditLog.resource_type == "shipment").count() == 0


@pytest.mark.unit
@pytest.mark.parametrize("method", ["log", "log_create", "log_update", "log_status_change"])
def test_hank_required_audit_reports_a_reraised_database_error_as_audit_write_error(method):
    """``_RequiredCreateAudit`` makes every legacy audit call in a Hank action required: a re-raised
    ``DBAPIError`` is required evidence lost, so it must be ``AuditWriteError`` like a ``None``."""
    from app.services.hank_task_service import _RequiredCreateAudit

    def lose(*_args, **_kwargs):
        raise _driver_error("40P01")

    required = _RequiredCreateAudit(SimpleNamespace(**{method: lose}))
    with pytest.raises(AuditWriteError) as raised:
        getattr(required, method)("work_order", 1, "WO-1")
    assert raised.value.__cause__.orig.pgcode == "40P01"


# --------------------------------------------------------------------------------------------
# Per-tenant job loops share one session: a failed tenant must not sink the ones after it
# --------------------------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "module_name, runner, task",
    [
        ("mrp_jobs", "_run_mrp_for_company", "run_mrp_task"),
        ("scheduling_jobs", "_run_scheduling_for_company", "run_scheduling_task"),
    ],
)
async def test_a_failed_tenant_does_not_poison_the_next_one(
    db_session: Session, monkeypatch, module_name, runner, task
):
    """Each loop catches a tenant's failure and moves on, so it must reset the shared session.

    A refused COMMIT leaves it "prepared" and a database error leaves it needing a rollback;
    either way every later tenant would fail too. The first tenant here fails a flush, the
    session-poisoning shape SQLite can produce.
    """
    import importlib

    module = importlib.import_module(f"app.jobs.{module_name}")
    db_session.add(Company(id=2, name="Second tenant", slug=f"second-{next(_ids)}", is_active=True))
    db_session.commit()
    processed = []

    async def fake_runner(db, company_id, **_kwargs):
        if not processed and company_id == 1:
            number = f"ABORT-JOB-{next(_ids)}"
            for name in ("first", "duplicate"):
                db.add(
                    Part(part_number=number, name=name, part_type="manufactured", unit_of_measure="each", company_id=1)
                )
            processed.append("failed")
            db.flush()  # IntegrityError: the session now needs a rollback
        db.query(Company).count()  # PendingRollbackError here if the loop did not reset the session
        processed.append(company_id)
        return {"company_id": company_id}

    monkeypatch.setattr(module, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(module, runner, fake_runner)
    result = await getattr(module, task)()
    assert result["companies_processed"] == 1
    assert result["runs"] == [{"company_id": 2}]


class _PoisoningSession:
    """A job's shared session whose first query on ``poison_on`` fails the way a database error does.

    It fails a flush first (two parts, one number: IntegrityError), which leaves the real session
    needing a rollback -- the state a Postgres error, aborted transaction or refused COMMIT also
    leaves -- then raises. Every other call goes to the real session.
    """

    def __init__(self, real: Session, poison_on) -> None:
        self._real = real
        self._poison_on = poison_on
        self.poisoned = False

    def query(self, *entities, **kwargs):
        if not self.poisoned and entities and entities[0] is self._poison_on:
            self.poisoned = True
            _poison(self._real)
        return self._real.query(*entities, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _poison(db: Session) -> None:
    number = f"ABORT-JOB-{next(_ids)}"
    for name in ("first", "duplicate"):
        db.add(Part(part_number=number, name=name, part_type="manufactured", unit_of_measure="each", company_id=1))
    db.flush()  # IntegrityError: the session now needs a rollback


def _job_failures(caplog, company_id: int) -> list:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.ERROR and f"company {company_id}" in record.getMessage()
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    "module_name, task, first_query",
    [
        ("notification_jobs", "check_calibrations_task", "app.models.calibration:Equipment"),
        ("notification_jobs", "check_late_work_orders_task", "app.models.work_order:WorkOrder"),
        ("notification_jobs", "check_low_stock_task", "app.models.inventory:InventoryItem"),
        ("notification_jobs", "check_quote_expiring_task", "app.models.quote:Quote"),
        ("shipping_jobs", "poll_tracking_task", "app.models.carrier_account:CompanyShippingProfile"),
    ],
)
async def test_a_failed_tenant_does_not_fail_the_next_one_in_the_digest_and_tracking_loops(
    db_session: Session, monkeypatch, caplog, module_name, task, first_query
):
    """The same reset as mrp/scheduling: tenant 1's failed transaction must not fail tenant 2.

    Each loop's first statement for tenant 1 fails and leaves the shared session needing a rollback.
    Without the reset in the loop's ``except``, tenant 2's first statement fails on it too.
    """
    import importlib

    module = importlib.import_module(f"app.jobs.{module_name}")
    model_module, model_name = first_query.split(":")
    model = getattr(importlib.import_module(model_module), model_name)
    db_session.add(Company(id=2, name="Second tenant", slug=f"second-{next(_ids)}", is_active=True))
    db_session.commit()
    session = _PoisoningSession(db_session, model)
    monkeypatch.setattr(module, "SessionLocal", lambda: session)
    if module_name == "notification_jobs":
        monkeypatch.setattr(module, "_active_company_ids", lambda _db: [1, 2])

    await getattr(module, task)()

    assert session.poisoned
    assert _job_failures(caplog, 1), "tenant 1 was meant to fail"
    assert _job_failures(caplog, 2) == []


@pytest.mark.unit
@pytest.mark.parametrize("failing_step", ["get_tracking", "apply"])
async def test_a_failed_shipment_does_not_fail_the_next_one_in_the_tracking_poll(
    db_session: Session, monkeypatch, test_work_order, failing_step
):
    """``_poll_tracking_for_company`` swallows one shipment's failure; the next shipment must still apply."""
    from app.jobs import shipping_jobs
    from app.services.shipping_service import ShippingService

    for n in (1, 2):
        db_session.add(
            Shipment(
                shipment_number=f"SHP-GUARD-{next(_ids)}",
                work_order_id=test_work_order.id,
                tracking_number=f"TRACK-{n}",
                company_id=1,
            )
        )
    db_session.commit()
    calls = {"fetch": 0, "apply": 0}

    async def fetch(_service, _company_id, _shipment):
        calls["fetch"] += 1
        if failing_step == "get_tracking" and calls["fetch"] == 1:
            _poison(db_session)
        db_session.query(Shipment).count()  # PendingRollbackError if the loop did not reset the session
        return ["event"]

    def apply(self, **_kwargs):
        calls["apply"] += 1
        if failing_step == "apply" and calls["apply"] == 1:
            _poison(self.db)
        self.db.query(Shipment).count()
        return ["inserted"]

    monkeypatch.setattr(shipping_jobs, "_fetch_tracking", fetch)
    monkeypatch.setattr(ShippingService, "record_tracking_events", apply)

    polled, updated = await shipping_jobs._poll_tracking_for_company(db_session, 1)

    assert (polled, updated) == ((1, 1) if failing_step == "get_tracking" else (2, 1))
