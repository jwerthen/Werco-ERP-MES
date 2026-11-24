"""shipping/2: open quality items on a job, and what they do to the certificate and the ship.

Step 1 (read only): ``shipment_release_exceptions`` lists, per work order, NCRs that
hold the shipment (not dispositioned, or rework / repair / use-as-is and not closed;
scrap and return-to-vendor never), the latest first article not accepted, and open
quality-hold blockers. Served on ready-to-ship rows and ``GET /shipping/{id}``.
"""

from datetime import datetime

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.quality import (
    FAIStatus,
    FirstArticleInspection,
    NCRDisposition,
    NCRSource,
    NCRStatus,
    NonConformanceReport,
)
from app.models.work_order_blocker import WorkOrderBlocker
from app.services.quality_gate_service import shipment_release_exceptions
from tests.api.test_coc_generation_batch11c import (
    COMPANY_A,
    COMPANY_B,
    _next,
    headers_for,
    make_part,
    make_shipment,
    make_user,
    make_wo,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]


def make_ncr(db: Session, wo, *, status_: NCRStatus, disposition) -> NonConformanceReport:
    ncr = NonConformanceReport(
        ncr_number=f"SRX-NCR-{_next()}",
        work_order_id=wo.id,
        source=NCRSource.IN_PROCESS,
        status=status_,
        disposition=disposition,
        title="Fixture NCR",
        description="fixture",
        company_id=wo.company_id,
    )
    db.add(ncr)
    db.flush()
    return ncr


def make_fai(db: Session, wo, status_: FAIStatus, *, minute: int = 0) -> FirstArticleInspection:
    fai = FirstArticleInspection(
        fai_number=f"SRX-FAI-{_next()}",
        part_id=wo.part_id,
        work_order_id=wo.id,
        status=status_,
        created_at=datetime(2026, 11, 1, 8, minute),
        company_id=wo.company_id,
    )
    db.add(fai)
    db.flush()
    return fai


def make_blocker(db: Session, wo, *, category: str = "quality_hold", status_: str = "open") -> WorkOrderBlocker:
    blocker = WorkOrderBlocker(
        company_id=wo.company_id,
        work_order_id=wo.id,
        category=category,
        severity="high",
        status=status_,
        title="Hold for review",
    )
    db.add(blocker)
    db.flush()
    return blocker


@pytest.mark.parametrize(
    "status_, disposition, holds",
    [
        (NCRStatus.OPEN, NCRDisposition.PENDING, True),
        (NCRStatus.CLOSED, NCRDisposition.PENDING, True),
        (NCRStatus.UNDER_REVIEW, NCRDisposition.REWORK, True),
        (NCRStatus.PENDING_DISPOSITION, NCRDisposition.USE_AS_IS, True),
        (NCRStatus.OPEN, NCRDisposition.REPAIR, True),
        (NCRStatus.CLOSED, NCRDisposition.REWORK, False),
        (NCRStatus.CLOSED, NCRDisposition.USE_AS_IS, False),
        (NCRStatus.OPEN, NCRDisposition.SCRAP, False),
        (NCRStatus.OPEN, NCRDisposition.RETURN_TO_VENDOR, False),
        (NCRStatus.VOID, NCRDisposition.PENDING, False),
    ],
)
def test_ncr_rule_matrix(db_session: Session, status_, disposition, holds):
    wo = make_wo(db_session, make_part(db_session))
    ncr = make_ncr(db_session, wo, status_=status_, disposition=disposition)
    excs = shipment_release_exceptions(db_session, COMPANY_A, [wo.id]).get(wo.id, [])
    assert ([e.reference_id for e in excs if e.code == "open_ncr"] == [ncr.id]) is holds


def test_deleted_ncr_does_not_hold(db_session: Session):
    wo = make_wo(db_session, make_part(db_session))
    ncr = make_ncr(db_session, wo, status_=NCRStatus.OPEN, disposition=NCRDisposition.PENDING)
    ncr.is_deleted = True
    db_session.flush()
    assert shipment_release_exceptions(db_session, COMPANY_A, [wo.id]) == {}


def test_latest_fai_and_quality_hold_blockers_only(db_session: Session):
    wo = make_wo(db_session, make_part(db_session))
    make_fai(db_session, wo, FAIStatus.FAILED, minute=0)
    redo = make_fai(db_session, wo, FAIStatus.IN_PROGRESS, minute=5)
    hold = make_blocker(db_session, wo)
    make_blocker(db_session, wo, category="material_missing")
    make_blocker(db_session, wo, status_="resolved")
    excs = shipment_release_exceptions(db_session, COMPANY_A, [wo.id])[wo.id]
    assert [(e.code, e.reference_id) for e in excs] == [("fai_not_passed", redo.id), ("open_blocker", hold.id)]


def test_unsigned_inspection_point_is_not_an_exception(db_session: Session):
    """Information only in this build -- nothing on the job but an inspection point."""
    wo = make_wo(db_session, make_part(db_session))
    assert shipment_release_exceptions(db_session, COMPANY_A, [wo.id]) == {}


def test_tenant_scoped(db_session: Session):
    wo_b = make_wo(db_session, make_part(db_session, company_id=COMPANY_B), company_id=COMPANY_B)
    make_ncr(db_session, wo_b, status_=NCRStatus.OPEN, disposition=NCRDisposition.PENDING)
    assert shipment_release_exceptions(db_session, COMPANY_A, [wo_b.id]) == {}


def test_ready_to_ship_and_detail_serve_the_chips(client: TestClient, db_session: Session):
    user = make_user(db_session)
    wo = make_wo(db_session, make_part(db_session))
    clean = make_wo(db_session, make_part(db_session))
    ncr = make_ncr(db_session, wo, status_=NCRStatus.OPEN, disposition=NCRDisposition.PENDING)
    shipment = make_shipment(db_session, wo, quantity_shipped=2)
    db_session.commit()

    rows = {
        r["work_order_id"]: r for r in client.get("/api/v1/shipping/ready-to-ship", headers=headers_for(user)).json()
    }
    assert [e["reference_id"] for e in rows[wo.id]["release_exceptions"]] == [ncr.id]
    assert rows[wo.id]["release_exceptions"][0]["code"] == "open_ncr"
    assert rows[clean.id]["release_exceptions"] == []

    detail = client.get(f"/api/v1/shipping/{shipment.id}", headers=headers_for(user))
    assert detail.status_code == status.HTTP_200_OK
    assert [e["code"] for e in detail.json()["release_exceptions"]] == ["open_ncr"]
