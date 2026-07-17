"""Compliance re-review locks for the reduce-production v2 correction (F1/F3/F5).

F1 (approved-entry protection): an APPROVED open entry is a signed-off labor record.
It was already excluded from the quantity walk, but the operator path used to hand it
to the shared core as ``notes_entry``, which appended notes, overwrote ``source``
(kiosk tokens ALWAYS force ``"kiosk"``), and bumped ``updated_at``/``version`` -- a
mutation of approved labor outside the audited diff. Locked here: with an approved
open entry the reduce still succeeds against the caller's closed unapproved
allowance, and the approved row stays BYTE-FOR-BYTE untouched (quantity, notes,
source, updated_at, version) -- including under a kiosk-forced source. The unapproved
open entry keeps the original notes/source behavior.

F3 (per-entry audit discoverability): each walked entry gets its own
``resource_type="time_entry"`` audit row (same action, same transaction) so an
auditor sampling a specific TimeEntry surfaces the correction by resource-keyed
lookup -- the operation-level row stays the aggregate.

F5: the operation-level row's ``extra_data.path`` disambiguates the operator verb
(``"shop_floor"``) from the office verb (``"office"``).

Deliberately self-contained on ``kiosk_test_helpers`` (NOT the two reduce test
modules, which are being extended concurrently).
"""

from datetime import datetime, timedelta
from typing import Optional

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User, UserRole
from app.models.work_order import (
    OperationStatus,
    WorkOrder,
    WorkOrderOperation,
    WorkOrderStatus,
)
from tests.api.kiosk_test_helpers import (
    bearer,
    kiosk_token_for,
    make_kiosk_station,
    make_user,
    make_wo_with_operation,
    make_work_center,
    mint_badge_token,
    user_headers,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

REDUCE_ACTION = "REDUCE_OPERATION_PRODUCTION"


def reduce_url(op: WorkOrderOperation) -> str:
    return f"/api/v1/shop-floor/operations/{op.id}/reduce-production"


def office_reduce_url(op: WorkOrderOperation) -> str:
    return f"/api/v1/work-orders/operations/{op.id}/reduce-production"


def make_in_progress_wo_op(db: Session, work_center) -> tuple[WorkOrder, WorkOrderOperation]:
    wo, op = make_wo_with_operation(
        db,
        work_center=work_center,
        quantity_ordered=50,
        op_status=OperationStatus.IN_PROGRESS,
        wo_status=WorkOrderStatus.IN_PROGRESS,
    )
    return wo, op


def make_entry(
    db: Session,
    user: User,
    wo: WorkOrder,
    op: WorkOrderOperation,
    *,
    quantity_produced: float,
    open_entry: bool,
    hours_ago: float = 2.0,
    entry_type: TimeEntryType = TimeEntryType.RUN,
    approved_by: Optional[int] = None,
    notes: Optional[str] = None,
    source: Optional[str] = None,
) -> TimeEntry:
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=wo.id,
        operation_id=op.id,
        work_center_id=op.work_center_id,
        entry_type=entry_type,
        clock_in=datetime.utcnow() - timedelta(hours=hours_ago + 1),
        clock_out=None if open_entry else datetime.utcnow() - timedelta(hours=hours_ago),
        quantity_produced=quantity_produced,
        approved=(datetime.utcnow() if approved_by is not None else None),
        approved_by=approved_by,
        notes=notes,
        source=source,
        company_id=op.company_id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def snapshot_entry(db: Session, entry_id: int) -> dict:
    db.expire_all()
    row = db.get(TimeEntry, entry_id)
    return {
        "quantity_produced": row.quantity_produced,
        "quantity_scrapped": row.quantity_scrapped,
        "notes": row.notes,
        "source": row.source,
        "updated_at": row.updated_at,
        "version": row.version,
        "approved": row.approved,
        "approved_by": row.approved_by,
    }


def set_op_quantity(db: Session, op: WorkOrderOperation, value: float) -> None:
    row = db.get(WorkOrderOperation, op.id)
    row.quantity_complete = value
    db.commit()


def reduce_audit_rows(db: Session, *, resource_type: str, resource_id: int) -> list[AuditLog]:
    db.expire_all()
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.resource_type == resource_type,
            AuditLog.resource_id == resource_id,
            AuditLog.action == REDUCE_ACTION,
        )
        .order_by(AuditLog.sequence_number)
        .all()
    )


# ===========================================================================
# F1 -- approved open entry is byte-for-byte untouched
# ===========================================================================


def test_reduce_succeeds_but_approved_open_entry_untouched(client: TestClient, db_session: Session):
    """Approved open entry + closed unapproved allowance: the reduce succeeds off the
    closed session, and the approved record is not mutated in ANY column."""
    operator = make_user(db_session)
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    wc = make_work_center(db_session)
    wo, op = make_in_progress_wo_op(db_session, wc)
    closed = make_entry(db_session, operator, wo, op, quantity_produced=10, open_entry=False)
    approved_open = make_entry(
        db_session,
        operator,
        wo,
        op,
        quantity_produced=3,
        open_entry=True,
        approved_by=supervisor.id,
        notes="approved session note",
        source="desktop",
    )
    set_op_quantity(db_session, op, 13)
    before = snapshot_entry(db_session, approved_open.id)

    resp = client.post(
        reduce_url(op),
        json={
            "quantity_delta": 4,
            "reason": "over-count on the earlier session",
            "notes": "should NOT land on the approved record",
            "source": "desktop",
        },
        headers=user_headers(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    # The walk lowered ONLY the closed unapproved session.
    assert [w["time_entry_id"] for w in body["reduced_time_entries"]] == [closed.id]

    db_session.expire_all()
    assert db_session.get(TimeEntry, closed.id).quantity_produced == 6
    assert db_session.get(WorkOrderOperation, op.id).quantity_complete == 9

    # The approved open entry is BYTE-FOR-BYTE untouched: quantity, notes, source,
    # updated_at, and version all unchanged (no gratuitous dirty/version bump).
    after = snapshot_entry(db_session, approved_open.id)
    assert after == before, f"approved open entry mutated: {before} -> {after}"


def test_kiosk_forced_source_does_not_touch_approved_open_entry(client: TestClient, db_session: Session):
    """A kiosk-minted token ALWAYS forces source='kiosk'; that forced channel must not
    be stamped onto an approved open entry (nor anything else on it)."""
    wc = make_work_center(db_session)
    station = make_kiosk_station(db_session, work_center=wc)
    operator = make_user(db_session)
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    wo, op = make_in_progress_wo_op(db_session, wc)
    closed = make_entry(db_session, operator, wo, op, quantity_produced=8, open_entry=False)
    approved_open = make_entry(
        db_session,
        operator,
        wo,
        op,
        quantity_produced=2,
        open_entry=True,
        approved_by=supervisor.id,
        source="desktop",  # must NOT become 'kiosk'
    )
    set_op_quantity(db_session, op, 10)

    minted = mint_badge_token(client, kiosk_token_for(station), operator.employee_id)
    assert minted.status_code == status.HTTP_200_OK, minted.text
    kiosk_headers = bearer(minted.json()["access_token"])
    before = snapshot_entry(db_session, approved_open.id)

    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 3, "reason": "badge correction of the earlier shift"},
        headers=kiosk_headers,
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    assert db_session.get(TimeEntry, closed.id).quantity_produced == 5
    after = snapshot_entry(db_session, approved_open.id)
    assert after == before, f"kiosk-forced source leaked onto approved entry: {before} -> {after}"
    assert after["source"] == "desktop"


def test_unapproved_open_entry_still_receives_notes_and_source(client: TestClient, db_session: Session):
    """The original happy path is preserved: an UNAPPROVED open entry gets notes/source."""
    operator = make_user(db_session)
    wc = make_work_center(db_session)
    wo, op = make_in_progress_wo_op(db_session, wc)
    open_entry = make_entry(db_session, operator, wo, op, quantity_produced=5, open_entry=True)
    set_op_quantity(db_session, op, 5)

    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 2, "reason": "miscount", "notes": "corrected at the station", "source": "desktop"},
        headers=user_headers(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    row = db_session.get(TimeEntry, open_entry.id)
    assert row.quantity_produced == 3
    assert "corrected at the station" in (row.notes or "")
    assert row.source == "desktop"


# ===========================================================================
# F3 -- per-walked-entry audit rows (resource-keyed discoverability)
# ===========================================================================


def test_two_entry_walk_emits_operation_row_plus_per_entry_rows(client: TestClient, db_session: Session):
    """A 2-entry walk lands 1 aggregate op row + 2 time_entry rows, keyed and chained."""
    operator = make_user(db_session)
    wc = make_work_center(db_session)
    wo, op = make_in_progress_wo_op(db_session, wc)
    closed = make_entry(db_session, operator, wo, op, quantity_produced=5, open_entry=False)
    open_entry = make_entry(db_session, operator, wo, op, quantity_produced=2, open_entry=True)
    set_op_quantity(db_session, op, 7)

    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 6, "reason": "double-keyed across the shift"},
        headers=user_headers(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    # Walk order: open (2 -> 0), then closed (5 -> 1).
    assert [w["time_entry_id"] for w in resp.json()["reduced_time_entries"]] == [open_entry.id, closed.id]

    # ONE aggregate operation-level row.
    op_rows = reduce_audit_rows(db_session, resource_type="work_order_operation", resource_id=op.id)
    assert len(op_rows) == 1
    op_row = op_rows[0]
    assert op_row.extra_data["time_entries"] == [
        {
            "time_entry_id": open_entry.id,
            "entry_type": "run",
            "quantity_produced_before": 2,
            "quantity_produced_after": 0,
        },
        {"time_entry_id": closed.id, "entry_type": "run", "quantity_produced_before": 5, "quantity_produced_after": 1},
    ]

    # One resource-keyed row PER WALKED ENTRY -- discoverable by sampling the entry.
    open_rows = reduce_audit_rows(db_session, resource_type="time_entry", resource_id=open_entry.id)
    closed_rows = reduce_audit_rows(db_session, resource_type="time_entry", resource_id=closed.id)
    assert len(open_rows) == 1 and len(closed_rows) == 1
    assert open_rows[0].old_values == {"quantity_produced": 2}
    assert open_rows[0].new_values == {"quantity_produced": 0}
    assert closed_rows[0].old_values == {"quantity_produced": 5}
    assert closed_rows[0].new_values == {"quantity_produced": 1}
    for row in (open_rows[0], closed_rows[0]):
        assert row.extra_data["operation_id"] == op.id
        assert row.extra_data["work_order_id"] == wo.id
        assert row.extra_data["entry_user_id"] == operator.id
        assert row.extra_data["reason"] == "double-keyed across the shift"
        assert row.user_id == operator.id  # the actor

    # Chained: the three rows are consecutive on the tamper-evident hash chain
    # (same request, one unit of work) -- sequence increments and hashes link.
    trio = sorted([op_row, open_rows[0], closed_rows[0]], key=lambda r: r.sequence_number)
    for row in trio:
        assert row.integrity_hash
    for prev, nxt in zip(trio, trio[1:]):
        assert nxt.sequence_number == prev.sequence_number + 1
        assert nxt.previous_hash == prev.integrity_hash


def test_office_walk_per_entry_rows_carry_original_operators(client: TestClient, db_session: Session):
    """Cross-operator office walk: each time_entry row names the ORIGINAL operator
    (entry_user_id) while the actor (user_id) is the supervisor."""
    op_a = make_user(db_session)
    op_b = make_user(db_session)
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    wc = make_work_center(db_session)
    wo, op = make_in_progress_wo_op(db_session, wc)
    entry_a = make_entry(db_session, op_a, wo, op, quantity_produced=4, open_entry=True)
    entry_b = make_entry(db_session, op_b, wo, op, quantity_produced=5, open_entry=False)
    set_op_quantity(db_session, op, 9)

    resp = client.post(
        office_reduce_url(op),
        json={"quantity_delta": 7, "reason": "desk correction across the crew"},
        headers=user_headers(supervisor),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    rows_a = reduce_audit_rows(db_session, resource_type="time_entry", resource_id=entry_a.id)
    rows_b = reduce_audit_rows(db_session, resource_type="time_entry", resource_id=entry_b.id)
    assert len(rows_a) == 1 and len(rows_b) == 1
    assert rows_a[0].extra_data["entry_user_id"] == op_a.id
    assert rows_b[0].extra_data["entry_user_id"] == op_b.id
    assert rows_a[0].user_id == supervisor.id
    assert rows_b[0].user_id == supervisor.id


# ===========================================================================
# F5 -- path disambiguation on the audit chain
# ===========================================================================


def test_audit_rows_tag_shop_floor_path(client: TestClient, db_session: Session):
    operator = make_user(db_session)
    wc = make_work_center(db_session)
    wo, op = make_in_progress_wo_op(db_session, wc)
    make_entry(db_session, operator, wo, op, quantity_produced=5, open_entry=True)
    set_op_quantity(db_session, op, 5)

    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 1, "reason": "floor fix"},
        headers=user_headers(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    op_rows = reduce_audit_rows(db_session, resource_type="work_order_operation", resource_id=op.id)
    assert op_rows[0].extra_data["path"] == "shop_floor"


def test_audit_rows_tag_office_path(client: TestClient, db_session: Session):
    operator = make_user(db_session)
    manager = make_user(db_session, role=UserRole.MANAGER)
    wc = make_work_center(db_session)
    wo, op = make_in_progress_wo_op(db_session, wc)
    make_entry(db_session, operator, wo, op, quantity_produced=5, open_entry=False)
    set_op_quantity(db_session, op, 5)

    resp = client.post(
        office_reduce_url(op),
        json={"quantity_delta": 1, "reason": "office fix"},
        headers=user_headers(manager),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    op_rows = reduce_audit_rows(db_session, resource_type="work_order_operation", resource_id=op.id)
    assert op_rows[0].extra_data["path"] == "office"
