"""Behavior locks for the AS9100D scrap-reason enforcement invariant on the four
OFFICE/ADMIN work-order endpoints (the desktop/manager counterparts to the
shop-floor rules locked in ``test_scrap_reason_enforcement.py``).

A scrapped quantity is a quality/defect event: AS9100D defect-traceability requires
it to carry a reason. The rule -- "when the scrap quantity being written is > 0, a
non-blank ``scrap_reason`` is required, else HTTP 422 with detail exactly
``scrap_reason is required when quantity_scrapped is greater than 0``" -- is now
enforced server-side on all four office paths so a scripted/API client can't record
reasonless scrap. Blank/whitespace counts as missing; scrap == 0 / absent stays valid.

The four endpoints (and how scrap is supplied):

1. ``PUT  /work-orders/operations/{operation_id}``      -- body ``WorkOrderOperationUpdate``
   (``scrap_reason`` in the JSON body; a Pydantic ``model_validator`` raises -> 422).
   Body REQUIRES ``version`` (optimistic lock). This handler also GAINED an
   ``AuditService.log_update`` call -- asserted lightly here.
2. ``PUT  /work-orders/{work_order_id}``                 -- body ``WorkOrderUpdate`` (same shape).
3. ``POST /work-orders/operations/{operation_id}/complete`` -- scrap via QUERY params
   ``?quantity_complete=&quantity_scrapped=&scrap_reason=`` (handler guard -> 422),
   plus a ``quantity_scrapped < 0 -> 400`` guard.
4. ``POST /work-orders/{work_order_id}/complete``        -- scrap via QUERY params, as #3.

Contracts locked here, per endpoint:
  * scrap > 0 with NO reason                 -> 422 (and nothing persisted where practical).
  * scrap > 0 with a blank/whitespace reason -> 422 (blank counts as missing).
  * scrap > 0 WITH a real reason             -> 2xx, the reason is persisted on the WO/op row.
  * scrap == 0 (or absent) with no reason    -> 2xx (regression guard).
  * (PUT paths) a partial update that does NOT touch ``quantity_scrapped`` -> 2xx
    (the ``is not None`` guard never forces a reason on an unrelated edit).
  * (complete_operation) ``quantity_scrapped < 0`` -> 400.

Two follow-up hardening fixes (compliance auditor) are also locked below:

Fix 1 -- non-finite (NaN/Inf) quantities are rejected up front on BOTH /complete
query-param endpoints with HTTP 400, detail exactly ``Quantity must be a valid number``
(mirroring the shop-floor /production guard). This closes a bypass: a plain float query
param accepts the literal ``nan``/``inf``, and NaN slips past every ``> 0``/``< 0`` guard
(including the scrap-reason guard), which would otherwise persist a reasonless NaN scrap.
The literal ``nan``/``inf`` must reach FastAPI's float coercion, so these are sent as RAW
query strings (not ``params=``). Asserted on:
  * POST /work-orders/operations/{operation_id}/complete
  * POST /work-orders/{work_order_id}/complete

Fix 2 -- ``PUT /work-orders/operations/{operation_id}`` (update_operation) is now gated
``require_role([ADMIN, MANAGER, SUPERVISOR])`` (was ``get_current_user`` only). A
non-privileged OPERATOR -> 403 (no mutation persisted); a privileged SUPERVISOR still
succeeds (over-tightening guard).
"""

from datetime import date, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
_seq = {"n": 0}

SCRAP_REASON_DETAIL = "scrap_reason is required when quantity_scrapped is greater than 0"


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True)
        db.add(company)
        db.commit()
    return company


def make_user(db: Session, *, role: UserRole = UserRole.MANAGER, company_id: int = COMPANY_A) -> User:
    """A MANAGER by default -- the office endpoints are gated to ADMIN/MANAGER/SUPERVISOR
    (and QUALITY for the WO-complete path), so a manager passes RBAC on all four."""
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"scrap-office-{n}@co{company_id}.test",
        employee_id=f"SCRAPOFF-{n:05d}",
        first_name="Scrap",
        last_name="Office",
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


def headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def make_wo_op(
    db: Session,
    *,
    company_id: int = COMPANY_A,
    op_status: OperationStatus = OperationStatus.IN_PROGRESS,
    wo_status: WorkOrderStatus = WorkOrderStatus.IN_PROGRESS,
    quantity_ordered: int = 10,
) -> tuple[WorkOrder, WorkOrderOperation, WorkCenter]:
    """One work order with a single operation. Defaults to IN_PROGRESS/IN_PROGRESS
    (the completable shape); callers override for the PUT-only tests."""
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"SCRAPOFF-P-{n}",
        name=f"Part {n}",
        description="office scrap-reason enforcement fixture part",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()
    wc = WorkCenter(
        name=f"SCRAPOFF-WC-{n}",
        code=f"SCRAPOFF-WC-{n}",
        work_center_type="welding",
        description="office scrap-reason enforcement fixture work center",
        hourly_rate=100.0,
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.flush()
    wo = WorkOrder(
        work_order_number=f"SCRAPOFF-WO-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=quantity_ordered,
        status=wo_status,
        priority=5,
        due_date=date.today() + timedelta(days=30),
        company_id=company_id,
    )
    db.add(wo)
    db.flush()
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        sequence=10,
        operation_number="OP10",
        name="Op 10",
        status=op_status,
        quantity_complete=0,
        quantity_scrapped=0,
        company_id=company_id,
    )
    db.add(op)
    db.commit()
    db.refresh(op)
    db.refresh(wo)
    return wo, op, wc


# ===========================================================================
# 1) PUT /work-orders/operations/{operation_id}   (body WorkOrderOperationUpdate)
#    scrap_reason in the JSON body; body REQUIRES version (optimistic lock).
# ===========================================================================


def test_put_operation_scrap_without_reason_is_422_and_persists_nothing(client: TestClient, db_session: Session):
    """Positive scrap with NO reason -> 422; the operation's scrap stays untouched."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    op_id = op.id

    resp = client.put(
        f"/api/v1/work-orders/operations/{op_id}",
        json={"version": op.version, "quantity_scrapped": 2},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text
    assert resp.json()["detail"][0]["msg"].endswith(SCRAP_REASON_DETAIL)

    db_session.expire_all()
    op = db_session.get(WorkOrderOperation, op_id)
    assert (op.quantity_scrapped or 0) == 0, "a 422 update must not persist scrap"
    assert op.scrap_reason is None


def test_put_operation_scrap_with_blank_reason_is_422(client: TestClient, db_session: Session):
    """A whitespace-only reason counts as missing -> 422; nothing is written."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    op_id = op.id

    resp = client.put(
        f"/api/v1/work-orders/operations/{op_id}",
        json={"version": op.version, "quantity_scrapped": 3, "scrap_reason": "   "},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text

    db_session.expire_all()
    op = db_session.get(WorkOrderOperation, op_id)
    assert (op.quantity_scrapped or 0) == 0 and op.scrap_reason is None


def test_put_operation_scrap_with_reason_succeeds_persists_and_audits(client: TestClient, db_session: Session):
    """Positive scrap WITH a real reason succeeds, persists the reason on the operation,
    and writes an UPDATE audit row (the log_update call this handler gained)."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    op_id = op.id

    resp = client.put(
        f"/api/v1/work-orders/operations/{op_id}",
        json={"version": op.version, "quantity_scrapped": 2, "scrap_reason": "Burr on edge"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    # WorkOrderOperationResponse exposes quantity_scrapped but NOT scrap_reason, so
    # the reason is verified by re-fetching the row below.
    assert resp.json()["quantity_scrapped"] == 2

    db_session.expire_all()
    op = db_session.get(WorkOrderOperation, op_id)
    assert op.quantity_scrapped == 2
    assert op.scrap_reason == "Burr on edge"

    # Light audit assertion: an UPDATE row was written for this operation (the handler
    # gained an AuditService.log_update call). Matches how other audit tests assert.
    audit = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.resource_type == "work_order_operation",
            AuditLog.resource_id == op_id,
            AuditLog.action == "UPDATE",
        )
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert audit is not None, "the operation update must write an UPDATE audit row"


def test_put_operation_zero_scrap_no_reason_succeeds(client: TestClient, db_session: Session):
    """Regression guard: writing zero scrap with no reason must still succeed --
    the rule only fires for a *positive* scrap quantity."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    op_id = op.id

    resp = client.put(
        f"/api/v1/work-orders/operations/{op_id}",
        json={"version": op.version, "quantity_complete": 1, "quantity_scrapped": 0},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    op = db_session.get(WorkOrderOperation, op_id)
    assert (op.quantity_scrapped or 0) == 0
    assert op.scrap_reason is None
    assert op.quantity_complete == 1


def test_put_operation_partial_update_without_scrap_field_succeeds(client: TestClient, db_session: Session):
    """The ``is not None`` guard: a partial update that does NOT include
    quantity_scrapped (here just a name change) must NOT be forced to carry a reason."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    op_id = op.id

    resp = client.put(
        f"/api/v1/work-orders/operations/{op_id}",
        json={"version": op.version, "name": "Renamed Op"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    op = db_session.get(WorkOrderOperation, op_id)
    assert op.name == "Renamed Op"
    assert op.scrap_reason is None


# ===========================================================================
# 2) PUT /work-orders/{work_order_id}             (body WorkOrderUpdate)
#    scrap_reason in the JSON body; body REQUIRES version (optimistic lock).
# ===========================================================================


def test_put_work_order_scrap_without_reason_is_422_and_persists_nothing(client: TestClient, db_session: Session):
    """Positive scrap with NO reason -> 422; the WO's scrap stays untouched."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    wo_id = wo.id

    resp = client.put(
        f"/api/v1/work-orders/{wo_id}",
        json={"version": 0, "quantity_scrapped": 2},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text
    assert resp.json()["detail"][0]["msg"].endswith(SCRAP_REASON_DETAIL)

    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo_id)
    assert (wo.quantity_scrapped or 0) == 0, "a 422 update must not persist scrap"
    assert wo.scrap_reason is None


def test_put_work_order_scrap_with_blank_reason_is_422(client: TestClient, db_session: Session):
    """A whitespace-only reason counts as missing -> 422; nothing is written."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    wo_id = wo.id

    resp = client.put(
        f"/api/v1/work-orders/{wo_id}",
        json={"version": 0, "quantity_scrapped": 3, "scrap_reason": "   "},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text

    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo_id)
    assert (wo.quantity_scrapped or 0) == 0 and wo.scrap_reason is None


def test_put_work_order_scrap_with_reason_succeeds_and_persists_reason(client: TestClient, db_session: Session):
    """Positive scrap WITH a real reason succeeds and stamps the reason on the WO."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    wo_id = wo.id

    resp = client.put(
        f"/api/v1/work-orders/{wo_id}",
        json={"version": 0, "quantity_scrapped": 4, "scrap_reason": "Material out of spec"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    # WorkOrderResponse exposes quantity_scrapped but NOT scrap_reason, so the reason
    # is verified by re-fetching the row below.
    assert resp.json()["quantity_scrapped"] == 4

    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo_id)
    assert wo.quantity_scrapped == 4
    assert wo.scrap_reason == "Material out of spec"


def test_put_work_order_zero_scrap_no_reason_succeeds(client: TestClient, db_session: Session):
    """Regression guard: writing zero scrap with no reason must still succeed."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    wo_id = wo.id

    resp = client.put(
        f"/api/v1/work-orders/{wo_id}",
        json={"version": 0, "quantity_scrapped": 0},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo_id)
    assert (wo.quantity_scrapped or 0) == 0
    assert wo.scrap_reason is None


def test_put_work_order_partial_update_without_scrap_field_succeeds(client: TestClient, db_session: Session):
    """The ``is not None`` guard: a partial update that does NOT include
    quantity_scrapped (here just a priority change) must NOT require a reason."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    wo_id = wo.id

    resp = client.put(
        f"/api/v1/work-orders/{wo_id}",
        json={"version": 0, "priority": 1},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo_id)
    assert wo.priority == 1
    assert wo.scrap_reason is None


# ===========================================================================
# 3) POST /work-orders/operations/{operation_id}/complete   (scrap via QUERY params)
# ===========================================================================


def test_complete_operation_scrap_without_reason_is_422_and_persists_nothing(client: TestClient, db_session: Session):
    """Positive scrap with NO reason -> 422; the operation stays IN_PROGRESS and clean."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    op_id = op.id

    resp = client.post(
        f"/api/v1/work-orders/operations/{op_id}/complete",
        params={"quantity_complete": 5, "quantity_scrapped": 2},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text
    assert resp.json()["detail"] == SCRAP_REASON_DETAIL

    db_session.expire_all()
    op = db_session.get(WorkOrderOperation, op_id)
    assert (op.quantity_scrapped or 0) == 0, "a 422 complete must not persist scrap"
    assert op.scrap_reason is None
    assert op.status == OperationStatus.IN_PROGRESS, "a 422 complete must not flip the op COMPLETE"


def test_complete_operation_scrap_with_blank_reason_is_422(client: TestClient, db_session: Session):
    """A whitespace-only reason counts as missing -> 422; nothing is written."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    op_id = op.id

    resp = client.post(
        f"/api/v1/work-orders/operations/{op_id}/complete",
        params={"quantity_complete": 5, "quantity_scrapped": 1, "scrap_reason": "   "},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text

    db_session.expire_all()
    op = db_session.get(WorkOrderOperation, op_id)
    assert (op.quantity_scrapped or 0) == 0 and op.scrap_reason is None


def test_complete_operation_negative_scrap_is_400(client: TestClient, db_session: Session):
    """A negative scrap quantity is a 400 (the non-negative guard), distinct from the
    reasonless-scrap 422."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    op_id = op.id

    resp = client.post(
        f"/api/v1/work-orders/operations/{op_id}/complete",
        params={"quantity_complete": 5, "quantity_scrapped": -1},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert resp.json()["detail"] == "quantity_scrapped cannot be negative"

    db_session.expire_all()
    op = db_session.get(WorkOrderOperation, op_id)
    assert (op.quantity_scrapped or 0) == 0


def test_complete_operation_scrap_with_reason_succeeds_and_persists_reason(client: TestClient, db_session: Session):
    """Positive scrap WITH a real reason succeeds and stamps it on the operation."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session, quantity_ordered=10)
    op_id = op.id

    resp = client.post(
        f"/api/v1/work-orders/operations/{op_id}/complete",
        params={"quantity_complete": 8, "quantity_scrapped": 2, "scrap_reason": "Cracked weld"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    op = db_session.get(WorkOrderOperation, op_id)
    assert op.quantity_scrapped == 2
    assert op.scrap_reason == "Cracked weld"


def test_complete_operation_zero_scrap_no_reason_succeeds(client: TestClient, db_session: Session):
    """Regression guard: completing with zero scrap and no reason must still succeed."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session, quantity_ordered=10)
    op_id = op.id

    resp = client.post(
        f"/api/v1/work-orders/operations/{op_id}/complete",
        params={"quantity_complete": 10, "quantity_scrapped": 0},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    op = db_session.get(WorkOrderOperation, op_id)
    assert (op.quantity_scrapped or 0) == 0
    assert op.scrap_reason is None
    assert op.status == OperationStatus.COMPLETE


# ===========================================================================
# 4) POST /work-orders/{work_order_id}/complete   (scrap via QUERY params)
# ===========================================================================


def test_complete_work_order_scrap_without_reason_is_422_and_persists_nothing(client: TestClient, db_session: Session):
    """Positive scrap with NO reason -> 422; the WO stays IN_PROGRESS and un-scrapped."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    wo_id = wo.id

    resp = client.post(
        f"/api/v1/work-orders/{wo_id}/complete",
        params={"quantity_complete": 8, "quantity_scrapped": 2},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text
    assert resp.json()["detail"] == SCRAP_REASON_DETAIL

    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo_id)
    assert (wo.quantity_scrapped or 0) == 0, "a 422 complete must not persist scrap"
    assert wo.scrap_reason is None
    assert wo.status == WorkOrderStatus.IN_PROGRESS, "a 422 complete must not flip the WO COMPLETE"


def test_complete_work_order_scrap_with_blank_reason_is_422(client: TestClient, db_session: Session):
    """A whitespace-only reason counts as missing -> 422; nothing is written."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    wo_id = wo.id

    resp = client.post(
        f"/api/v1/work-orders/{wo_id}/complete",
        params={"quantity_complete": 8, "quantity_scrapped": 3, "scrap_reason": "   "},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text

    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo_id)
    assert (wo.quantity_scrapped or 0) == 0 and wo.scrap_reason is None
    assert wo.status == WorkOrderStatus.IN_PROGRESS


def test_complete_work_order_negative_scrap_is_400(client: TestClient, db_session: Session):
    """A negative scrap quantity is a 400 (the non-negative guard), distinct from the
    reasonless-scrap 422."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    wo_id = wo.id

    resp = client.post(
        f"/api/v1/work-orders/{wo_id}/complete",
        params={"quantity_complete": 8, "quantity_scrapped": -1},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert resp.json()["detail"] == "quantity_scrapped cannot be negative"

    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo_id)
    assert (wo.quantity_scrapped or 0) == 0
    assert wo.status == WorkOrderStatus.IN_PROGRESS


def test_complete_work_order_scrap_with_reason_succeeds_and_persists_reason(client: TestClient, db_session: Session):
    """Positive scrap WITH a real reason succeeds and stamps it on the WO."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session, quantity_ordered=10)
    wo_id = wo.id

    resp = client.post(
        f"/api/v1/work-orders/{wo_id}/complete",
        params={"quantity_complete": 8, "quantity_scrapped": 2, "scrap_reason": "Failed final inspection"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo_id)
    assert wo.quantity_scrapped == 2
    assert wo.scrap_reason == "Failed final inspection"
    assert wo.status == WorkOrderStatus.COMPLETE


def test_complete_work_order_zero_scrap_no_reason_succeeds(client: TestClient, db_session: Session):
    """Regression guard: completing a WO with zero scrap and no reason must still succeed."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session, quantity_ordered=10)
    wo_id = wo.id

    resp = client.post(
        f"/api/v1/work-orders/{wo_id}/complete",
        params={"quantity_complete": 10, "quantity_scrapped": 0},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo_id)
    assert (wo.quantity_scrapped or 0) == 0
    assert wo.scrap_reason is None
    assert wo.status == WorkOrderStatus.COMPLETE


def test_complete_work_order_absent_scrap_no_reason_succeeds(client: TestClient, db_session: Session):
    """Regression guard: omitting quantity_scrapped entirely (the defaulted None path)
    with no reason must still succeed -- the guard only fires for a positive scrap."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session, quantity_ordered=10)
    wo_id = wo.id

    resp = client.post(
        f"/api/v1/work-orders/{wo_id}/complete",
        params={"quantity_complete": 10},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo_id)
    assert wo.scrap_reason is None
    assert wo.status == WorkOrderStatus.COMPLETE


# ===========================================================================
# Fix 1) Non-finite (NaN/Inf) quantities rejected up front -> 400 on BOTH
#        /complete query-param endpoints.
#
# A plain ``float`` query param accepts the literal ``nan``/``inf``, and NaN slips
# past every ``> 0``/``< 0`` guard (incl. the reasonless-scrap 422 guard), which would
# persist a reasonless NaN scrap on Postgres. The handler now rejects non-finite
# floats with HTTP 400, detail exactly ``Quantity must be a valid number`` (mirroring
# the shop-floor /production guard). The literal must reach FastAPI's float coercion,
# so these are sent as RAW query strings (NOT ``params=``, which the client would
# re-serialize). Asserting 400 (NOT 422 -- it isn't a Pydantic coercion error, since
# ``nan``/``inf`` are valid floats -- and NOT 200) and that nothing persisted.
# ===========================================================================


def test_complete_operation_nan_scrap_without_reason_is_400_and_persists_nothing(
    client: TestClient, db_session: Session
):
    """``?quantity_complete=10&quantity_scrapped=nan`` (no reason) -> 400, not 422/200;
    the operation stays IN_PROGRESS, un-scrapped, reason null (the NaN bypass is closed)."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    op_id = op.id

    resp = client.post(
        f"/api/v1/work-orders/operations/{op_id}/complete?quantity_complete=10&quantity_scrapped=nan",
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert resp.json()["detail"] == "Quantity must be a valid number"

    db_session.expire_all()
    op = db_session.get(WorkOrderOperation, op_id)
    assert (op.quantity_scrapped or 0) == 0, "a 400 (non-finite) complete must not persist scrap"
    assert op.scrap_reason is None
    assert op.status == OperationStatus.IN_PROGRESS, "a 400 complete must not flip the op COMPLETE"


def test_complete_operation_nan_quantity_complete_is_400(client: TestClient, db_session: Session):
    """``?quantity_complete=nan`` -> 400; nothing is completed."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    op_id = op.id

    resp = client.post(
        f"/api/v1/work-orders/operations/{op_id}/complete?quantity_complete=nan",
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert resp.json()["detail"] == "Quantity must be a valid number"

    db_session.expire_all()
    op = db_session.get(WorkOrderOperation, op_id)
    assert op.status == OperationStatus.IN_PROGRESS
    assert (op.quantity_complete or 0) == 0


def test_complete_operation_inf_scrap_with_reason_is_400(client: TestClient, db_session: Session):
    """An ``inf`` scrap quantity is rejected up front (400) even WITH a reason -- a
    non-finite quantity is invalid regardless of the reason guard passing."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    op_id = op.id

    resp = client.post(
        f"/api/v1/work-orders/operations/{op_id}/complete"
        f"?quantity_complete=8&quantity_scrapped=inf&scrap_reason=Cracked",
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert resp.json()["detail"] == "Quantity must be a valid number"

    db_session.expire_all()
    op = db_session.get(WorkOrderOperation, op_id)
    assert (op.quantity_scrapped or 0) == 0
    assert op.scrap_reason is None
    assert op.status == OperationStatus.IN_PROGRESS


def test_complete_work_order_nan_scrap_without_reason_is_400_and_persists_nothing(
    client: TestClient, db_session: Session
):
    """``?quantity_complete=10&quantity_scrapped=nan`` (no reason) -> 400, not 422/200;
    the WO stays IN_PROGRESS, un-scrapped, reason null (the NaN bypass is closed)."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    wo_id = wo.id

    resp = client.post(
        f"/api/v1/work-orders/{wo_id}/complete?quantity_complete=10&quantity_scrapped=nan",
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert resp.json()["detail"] == "Quantity must be a valid number"

    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo_id)
    assert (wo.quantity_scrapped or 0) == 0, "a 400 (non-finite) complete must not persist scrap"
    assert wo.scrap_reason is None
    assert wo.status == WorkOrderStatus.IN_PROGRESS, "a 400 complete must not flip the WO COMPLETE"


def test_complete_work_order_nan_quantity_complete_is_400(client: TestClient, db_session: Session):
    """``?quantity_complete=nan`` -> 400; the WO stays IN_PROGRESS."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    wo_id = wo.id

    resp = client.post(
        f"/api/v1/work-orders/{wo_id}/complete?quantity_complete=nan",
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert resp.json()["detail"] == "Quantity must be a valid number"

    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo_id)
    assert wo.status == WorkOrderStatus.IN_PROGRESS


def test_complete_work_order_inf_scrap_with_reason_is_400(client: TestClient, db_session: Session):
    """An ``inf`` scrap quantity is rejected (400) even WITH a reason supplied."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    wo_id = wo.id

    resp = client.post(
        f"/api/v1/work-orders/{wo_id}/complete"
        f"?quantity_complete=8&quantity_scrapped=inf&scrap_reason=Failed",
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert resp.json()["detail"] == "Quantity must be a valid number"

    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo_id)
    assert (wo.quantity_scrapped or 0) == 0
    assert wo.scrap_reason is None
    assert wo.status == WorkOrderStatus.IN_PROGRESS


# ===========================================================================
# Fix 2) RBAC gate on PUT /work-orders/operations/{operation_id} (update_operation).
#
# This path edits operation fields (incl. quantity_scrapped/scrap_reason) and is now
# gated ``require_role([ADMIN, MANAGER, SUPERVISOR])`` -- it was ``get_current_user``
# only, letting any authenticated user (Operator/Viewer) edit/scrap an operation.
#   * A non-privileged OPERATOR -> 403 and NO mutation persisted.
#   * A privileged SUPERVISOR   -> still succeeds (a valid no-scrap edit returns 2xx),
#     guarding against over-tightening.
# ===========================================================================


def test_put_operation_as_operator_is_403_and_persists_nothing(client: TestClient, db_session: Session):
    """An OPERATOR (non-privileged) is rejected by the RBAC gate with 403; the
    operation is unchanged -- the request never reaches the handler body."""
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wo, op, wc = make_wo_op(db_session)
    op_id = op.id
    original_name = op.name

    resp = client.put(
        f"/api/v1/work-orders/operations/{op_id}",
        json={"version": op.version, "name": "Operator Edit Attempt"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text

    db_session.expire_all()
    op = db_session.get(WorkOrderOperation, op_id)
    assert op.name == original_name, "a 403 must not mutate the operation"


def test_put_operation_scrap_as_operator_is_403_and_persists_no_scrap(client: TestClient, db_session: Session):
    """The gate fires before the scrap-reason validator: an OPERATOR attempting to
    book scrap (even with a reason) is refused with 403, and no scrap is written."""
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wo, op, wc = make_wo_op(db_session)
    op_id = op.id

    resp = client.put(
        f"/api/v1/work-orders/operations/{op_id}",
        json={"version": op.version, "quantity_scrapped": 2, "scrap_reason": "Burr on edge"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text

    db_session.expire_all()
    op = db_session.get(WorkOrderOperation, op_id)
    assert (op.quantity_scrapped or 0) == 0, "a 403 must not persist scrap"
    assert op.scrap_reason is None


def test_put_operation_as_supervisor_still_succeeds(client: TestClient, db_session: Session):
    """Over-tightening guard: a privileged SUPERVISOR can still update an operation
    (a valid no-scrap field edit) and the change persists."""
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    wo, op, wc = make_wo_op(db_session)
    op_id = op.id

    resp = client.put(
        f"/api/v1/work-orders/operations/{op_id}",
        json={"version": op.version, "name": "Supervisor Renamed Op"},
        headers=headers_for(supervisor),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    op = db_session.get(WorkOrderOperation, op_id)
    assert op.name == "Supervisor Renamed Op"
