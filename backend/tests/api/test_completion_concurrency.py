"""Concurrency-safety regression coverage for the work-order completion paths.

Locks in the Batch-2 hardening (rank 5, branch qa/full-pass-2026-06-04) that made
the shop-floor / office completion + clock endpoints safe under concurrent writers.
Three independent guards are covered here:

1. **Optimistic locking is no longer inert (LOCK-1 / invariant #4).**
   ``WorkOrderOperation`` and ``TimeEntry`` now map the ``version`` column via
   ``__mapper_args__["version_id_col"]``. The column existed at the DB level since
   migration ``004`` but was never mapped, so a stale-version UPDATE silently
   clobbered a concurrent writer. With the mapping in place SQLAlchemy emits
   ``UPDATE ... WHERE id=? AND version=?`` and raises ``StaleDataError`` when zero
   rows match. ``version_id_col`` is enforced by SQLAlchemy in the ORM layer, so it
   is exercisable directly on the sqlite test DB (two Sessions, no Postgres needed).

2. **Commit-time concurrency exceptions are translated, not 500'd.**
   The clock/completion handlers wrap their terminal ``db.commit()`` so that a
   ``StaleDataError`` (concurrent version bump) becomes HTTP 409 and an
   ``IntegrityError`` (duplicate open clock-in racing past the pre-check) becomes
   HTTP 400 -- instead of a bare 500. We assert the translation on the
   ``clock-in`` handler, whose single terminal commit makes the assertion faithful
   and non-flaky (other completion handlers call SchedulingService, which commits
   internally, so a global commit patch would fire too early there).

3. **The duplicate-open-clock-in pre-check still returns 400.**
   The DB-level partial unique index ``uq_open_time_entry`` that backs this guard
   is Postgres-only (CREATE INDEX ... WHERE clock_out IS NULL, built CONCURRENTLY)
   and is skipped on sqlite; the application-level pre-check is what we assert
   in-harness.

A single-writer happy-path test guards against the row locks / version mapping
silently changing the normal clock-out -> operation/WO rollup result.

Harness vs Postgres-only
------------------------
- In-harness (sqlite, run here): the two-Session optimistic-lock tests, the 409/400
  commit-translation tests, the duplicate-clock-in pre-check, and the happy path.
- Postgres-only (NOT asserted here): the actual ``with_for_update()`` row-lock
  serialization (a no-op on sqlite) and the ``uq_open_time_entry`` partial unique
  index (migration ``039``). The migration specialist round-tripped ``038``/``039``
  on Postgres; there is no in-repo Alembic-migration test harness to extend.

Fixtures mirror the sibling completion suites (test_completion_audit_persistence.py,
test_completion_tenant_isolation.py): rows are created directly in the shared
``db_session`` (tests/conftest.py) and requests are made with a directly-minted
token. The ``client`` fixture overrides ``get_db`` to yield that same session, so a
handler and the test share one transaction.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from app.core.security import create_access_token
from app.models.part import Part
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import (
    OperationStatus,
    WorkOrder,
    WorkOrderOperation,
    WorkOrderStatus,
)

# These conftest objects are the shared test engine / session factory. Importing
# them lets the optimistic-lock tests open a SECOND independent Session bound to
# the same DB without inventing a parallel fixture.
from tests.conftest import engine as test_engine

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1

TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens are minted directly; never used for login

# Module-level counter so every fixture row gets a globally unique natural key,
# even across tests sharing a worker DB under -n auto.
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def make_user(db: Session, *, role: UserRole = UserRole.OPERATOR) -> User:
    n = _next()
    user = User(
        email=f"comp-conc-{n}@co{COMPANY_A}.test",
        employee_id=f"CCONC-{n:05d}",
        first_name="Conc",
        last_name="CA",
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=True,
        is_superuser=False,
        company_id=COMPANY_A,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def make_part(db: Session) -> Part:
    n = _next()
    part = Part(
        part_number=f"CCONC-P-{n}",
        name=f"Part {n}",
        description="completion-concurrency fixture part",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=COMPANY_A,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_work_center(db: Session) -> WorkCenter:
    n = _next()
    wc = WorkCenter(
        name=f"CCONC-WC-{n}",
        code=f"CCONC-WC-{n}",
        work_center_type="welding",
        description="completion-concurrency fixture work center",
        hourly_rate=100,
        is_active=True,
        company_id=COMPANY_A,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def make_work_order_with_operation(
    db: Session,
    *,
    wo_status: WorkOrderStatus = WorkOrderStatus.IN_PROGRESS,
    op_status: OperationStatus = OperationStatus.IN_PROGRESS,
    quantity_ordered: float = 10,
) -> tuple[WorkOrder, WorkOrderOperation, WorkCenter]:
    part = make_part(db)
    wc = make_work_center(db)
    n = _next()
    wo = WorkOrder(
        work_order_number=f"CCONC-WO-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=quantity_ordered,
        status=wo_status,
        priority=5,
        due_date=date.today() + timedelta(days=30),
        company_id=COMPANY_A,
    )
    db.add(wo)
    db.flush()
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        sequence=10,
        operation_number="OP10",
        name="Conc Op",
        status=op_status,
        company_id=COMPANY_A,
    )
    db.add(op)
    db.commit()
    db.refresh(wo)
    db.refresh(op)
    return wo, op, wc


def make_open_time_entry(
    db: Session,
    *,
    user: User,
    work_order: WorkOrder,
    operation: WorkOrderOperation,
    work_center: WorkCenter,
    hours_ago: float = 2.0,
) -> TimeEntry:
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=work_order.id,
        operation_id=operation.id,
        work_center_id=work_center.id,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow() - timedelta(hours=hours_ago),
        company_id=COMPANY_A,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def _reload(db: Session, model, pk: int):
    db.expire_all()
    return db.query(model).filter(model.id == pk).first()


# ===========================================================================
# 1. Optimistic locking is live (LOCK-1 / invariant #4) -- pure two-Session DB
#    tests, no HTTP. These prove ``version_id_col`` is actually enforced now;
#    before the mapping was added the second commit silently overwrote the first.
# ===========================================================================


def test_work_order_operation_stale_version_update_raises_staledata(db_session: Session):
    """Two Sessions load the same operation; A commits a change (version 1 -> 2),
    then B commits its own change against the now-stale version -> StaleDataError.

    Proves the operation's optimistic lock is no longer inert: a lost-update race
    on the over-completion read-modify-write is converted into a hard failure
    instead of silently clobbering the first writer's quantity."""
    _wo, op, _wc = make_work_order_with_operation(db_session)
    op_id = op.id

    SecondSession = sessionmaker(bind=test_engine, autocommit=False, autoflush=False)
    sess_a = SecondSession()
    sess_b = SecondSession()
    try:
        op_a = sess_a.query(WorkOrderOperation).filter(WorkOrderOperation.id == op_id).one()
        op_b = sess_b.query(WorkOrderOperation).filter(WorkOrderOperation.id == op_id).one()
        start_version = op_a.version
        assert start_version == op_b.version

        # Writer A wins and bumps the version.
        op_a.quantity_complete = 5
        sess_a.commit()
        assert op_a.version == start_version + 1

        # Writer B is now stale: its UPDATE ... WHERE version=<old> matches 0 rows.
        op_b.quantity_complete = 7
        with pytest.raises(StaleDataError):
            sess_b.commit()
    finally:
        sess_a.close()
        sess_b.rollback()
        sess_b.close()

    # Writer A's value survived; B's clobber was rejected, not silently applied.
    op_final = _reload(db_session, WorkOrderOperation, op_id)
    assert float(op_final.quantity_complete) == 5.0


def test_time_entry_stale_version_update_raises_staledata(db_session: Session):
    """Same optimistic-lock proof for ``TimeEntry`` (the other newly-versioned
    completion-path model): concurrent stale write -> StaleDataError."""
    user = make_user(db_session)
    wo, op, wc = make_work_order_with_operation(db_session)
    entry = make_open_time_entry(db_session, user=user, work_order=wo, operation=op, work_center=wc)
    entry_id = entry.id

    SecondSession = sessionmaker(bind=test_engine, autocommit=False, autoflush=False)
    sess_a = SecondSession()
    sess_b = SecondSession()
    try:
        e_a = sess_a.query(TimeEntry).filter(TimeEntry.id == entry_id).one()
        e_b = sess_b.query(TimeEntry).filter(TimeEntry.id == entry_id).one()
        start_version = e_a.version
        assert start_version == e_b.version

        e_a.quantity_produced = 3
        sess_a.commit()
        assert e_a.version == start_version + 1

        e_b.quantity_produced = 9
        with pytest.raises(StaleDataError):
            sess_b.commit()
    finally:
        sess_a.close()
        sess_b.rollback()
        sess_b.close()

    e_final = _reload(db_session, TimeEntry, entry_id)
    assert float(e_final.quantity_produced) == 3.0


# ===========================================================================
# 2. Commit-time concurrency exceptions are translated (not 500'd).
#
# We drive the clock-in handler (single terminal commit, no intermediate
# SchedulingService commits) and make that commit raise once. This isolates the
# handler's translation block: StaleDataError -> 409, IntegrityError -> 400.
# ===========================================================================


def _ready_op_clock_in_body(db: Session):
    """A RELEASED WO with a READY op a fresh operator can clock in to."""
    user = make_user(db, role=UserRole.OPERATOR)
    wo, op, wc = make_work_order_with_operation(db, wo_status=WorkOrderStatus.RELEASED, op_status=OperationStatus.READY)
    body = {
        "work_order_id": wo.id,
        "operation_id": op.id,
        "work_center_id": wc.id,
        "entry_type": "run",
    }
    return user, wo, op, body


def _patch_commit_to_raise_once(db: Session, exc: Exception, monkeypatch: pytest.MonkeyPatch):
    """Make the next ``db.commit()`` raise ``exc`` exactly once, then behave
    normally. The fixtures above already committed their setup before this is
    installed, so the first commit intercepted here is the handler's own."""
    real_commit = db.commit
    state = {"fired": False}

    def flaky_commit():
        if not state["fired"]:
            state["fired"] = True
            raise exc
        return real_commit()

    monkeypatch.setattr(db, "commit", flaky_commit)


def test_clock_in_staledata_at_commit_is_409(client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch):
    """A StaleDataError surfacing at the clock-in commit (a concurrent version
    bump on the operation/WO) is translated to HTTP 409, not a 500."""
    user, _wo, _op, body = _ready_op_clock_in_body(db_session)
    _patch_commit_to_raise_once(db_session, StaleDataError("concurrent version bump"), monkeypatch)

    resp = client.post("/api/v1/shop-floor/clock-in", headers=headers_for(user), json=body)

    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert "concurrently" in resp.json()["detail"].lower()


def test_clock_in_integrityerror_at_commit_is_400(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """An IntegrityError at the clock-in commit (the uq_open_time_entry partial
    unique index rejecting a duplicate open row that raced past the pre-check) is
    translated to HTTP 400 -- the same message the pre-check returns -- not a 500."""
    user, _wo, _op, body = _ready_op_clock_in_body(db_session)
    _patch_commit_to_raise_once(
        db_session, IntegrityError("dup open clock-in", None, Exception("uq_open_time_entry")), monkeypatch
    )

    resp = client.post("/api/v1/shop-floor/clock-in", headers=headers_for(user), json=body)

    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert resp.json()["detail"] == "You are already clocked in to this operation."


# ===========================================================================
# 3. Duplicate-open-clock-in PRE-CHECK still returns 400 (application-level guard;
#    the DB index that also backs it is Postgres-only and skipped on sqlite).
# ===========================================================================


def test_second_open_clock_in_same_operation_is_400(client: TestClient, db_session: Session):
    """The same operator clocking in twice to the same operation (without clocking
    out) is rejected with HTTP 400 by the application pre-check -- and no second
    open time entry is created."""
    user = make_user(db_session, role=UserRole.OPERATOR)
    wo, op, wc = make_work_order_with_operation(
        db_session, wo_status=WorkOrderStatus.RELEASED, op_status=OperationStatus.READY
    )
    body = {
        "work_order_id": wo.id,
        "operation_id": op.id,
        "work_center_id": wc.id,
        "entry_type": "run",
    }

    first = client.post("/api/v1/shop-floor/clock-in", headers=headers_for(user), json=body)
    assert first.status_code == status.HTTP_200_OK, first.text

    second = client.post("/api/v1/shop-floor/clock-in", headers=headers_for(user), json=body)
    assert second.status_code == status.HTTP_400_BAD_REQUEST, second.text
    assert second.json()["detail"] == "You are already clocked in to this operation."

    # Exactly one OPEN entry exists for this (user, operation).
    open_count = (
        db_session.query(TimeEntry)
        .filter(
            TimeEntry.user_id == user.id,
            TimeEntry.operation_id == op.id,
            TimeEntry.clock_out.is_(None),
        )
        .count()
    )
    assert open_count == 1, "the duplicate clock-in must not create a second open entry"


# ===========================================================================
# 4. Single-writer happy path is unchanged: a normal clock-out that finishes the
#    sole operation still completes the op AND rolls the WO up to COMPLETE. Guards
#    against the version mapping / row locks regressing the normal result.
# ===========================================================================


def test_single_writer_clock_out_completes_operation_and_rolls_up_work_order(client: TestClient, db_session: Session):
    """An uncontended clock-out producing the full ordered quantity completes the
    operation and (as the only operation) the work order -- the version columns
    are bumped but the functional outcome is exactly as before."""
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wo, op, wc = make_work_order_with_operation(
        db_session,
        wo_status=WorkOrderStatus.IN_PROGRESS,
        op_status=OperationStatus.IN_PROGRESS,
        quantity_ordered=10,
    )
    op_version_before = op.version
    entry = make_open_time_entry(db_session, user=operator, work_order=wo, operation=op, work_center=wc)

    resp = client.post(
        f"/api/v1/shop-floor/clock-out/{entry.id}",
        headers=headers_for(operator),
        json={"quantity_produced": 10, "quantity_scrapped": 0},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    op_after = _reload(db_session, WorkOrderOperation, op.id)
    wo_after = _reload(db_session, WorkOrder, wo.id)
    entry_after = _reload(db_session, TimeEntry, entry.id)

    assert op_after.status == OperationStatus.COMPLETE
    assert op_after.actual_end is not None
    assert float(op_after.quantity_complete) == 10.0
    assert wo_after.status == WorkOrderStatus.COMPLETE
    assert wo_after.actual_end is not None
    assert entry_after.clock_out is not None
    assert float(entry_after.quantity_produced) == 10.0
    # The mutation went through the versioned UPDATE path: version advanced.
    assert op_after.version > op_version_before


# ===========================================================================
# 5. Reconcile-on-READ never 500s on a concurrent-version conflict (B1).
#
# ``reconcile_work_orders_from_completion_evidence`` runs inside GET/list/
# dashboard handlers and mutates version-mapped operation rows, which they then
# commit. Now that ``version_id_col`` is live, that commit can raise
# ``StaleDataError`` when another writer bumped the same rows first. On a READ a
# benign reconcile conflict must NOT become a 500 (and must NOT 409 either) --
# the handler's ``_reconcile_and_commit`` helper rolls the reconcile back and
# serves the read against the freshest committed state, returning 200.
#
# We force the conflict by making the reconcile commit raise StaleDataError
# exactly once (the same single-shot commit patch the 409/400 tests use). The
# fixtures below create durable completion evidence (a closed time entry with
# quantity_produced > the operation's recorded quantity_complete) so the
# reconcile actually mutates a row and therefore actually issues the commit that
# the patch intercepts -- otherwise the commit never fires and the test is inert.
# ===========================================================================


def _wo_with_reconcilable_completion_evidence(db: Session) -> tuple[User, WorkOrder, WorkOrderOperation]:
    """A WO whose operation has durable completion evidence that the reconcile
    will write back (closed time entry produced 10; op.quantity_complete is 0).

    The part is a plain manufactured part with no BOM, so the component-quantity
    reconcile in ``get_work_order`` is a no-op and the FIRST (and only) commit the
    handler issues is the completion-evidence reconcile -- the one we patch."""
    user = make_user(db, role=UserRole.OPERATOR)
    wo, op, wc = make_work_order_with_operation(
        db,
        wo_status=WorkOrderStatus.IN_PROGRESS,
        op_status=OperationStatus.IN_PROGRESS,
        quantity_ordered=10,
    )
    # Closed entry that produced the full quantity; op.quantity_complete is still
    # 0, so reconcile will raise it to 10 -> a real mutation -> a real commit.
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=wo.id,
        operation_id=op.id,
        work_center_id=wc.id,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow() - timedelta(hours=2),
        clock_out=datetime.utcnow() - timedelta(hours=1),
        quantity_produced=10,
        company_id=COMPANY_A,
    )
    db.add(entry)
    db.commit()
    return user, wo, op


def test_get_work_order_reconcile_staledata_at_commit_is_200_not_500(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """GET /work-orders/{id}: a StaleDataError at the reconcile-on-read commit is
    swallowed (benign, idempotent) -- the read still returns 200 with the work
    order, never a 500."""
    user, wo, _op = _wo_with_reconcilable_completion_evidence(db_session)
    _patch_commit_to_raise_once(db_session, StaleDataError("concurrent reconcile race"), monkeypatch)

    resp = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(user))

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["id"] == wo.id
    assert body["work_order_number"] == wo.work_order_number
    # The reconcile commit was rolled back, so the patch fired exactly once and
    # the read was served against the freshest committed state.


def test_shop_floor_dashboard_reconcile_staledata_at_commit_is_200_not_500(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """GET /shop-floor/dashboard: a StaleDataError at the reconcile-on-read commit
    is swallowed -- the dashboard still returns 200, never a 500."""
    user, _wo, _op = _wo_with_reconcilable_completion_evidence(db_session)
    _patch_commit_to_raise_once(db_session, StaleDataError("concurrent reconcile race"), monkeypatch)

    resp = client.get("/api/v1/shop-floor/dashboard", headers=headers_for(user))

    assert resp.status_code == status.HTTP_200_OK, resp.text
    # Dashboard returns its aggregate payload; the read survived the benign
    # reconcile conflict instead of 500'ing.
    assert isinstance(resp.json(), dict)
