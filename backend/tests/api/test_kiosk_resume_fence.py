"""Can the kiosk actually RESUME? The path fence, answered.

Making a held operation visible on the kiosk is worthless if the resume control
403s. The kiosk's badge-minted operator token carries ``scope="kiosk"`` and is
path-fenced by ``deps._is_kiosk_scope_allowed_path``, so whether the EXISTING
``PUT /shop-floor/operations/{id}/resume`` is reachable from a crew station is a
fact about that fence, not an opinion -- and the frontend is building a control
against the answer.

The answer is YES, today, with no fence change: the path sits under the
``/api/v1/shop-floor`` allow prefix and matches none of the deny rules
(``kiosk-stations`` / ``dispatch-board`` prefixes, the ``/time-entries/…/approve``
approval suffixes, the ``/work-centers/…/run-order`` planner suffix), and the
endpoint carries no ``require_role``, so an OPERATOR badge may resume. These
tests pin that rather than leaving it to be re-derived, and pin the deny
controls alongside so "resume is allowed" can never be read as "the fence is
open".

Also pinned here, because it is the same shop-floor recovery path: resume still
writes its audit row, and still returns the still-open blockers it did NOT
resolve (BLK-4 warn-and-record -- resume deliberately does not close the
blocker, and that divergence is what the warning exists to surface).
"""

from datetime import datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.work_order import OperationStatus
from app.models.work_order_blocker import (
    WorkOrderBlocker,
    WorkOrderBlockerCategory,
    WorkOrderBlockerSeverity,
    WorkOrderBlockerStatus,
)
from tests.api.kiosk_test_helpers import (
    COMPANY_A,
    bearer,
    make_user,
    make_wo_with_operation,
    make_work_center,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]


def resume_url(operation_id: int) -> str:
    return f"/api/v1/shop-floor/operations/{operation_id}/resume"


def badge_headers(user) -> dict:
    """A 5-minute badge-minted operator token, exactly as POST /auth/kiosk-badge-token mints it."""
    return bearer(
        create_access_token(
            subject=user.id,
            company_id=user.company_id,
            expires_delta=timedelta(minutes=5),
            scope="kiosk",
        )
    )


def test_badge_minted_kiosk_token_may_resume_a_held_operation(client: TestClient, db_session: Session):
    """THE fence answer: the kiosk can drive the existing resume endpoint today."""
    wc = make_work_center(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)

    resp = client.put(resume_url(held_op.id), headers=badge_headers(operator))

    assert resp.status_code == status.HTTP_200_OK, resp.text
    # No actual_start -> resumes to READY, so the job returns to the kiosk queue.
    assert resp.json()["status"] == OperationStatus.READY.value
    db_session.expire_all()
    db_session.refresh(held_op)
    assert held_op.status == OperationStatus.READY


def test_the_fence_is_not_open_just_because_resume_is_allowed(client: TestClient, db_session: Session):
    """Control: the same token is still 403 on the deny-listed planner paths."""
    wc = make_work_center(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    headers = badge_headers(operator)

    assert client.get("/api/v1/shop-floor/dispatch-board", headers=headers).status_code == status.HTTP_403_FORBIDDEN
    run_order = client.put(
        f"/api/v1/shop-floor/work-centers/{wc.id}/run-order",
        headers=headers,
        json={"operation_ids": []},
    )
    assert run_order.status_code == status.HTTP_403_FORBIDDEN
    # And a path entirely outside the shop-floor prefix.
    assert client.get("/api/v1/work-orders/", headers=headers).status_code == status.HTTP_403_FORBIDDEN


def test_resume_from_the_kiosk_still_writes_its_audit_row(client: TestClient, db_session: Session):
    """Invariant 2: a status change through a kiosk token audits like any other."""
    wc = make_work_center(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)

    client.put(resume_url(held_op.id), headers=badge_headers(operator))

    row = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.action == "RESUME_OPERATION",
            AuditLog.resource_type == "work_order_operation",
            AuditLog.resource_id == held_op.id,
        )
        .one()
    )
    assert row.company_id == COMPANY_A
    assert row.user_id == operator.id


def test_resume_returns_the_blocker_it_did_not_resolve(client: TestClient, db_session: Session):
    """BLK-4: resume does NOT resolve the blocker, and says so, verbatim.

    The decoupling is deliberate -- resolution stays owned by the blocker
    resolve/dismiss flow -- so the operation/blocker divergence has to reach the
    operator's screen. The kiosk needs these fields to warn with.
    """
    wc = make_work_center(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    blocker = WorkOrderBlocker(
        company_id=COMPANY_A,
        work_order_id=held_op.work_order_id,
        operation_id=held_op.id,
        category=WorkOrderBlockerCategory.QUALITY_HOLD.value,
        severity=WorkOrderBlockerSeverity.CRITICAL.value,
        status=WorkOrderBlockerStatus.OPEN.value,
        title="Quality Hold: op 10",
        note="Awaiting CMM",
        reported_by=operator.id,
        reported_at=datetime.utcnow(),
    )
    db_session.add(blocker)
    db_session.commit()

    body = client.put(resume_url(held_op.id), headers=badge_headers(operator)).json()

    assert [b["id"] for b in body["open_blockers"]] == [blocker.id]
    assert body["open_blockers"][0]["category"] == WorkOrderBlockerCategory.QUALITY_HOLD.value
    assert body["open_blockers"][0]["severity"] == WorkOrderBlockerSeverity.CRITICAL.value
    # Still open: resume resolved nothing.
    db_session.refresh(blocker)
    assert blocker.status == WorkOrderBlockerStatus.OPEN.value


def test_resume_refuses_an_operation_that_is_not_on_hold(client: TestClient, db_session: Session):
    wc = make_work_center(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    _, ready_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.READY)

    resp = client.put(resume_url(ready_op.id), headers=badge_headers(operator))

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["detail"] == "Operation is not on hold"
