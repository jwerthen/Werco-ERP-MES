"""Committed-only audit-persistence coverage for the work-orders endpoints.

Locks in the hardening on branch qa/full-pass-2026-06-04 that moved every
``AuditService`` call in ``app.api.endpoints.work_orders`` to BEFORE the
handler's terminal ``db.commit()``. ``AuditService.log()`` only ``flush()``es --
the handler owns the ``commit()``. The previous (buggy) shape logged the audit
row AFTER ``db.commit()``: the row was flushed into a fresh, never-committed
transaction and was therefore silently discarded on request teardown, leaving
the state change with no audit trail (an AS9100D / CMMC AU-3.3.8 violation).

Why a naive test does NOT catch that bug
----------------------------------------
The ``client`` fixture (tests/conftest.py) overrides ``get_db`` to yield ONE
shared, never-closed ``db_session``. The endpoint and the test share a single
open transaction, so a plain ``db.query(AuditLog)`` in the test SEES a
flushed-but-uncommitted row -- a naive assertion passes even against the bug.

The guard, proven in tests/api/test_qms_soft_delete_audit.py, is to
``db.rollback()`` BEFORE querying ``AuditLog``: a COMMITTED audit row survives
the rollback (its transaction already ended), while a flushed-only row is
discarded. The entity itself was committed by the handler in both the buggy and
fixed versions, so only the audit row's durability is being probed.

Coverage (one assertion per fixed handler's audit action):
- create_work_order   -> COMMITTED CREATE row        (resource_type 'work_order')
- update_work_order   -> COMMITTED UPDATE row
- delete_work_order   -> COMMITTED DELETE row         (default soft-delete path)
- restore_work_order  -> COMMITTED RESTORE row        (log_update action='restore')
- release_work_order  -> COMMITTED STATUS_CHANGE row  (draft -> released)

Every assertion checks action, resource_type, resource_id, and the tenant
``company_id`` tag. We never INSERT ``AuditLog`` rows (tamper-evident hash
chain) -- they are produced by the endpoints and only read back here. The
default seeded company is id=1 (tests/conftest.py).
"""

from datetime import date, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1

# Module-level counter so every fixture row gets a globally unique natural key,
# even across tests sharing a worker DB under -n auto.
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _make_user(db: Session, *, company_id: int = COMPANY_A, role: UserRole = UserRole.ADMIN) -> User:
    n = _next()
    user = User(
        email=f"wo-audit-{n}@co{company_id}.test",
        employee_id=f"WOAUD-{n:05d}",
        first_name="Wo",
        last_name=f"C{company_id}",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",  # tokens are minted directly; never used for login
        role=role,
        is_active=True,
        is_superuser=False,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _make_part(db: Session, *, company_id: int = COMPANY_A) -> Part:
    n = _next()
    part = Part(
        part_number=f"WOAUD-P-{n}",
        name=f"Part {n}",
        description="audit-persistence fixture part",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def _make_work_center(db: Session, *, company_id: int = COMPANY_A) -> WorkCenter:
    n = _next()
    wc = WorkCenter(
        name=f"WOAUD-WC-{n}",
        code=f"WOAUD-WC-{n}",
        work_center_type="welding",
        description="audit-persistence fixture work center",
        hourly_rate=100,
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def _make_work_order(
    db: Session,
    *,
    part: Part,
    company_id: int = COMPANY_A,
    status_value: WorkOrderStatus = WorkOrderStatus.DRAFT,
    with_operation: bool = False,
    work_center: WorkCenter = None,
    is_deleted: bool = False,
) -> WorkOrder:
    n = _next()
    work_order = WorkOrder(
        work_order_number=f"WOAUD-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=10,
        status=status_value,
        priority=5,
        due_date=date.today() + timedelta(days=30),
        company_id=company_id,
        is_deleted=is_deleted,
    )
    db.add(work_order)
    db.flush()
    if with_operation:
        operation = WorkOrderOperation(
            work_order_id=work_order.id,
            work_center_id=work_center.id if work_center else None,
            sequence=10,
            name="Audit Op",
            status=OperationStatus.PENDING,
            company_id=company_id,
        )
        db.add(operation)
    db.commit()
    db.refresh(work_order)
    return work_order


def _audit_rows(db: Session, *, resource_type: str, resource_id: int, action: str = None):
    """Fetch AuditLog rows for a resource, newest first, optionally filtered by action."""
    db.expire_all()
    q = db.query(AuditLog).filter(
        AuditLog.resource_type == resource_type,
        AuditLog.resource_id == resource_id,
    )
    if action is not None:
        q = q.filter(AuditLog.action == action)
    return q.order_by(AuditLog.sequence_number.desc()).all()


def _committed_audit_rows(db: Session, *, resource_type: str, resource_id: int, action: str = None):
    """Fetch AuditLog rows that were actually COMMITTED, not merely flushed.

    This is the real guard against the production bug. The ``client`` fixture
    overrides ``get_db`` to yield ONE shared, never-closed ``db_session``, so the
    endpoint and the test share a single open transaction. ``AuditService.log()``
    only ``flush()``es; the handler owns the ``commit()``. If a handler called the
    audit helper AFTER ``db.commit()`` (the bug that was just fixed), the audit row
    would be flushed into a fresh, never-committed transaction -- yet a plain
    ``db.query(AuditLog)`` in the test would still SEE it, because the read happens
    inside that same open transaction. So a naive assertion passes against broken
    code.

    Rolling back BEFORE querying closes that loophole: a committed audit row
    survives the rollback (commit already ended its transaction), while a
    flushed-but-uncommitted one is discarded. The entity itself was committed by
    the handler in both the buggy and fixed versions, so only the audit row's
    durability is being probed here.
    """
    db.rollback()
    return _audit_rows(db, resource_type=resource_type, resource_id=resource_id, action=action)


# ---------------------------------------------------------------------------
# create_work_order -> CREATE
# ---------------------------------------------------------------------------


def test_create_work_order_emits_committed_create_audit(client: TestClient, db_session: Session):
    """POST /work-orders/ persists a CREATE AuditLog row for the new work order.

    FAILS against the old audit-after-commit code: the create handler committed
    the work order first and only then called ``audit.log_create``, which merely
    flushed into a never-committed transaction. After ``db.rollback()`` here that
    flushed row vanishes and ``len(rows) == 1`` fails (rows is empty), while the
    work order itself -- committed before the audit call -- still exists.
    """
    admin = _make_user(db_session, role=UserRole.ADMIN)
    part = _make_part(db_session)

    resp = client.post(
        "/api/v1/work-orders/?auto_routing=false",
        headers=_headers_for(admin),
        json={"part_id": part.id, "quantity_ordered": 10, "customer_name": "Acme", "priority": 5},
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    new_id = resp.json()["id"]

    rows = _committed_audit_rows(db_session, resource_type="work_order", resource_id=new_id, action="CREATE")
    assert len(rows) == 1, "expected exactly one COMMITTED CREATE audit row for the new work order"
    assert rows[0].action == "CREATE"
    assert rows[0].resource_type == "work_order"
    assert rows[0].resource_id == new_id
    assert rows[0].company_id == COMPANY_A


# ---------------------------------------------------------------------------
# update_work_order -> UPDATE
# ---------------------------------------------------------------------------


def test_update_work_order_emits_committed_update_audit(client: TestClient, db_session: Session):
    """PUT /work-orders/{id} that changes a field persists an UPDATE AuditLog row.

    FAILS against the old audit-after-commit code: the update handler committed
    the work-order change first, so the subsequent ``audit.log_update`` row was
    flushed-only. The ``db.rollback()`` discards it and the committed-row
    assertion fails. (priority 5 -> 3 is a real change, so ``log_update`` does
    not short-circuit on "no changes".)
    """
    admin = _make_user(db_session, role=UserRole.ADMIN)
    part = _make_part(db_session)
    work_order = _make_work_order(db_session, part=part)

    resp = client.put(
        f"/api/v1/work-orders/{work_order.id}",
        headers=_headers_for(admin),
        json={"version": work_order.version, "priority": 3},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    rows = _committed_audit_rows(db_session, resource_type="work_order", resource_id=work_order.id, action="UPDATE")
    assert len(rows) == 1, "expected exactly one COMMITTED UPDATE audit row"
    assert rows[0].action == "UPDATE"
    assert rows[0].resource_type == "work_order"
    assert rows[0].resource_id == work_order.id
    assert rows[0].company_id == COMPANY_A


# ---------------------------------------------------------------------------
# delete_work_order -> DELETE (default soft-delete path)
# ---------------------------------------------------------------------------


def test_delete_work_order_emits_committed_delete_audit(client: TestClient, db_session: Session):
    """DELETE /work-orders/{id} (soft delete) persists a DELETE AuditLog row.

    FAILS against the old audit-after-commit code: the soft-delete handler
    committed the ``is_deleted`` flip first, then called ``audit.log_delete``,
    whose row was flushed-only. After ``db.rollback()`` the row is gone and the
    assertion fails -- while the work order remains soft-deleted, i.e. a state
    change with no durable audit trail.
    """
    admin = _make_user(db_session, role=UserRole.ADMIN)
    part = _make_part(db_session)
    work_order = _make_work_order(db_session, part=part)

    resp = client.delete(f"/api/v1/work-orders/{work_order.id}", headers=_headers_for(admin))
    assert resp.status_code == status.HTTP_204_NO_CONTENT, resp.text

    rows = _committed_audit_rows(db_session, resource_type="work_order", resource_id=work_order.id, action="DELETE")
    assert len(rows) == 1, "expected exactly one COMMITTED DELETE audit row"
    assert rows[0].action == "DELETE"
    assert rows[0].resource_type == "work_order"
    assert rows[0].resource_id == work_order.id
    assert rows[0].company_id == COMPANY_A


# ---------------------------------------------------------------------------
# restore_work_order -> RESTORE (log_update with action='restore')
# ---------------------------------------------------------------------------


def test_restore_work_order_emits_committed_restore_audit(client: TestClient, db_session: Session):
    """POST /work-orders/{id}/restore persists a RESTORE AuditLog row.

    The handler logs via ``audit.log_update(..., action='restore')`` -> action
    string 'RESTORE'. FAILS against the old audit-after-commit code: the restore
    handler committed the ``is_deleted`` flip back to False first, so the audit
    row was flushed-only and is discarded by ``db.rollback()`` here.
    """
    admin = _make_user(db_session, role=UserRole.ADMIN)
    part = _make_part(db_session)
    work_order = _make_work_order(db_session, part=part, is_deleted=True)

    resp = client.post(f"/api/v1/work-orders/{work_order.id}/restore", headers=_headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    rows = _committed_audit_rows(db_session, resource_type="work_order", resource_id=work_order.id, action="RESTORE")
    assert len(rows) == 1, "expected exactly one COMMITTED RESTORE audit row"
    assert rows[0].action == "RESTORE"
    assert rows[0].resource_type == "work_order"
    assert rows[0].resource_id == work_order.id
    assert rows[0].company_id == COMPANY_A


# ---------------------------------------------------------------------------
# release_work_order -> STATUS_CHANGE (draft -> released)
# ---------------------------------------------------------------------------


def test_release_work_order_emits_committed_status_change_audit(client: TestClient, db_session: Session):
    """POST /work-orders/{id}/release persists a STATUS_CHANGE AuditLog row.

    The handler logs via ``audit.log_status_change`` carrying old/new status.
    FAILS against the old audit-after-commit code: the release handler committed
    the draft -> released transition first, so the STATUS_CHANGE row was
    flushed-only and is discarded by ``db.rollback()`` here -- the headline
    compliance event (a release with no audit trail).
    """
    admin = _make_user(db_session, role=UserRole.ADMIN)
    part = _make_part(db_session)
    work_center = _make_work_center(db_session)
    # Release requires a DRAFT work order with at least one operation.
    work_order = _make_work_order(
        db_session,
        part=part,
        status_value=WorkOrderStatus.DRAFT,
        with_operation=True,
        work_center=work_center,
    )

    resp = client.post(f"/api/v1/work-orders/{work_order.id}/release", headers=_headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["status"] == WorkOrderStatus.RELEASED.value

    rows = _committed_audit_rows(
        db_session, resource_type="work_order", resource_id=work_order.id, action="STATUS_CHANGE"
    )
    assert len(rows) == 1, "expected exactly one COMMITTED STATUS_CHANGE audit row"
    row = rows[0]
    assert row.action == "STATUS_CHANGE"
    assert row.resource_type == "work_order"
    assert row.resource_id == work_order.id
    assert row.company_id == COMPANY_A
    # old/new status captured in the audit payload (log_status_change shape).
    assert row.old_values == {"status": "draft"}
    assert row.new_values == {"status": "released"}
