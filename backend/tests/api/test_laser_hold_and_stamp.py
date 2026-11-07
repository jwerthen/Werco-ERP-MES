"""The laser force-RELEASED routes under ruling 2: they keep a hold, and they record the release.

Import Nest Package (standalone and work-order-addressed) and the manual nest add put the
laser work order on the floor RELEASED -- a nest package is born released. Two rules ride
on top of that write now (``work_orders._release_laser_work_order_for_nests``):

1. An **ON_HOLD** laser job stays ON_HOLD through a corrective re-import or a nest add. The
   usual reason to hold a package is bad nests and the fix is a re-import; silently lifting
   the hold put the rebuilt nests straight back on the floor. The new nest operations are
   then refused by clock-in (409) until someone runs Resume Work Order.
2. Moving the job INTO RELEASED stamps ``released_by`` / ``released_at`` when it was DRAFT
   or carries no stamp yet -- the importer IS who authorized it, and the resume verb reads
   the stamp as the release record. An existing stamp on a RELEASED job is preserved, and
   the WO-level ``log_update`` records the stamp.

Offline: CNC-file packages only (filename inference, no AI).
"""

import io
import zipfile
from datetime import datetime, timedelta

import pytest
from fastapi import status
from sqlalchemy.orm import Session

import app.api.endpoints.work_orders as work_orders_endpoint
from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services.operation_action_gates import MSG_WORK_ORDER_ON_HOLD

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


@pytest.fixture(autouse=True)
def upload_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def _no_ai(monkeypatch):
    monkeypatch.setattr(
        work_orders_endpoint,
        "extract_nest_fields_from_pdf",
        lambda *a, **k: pytest.fail("these tests must not call the AI extractor"),
    )


def _ensure_company(db: Session) -> None:
    if not db.query(Company).filter(Company.id == COMPANY_A).first():
        db.add(Company(id=COMPANY_A, name="Company 1", slug="company-1", is_active=True))
        db.commit()


def make_user(db: Session, *, role: UserRole) -> User:
    _ensure_company(db)
    n = _next()
    user = User(
        email=f"laser-hold-{n}@co1.test",
        employee_id=f"LHOLD-{n:05d}",
        first_name="Laser",
        last_name="Hold",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=role,
        is_active=True,
        company_id=COMPANY_A,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def make_laser_work_center(db: Session) -> WorkCenter:
    _ensure_company(db)
    n = _next()
    wc = WorkCenter(
        name=f"Laser Hold {n}",
        code=f"LASER-HOLD-{n}",
        work_center_type="laser",
        description="laser fixture",
        hourly_rate=120,
        is_active=True,
        company_id=COMPANY_A,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def _cnc_zip(*names: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in names:
            zf.writestr(name, "M30")
    return buf.getvalue()


def _post_import(client, headers, url: str, zip_bytes: bytes, work_center_id: int):
    return client.post(
        url,
        headers=headers,
        data={"work_center_id": str(work_center_id)},
        files={"file": ("nests.zip", io.BytesIO(zip_bytes), "application/zip")},
    )


def standalone_import(client, user, wc) -> dict:
    resp = _post_import(
        client,
        headers_for(user),
        "/api/v1/work-orders/laser-nest-packages/standalone/import",
        _cnc_zip("NEST-A_QTY3.nc", "NEST-B_QTY2.nc"),
        wc.id,
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()["child_work_order"]


def reimport(client, user, wo_id: int, wc, *names: str):
    return _post_import(
        client,
        headers_for(user),
        f"/api/v1/work-orders/{wo_id}/laser-nest-packages/import",
        _cnc_zip(*names),
        wc.id,
    )


def add_manual_nest(client, user, wo_id: int, wc):
    return client.post(
        f"/api/v1/work-orders/{wo_id}/laser-nests/manual",
        headers=headers_for(user),
        json={"cnc_number": f"M{_next()}", "planned_runs": 2, "material": "A36", "work_center_id": wc.id},
    )


def reread(db: Session, wo_id: int) -> WorkOrder:
    db.expire_all()
    return db.query(WorkOrder).filter(WorkOrder.id == wo_id).one()


def wo_update_rows(db: Session, wo_id: int) -> list[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(AuditLog.resource_type == "work_order", AuditLog.action == "UPDATE", AuditLog.resource_id == wo_id)
        .order_by(AuditLog.id)
        .all()
    )


def set_status(db: Session, wo_id: int, status_: WorkOrderStatus, **fields) -> WorkOrder:
    wo = reread(db, wo_id)
    wo.status = status_
    for key, value in fields.items():
        setattr(wo, key, value)
    db.commit()
    return wo


# =========================================================================== 1. a hold survives


def test_reimport_onto_a_held_laser_job_keeps_the_hold_and_the_new_nests_refuse_clock_in(client, db_session):
    admin = make_user(db_session, role=UserRole.ADMIN)
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wc = make_laser_work_center(db_session)
    child = standalone_import(client, admin, wc)
    set_status(db_session, child["id"], WorkOrderStatus.ON_HOLD)

    resp = reimport(client, admin, child["id"], wc, "NEST-FIXED_QTY4.nc")

    assert resp.status_code == status.HTTP_200_OK, resp.text
    rebuilt = resp.json()["child_work_order"]
    assert rebuilt["status"] == "on_hold"
    assert reread(db_session, child["id"]).status == WorkOrderStatus.ON_HOLD
    new_ops = db_session.query(WorkOrderOperation).filter(WorkOrderOperation.work_order_id == child["id"]).all()
    assert len(new_ops) == 1, "the nests were rebuilt from the corrective package"
    op = new_ops[0]

    clock_in = client.post(
        "/api/v1/shop-floor/clock-in",
        json={
            "work_order_id": child["id"],
            "operation_id": op.id,
            "work_center_id": op.work_center_id,
            "entry_type": "run",
        },
        headers=headers_for(operator),
    )
    assert clock_in.status_code == status.HTTP_409_CONFLICT, clock_in.text
    assert clock_in.json()["detail"] == MSG_WORK_ORDER_ON_HOLD
    # The WO-level audit row records that the status did NOT move.
    [row] = wo_update_rows(db_session, child["id"])
    assert row.old_values["status"] == "on_hold" and row.new_values["status"] == "on_hold"


def test_manual_nest_add_on_a_held_laser_job_keeps_the_hold(client, db_session):
    admin = make_user(db_session, role=UserRole.ADMIN)
    wc = make_laser_work_center(db_session)
    child = standalone_import(client, admin, wc)
    set_status(db_session, child["id"], WorkOrderStatus.ON_HOLD)

    resp = add_manual_nest(client, admin, child["id"], wc)

    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    assert reread(db_session, child["id"]).status == WorkOrderStatus.ON_HOLD


# =========================================================================== 2. the release is recorded


def test_a_standalone_import_is_stamped_as_the_importers_release(client, db_session):
    admin = make_user(db_session, role=UserRole.ADMIN)
    wc = make_laser_work_center(db_session)
    before = datetime.utcnow() - timedelta(seconds=5)

    child = standalone_import(client, admin, wc)

    row = reread(db_session, child["id"])
    assert row.status == WorkOrderStatus.RELEASED
    assert row.released_by == admin.id
    assert row.released_at is not None and row.released_at >= before
    # The creation audit snapshot carries the stamp (taken after the build).
    create = (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == "work_order", AuditLog.action == "CREATE", AuditLog.resource_id == row.id)
        .one()
    )
    assert create.new_values["released_by"] == admin.id


def test_a_draft_target_is_released_and_re_stamped_with_the_stamp_in_the_audit_row(client, db_session):
    first_importer = make_user(db_session, role=UserRole.ADMIN)
    planner = make_user(db_session, role=UserRole.MANAGER)
    wc = make_laser_work_center(db_session)
    child = standalone_import(client, first_importer, wc)
    stale = datetime(2026, 1, 2, 3, 4, 5)
    set_status(db_session, child["id"], WorkOrderStatus.DRAFT, released_at=stale, released_by=first_importer.id)

    resp = reimport(client, planner, child["id"], wc, "NEST-R_QTY7.nc")

    assert resp.status_code == status.HTTP_200_OK, resp.text
    row = reread(db_session, child["id"])
    assert row.status == WorkOrderStatus.RELEASED
    assert row.released_by == planner.id
    assert row.released_at is not None and row.released_at > stale
    [update] = wo_update_rows(db_session, child["id"])
    assert update.old_values["status"] == "draft" and update.new_values["status"] == "released"
    assert update.old_values["released_by"] == first_importer.id
    assert update.new_values["released_by"] == planner.id
    assert update.old_values["released_at"] != update.new_values["released_at"]


def test_reimport_of_a_stamped_released_job_keeps_the_original_stamp(client, db_session):
    first_importer = make_user(db_session, role=UserRole.ADMIN)
    other = make_user(db_session, role=UserRole.MANAGER)
    wc = make_laser_work_center(db_session)
    child = standalone_import(client, first_importer, wc)
    original = reread(db_session, child["id"])
    original_at, original_by = original.released_at, original.released_by

    resp = reimport(client, other, child["id"], wc, "NEST-AGAIN_QTY1.nc")

    assert resp.status_code == status.HTTP_200_OK, resp.text
    row = reread(db_session, child["id"])
    assert row.status == WorkOrderStatus.RELEASED
    assert (row.released_at, row.released_by) == (original_at, original_by)
    [update] = wo_update_rows(db_session, child["id"])
    assert update.old_values["released_by"] == update.new_values["released_by"] == first_importer.id
    assert update.old_values["released_at"] == update.new_values["released_at"]


def test_manual_add_on_an_unstamped_released_job_stamps_it(client, db_session):
    """A pre-deploy package (released, never stamped) gets its release recorded on the next write."""
    admin = make_user(db_session, role=UserRole.ADMIN)
    wc = make_laser_work_center(db_session)
    child = standalone_import(client, admin, wc)
    set_status(db_session, child["id"], WorkOrderStatus.RELEASED, released_at=None, released_by=None)

    resp = add_manual_nest(client, admin, child["id"], wc)

    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    row = reread(db_session, child["id"])
    assert row.released_by == admin.id and row.released_at is not None
    update = wo_update_rows(db_session, child["id"])[-1]
    assert update.old_values["released_by"] is None and update.new_values["released_by"] == admin.id


def test_classic_parent_flow_stamps_the_new_laser_child(client, db_session):
    admin = make_user(db_session, role=UserRole.ADMIN)
    wc = make_laser_work_center(db_session)
    n = _next()
    part = Part(
        part_number=f"ASM-HOLD-{n}",
        name="Assembly",
        part_type="assembly",
        unit_of_measure="each",
        is_active=True,
        company_id=COMPANY_A,
    )
    db_session.add(part)
    db_session.flush()
    parent = WorkOrder(
        work_order_number=f"WO-HOLD-{n}",
        part_id=part.id,
        quantity_ordered=1,
        status=WorkOrderStatus.RELEASED,
        priority=3,
        company_id=COMPANY_A,
    )
    db_session.add(parent)
    db_session.commit()

    resp = reimport(client, admin, parent.id, wc, "NEST-C_QTY2.nc")

    assert resp.status_code == status.HTTP_200_OK, resp.text
    child = reread(db_session, resp.json()["child_work_order"]["id"])
    assert child.parent_work_order_id == parent.id
    assert child.status == WorkOrderStatus.RELEASED
    assert child.released_by == admin.id and child.released_at is not None
    assert all(
        op.status == OperationStatus.READY
        for op in db_session.query(WorkOrderOperation).filter(WorkOrderOperation.work_order_id == child.id).all()
    )
