"""Scrap -> NCR on the kiosk production report (Kiosk Foundry redesign, B6).

``POST /shop-floor/operations/{id}/production`` gains ``open_ncr`` /
``ncr_description``: when ``open_ncr`` rides a report with scrap, an
IN_PROCESS NonConformanceReport is filed in the SAME transaction as the
production write -- deliberately with **NO hold and NO blocker** (the machine
keeps running; contrast with the process-step OOT quality hold). Locked here:

- the NCR row itself: tenant ``company_id``, ``source=IN_PROCESS``,
  ``quantity_affected`` = THIS report's scrap delta (not the running total),
  WO/part linkage, ``detected_by``, and the description fallback (operator
  narrative when given, else a WO/op/reason synthesis);
- the response's ``ncr: {id, ncr_number}`` block (the kiosk toast quotes the
  real number) -- and ``ncr: null`` when no NCR was requested;
- the COMMITTED audit trail: ``log_create`` -> a CREATE row with
  ``resource_type="ncr"`` that survives a rollback (the flushed-only-row trap
  documented in tests/api/test_work_orders_audit_persistence.py);
- the no-hold/no-blocker contract: the operation stays IN_PROGRESS and no
  WorkOrderBlocker is filed;
- the 400 guard: ``open_ncr`` without scrap refuses BEFORE any mutation
  (op/entry quantities untouched, no NCR row);
- scrap-reason enforcement (422) is unchanged by the new fields.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.quality import NCRSource, NonConformanceReport
from app.models.work_order import OperationStatus
from app.models.work_order_blocker import WorkOrderBlocker
from tests.api.kiosk_test_helpers import (
    COMPANY_A,
    make_user,
    make_wo_with_operation,
    make_work_center,
    user_headers,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]


def production_url(operation_id: int) -> str:
    return f"/api/v1/shop-floor/operations/{operation_id}/production"


def clocked_in_job(client: TestClient, db_session: Session, *, quantity_ordered: float = 50):
    """Operator clocked into a fresh single-op WO; returns (headers, wo, op)."""
    operator = make_user(db_session)
    headers = user_headers(operator)
    wc = make_work_center(db_session)
    wo, op = make_wo_with_operation(db_session, work_center=wc, quantity_ordered=quantity_ordered)
    resp = client.post(
        "/api/v1/shop-floor/clock-in",
        headers=headers,
        json={"work_order_id": wo.id, "operation_id": op.id, "work_center_id": wc.id},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return operator, headers, wo, op


def committed_ncr_audit_rows(db: Session, ncr_id: int) -> list:
    """CREATE audit rows for the NCR that were actually COMMITTED.

    ``db.rollback()`` first: the client fixture shares ONE session with the
    endpoint, so a flushed-but-uncommitted audit row would still be visible to
    a naive query. A committed row survives the rollback; a flushed-only row
    is discarded (the audit-persistence guard proven in
    tests/api/test_qms_soft_delete_audit.py).
    """
    db.rollback()
    return (
        db.query(AuditLog)
        .filter(AuditLog.resource_type == "ncr", AuditLog.resource_id == ncr_id, AuditLog.action == "CREATE")
        .order_by(AuditLog.sequence_number.desc())
        .all()
    )


class TestScrapOpensNcr:
    def test_scrap_report_with_open_ncr_files_an_in_process_ncr(self, client: TestClient, db_session: Session):
        operator, headers, wo, op = clocked_in_job(client, db_session)

        resp = client.post(
            production_url(op.id),
            headers=headers,
            json={
                "quantity_complete_delta": 0,
                "quantity_scrapped_delta": 3,
                "scrap_reason": "Porosity on weld face",
                "open_ncr": True,
                "ncr_description": "Porosity across 3 pcs after torch change",
            },
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        body = resp.json()

        # Response block: the kiosk toast quotes the REAL number from here.
        assert body["ncr"] is not None
        ncr_id = body["ncr"]["id"]
        assert body["ncr"]["ncr_number"].startswith("NCR-")

        ncr = db_session.query(NonConformanceReport).filter(NonConformanceReport.id == ncr_id).one()
        assert ncr.company_id == COMPANY_A
        assert ncr.source == NCRSource.IN_PROCESS
        assert ncr.quantity_affected == 3.0
        assert ncr.work_order_id == wo.id
        assert ncr.part_id == wo.part_id
        assert ncr.detected_by == operator.id
        assert ncr.ncr_number == body["ncr"]["ncr_number"]
        assert ncr.description == "Porosity across 3 pcs after torch change"
        assert wo.work_order_number in ncr.title

        # Deliberate contrast with the steps OOT hold: machine keeps running.
        db_session.refresh(op)
        assert op.status == OperationStatus.IN_PROGRESS
        assert op.quantity_scrapped == 3.0
        blockers = db_session.query(WorkOrderBlocker).filter(WorkOrderBlocker.operation_id == op.id).count()
        assert blockers == 0, "scrap->NCR must NOT file a blocker or hold"

        # Tamper-evident trail: a COMMITTED CREATE row for the NCR.
        rows = committed_ncr_audit_rows(db_session, ncr_id)
        assert len(rows) == 1
        assert rows[0].company_id == COMPANY_A
        assert rows[0].resource_identifier == ncr.ncr_number

    def test_quantity_affected_is_this_reports_delta_not_the_total(self, client: TestClient, db_session: Session):
        """Earlier scrap on the op must not inflate the NCR quantity."""
        _, headers, _, op = clocked_in_job(client, db_session)
        first = client.post(
            production_url(op.id),
            headers=headers,
            json={"quantity_complete_delta": 0, "quantity_scrapped_delta": 4, "scrap_reason": "Setup pieces"},
        )
        assert first.status_code == status.HTTP_200_OK, first.text

        second = client.post(
            production_url(op.id),
            headers=headers,
            json={
                "quantity_complete_delta": 0,
                "quantity_scrapped_delta": 2,
                "scrap_reason": "Crack",
                "open_ncr": True,
            },
        )
        assert second.status_code == status.HTTP_200_OK, second.text
        ncr = db_session.query(NonConformanceReport).filter(NonConformanceReport.id == second.json()["ncr"]["id"]).one()
        assert ncr.quantity_affected == 2.0
        db_session.refresh(op)
        assert op.quantity_scrapped == 6.0  # totals keep accruing separately

    def test_description_falls_back_to_wo_op_and_reason(self, client: TestClient, db_session: Session):
        _, headers, wo, op = clocked_in_job(client, db_session)
        resp = client.post(
            production_url(op.id),
            headers=headers,
            json={
                "quantity_complete_delta": 0,
                "quantity_scrapped_delta": 1,
                "scrap_reason": "Burn-through",
                "open_ncr": True,
            },
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        ncr = db_session.query(NonConformanceReport).filter(NonConformanceReport.id == resp.json()["ncr"]["id"]).one()
        assert wo.work_order_number in ncr.description
        assert "Burn-through" in ncr.description


class TestOpenNcrGuards:
    def test_open_ncr_without_scrap_is_a_400_and_mutates_nothing(self, client: TestClient, db_session: Session):
        """An NCR documents scrap: requesting one on a good-only report is a
        client bug, refused BEFORE any write lands."""
        _, headers, _, op = clocked_in_job(client, db_session)

        resp = client.post(
            production_url(op.id),
            headers=headers,
            json={"quantity_complete_delta": 5, "quantity_scrapped_delta": 0, "open_ncr": True},
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        assert "open_ncr requires a scrap quantity" in resp.json()["detail"]

        db_session.rollback()
        db_session.refresh(op)
        assert float(op.quantity_complete or 0) == 0.0, "the refused report must not land its good delta"
        assert float(op.quantity_scrapped or 0) == 0.0
        assert op.last_reported_at is None
        assert db_session.query(NonConformanceReport).count() == 0

    def test_scrap_without_open_ncr_files_no_ncr(self, client: TestClient, db_session: Session):
        """open_ncr defaults False -- plain scrap stays an NCR-less report."""
        _, headers, _, op = clocked_in_job(client, db_session)

        resp = client.post(
            production_url(op.id),
            headers=headers,
            json={"quantity_complete_delta": 0, "quantity_scrapped_delta": 2, "scrap_reason": "Mis-key"},
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["ncr"] is None
        assert db_session.query(NonConformanceReport).count() == 0

    def test_scrap_reason_enforcement_is_unchanged_by_the_new_fields(self, client: TestClient, db_session: Session):
        """Reason-less scrap is still a 422 at the schema boundary, open_ncr or
        not -- the new fields must not have loosened the AS9100D rule."""
        _, headers, _, op = clocked_in_job(client, db_session)

        resp = client.post(
            production_url(op.id),
            headers=headers,
            json={"quantity_complete_delta": 0, "quantity_scrapped_delta": 2, "open_ncr": True},
        )
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text
        assert db_session.query(NonConformanceReport).count() == 0
