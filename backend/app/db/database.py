import logging
from contextlib import contextmanager
from typing import Any, Optional

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import InternalError
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from app.core.config import settings

logger = logging.getLogger(__name__)

# SQLite needs one connect arg that Postgres neither needs nor accepts.
#
# Production is Supabase Postgres and is completely unaffected by this branch -- it is
# guarded on the URL scheme, and for Postgres `_connect_args` stays empty, so the engine
# is constructed exactly as before.
#
# Why it is needed at all: sqlite3 refuses to use a connection from any thread other
# than the one that created it. QueuePool hands a pooled connection to whichever thread
# checks it out, so under the TestClient -- where requests are served on anyio worker
# threads -- a request can pick up a connection another thread opened and raise
# ProgrammingError, typically surfacing from pool_pre_ping rather than from app code.
# The test suite's own engine has always passed this arg for the same reason; the app
# engine needs it too now that both address one in-memory database.
_connect_args: dict = {}
if settings.is_sqlite_database:
    _connect_args["check_same_thread"] = False

# Create engine with connection pooling
engine = create_engine(
    settings.SQLALCHEMY_DATABASE_URL,
    connect_args=_connect_args,
    poolclass=QueuePool,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT,
    pool_recycle=settings.DB_POOL_RECYCLE,
    pool_pre_ping=settings.DB_POOL_PRE_PING,
    echo=False,  # SQL query logging disabled for security; enable explicitly if needed
)


# ---------------------------------------------------------------------------------------------
# Aborted-transaction COMMIT guard
# ---------------------------------------------------------------------------------------------
#
# PostgreSQL aborts the WHOLE transaction on the first statement that fails. Every later
# statement is refused, except COMMIT: the server ends the aborted transaction with a ROLLBACK,
# reports that as the command's outcome, and psycopg2 raises nothing. SQLAlchemy cannot see it
# either, because an error from ``Session.execute()`` leaves the session ACTIVE (only a failed
# flush deactivates it). So code that swallows a database error and then commits gets a normal
# return from ``commit()`` for a transaction that saved nothing. ``AuditService.log`` did exactly
# that when its wait on the global audit-chain lock was chosen as a deadlock victim (40P01). The
# route answered 200 and reloaded the OLD values into its response, with no audit row. Its
# post-commit side effects (realtime broadcasts) still fired.
#
# The guard closes that for every writer on this engine, not only the audit service. Right
# before the DBAPI COMMIT is sent, it reads libpq's transaction status. That is a local call with
# no round trip. If the transaction is already aborted, the guard raises
# ``AbortedTransactionCommitError`` instead of committing. The caller's error handling then sees
# a failure, and its rollback (``get_db``, ``atomic_transaction``) really ends the server
# transaction. ``app/main.py`` answers a lost lock race (``app.db.locks.is_lost_lock_race``) with
# the app-wide 409 and anything else with the usual 500.

# libpq's PQTRANS_INERROR: "idle, in a failed transaction block". psycopg2 names it
# ``psycopg2.extensions.TRANSACTION_STATUS_INERROR`` and psycopg 3 ``pq.TransactionStatus.INERROR``.
# Both are this libpq value, and a test pins psycopg2's.
LIBPQ_TRANSACTION_INERROR = 3

# Kept in the pooled DBAPI connection's ``info`` dict: ``(sqlstate, description)`` of the error
# that aborted the current transaction. Written once per transaction by ``_record_transaction_abort``
# (the FIRST error is the cause; every later one is a 25P02 "transaction is aborted" echo), and
# cleared when a transaction begins or a savepoint is rolled back (which un-aborts it).
_ABORT_CAUSE_KEY = "werco.aborted_transaction_cause"


class TransactionAlreadyAborted(Exception):
    """Stand-in driver error carried as ``AbortedTransactionCommitError.orig``.

    ``pgcode`` is the SQLSTATE of the statement that aborted the transaction, or None when the
    error never passed through SQLAlchemy. With that SQLSTATE, ``app.db.locks.is_lost_lock_race``
    judges the refused COMMIT exactly as it would have judged the original statement's error.
    """

    def __init__(self, pgcode: Optional[str], cause: Optional[str]) -> None:
        self.pgcode = pgcode
        self.cause = cause
        detail = f"aborted by {cause} (SQLSTATE {pgcode})" if cause else "aborted by an unrecorded error"
        super().__init__(f"COMMIT refused: the PostgreSQL transaction was already {detail}, so nothing in it was saved")


class AbortedTransactionCommitError(InternalError):
    """COMMIT refused because PostgreSQL had already aborted this transaction.

    An ``InternalError`` like psycopg2's own ``InFailedSqlTransaction``, so any ``except
    DBAPIError`` / ``SQLAlchemyError`` around a commit treats it as the database failure it is.
    """


def _dbapi_transaction_in_error(dbapi_connection: Any) -> bool:
    """True when the driver reports its server transaction as aborted (libpq PQTRANS_INERROR).

    Duck-typed on ``connection.info.transaction_status`` (psycopg2 >= 2.8, psycopg 3). A driver
    without it, such as sqlite3, is never judged aborted.
    """
    status = getattr(getattr(dbapi_connection, "info", None), "transaction_status", None)
    if status is None:
        return False
    try:
        return int(status) == LIBPQ_TRANSACTION_INERROR
    except (TypeError, ValueError):
        return False


def _pool_connection(conn: Connection) -> Any:
    """The pooled connection behind ``conn``, or None when it is invalidated or closed."""
    try:
        return conn.connection
    except Exception:  # invalidated or closed: there is no live transaction left to judge
        return None


def _record_transaction_abort(context: Any) -> None:
    """``handle_error`` listener: remember which error aborted the transaction.

    Only an error after which the driver reports the transaction aborted is recorded, and only
    the first. Never raises, and returns None, so the original error propagates unchanged.
    """
    try:
        if context.connection is None or context.is_disconnect:
            return
        pooled = _pool_connection(context.connection)
        if pooled is None or _ABORT_CAUSE_KEY in pooled.info:
            return
        if not _dbapi_transaction_in_error(pooled.dbapi_connection):
            return
        orig = context.original_exception
        first_line = (str(orig).strip().splitlines() or [""])[0]
        pooled.info[_ABORT_CAUSE_KEY] = (getattr(orig, "pgcode", None), f"{type(orig).__name__}: {first_line}")
    except Exception:  # pragma: no cover - diagnostics must never mask the original error
        logger.debug("Could not record the cause of an aborted transaction", exc_info=True)


def _forget_transaction_abort(conn: Connection, *_args: Any) -> None:
    """``begin`` / ``rollback_savepoint`` listener: no recorded cause outlives its abort."""
    pooled = _pool_connection(conn)
    if pooled is not None:
        pooled.info.pop(_ABORT_CAUSE_KEY, None)


def _refuse_commit_of_aborted_transaction(conn: Connection) -> None:
    """``commit`` listener: raise instead of letting COMMIT silently roll an aborted transaction back."""
    pooled = _pool_connection(conn)
    if pooled is None or not _dbapi_transaction_in_error(pooled.dbapi_connection):
        return
    pgcode, cause = pooled.info.get(_ABORT_CAUSE_KEY, (None, None))
    logger.error(
        "Refused COMMIT of a PostgreSQL transaction that was already aborted (cause: %s, SQLSTATE %s). "
        "Some code swallowed that database error; the request fails instead of reporting success.",
        cause or "unrecorded",
        pgcode,
    )
    raise AbortedTransactionCommitError("COMMIT", None, TransactionAlreadyAborted(pgcode, cause))


_GUARD_LISTENERS = (
    ("handle_error", _record_transaction_abort),
    ("begin", _forget_transaction_abort),
    ("rollback_savepoint", _forget_transaction_abort),
    ("commit", _refuse_commit_of_aborted_transaction),
)


def install_aborted_transaction_guard(target: Engine) -> None:
    """Attach the aborted-transaction COMMIT guard to ``target``. Idempotent.

    The app engine below always has it. An engine built elsewhere for app writes should call
    this too.
    """
    for identifier, listener in _GUARD_LISTENERS:
        if not event.contains(target, identifier, listener):
            event.listen(target, identifier, listener)


install_aborted_transaction_guard(engine)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """
    Dependency that provides a database session.
    Automatically rolls back on error and closes the session when the request is complete.
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def atomic_transaction(db: Session):
    """Context manager for atomic multi-step database operations.

    Usage:
        with atomic_transaction(db):
            db.add(record1)
            db.add(record2)
            # auto-commits on success, auto-rolls-back on exception
    """
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def get_pool_status() -> dict:
    """
    Get current connection pool status for monitoring.
    Returns pool size, checked out connections, and overflow.
    """
    pool = engine.pool
    return {
        "provider": settings.database_provider,
        "host": settings.safe_database_host,
        "pool_size": pool.size(),
        "checked_out": pool.checkedout(),
        "overflow": pool.overflow(),
        "checked_in": pool.checkedin(),
        "invalid": pool.invalidatedcount() if hasattr(pool, "invalidatedcount") else 0,
    }


# Optional: Log pool checkout/checkin events for debugging
@event.listens_for(engine, "checkout")
def receive_checkout(dbapi_connection, connection_record, connection_proxy):
    """Log when a connection is checked out from the pool."""
    if settings.DEBUG:
        logger.debug(f"Connection checked out from pool. Status: {get_pool_status()}")


@event.listens_for(engine, "checkin")
def receive_checkin(dbapi_connection, connection_record):
    """Log when a connection is returned to the pool."""
    if settings.DEBUG:
        logger.debug(f"Connection returned to pool. Status: {get_pool_status()}")
