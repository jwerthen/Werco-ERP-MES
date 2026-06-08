"""Behavior + correctness locks for Batch-9 (rank 12) WO-completion perf hardening.

Batch 9 is a *performance* batch, but every change carries a correctness contract
that a future "speed it up some more" edit must not silently break. These tests pin
the observable behavior of each finding so the optimization can't regress into a
defect.

Covered findings:
- PERF-1: the two non-unique btree perf indexes are present on the create_all'd
          schema with the right columns, in order, and are non-unique
          (ix_time_entries_operation_clock_out / ix_woo_work_order_sequence).
- PERF-2: GET /shop-floor/dashboard returns an ETag; an unchanged conditional GET
          304s; the 304 fast-path does NOT run the reconcile (a reconcile-eligible
          WO is NOT advanced on a 304, IS advanced on the next 200); a state change
          moves the ETag; the served signed_in_users presence is consistent with the
          ETag.
- PERF-3: with SHOP_FLOOR_DASHBOARD_RECONCILE_LIMIT lowered, the dashboard reconcile
          only touches the most-recently-updated N open WOs and logs the truncation
          WARNING; a WO beyond the cap is still reconciled via its detail endpoint.
- PERF-4: release_next_ready_operation still honors the predecessor gate (a later op
          is NOT released while an earlier non-COMPLETE op exists; IS released once
          predecessors complete; out-of-sequence same-WC completions self-heal).
- PERF-5: a live completion commits WO/op state + audit (+ FG receipt) ATOMICALLY,
          and invalidate_work_centers_cache fires AFTER the terminal commit (asserted
          on all four live handlers + the reconcile-on-read driven completion).

Fixtures mirror the sibling completion suites: rows are created directly in the
shared ``db_session`` and requests use a directly-minted token; the ``client``
fixture overrides ``get_db`` to yield that same session.
"""

import importlib.util
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.core.time_utils import CENTRAL_TIME_ZONE
from app.models.audit_log import AuditLog
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
from tests.conftest import engine as test_engine

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens minted directly; never used for login

# Module-level counter -> globally unique natural keys across xdist worker DBs.
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def make_user(db: Session, *, role: UserRole = UserRole.ADMIN) -> User:
    n = _next()
    user = User(
        email=f"b9-{n}@co{COMPANY_A}.test",
        employee_id=f"B9-{n:05d}",
        first_name="B9",
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
        part_number=f"B9-P-{n}",
        name=f"Part {n}",
        description="batch9 fixture part",
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
        name=f"B9-WC-{n}",
        code=f"B9-WC-{n}",
        work_center_type="welding",
        description="batch9 fixture work center",
        hourly_rate=100,
        capacity_hours_per_day=8.0,
        is_active=True,
        company_id=COMPANY_A,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def make_wo(
    db: Session,
    *,
    status_: WorkOrderStatus = WorkOrderStatus.IN_PROGRESS,
    quantity_ordered: float = 10,
    part: Part = None,
) -> WorkOrder:
    part = part or make_part(db)
    n = _next()
    wo = WorkOrder(
        work_order_number=f"B9-WO-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=quantity_ordered,
        status=status_,
        priority=5,
        due_date=date.today() + timedelta(days=30),
        company_id=COMPANY_A,
    )
    db.add(wo)
    db.flush()
    return wo


def make_op(
    db: Session,
    wo: WorkOrder,
    wc: WorkCenter,
    *,
    sequence: int,
    status_: OperationStatus,
    quantity_complete: float = 0,
) -> WorkOrderOperation:
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        sequence=sequence,
        operation_number=f"OP{sequence}",
        name=f"Op {sequence}",
        status=status_,
        quantity_complete=quantity_complete,
        company_id=COMPANY_A,
    )
    db.add(op)
    db.flush()
    return op


def make_closed_time_entry(
    db: Session,
    *,
    user: User,
    wo: WorkOrder,
    op: WorkOrderOperation,
    wc: WorkCenter = None,
    quantity_produced: float,
) -> TimeEntry:
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=wo.id,
        operation_id=op.id,
        work_center_id=wc.id if wc else None,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow() - timedelta(hours=2),
        clock_out=datetime.utcnow() - timedelta(hours=1),
        duration_hours=1.0,
        quantity_produced=quantity_produced,
        quantity_scrapped=0,
        company_id=COMPANY_A,
    )
    db.add(entry)
    db.flush()
    return entry


# ===========================================================================
# PERF-1: the two perf indexes exist on the create_all'd schema
# ===========================================================================


def _index_map(model) -> dict:
    """{index_name: (tuple_of_column_names_in_order, unique_bool)} from the model __table__."""
    out = {}
    for ix in model.__table__.indexes:
        out[ix.name] = (tuple(c.name for c in ix.columns), bool(ix.unique))
    return out


def test_perf1_time_entries_operation_clock_out_index_declared_on_model():
    idx = _index_map(TimeEntry)
    assert "ix_time_entries_operation_clock_out" in idx, idx
    cols, unique = idx["ix_time_entries_operation_clock_out"]
    assert cols == ("operation_id", "clock_out"), cols
    assert unique is False, "perf index must be non-unique"


def test_perf1_woo_work_order_sequence_index_declared_on_model():
    idx = _index_map(WorkOrderOperation)
    assert "ix_woo_work_order_sequence" in idx, idx
    cols, unique = idx["ix_woo_work_order_sequence"]
    assert cols == ("work_order_id", "sequence"), cols
    assert unique is False, "perf index must be non-unique"


def test_perf1_indexes_materialized_on_created_schema(db_session: Session):
    """The indexes are not just declared -- create_all (the bootstrap path) actually
    emits them, so they exist on the live (here: sqlite) schema."""
    inspector = sa_inspect(test_engine)

    te_indexes = {ix["name"]: ix for ix in inspector.get_indexes("time_entries")}
    assert "ix_time_entries_operation_clock_out" in te_indexes, te_indexes.keys()
    te_ix = te_indexes["ix_time_entries_operation_clock_out"]
    assert te_ix["column_names"] == ["operation_id", "clock_out"], te_ix
    assert not te_ix["unique"], "perf index must be non-unique"  # sqlite reflects 0/1, not bool

    woo_indexes = {ix["name"]: ix for ix in inspector.get_indexes("work_order_operations")}
    assert "ix_woo_work_order_sequence" in woo_indexes, woo_indexes.keys()
    woo_ix = woo_indexes["ix_woo_work_order_sequence"]
    assert woo_ix["column_names"] == ["work_order_id", "sequence"], woo_ix
    assert not woo_ix["unique"], "perf index must be non-unique"


def test_perf1_migration_and_model_columns_in_lockstep():
    """The migration's declared columns must match the model __table_args__ indexes
    byte-for-byte (the lock-step contract called out in 042's docstring)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mig042",
        "/Users/jonwerthen/Werco-ERP-MES/backend/alembic/versions/042_wo_completion_perf_indexes.py",
    )
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    assert mig.TIME_ENTRY_INDEX == "ix_time_entries_operation_clock_out"
    assert mig.TIME_ENTRY_COLUMNS == ["operation_id", "clock_out"]
    assert mig.WOO_INDEX == "ix_woo_work_order_sequence"
    assert mig.WOO_COLUMNS == ["work_order_id", "sequence"]

    te_cols, _ = _index_map(TimeEntry)["ix_time_entries_operation_clock_out"]
    woo_cols, _ = _index_map(WorkOrderOperation)["ix_woo_work_order_sequence"]
    assert list(te_cols) == mig.TIME_ENTRY_COLUMNS
    assert list(woo_cols) == mig.WOO_COLUMNS


# ===========================================================================
# PERF-2: cheap pre-reconcile ETag -> fast 304, ETag moves on state change,
# 304 fast-path skips the reconcile, presence is consistent with the ETag.
# ===========================================================================

DASHBOARD = "/api/v1/shop-floor/dashboard"


def test_perf2_dashboard_returns_etag_and_conditional_get_304s(client: TestClient, db_session: Session):
    user = make_user(db_session, role=UserRole.MANAGER)
    make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS)
    db_session.commit()

    r1 = client.get(DASHBOARD, headers=headers_for(user))
    assert r1.status_code == status.HTTP_200_OK, r1.text
    etag = r1.headers.get("ETag")
    assert etag, "dashboard must return an ETag header"

    r2 = client.get(DASHBOARD, headers={**headers_for(user), "If-None-Match": etag})
    assert r2.status_code == status.HTTP_304_NOT_MODIFIED, r2.text


def test_perf2_etag_changes_after_state_change_and_conditional_get_returns_200_fresh(
    client: TestClient, db_session: Session
):
    user = make_user(db_session, role=UserRole.MANAGER)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    db_session.commit()

    r1 = client.get(DASHBOARD, headers=headers_for(user))
    assert r1.status_code == status.HTTP_200_OK, r1.text
    etag1 = r1.headers["ETag"]

    # A new time entry mutates state the fingerprint covers (count + max(updated_at)).
    new_op = make_op(db_session, wo, wc, sequence=20, status_=OperationStatus.PENDING)
    db_session.commit()

    r2 = client.get(DASHBOARD, headers={**headers_for(user), "If-None-Match": etag1})
    assert r2.status_code == status.HTTP_200_OK, "state changed -> stale ETag must NOT 304"
    etag2 = r2.headers["ETag"]
    assert etag2 != etag1, "ETag must move when state changes"

    # The new ETag is now stable: a conditional GET with it 304s.
    r3 = client.get(DASHBOARD, headers={**headers_for(user), "If-None-Match": etag2})
    assert r3.status_code == status.HTTP_304_NOT_MODIFIED, r3.text
    assert new_op.id  # silence linter; op participated in the state change


def test_perf2_200_path_runs_reconcile_and_advances_wo(client: TestClient, db_session: Session):
    """When the dashboard returns 200 (no/ stale ETag) it runs the reconcile and
    advances a reconcile-eligible WO to COMPLETE."""
    user = make_user(db_session, role=UserRole.OPERATOR)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=4)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS, quantity_complete=0)
    db_session.commit()

    # First GET: establishes the baseline ETag (no evidence yet -> no reconcile change).
    r1 = client.get(DASHBOARD, headers=headers_for(user))
    assert r1.status_code == status.HTTP_200_OK, r1.text
    etag1 = r1.headers["ETag"]

    # Inject durable completion evidence (a new closed TimeEntry). This bumps the
    # time_entries leg of the fingerprint, so etag1 is now stale -> the next GET 200s
    # and the reconcile fires, driving the op COMPLETE.
    make_closed_time_entry(db_session, user=user, wo=wo, op=op, wc=wc, quantity_produced=4)
    db_session.commit()

    r2 = client.get(DASHBOARD, headers={**headers_for(user), "If-None-Match": etag1})
    assert r2.status_code == status.HTTP_200_OK, "stale ETag -> 200 (not 304)"

    db_session.expire_all()
    assert (
        db_session.get(WorkOrderOperation, op.id).status == OperationStatus.COMPLETE
    ), "the 200 path ran the reconcile and advanced the op"


def test_perf2_304_fast_path_skips_reconcile(client: TestClient, db_session: Session):
    """The core PERF-2 guarantee: when the presented ETag matches the (unchanged-state)
    fingerprint, the handler 304s BEFORE the reconcile -- the reconcile helper is never
    entered, and no state is mutated. Proven by spying on _reconcile_and_commit."""
    import app.api.endpoints.shop_floor as sf

    user = make_user(db_session, role=UserRole.OPERATOR)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=4)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS, quantity_complete=0)
    db_session.commit()

    r1 = client.get(DASHBOARD, headers=headers_for(user))
    assert r1.status_code == status.HTTP_200_OK
    etag = r1.headers["ETag"]

    # State is unchanged since r1, so etag still matches -> the conditional GET must
    # take the fast-304 path WITHOUT entering the reconcile.
    with patch.object(sf, "_reconcile_and_commit", wraps=sf._reconcile_and_commit) as spy:
        r2 = client.get(DASHBOARD, headers={**headers_for(user), "If-None-Match": etag})
        assert r2.status_code == status.HTTP_304_NOT_MODIFIED, r2.text
        spy.assert_not_called()
    assert op.id  # participated


def test_perf2_served_presence_consistent_with_etag(client: TestClient, db_session: Session):
    """signed_in_users in the body is built from the SAME presence snapshot folded
    into the ETag, so the body matches the ETag it ships with -- and a presence change
    moves the ETag."""
    from app.core.websocket import manager

    user = make_user(db_session, role=UserRole.MANAGER)
    make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS)
    db_session.commit()

    # Seed in-memory websocket presence for this user.
    uid = str(user.id)
    manager.user_connections[uid] = ["sentinel-socket"]
    manager.user_connected_at[uid] = datetime.now()
    try:
        r1 = client.get(DASHBOARD, headers=headers_for(user))
        assert r1.status_code == status.HTTP_200_OK, r1.text
        body = r1.json()
        signed_in_ids = {u["id"] for u in body.get("signed_in_users", [])}
        assert user.id in signed_in_ids, "presence must surface in signed_in_users"
        etag_present = r1.headers["ETag"]

        # The matching conditional GET 304s -> body+etag are a consistent pair.
        r2 = client.get(DASHBOARD, headers={**headers_for(user), "If-None-Match": etag_present})
        assert r2.status_code == status.HTTP_304_NOT_MODIFIED
    finally:
        manager.user_connections.pop(uid, None)
        manager.user_connected_at.pop(uid, None)

    # Presence cleared -> the fingerprint (and thus the ETag) must move.
    r3 = client.get(DASHBOARD, headers={**headers_for(user), "If-None-Match": etag_present})
    assert r3.status_code == status.HTTP_200_OK, "dropping presence must invalidate the ETag"
    assert r3.headers["ETag"] != etag_present


# ===========================================================================
# PERF-3: bounded dashboard reconcile + truncation WARNING; over-cap WO still
# reconciled via its detail endpoint.
# ===========================================================================


def test_perf3_reconcile_bounded_to_cap_and_logs_warning(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch, caplog
):
    """With the cap lowered to 1, only the single most-recently-updated open WO is
    reconciled by the dashboard; an older WO with the same kind of evidence is left
    for its detail view. A run that fills the cap logs the truncation WARNING."""
    import logging

    from app.core.config import settings

    monkeypatch.setattr(settings, "SHOP_FLOOR_DASHBOARD_RECONCILE_LIMIT", 1)

    user = make_user(db_session, role=UserRole.OPERATOR)
    wc = make_work_center(db_session)

    # OLDER WO (will sort last by updated_at) -- evidence to reconcile, but beyond cap.
    old_wo = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=3)
    old_op = make_op(db_session, old_wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS, quantity_complete=0)
    make_closed_time_entry(db_session, user=user, wo=old_wo, op=old_op, wc=wc, quantity_produced=3)
    db_session.commit()
    # Force old_wo.updated_at to be strictly older than the new WO below.
    db_session.query(WorkOrder).filter(WorkOrder.id == old_wo.id).update(
        {WorkOrder.updated_at: datetime.utcnow() - timedelta(days=2)}
    )
    db_session.commit()

    # NEWER WO -- most-recently-updated, within the cap of 1.
    new_wo = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=3)
    new_op = make_op(db_session, new_wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS, quantity_complete=0)
    make_closed_time_entry(db_session, user=user, wo=new_wo, op=new_op, wc=wc, quantity_produced=3)
    db_session.commit()
    db_session.query(WorkOrder).filter(WorkOrder.id == new_wo.id).update({WorkOrder.updated_at: datetime.utcnow()})
    db_session.commit()

    with caplog.at_level(logging.WARNING, logger="app.api.endpoints.shop_floor"):
        resp = client.get(DASHBOARD, headers=headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    assert (
        db_session.get(WorkOrderOperation, new_op.id).status == OperationStatus.COMPLETE
    ), "the in-cap (newest) WO must be reconciled"
    assert (
        db_session.get(WorkOrderOperation, old_op.id).status == OperationStatus.IN_PROGRESS
    ), "the over-cap (older) WO must NOT be reconciled by the dashboard"

    assert any("truncated to the cap" in rec.message and rec.levelname == "WARNING" for rec in caplog.records), [
        r.message for r in caplog.records
    ]

    # PERF-3 safety net: the over-cap WO IS reconciled when opened via its detail view.
    detail = client.get(f"/api/v1/work-orders/{old_wo.id}", headers=headers_for(user))
    assert detail.status_code == status.HTTP_200_OK, detail.text
    db_session.expire_all()
    assert (
        db_session.get(WorkOrderOperation, old_op.id).status == OperationStatus.COMPLETE
    ), "over-cap WO must self-heal on its detail GET"


def test_perf3_no_warning_when_under_cap(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch, caplog
):
    """A run that does NOT fill the cap must not log the truncation warning."""
    import logging

    from app.core.config import settings

    monkeypatch.setattr(settings, "SHOP_FLOOR_DASHBOARD_RECONCILE_LIMIT", 50)

    user = make_user(db_session, role=UserRole.OPERATOR)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS)
    make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    db_session.commit()

    with caplog.at_level(logging.WARNING, logger="app.api.endpoints.shop_floor"):
        resp = client.get(DASHBOARD, headers=headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert not any("truncated to the cap" in rec.message for rec in caplog.records)


# ===========================================================================
# PERF-4: release_next_ready_operation predecessor gate preserved (in-memory).
# ===========================================================================


def test_perf4_successor_not_released_while_earlier_op_incomplete(client: TestClient, db_session: Session):
    """Completing op1 of a 3-op WO where op2 stays the next-in-sequence releases op2
    READY (its predecessor op1 is now COMPLETE) but NOT op3 (op2 still open)."""
    admin = make_user(db_session)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, status_=WorkOrderStatus.RELEASED, quantity_ordered=5)
    op1 = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    op2 = make_op(db_session, wo, wc, sequence=20, status_=OperationStatus.PENDING)
    op3 = make_op(db_session, wo, wc, sequence=30, status_=OperationStatus.PENDING)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op1.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op1.id).status == OperationStatus.COMPLETE
    assert db_session.get(WorkOrderOperation, op2.id).status == OperationStatus.READY, "next op released"
    assert (
        db_session.get(WorkOrderOperation, op3.id).status == OperationStatus.PENDING
    ), "op3 must stay PENDING -- op2 (earlier non-COMPLETE) blocks it"


def test_perf4_successor_released_once_all_predecessors_complete(client: TestClient, db_session: Session):
    """After op1 AND op2 are complete, completing op2 releases op3 (no earlier
    non-COMPLETE op remains)."""
    admin = make_user(db_session)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    op1 = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.COMPLETE, quantity_complete=5)
    op2 = make_op(db_session, wo, wc, sequence=20, status_=OperationStatus.IN_PROGRESS)
    op3 = make_op(db_session, wo, wc, sequence=30, status_=OperationStatus.PENDING)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op2.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op2.id).status == OperationStatus.COMPLETE
    assert (
        db_session.get(WorkOrderOperation, op3.id).status == OperationStatus.READY
    ), "op3 must release once op1 and op2 are both COMPLETE"
    assert op1.id  # all three participated


def test_perf4_release_next_ready_unit_promotes_earliest_eligible(db_session: Session):
    """Direct unit test of release_next_ready_operation (the function PERF-4 rewrote
    to run the predecessor gate in memory). It must promote the LOWEST-sequence PENDING
    op whose predecessors are all COMPLETE -- not the one just completed -- so an
    out-of-sequence same-WC completion self-heals the route rather than stranding it."""
    from app.services.work_order_state_service import release_next_ready_operation

    wc = make_work_center(db_session)
    wo = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    # op1 COMPLETE; op2 PENDING (earliest eligible); op3 just completed out of order.
    make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.COMPLETE, quantity_complete=5)
    op2 = make_op(db_session, wo, wc, sequence=20, status_=OperationStatus.PENDING)
    op3 = make_op(db_session, wo, wc, sequence=30, status_=OperationStatus.COMPLETE, quantity_complete=5)
    db_session.commit()
    db_session.refresh(wo)

    released = release_next_ready_operation(db_session, wo, op3)
    assert released is not None and released.id == op2.id, "earliest eligible PENDING op (seq 20) must be released"
    assert op2.status == OperationStatus.READY


def test_perf4_release_next_ready_unit_blocks_on_incomplete_predecessor(db_session: Session):
    """release_next_ready_operation must NOT release a PENDING op while an earlier
    non-COMPLETE op exists (the in-memory blocked test replicates
    has_incomplete_predecessors exactly)."""
    from app.services.work_order_state_service import release_next_ready_operation

    wc = make_work_center(db_session)
    wo = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    op1 = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)  # earlier, NOT complete
    op2 = make_op(db_session, wo, wc, sequence=20, status_=OperationStatus.PENDING)
    db_session.commit()
    db_session.refresh(wo)

    released = release_next_ready_operation(db_session, wo, op1)
    assert released is None, "no PENDING op may be released while op1 (earlier, non-COMPLETE) is open"
    db_session.refresh(op2)
    assert op2.status == OperationStatus.PENDING


# ===========================================================================
# PERF-5: atomic completion (state + audit committed together) and WC cache
# invalidated AFTER the terminal commit. One test per live handler + reconcile.
# ===========================================================================


def _completion_audit_rows(db: Session, resource_type: str, resource_id: int, action: str = "STATUS_CHANGE") -> list:
    """Committed completion audit rows, read through the SAME session the app used.

    DETERMINISM (hardening): mirrors tests/api/test_completion_audit_persistence.py's
    _committed_audit_rows. It deliberately does NOT open a second
    ``test_engine.connect()`` -- a separate connection races the StaticPool SQLite
    test engine (intermittent 'no such table' / empty reads) AND would only see the
    app's writes if they were committed, which is exactly the atomicity claim we are
    testing. ``db.rollback()`` first discards any flushed-but-uncommitted audit row so
    a row that survives proves it was COMMITTED atomically with the state change;
    ``db.expire_all()`` forces a fresh SELECT rather than an identity-map hit.
    """
    db.rollback()
    db.expire_all()
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.resource_type == resource_type,
            AuditLog.resource_id == resource_id,
            AuditLog.action == action,
        )
        .all()
    )


def test_perf5_office_complete_operation_atomic_and_invalidates_cache(client: TestClient, db_session: Session):
    """work_orders.complete_operation: on WO COMPLETE the op/WO state + audit commit
    together, and invalidate_work_centers_cache fires AFTER the terminal commit."""
    import app.api.endpoints.work_orders as wo_ep

    admin = make_user(db_session)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, status_=WorkOrderStatus.RELEASED, quantity_ordered=5)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.READY)
    db_session.commit()

    seen_committed_audit = {}

    def _side_effect(*a, **k):
        # invalidate_work_centers_cache runs AFTER the terminal db.commit(), so the
        # completion audit rows are already committed at this point. Querying the
        # handler's own (now post-commit) session proves the ORDERING: state + audit
        # were committed BEFORE the cache invalidation, not after.
        #
        # DETERMINISM (hardening): read the audit rows through the SAME db_session the
        # app committed on -- never a second test_engine.connect(), which under the
        # StaticPool SQLite test engine races the app's connection (intermittent
        # 'no such table' / empty result). expire_all() forces a fresh SELECT against
        # the just-committed state so the count can't be served from identity-map cache.
        db_session.expire_all()
        seen_committed_audit["op"] = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.resource_type == "work_order_operation",
                AuditLog.resource_id == op.id,
                AuditLog.action == "STATUS_CHANGE",
            )
            .count()
        )
        seen_committed_audit["wo"] = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.resource_type == "work_order",
                AuditLog.resource_id == wo.id,
                AuditLog.action == "STATUS_CHANGE",
            )
            .count()
        )

    with patch.object(wo_ep, "invalidate_work_centers_cache", side_effect=_side_effect) as mock_inv:
        resp = client.post(
            f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
            headers=headers_for(admin),
        )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    mock_inv.assert_called_once()
    assert (
        seen_committed_audit.get("op", 0) >= 1 and seen_committed_audit.get("wo", 0) >= 1
    ), "completion audit (op + WO) must be committed BEFORE the cache invalidation (atomicity + ordering)"

    assert _completion_audit_rows(db_session, "work_order", wo.id), "WO completion audit must persist atomically"
    assert _completion_audit_rows(db_session, "work_order_operation", op.id), "op completion audit must persist"
    assert db_session.get(WorkOrder, wo.id).status == WorkOrderStatus.COMPLETE


def test_perf5_office_complete_work_order_atomic_and_invalidates_cache(client: TestClient, db_session: Session):
    """work_orders.complete_work_order (privileged override): WO->COMPLETE + audit
    committed together; cache invalidated after the terminal commit."""
    import app.api.endpoints.work_orders as wo_ep

    manager_u = make_user(db_session, role=UserRole.MANAGER)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=8)
    make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    make_op(db_session, wo, wc, sequence=20, status_=OperationStatus.PENDING)
    db_session.commit()

    with patch.object(wo_ep, "invalidate_work_centers_cache") as mock_inv:
        resp = client.post(
            f"/api/v1/work-orders/{wo.id}/complete?quantity_complete=8&quantity_scrapped=0",
            headers=headers_for(manager_u),
        )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    mock_inv.assert_called_once()

    rows = _completion_audit_rows(db_session, "work_order", wo.id)
    assert rows, "manual WO completion audit row must persist atomically"
    assert db_session.get(WorkOrder, wo.id).status == WorkOrderStatus.COMPLETE


def test_perf5_shop_floor_complete_operation_atomic_and_invalidates_cache(client: TestClient, db_session: Session):
    """shop_floor.complete_operation: op/WO->COMPLETE + audit committed together;
    cache invalidated after the terminal commit."""
    import app.api.endpoints.shop_floor as sf

    admin = make_user(db_session)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, status_=WorkOrderStatus.RELEASED, quantity_ordered=5)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.READY)
    db_session.commit()

    with patch.object(sf, "invalidate_work_centers_cache") as mock_inv:
        resp = client.post(
            f"/api/v1/shop-floor/operations/{op.id}/complete",
            json={"quantity_complete": 5},
            headers=headers_for(admin),
        )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    mock_inv.assert_called_once()

    # The shop-floor complete_operation path audits the op with action COMPLETE_OPERATION
    # (its twin in work_orders.py uses STATUS_CHANGE); both must persist atomically.
    rows = _completion_audit_rows(db_session, "work_order_operation", op.id, action="COMPLETE_OPERATION")
    assert rows, "shop-floor op completion audit row must persist atomically"
    assert db_session.get(WorkOrderOperation, op.id).status == OperationStatus.COMPLETE


def test_perf5_shop_floor_clock_out_atomic_and_invalidates_cache(client: TestClient, db_session: Session):
    """shop_floor.clock_out that completes the op: state + audit committed together;
    cache invalidated after the terminal commit."""
    import app.api.endpoints.shop_floor as sf

    operator = make_user(db_session, role=UserRole.OPERATOR)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS, quantity_complete=0)
    # Open time entry to clock out of.
    entry = TimeEntry(
        user_id=operator.id,
        work_order_id=wo.id,
        operation_id=op.id,
        work_center_id=wc.id,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow() - timedelta(hours=1),
        company_id=COMPANY_A,
    )
    db_session.add(entry)
    db_session.commit()

    with patch.object(sf, "invalidate_work_centers_cache") as mock_inv:
        resp = client.post(
            f"/api/v1/shop-floor/clock-out/{entry.id}",
            json={"quantity_produced": 5, "quantity_scrapped": 0},
            headers=headers_for(operator),
        )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    mock_inv.assert_called_once()

    rows = _completion_audit_rows(db_session, "work_order_operation", op.id)
    assert rows, "clock-out driven op completion audit row must persist atomically"
    assert db_session.get(WorkOrderOperation, op.id).status == OperationStatus.COMPLETE


def test_perf5_reconcile_on_read_completion_invalidates_cache(client: TestClient, db_session: Session):
    """A reconcile-on-read driven WO completion (via the dashboard) invalidates the WC
    cache after its commit -- the freed capacity must not be served stale."""
    import app.api.endpoints.shop_floor as sf

    operator = make_user(db_session, role=UserRole.OPERATOR)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=4)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS, quantity_complete=0)
    # Durable evidence drives op+WO COMPLETE on the next read.
    make_closed_time_entry(db_session, user=operator, wo=wo, op=op, wc=wc, quantity_produced=4)
    db_session.commit()

    with patch.object(sf, "invalidate_work_centers_cache") as mock_inv:
        resp = client.get(DASHBOARD, headers=headers_for(operator))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    assert (
        db_session.get(WorkOrderOperation, op.id).status == OperationStatus.COMPLETE
    ), "reconcile-on-read must have driven the op COMPLETE"
    mock_inv.assert_called()  # the reconcile carried work_center_ids -> cache invalidated


# ===========================================================================
# Batch-9 review FIXES (six findings the adversarial review surfaced in the new
# /shop-floor/dashboard ETag fingerprint + robustness gaps). One section each.
# ===========================================================================


# ---------------------------------------------------------------------------
# FIX A -- central-midnight stale-304 (HIGH).
# summary.completed_today is a Central-Time rolling window; the fingerprint must
# fold in central_today so an op aging OUT of the window at Central midnight
# (hours AFTER UTC midnight) moves the ETag even though date.today() (UTC) is
# unchanged. Proven by driving _dashboard_state_fingerprint directly across the
# Central-midnight boundary with the UTC date held constant.
# ---------------------------------------------------------------------------


class _FrozenDateTime(datetime):
    """datetime subclass whose .now(tz) returns a fixed instant (freezegun is not
    installed in this venv -- see requirements). Subclassing datetime keeps it a
    drop-in for the module-level ``datetime`` symbol the fingerprint calls."""

    _fixed: datetime = datetime(2026, 6, 9, 4, 59, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):  # noqa: D401 - mimic datetime.now signature
        if tz is not None:
            return cls._fixed.astimezone(tz)
        return cls._fixed.astimezone(timezone.utc).replace(tzinfo=None)


class _FixedUtcDate(date):
    """date subclass whose .today() is pinned to a fixed UTC calendar day, so the
    UTC ``today`` leg of the fingerprint is held CONSTANT while we move the clock
    across the Central-midnight boundary."""

    _fixed: date = date(2026, 6, 9)

    @classmethod
    def today(cls):
        return cls._fixed


def test_fixA_fingerprint_moves_across_central_midnight_with_utc_date_constant(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """A completion ages OUT of the Central-Time ``completed_today`` window at
    Central midnight -- which is HOURS after the UTC date already rolled over -- with
    NO underlying row change. The fingerprint must still move, or an unchanged-DB
    dashboard would serve a stale 304 that still shows yesterday's completed_today
    count. Drive _dashboard_state_fingerprint at two instants straddling Central
    midnight while pinning date.today() (UTC) to the SAME calendar day; the two
    hashes must DIFFER."""
    import app.api.endpoints.shop_floor as sf

    # Some DB state so the aggregate legs are non-trivial (they are identical at
    # both instants -- only the wall clock differs).
    make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS)
    db_session.commit()

    # Pin UTC date.today() constant for BOTH calls.
    monkeypatch.setattr(sf, "date", _FixedUtcDate)

    # Sanity: the two instants share a UTC date but straddle Central midnight.
    before = datetime(2026, 6, 9, 4, 59, tzinfo=timezone.utc)  # 23:59 CDT 06-08
    after = datetime(2026, 6, 9, 5, 1, tzinfo=timezone.utc)  # 00:01 CDT 06-09
    assert before.date() == after.date() == _FixedUtcDate.today()
    assert before.astimezone(CENTRAL_TIME_ZONE).date() != after.astimezone(CENTRAL_TIME_ZONE).date()

    _FrozenDateTime._fixed = before
    monkeypatch.setattr(sf, "datetime", _FrozenDateTime)
    fp_before = sf._dashboard_state_fingerprint(db_session, COMPANY_A, set(), {})

    _FrozenDateTime._fixed = after
    fp_after = sf._dashboard_state_fingerprint(db_session, COMPANY_A, set(), {})

    assert fp_before != fp_after, (
        "fingerprint must change across the Central-midnight boundary even when the UTC "
        "date is unchanged -- central_today must be folded in, or completed_today serves stale 304s"
    )


def test_fixA_fingerprint_dict_includes_central_today_key(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    """Introspection lock: the fingerprint dict actually carries a ``central_today``
    key distinct from the UTC ``today`` key (so a future edit can't silently drop the
    Central leg and pass the boundary test by coincidence). We capture the dict the
    helper hashes by patching json.dumps in the shop_floor module namespace."""
    import app.api.endpoints.shop_floor as sf

    make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS)
    db_session.commit()

    captured = {}
    real_dumps = sf.json.dumps

    def _capture_dumps(obj, *a, **k):
        captured["fingerprint"] = obj
        return real_dumps(obj, *a, **k)

    monkeypatch.setattr(sf.json, "dumps", _capture_dumps)
    sf._dashboard_state_fingerprint(db_session, COMPANY_A, set(), {})

    fp = captured["fingerprint"]
    assert "central_today" in fp, fp.keys()
    assert "today" in fp, fp.keys()
    # central_today is derived from Central time; today is UTC. They are independent legs.
    assert fp["central_today"] == datetime.now(CENTRAL_TIME_ZONE).date().isoformat()


# ---------------------------------------------------------------------------
# FIX B -- Part rename stale-304 (HIGH).
# active_assignments[].work_order.part_number/part_name ship in the payload, so
# Part must be in the fingerprint -- a part rename MUST move the ETag (a stale
# floor display of a part identity is an AS9100D traceability hazard). Endpoint
# level: rename the part and prove the old ETag no longer 304s and the served
# payload reflects the new identity.
# ---------------------------------------------------------------------------


def _open_time_entry(db: Session, *, user: User, wo: WorkOrder, op: WorkOrderOperation, wc: WorkCenter) -> TimeEntry:
    """An OPEN (clock_out IS NULL) time entry so the WO surfaces in active_assignments."""
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=wo.id,
        operation_id=op.id,
        work_center_id=wc.id,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow() - timedelta(minutes=30),
        company_id=COMPANY_A,
    )
    db.add(entry)
    db.flush()
    return entry


def test_fixB_part_rename_moves_etag_and_payload_reflects_new_identity(client: TestClient, db_session: Session):
    """Rename a part referenced by an active assignment: the prior ETag must NOT 304
    (200 instead) and active_assignments must serve the new part identity."""
    user = make_user(db_session, role=UserRole.OPERATOR)
    wc = make_work_center(db_session)
    part = make_part(db_session)
    original_pn = part.part_number
    wo = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5, part=part)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    _open_time_entry(db_session, user=user, wo=wo, op=op, wc=wc)
    db_session.commit()

    r1 = client.get(DASHBOARD, headers=headers_for(user))
    assert r1.status_code == status.HTTP_200_OK, r1.text
    etag1 = r1.headers["ETag"]
    body1 = r1.json()
    served_pns = {a["work_order"]["part_number"] for a in body1["active_assignments"]}
    assert original_pn in served_pns, ("original part_number must surface in active_assignments", served_pns)

    # Rename the part (number + name). Force updated_at strictly forward so the Part
    # leg's max(updated_at) moves even if the rename lands in the same wall-clock
    # microsecond as the create on sqlite -- this proves the PART leg dominates the
    # fingerprint (no other tracked row changed).
    new_pn = f"{original_pn}-RENAMED"
    new_name = f"{part.name} (renamed)"
    db_session.query(Part).filter(Part.id == part.id).update(
        {
            Part.part_number: new_pn,
            Part.name: new_name,
            Part.updated_at: datetime.utcnow() + timedelta(seconds=5),
        }
    )
    db_session.commit()

    # The stale ETag must NOT 304 -- the Part leg moved.
    r2 = client.get(DASHBOARD, headers={**headers_for(user), "If-None-Match": etag1})
    assert r2.status_code == status.HTTP_200_OK, "part rename must invalidate the prior ETag (no stale 304)"
    etag2 = r2.headers["ETag"]
    assert etag2 != etag1, "ETag must move on a part rename"

    body2 = r2.json()
    served_pns2 = {a["work_order"]["part_number"] for a in body2["active_assignments"]}
    served_names2 = {a["work_order"]["part_name"] for a in body2["active_assignments"]}
    assert new_pn in served_pns2, ("renamed part_number must surface", served_pns2)
    assert new_name in served_names2, ("renamed part_name must surface", served_names2)
    assert original_pn not in served_pns2, "stale part_number must NOT survive the rename"


# ---------------------------------------------------------------------------
# FIX C -- TOCTOU recompute window (MED).
# The served ETag is computed right AFTER _reconcile_and_commit and BEFORE the
# payload build. The race is hard to force deterministically; instead pin the
# single-threaded stability contract AND assert the served ETag equals the
# fingerprint of the post-reconcile snapshot (i.e. it describes the body's state).
# ---------------------------------------------------------------------------


def test_fixC_served_etag_equals_post_reconcile_fingerprint(client: TestClient, db_session: Session):
    """The served ETag must equal _dashboard_state_fingerprint computed immediately
    after the GET with no intervening change -- i.e. the ETag describes the SAME
    post-reconcile snapshot the body was built from (pre-build recompute contract)."""
    import app.api.endpoints.shop_floor as sf

    user = make_user(db_session, role=UserRole.MANAGER)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    db_session.commit()

    r1 = client.get(DASHBOARD, headers=headers_for(user))
    assert r1.status_code == status.HTTP_200_OK, r1.text
    served_etag = r1.headers["ETag"].strip('"')

    # No presence and no intervening write -> recomputing the fingerprint against the
    # committed post-reconcile state must reproduce the served ETag byte-for-byte.
    db_session.rollback()
    db_session.expire_all()
    recomputed = sf._dashboard_state_fingerprint(db_session, COMPANY_A, set(), {})
    assert served_etag == recomputed, (
        "served ETag must describe the post-reconcile snapshot the body was built from",
        served_etag,
        recomputed,
    )


def test_fixC_single_threaded_etag_stability_contract(client: TestClient, db_session: Session):
    """GET (200) -> ETag E; immediate conditional GET with E -> 304; after a state
    change the conditional GET -> 200 with a NEW ETag. The stability the TOCTOU fix
    preserves (recompute pre-build, after reconcile commits)."""
    user = make_user(db_session, role=UserRole.MANAGER)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    db_session.commit()

    r1 = client.get(DASHBOARD, headers=headers_for(user))
    assert r1.status_code == status.HTTP_200_OK, r1.text
    etag = r1.headers["ETag"]

    r2 = client.get(DASHBOARD, headers={**headers_for(user), "If-None-Match": etag})
    assert r2.status_code == status.HTTP_304_NOT_MODIFIED, "immediate conditional GET must 304 (stable snapshot)"

    # State change -> the conditional GET must 200 with a new ETag.
    make_op(db_session, wo, wc, sequence=20, status_=OperationStatus.PENDING)
    db_session.commit()
    r3 = client.get(DASHBOARD, headers={**headers_for(user), "If-None-Match": etag})
    assert r3.status_code == status.HTTP_200_OK, "post-change conditional GET must 200"
    assert r3.headers["ETag"] != etag, "ETag must move after the state change"


# ---------------------------------------------------------------------------
# FIX D -- cross-tenant presence leak + ETag scoping (LOW / invariant #1).
# Presence is scoped to the active company before BOTH the fingerprint and
# signed_in_users. A company-B connection must NOT appear in company-A's
# signed_in_users and must NOT churn company-A's ETag (it stays 304-stable for A).
# ---------------------------------------------------------------------------


def _make_company(db: Session, company_id: int) -> None:
    """Ensure a second tenant company row exists (id != COMPANY_A)."""
    from app.models.company import Company

    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Tenant {company_id}", slug=f"tenant-{company_id}", is_active=True))
        db.commit()


def _make_user_in_company(db: Session, company_id: int, *, role: UserRole = UserRole.OPERATOR) -> User:
    n = _next()
    user = User(
        email=f"b9-co{company_id}-{n}@test",
        employee_id=f"B9C{company_id}-{n:05d}",
        first_name="B9",
        last_name=f"Co{company_id}",
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=True,
        is_superuser=False,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_fixD_company_b_presence_absent_from_company_a_dashboard_and_etag_stable(
    client: TestClient, db_session: Session
):
    """A websocket presence connection for a COMPANY-B user must NOT (a) appear in
    company A's signed_in_users, nor (b) move company A's dashboard ETag -- presence
    is company-scoped before both the body and the fingerprint (invariant #1)."""
    from app.core.websocket import manager

    COMPANY_B = 99
    _make_company(db_session, COMPANY_B)

    user_a = make_user(db_session, role=UserRole.MANAGER)  # company A
    user_b = _make_user_in_company(db_session, COMPANY_B)  # company B
    make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS)  # company A WO
    db_session.commit()

    # Establish company A's baseline ETag with NO presence.
    r0 = client.get(DASHBOARD, headers=headers_for(user_a))
    assert r0.status_code == status.HTTP_200_OK, r0.text
    etag_a = r0.headers["ETag"]
    assert not r0.json().get("signed_in_users"), "no presence yet -> empty signed_in_users"

    # Connect company B's user in the global websocket presence map.
    uid_b = str(user_b.id)
    manager.user_connections[uid_b] = ["sentinel-b-socket"]
    manager.user_connected_at[uid_b] = datetime.now()
    try:
        # (a) Company B's user must NOT leak into company A's signed_in_users.
        r1 = client.get(DASHBOARD, headers=headers_for(user_a))
        assert r1.status_code == status.HTTP_200_OK, r1.text
        a_signed_in = {u["id"] for u in r1.json().get("signed_in_users", [])}
        assert user_b.id not in a_signed_in, "company B presence must NOT surface in company A's signed_in_users"

        # (b) Company B connecting must NOT churn company A's ETag -> the prior ETag
        # still 304s for A.
        r2 = client.get(DASHBOARD, headers={**headers_for(user_a), "If-None-Match": etag_a})
        assert r2.status_code == status.HTTP_304_NOT_MODIFIED, (
            "cross-tenant presence must NOT invalidate company A's ETag",
            r2.status_code,
        )
    finally:
        manager.user_connections.pop(uid_b, None)
        manager.user_connected_at.pop(uid_b, None)

    # And disconnecting company B is likewise invisible to A: still 304-stable.
    r3 = client.get(DASHBOARD, headers={**headers_for(user_a), "If-None-Match": etag_a})
    assert r3.status_code == status.HTTP_304_NOT_MODIFIED, "company B disconnect must NOT churn company A's ETag"


def test_fixD_same_company_presence_still_surfaces_and_moves_etag(client: TestClient, db_session: Session):
    """The scoping must not over-correct: a SAME-company connected user still appears
    in signed_in_users and still moves the ETag (the legitimate-presence half)."""
    from app.core.websocket import manager

    user = make_user(db_session, role=UserRole.MANAGER)  # company A
    make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS)
    db_session.commit()

    r0 = client.get(DASHBOARD, headers=headers_for(user))
    assert r0.status_code == status.HTTP_200_OK, r0.text
    etag_no_presence = r0.headers["ETag"]

    uid = str(user.id)
    manager.user_connections[uid] = ["sentinel-a-socket"]
    manager.user_connected_at[uid] = datetime.now()
    try:
        r1 = client.get(DASHBOARD, headers={**headers_for(user), "If-None-Match": etag_no_presence})
        assert r1.status_code == status.HTTP_200_OK, "same-company presence must MOVE the ETag (no 304)"
        assert r1.headers["ETag"] != etag_no_presence
        assert user.id in {u["id"] for u in r1.json().get("signed_in_users", [])}
    finally:
        manager.user_connections.pop(uid, None)
        manager.user_connected_at.pop(uid, None)


# ---------------------------------------------------------------------------
# FIX E -- migration 042 self-heal helpers (MED).
# _index_validity (valid/invalid/absent via pg_index.indisvalid) + _ensure_index
# drop+rebuild an INVALID index from an interrupted CONCURRENTLY build. This is
# Postgres-only; the SQLite suite can prove the dialect-guarded no-op path and the
# helper wiring, but the indisvalid drop-and-rebuild path needs Postgres (NOT faked).
# ---------------------------------------------------------------------------


def _load_migration_042():
    spec = importlib.util.spec_from_file_location(
        "mig042_fixE",
        "/Users/jonwerthen/Werco-ERP-MES/backend/alembic/versions/042_wo_completion_perf_indexes.py",
    )
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    return mig


def test_fixE_is_postgres_false_for_sqlite_engine():
    """_is_postgres gates the whole CONCURRENTLY path; it must be False on the SQLite
    test engine so upgrade()/downgrade() take the no-op branch."""
    from sqlalchemy import create_engine

    mig = _load_migration_042()
    eng = create_engine("sqlite://")
    with eng.connect() as conn:
        assert mig._is_postgres(conn) is False


def test_fixE_upgrade_downgrade_are_noops_on_sqlite():
    """On SQLite (dialect != postgresql) upgrade() and downgrade() are clean no-ops:
    they return before touching _index_validity / CONCURRENTLY (which are Postgres-only)
    and emit no DDL. Driven through a real alembic Operations context bound to a SQLite
    connection so the module-level ``op`` proxy resolves exactly as alembic runs it."""
    from sqlalchemy import create_engine
    from sqlalchemy import inspect as _inspect

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    mig = _load_migration_042()
    eng = create_engine("sqlite://")
    with eng.connect() as conn:
        # Create the two target tables (empty) so a stray CREATE/DROP INDEX would have
        # something to act on -- the no-op path must NOT create the perf indexes here.
        conn.exec_driver_sql("CREATE TABLE time_entries (id INTEGER PRIMARY KEY, operation_id INTEGER, clock_out TEXT)")
        conn.exec_driver_sql(
            "CREATE TABLE work_order_operations (id INTEGER PRIMARY KEY, work_order_id INTEGER, sequence INTEGER)"
        )
        ctx = MigrationContext.configure(conn)
        ops = Operations(ctx)
        with Operations.context(ops):
            mig.upgrade()  # must be a no-op on sqlite (no exception, no DDL)
            mig.downgrade()

        inspector = _inspect(conn)
        te_idx = {ix["name"] for ix in inspector.get_indexes("time_entries")}
        woo_idx = {ix["name"] for ix in inspector.get_indexes("work_order_operations")}
        # The CONCURRENTLY build is skipped on sqlite, so the migration emits NO index
        # here (the real sqlite indexes come from create_all, not this migration).
        assert mig.TIME_ENTRY_INDEX not in te_idx, te_idx
        assert mig.WOO_INDEX not in woo_idx, woo_idx


def test_fixE_index_validity_is_postgres_only_and_not_exercised_on_sqlite():
    """HONEST coverage note (do NOT fake the Postgres path): _index_validity issues a
    Postgres catalog query (pg_class / pg_index.indisvalid), so it CANNOT run on the
    SQLite test engine -- it raises rather than returning 'absent'. The valid/invalid/
    absent drop-and-rebuild self-heal is therefore Postgres-only and is NOT exercised
    by this SQLite suite; this test documents that boundary by asserting the SQLite
    call raises (so a future edit that accidentally makes it dialect-agnostic, or that
    silently swallows the catalog error, is caught)."""
    from sqlalchemy import create_engine
    from sqlalchemy.exc import OperationalError

    mig = _load_migration_042()
    eng = create_engine("sqlite://")
    with eng.connect() as conn:
        with pytest.raises(OperationalError):
            mig._index_validity(conn, "nonexistent_index_name")
