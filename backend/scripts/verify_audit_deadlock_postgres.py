"""Prove on real PostgreSQL that a deadlock victim's request fails instead of answering 200.

The defect this guards (fixed together with this script). ``AuditService.log`` took the global
audit-chain advisory lock inside an ``except Exception`` that swallowed everything. Suppose a
request holding a row lock waits there, while another writer that audited first (so it holds the
chain lock) waits for that row. Postgres breaks the cycle by aborting one of them (40P01). When
the victim was the request, the error was swallowed, and SQLAlchemy still reported the session
ACTIVE. ``Session.commit()`` then sent COMMIT; Postgres answered it with a ROLLBACK, and psycopg2
raised nothing. The route answered 200 for a write that was never saved and has no audit row.

SQLite, the unit-test backend, has neither the lock nor aborted-transaction semantics, so this
runs where it can be real. Every scenario builds the cycle deterministically: writer B audits
first (holding the chain lock) and waits until the request is blocked on that lock (``pg_locks``)
before it asks for the request's row. B runs with ``deadlock_timeout`` 10s and the request with
the server default of 1s, so the request is always the victim. Each scenario then asserts that
the victim's request failed with the app-wide 409, that nothing it wrote is in the database,
that B's write landed, and that a retry succeeds on the same pool with its audit row.

1. ``PUT /work-orders/{id}/priority`` and ``PUT /work-orders/{id}`` (UPDATEs, audited with
   ``log_update``).
2. ``PUT /scheduling/work-centers/{id}/calendar`` (an INSERT, audited with ``log_create``).
2b. ``POST /work-orders/`` (an INSERT whose creation audit commits in the same transaction as
   the work order). The request holds a key-share lock on the part its new work order references
   (the foreign-key check) while it waits on the chain lock, and B wants that part row. The
   victim's work order must not exist afterwards: before the fix the work order committed first
   and only its audit row was written later, so a 409 there left a work order a retry duplicated.
   In 1, 2 and 2b the request must fail at the audit write itself, because ``AuditService.log``
   re-raises the 40P01; the COMMIT guard must not have had to act.
3. The COMMIT guard on its own: the same priority request with ``AuditService.log`` wrapped in
   a swallowing ``try/except`` (the shape of a legacy best-effort caller). Here only the guard in
   ``app.db.database`` stands between the victim and a silent 200.
4. The guard at session level: a swallowed 40P01, then ``commit()`` raises
   ``AbortedTransactionCommitError``, carrying SQLSTATE 40P01, and has already ended the server
   transaction, so a session that is only closed returns a clean connection to the pool.

Non-blocking, like every ``verify_*_postgres`` step in ``.github/workflows/e2e.yml``. It runs in
a scratch database created beside ``DATABASE_URL`` and dropped afterwards, and refuses anything
but a local disposable PostgreSQL (``scripts._pg_verify``)::

    ENVIRONMENT=test DATABASE_URL=postgresql://user:pw@localhost:5432/db \\
        python -m scripts.verify_audit_deadlock_postgres
"""

import logging
import os
import sys
import threading
import time
from unittest.mock import patch
from uuid import uuid4

import psycopg2.extensions
import sqlalchemy as sa
from sqlalchemy import text

from scripts._pg_verify import require_local_test_postgres, scratch_database

# The request runs on the server default deadlock_timeout (1s); B waits ten times longer, so the
# request's deadlock check always fires first and the request is the victim.
B_DEADLOCK_TIMEOUT = "10s"
WAIT_FOR_REQUEST_SECONDS = 20.0


class _Captured(logging.Handler):
    """Collects formatted records from the loggers it is attached to."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def main() -> None:
    url = require_local_test_postgres(os.environ.get("DATABASE_URL", ""))
    if "app.core.config" in sys.modules:
        raise RuntimeError("app settings were imported before the scratch database was selected")
    with scratch_database(url, "audit_deadlock_") as scratch_url:
        os.environ["DATABASE_URL"] = scratch_url.render_as_string(hide_password=False)
        _verify(scratch_url.database)
    print("audit deadlock verification passed: every victim request failed loudly; nothing it wrote was saved")


def _verify(scratch_name: str) -> None:
    from fastapi.testclient import TestClient

    from app.core.config import settings
    from app.core.security import create_access_token
    from app.db.database import AbortedTransactionCommitError, Base, SessionLocal, engine
    from app.db.locks import is_lost_lock_race
    from app.db.mixins import STALE_WRITE_CONFLICT_DETAIL
    from app.main import app
    from app.models.company import Company
    from app.models.part import Part
    from app.models.user import User, UserRole
    from app.models.work_center import WorkCenter
    from app.models.work_order import WorkOrder
    from app.services.audit_service import _AUDIT_CHAIN_LOCK_KEY, AuditService

    if engine.url.database != scratch_name:
        raise RuntimeError(f"the app engine points at {engine.url.database!r}, not the scratch database")
    if engine.dialect.name != "postgresql":
        raise RuntimeError("the app engine is not PostgreSQL")
    if not settings.AUDIT_HASH_CHAIN_ENABLED:
        raise RuntimeError("AUDIT_HASH_CHAIN_ENABLED is off: there is no chain lock to deadlock on")

    Base.metadata.create_all(engine)
    tag = uuid4().hex[:8]
    with SessionLocal() as db:
        company = Company(name=f"Deadlock verify {tag}", slug=f"deadlock-{tag}", is_active=True)
        db.add(company)
        db.flush()
        user = User(
            company_id=company.id,
            email=f"deadlock-{tag}@example.test",
            employee_id=f"DL-{tag}",
            first_name="Synthetic",
            last_name="Scheduler",
            hashed_password="unused-test-fixture",
            role=UserRole.ADMIN,
            is_active=True,
        )
        db.add(user)
        db.commit()
        company_id, user_id = company.id, user.id

    headers = {
        "Authorization": f"Bearer {create_access_token(subject=user_id, company_id=company_id)}",
        "X-Requested-With": "XMLHttpRequest",
    }
    counter = iter(range(1, 1000))

    def new_work_order() -> int:
        with SessionLocal() as db:
            wo = WorkOrder(
                company_id=company_id,
                work_order_number=f"WO-DL-{tag}-{next(counter)}",
                quantity_ordered=5,
                priority=5,
                work_order_type="laser_cutting",
            )
            db.add(wo)
            db.commit()
            return wo.id

    def new_work_center() -> int:
        with SessionLocal() as db:
            wc = WorkCenter(
                company_id=company_id,
                code=f"DL{tag[:4]}{next(counter)}",
                name="Deadlock verify",
                work_center_type="laser",
                is_active=True,
            )
            db.add(wc)
            db.commit()
            return wc.id

    def new_part() -> int:
        with SessionLocal() as db:
            part = Part(
                company_id=company_id,
                part_number=f"DL-PART-{tag}-{next(counter)}",
                name="Deadlock verify",
                part_type="manufactured",
                unit_of_measure="each",
                is_active=True,
            )
            db.add(part)
            db.commit()
            return part.id

    def scalar(sql: str, **params):
        with engine.connect() as conn:
            return conn.execute(text(sql), params).scalar()

    def audit_rows(resource_type: str, resource_id=None) -> int:
        if resource_id is None:
            return scalar("SELECT count(*) FROM audit_logs WHERE resource_type = :t", t=resource_type)
        return scalar(
            "SELECT count(*) FROM audit_logs WHERE resource_type = :t AND resource_id = :i",
            t=resource_type,
            i=resource_id,
        )

    def request_waits_on_chain_lock() -> bool:
        return bool(
            scalar(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
                "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
            )
        )

    def writer_b(row_sql: str, params: dict) -> dict:
        """Start writer B: audit now (taking the chain lock), then want the request's row.

        Returns the outcome dict the thread fills in. B joins the cycle only once the request is
        seen waiting on the chain lock, so the request already holds its row lock by then.
        """
        outcome: dict = {}
        b = SessionLocal()
        b.execute(text(f"SET LOCAL deadlock_timeout = '{B_DEADLOCK_TIMEOUT}'"))
        AuditService(b, None, None, company_id=company_id).log(
            action="UPDATE", resource_type="deadlock_verify_b", description="B audits first"
        )

        def run() -> None:
            try:
                deadline = time.monotonic() + WAIT_FOR_REQUEST_SECONDS
                while not request_waits_on_chain_lock():
                    if time.monotonic() > deadline:
                        outcome["result"] = "the request never waited on the audit-chain lock"
                        b.rollback()
                        return
                    time.sleep(0.02)
                b.execute(text(row_sql), params)
                b.commit()
                outcome["result"] = "committed"
            except Exception as exc:  # reported, then asserted on by the caller
                outcome["result"] = f"raised {type(exc).__name__}: {exc}"
                b.rollback()
            finally:
                b.close()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        outcome["thread"] = thread
        return outcome

    def finish_b(outcome: dict, label: str) -> None:
        outcome["thread"].join(WAIT_FOR_REQUEST_SECONDS + 15)
        if outcome.get("result") != "committed":
            raise AssertionError(f"{label}: writer B did not commit ({outcome.get('result')})")

    # The 409 handler (app.main) logs the SQLSTATE it answered; the COMMIT guard (app.db.database)
    # logs every COMMIT it refuses. Together they say WHERE a victim's request failed.
    captured = _Captured()
    watched = [logging.getLogger("app.main"), logging.getLogger("app.db.database")]
    for watched_logger in watched:
        watched_logger.addHandler(captured)
    client = TestClient(app, raise_server_exceptions=False)
    try:
        # Warm the pool and the auth path, so the first scenario's timing is not the cold start.
        warm = client.get("/api/v1/work-orders/", headers=headers)
        assert warm.status_code == 200, f"warm-up GET failed: {warm.status_code} {warm.text}"

        def expect_lost_race(label: str, response, *, at_commit: bool) -> None:
            """The victim answered the lost-race 409, and failed where it should have.

            ``at_commit`` False: the audit write itself raised (``AuditService.log`` re-raises the
            40P01), so the COMMIT guard never had to act. True: something swallowed the error and
            the guard refused the COMMIT.
            """
            if response.status_code == 200:
                raise AssertionError(
                    f"{label}: the deadlock victim's request answered 200. Its write was rolled back and "
                    "it has no audit row: the silent-commit defect is present."
                )
            if response.status_code != 409 or response.json().get("detail") != STALE_WRITE_CONFLICT_DETAIL:
                raise AssertionError(
                    f"{label}: expected the lost-lock-race 409, got {response.status_code} {response.text}"
                )
            if not any("SQLSTATE 40P01" in message for message in captured.messages):
                raise AssertionError(f"{label}: the 409 was not a deadlock (no 40P01 logged): {captured.messages}")
            refused_commit = any("Refused COMMIT" in message for message in captured.messages)
            if refused_commit != at_commit:
                where = "at COMMIT (the guard)" if refused_commit else "before COMMIT"
                raise AssertionError(f"{label}: the request failed {where}, not where expected: {captured.messages}")
            captured.messages.clear()

        # -- 1. PUT /work-orders/{id}/priority -------------------------------------------------
        label = "1 priority"
        wo_id = new_work_order()
        b = writer_b("UPDATE work_orders SET notes = 'B' WHERE id = :i", {"i": wo_id})
        response = client.put(
            f"/api/v1/work-orders/{wo_id}/priority", json={"priority": 1, "reason": "rush"}, headers=headers
        )
        finish_b(b, label)
        expect_lost_race(label, response, at_commit=False)
        assert scalar("SELECT priority FROM work_orders WHERE id = :i", i=wo_id) == 5, f"{label}: priority saved"
        assert scalar("SELECT notes FROM work_orders WHERE id = :i", i=wo_id) == "B", f"{label}: B's write missing"
        assert audit_rows("work_order", wo_id) == 0, f"{label}: an audit row exists for a rolled-back write"
        retry = client.put(
            f"/api/v1/work-orders/{wo_id}/priority", json={"priority": 1, "reason": "rush"}, headers=headers
        )
        assert retry.status_code == 200, f"{label}: retry failed: {retry.status_code} {retry.text}"
        assert scalar("SELECT priority FROM work_orders WHERE id = :i", i=wo_id) == 1, f"{label}: retry not saved"
        assert audit_rows("work_order", wo_id) == 1, f"{label}: the retry has no audit row"
        print(f"{label}: victim answered 409, nothing saved; retry 200 with its audit row")

        # -- 1b. PUT /work-orders/{id} -----------------------------------------------------------
        label = "1b work order update"
        wo_id = new_work_order()
        body = {"version": 1, "notes": "A-edit", "priority": 2}
        b = writer_b("UPDATE work_orders SET special_instructions = 'B' WHERE id = :i", {"i": wo_id})
        response = client.put(f"/api/v1/work-orders/{wo_id}", json=body, headers=headers)
        finish_b(b, label)
        expect_lost_race(label, response, at_commit=False)
        saved = "SELECT notes, priority, special_instructions FROM work_orders WHERE id = :i"
        with engine.connect() as conn:
            row = conn.execute(text(saved), {"i": wo_id}).one()
        assert (row.notes, row.priority) == (None, 5), f"{label}: the rolled-back edit was saved: {row}"
        assert row.special_instructions == "B", f"{label}: B's write missing"
        assert audit_rows("work_order", wo_id) == 0, f"{label}: an audit row exists for a rolled-back write"
        retry = client.put(f"/api/v1/work-orders/{wo_id}", json=body, headers=headers)
        assert retry.status_code == 200, f"{label}: retry failed: {retry.status_code} {retry.text}"
        assert scalar("SELECT notes FROM work_orders WHERE id = :i", i=wo_id) == "A-edit", f"{label}: retry not saved"
        assert audit_rows("work_order", wo_id) >= 1, f"{label}: the retry has no audit row"
        print(f"{label}: victim answered 409, nothing saved; retry 200 with its audit row")

        # -- 2. PUT /scheduling/work-centers/{id}/calendar -------------------------------------
        label = "2 calendar"
        wc_id = new_work_center()
        payload = {"expected_version": 0, "weekly_hours": [8.0] * 7, "overrides": []}
        b = writer_b("UPDATE work_centers SET description = 'B' WHERE id = :i", {"i": wc_id})
        response = client.put(f"/api/v1/scheduling/work-centers/{wc_id}/calendar", json=payload, headers=headers)
        finish_b(b, label)
        expect_lost_race(label, response, at_commit=False)
        calendars = "SELECT count(*) FROM working_calendars WHERE work_center_id = :i"
        assert scalar(calendars, i=wc_id) == 0, f"{label}: the calendar row was saved"
        assert scalar("SELECT description FROM work_centers WHERE id = :i", i=wc_id) == "B", f"{label}: B missing"
        assert audit_rows("working_calendar") == 0, f"{label}: an audit row exists for a rolled-back insert"
        retry = client.put(f"/api/v1/scheduling/work-centers/{wc_id}/calendar", json=payload, headers=headers)
        assert retry.status_code == 200, f"{label}: retry failed: {retry.status_code} {retry.text}"
        assert scalar(calendars, i=wc_id) == 1, f"{label}: the retry's calendar row is missing"
        assert audit_rows("working_calendar") == 1, f"{label}: the retry has no audit row"
        print(f"{label}: victim answered 409, nothing saved; retry 200 with its audit row")

        # -- 2b. POST /work-orders/ -------------------------------------------------------------
        label = "2b work order create"
        part_id = new_part()
        # The request's INSERT takes FOR KEY SHARE on the part (the foreign-key check), which only
        # FOR UPDATE conflicts with -- a plain UPDATE of a non-key column would not wait on it.
        b = writer_b(
            "WITH locked AS (SELECT id FROM parts WHERE id = :i FOR UPDATE) "
            "UPDATE parts SET description = 'B' FROM locked WHERE parts.id = locked.id",
            {"i": part_id},
        )
        body = {"part_id": part_id, "quantity_ordered": 3}
        response = client.post("/api/v1/work-orders/", json=body, headers=headers)
        finish_b(b, label)
        expect_lost_race(label, response, at_commit=False)
        created = "SELECT count(*) FROM work_orders WHERE part_id = :p"
        assert scalar(created, p=part_id) == 0, f"{label}: the victim's work order was saved"
        creates = "SELECT count(*) FROM audit_logs WHERE resource_type = 'work_order' AND action = 'CREATE'"
        assert scalar(creates) == 0, f"{label}: a creation audit row exists for a rolled-back work order"
        assert scalar("SELECT description FROM parts WHERE id = :i", i=part_id) == "B", f"{label}: B's write missing"
        retry = client.post("/api/v1/work-orders/", json=body, headers=headers)
        assert retry.status_code == 201, f"{label}: retry failed: {retry.status_code} {retry.text}"
        assert scalar(created, p=part_id) == 1, f"{label}: the retry's work order is missing"
        new_id = retry.json()["id"]
        assert audit_rows("work_order", new_id) == 1, f"{label}: the retry has no creation audit row"
        print(f"{label}: victim answered 409, no work order saved; retry 201 with its audit row")

        # -- 3. The COMMIT guard alone, behind a caller that swallows the audit error ----------
        label = "3 guard via HTTP"
        real_log = AuditService.log

        def swallowing_log(self, *args, **kwargs):
            try:
                return real_log(self, *args, **kwargs)
            except Exception:  # a legacy best-effort wrapper: exactly what the guard is for
                return None

        wo_id = new_work_order()
        b = writer_b("UPDATE work_orders SET notes = 'B' WHERE id = :i", {"i": wo_id})
        with patch.object(AuditService, "log", swallowing_log):
            response = client.put(
                f"/api/v1/work-orders/{wo_id}/priority", json={"priority": 2, "reason": "rush"}, headers=headers
            )
        finish_b(b, label)
        expect_lost_race(label, response, at_commit=True)
        assert scalar("SELECT priority FROM work_orders WHERE id = :i", i=wo_id) == 5, f"{label}: priority saved"
        assert audit_rows("work_order", wo_id) == 0, f"{label}: an audit row exists for a rolled-back write"
        print(f"{label}: the swallowed deadlock reached COMMIT and was refused with 409")

        # -- 4. The guard at session level ----------------------------------------------------
        label = "4 guard via session"
        wc_id = new_work_center()
        b = writer_b("UPDATE work_centers SET description = 'B' WHERE id = :i", {"i": wc_id})
        a = SessionLocal()
        try:
            a.execute(text("UPDATE work_centers SET description = 'A' WHERE id = :i"), {"i": wc_id})
            driver_connection = a.connection().connection.dbapi_connection
            swallowed = None
            try:
                a.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _AUDIT_CHAIN_LOCK_KEY})
            except sa.exc.DBAPIError as exc:
                swallowed = exc
            if swallowed is None or getattr(swallowed.orig, "pgcode", None) != "40P01":
                raise AssertionError(f"{label}: expected the chain-lock wait to lose a deadlock, got {swallowed!r}")
            try:
                a.commit()
            except AbortedTransactionCommitError as exc:
                refused = exc
            else:
                raise AssertionError(f"{label}: commit() returned normally for an aborted transaction")
            # Close-only cleanup (no rollback), as ``with SessionLocal() as db:`` does: the guard
            # must already have ended the server transaction, or the pool would hand this
            # connection to the next checkout still aborted.
            status = driver_connection.info.transaction_status
            if status != psycopg2.extensions.TRANSACTION_STATUS_IDLE:
                raise AssertionError(f"{label}: the refused transaction was left open (libpq status {status})")
        finally:
            a.close()
        finish_b(b, label)
        assert refused.orig.pgcode == "40P01", f"{label}: the refusal lost the cause: {refused.orig!r}"
        assert is_lost_lock_race(refused), f"{label}: the refusal is not recognized as a lost lock race"
        assert scalar("SELECT description FROM work_centers WHERE id = :i", i=wc_id) == "B", f"{label}: A saved"
        print(f"{label}: commit() raised AbortedTransactionCommitError (SQLSTATE 40P01); connection left idle")
    finally:
        for watched_logger in watched:
            watched_logger.removeHandler(captured)
        client.close()
        engine.dispose()


if __name__ == "__main__":
    main()
