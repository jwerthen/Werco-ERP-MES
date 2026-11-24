"""shipping/2: open quality items on a job, and what they do to the certificate and the ship.

Step 1 (read only): ``shipment_release_exceptions`` lists, per work order, NCRs that
hold the shipment (not dispositioned, or rework / repair / use-as-is and not closed;
scrap and return-to-vendor never), the latest first article not accepted, and open
quality-hold blockers. Served on ready-to-ship rows and ``GET /shipping/{id}``.
"""

from datetime import date, datetime

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


# ---------------------------------------------------------------------------
# Step 2: the automatic certificate is withheld; a manual issue accepts the items.
# ---------------------------------------------------------------------------

API_TOKENS_URL = "/api/v1/api-tokens/"


def _token_headers(client: TestClient, db: Session, user) -> dict:
    from app.models.user import UserRole

    admin = make_user(db, role=UserRole.ADMIN, company_id=user.company_id)
    issued = client.post(API_TOKENS_URL, json={"user_id": user.id, "label": "coc probe"}, headers=headers_for(admin))
    assert issued.status_code == 201, issued.text
    return {"Authorization": f"Bearer {issued.json()['token']}", "X-Requested-With": "XMLHttpRequest"}


def _audit(db: Session, action: str):
    from app.models.audit_log import AuditLog

    db.expire_all()
    return db.query(AuditLog).filter(AuditLog.company_id == COMPANY_A, AuditLog.action == action).all()


def _ship_with_open_ncr(db: Session, *, cert: bool = True):
    from app.models.shipping import ShipmentStatus

    wo = make_wo(db, make_part(db))
    ncr = make_ncr(db, wo, status_=NCRStatus.OPEN, disposition=NCRDisposition.PENDING)
    shipment = make_shipment(db, wo, quantity_shipped=4, cert_of_conformance=cert, status_=ShipmentStatus.PENDING)
    db.commit()
    return wo, ncr, shipment


def _accept(client: TestClient, db: Session, shipment, user):
    return client.post(
        f"/api/v1/shipping/{shipment.id}/ship",
        json={"acknowledge_exceptions": True, "reason": "Customer accepted per deviation D-1"},
        headers=headers_for(user),
    )


def _ship(client: TestClient, shipment, user):
    return client.post(f"/api/v1/shipping/{shipment.id}/ship", headers=headers_for(user))


def test_ship_withholds_the_automatic_coc_and_records_why(client: TestClient, db_session: Session):
    from app.models.operational_event import OperationalEvent
    from app.models.user import UserRole
    from tests.api.test_coc_generation_batch11c import _coc_rows

    manager = make_user(db_session, role=UserRole.MANAGER)
    wo, ncr, shipment = _ship_with_open_ncr(db_session)

    resp = _accept(client, db_session, shipment, manager)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["coc_withheld"] is True
    assert [e["reference_id"] for e in body["release_exceptions"]] == [ncr.id]

    assert _coc_rows(db_session, shipment.id) == []
    rows = [r for r in _audit(db_session, "COC_WITHHELD") if r.resource_id == shipment.id]
    assert len(rows) == 1 and rows[0].resource_type == "shipment"
    assert rows[0].extra_data["release_exceptions"][0]["code"] == "open_ncr"
    events = (
        db_session.query(OperationalEvent)
        .filter(OperationalEvent.event_type == "coc_withheld", OperationalEvent.entity_id == shipment.id)
        .all()
    )
    assert len(events) == 1

    listed = {r["id"]: r for r in client.get("/api/v1/shipping/", headers=headers_for(manager)).json()}
    assert listed[shipment.id]["coc_withheld"] is True
    detail = client.get(f"/api/v1/shipping/{shipment.id}", headers=headers_for(manager)).json()
    assert detail["coc_withheld"] is True and detail["coc_number"] is None


def test_clean_job_still_auto_issues(client: TestClient, db_session: Session):
    from app.models.user import UserRole
    from tests.api.test_coc_generation_batch11c import _coc_rows

    manager = make_user(db_session, role=UserRole.MANAGER)
    wo = make_wo(db_session, make_part(db_session))
    shipment = make_shipment(db_session, wo, cert_of_conformance=True)
    db_session.commit()
    resp = _ship(client, shipment, manager)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["coc_withheld"] is False
    assert len(_coc_rows(db_session, shipment.id)) == 1


def test_manual_issue_over_open_items_needs_acceptance_and_records_it(client: TestClient, db_session: Session):
    import json

    from app.models.user import UserRole

    manager = make_user(db_session, role=UserRole.MANAGER)
    quality = make_user(db_session, role=UserRole.QUALITY)
    wo, ncr, shipment = _ship_with_open_ncr(db_session)
    _ship_over(client, db_session, shipment, manager)

    url = f"/api/v1/shipping/{shipment.id}/coc"
    refused = client.post(url, headers=headers_for(quality))
    assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
    detail = refused.json()["detail"]
    assert detail["code"] == "release_exceptions_open"
    assert [e["reference_id"] for e in detail["release_exceptions"]] == [ncr.id]

    no_reason = client.post(url, json={"acknowledge_exceptions": True, "reason": " "}, headers=headers_for(quality))
    assert no_reason.status_code == 422, no_reason.text

    ok = client.post(
        url, json={"acknowledge_exceptions": True, "reason": "Rework verified at final"}, headers=headers_for(quality)
    )
    assert ok.status_code == status.HTTP_200_OK, ok.text
    rows = _audit(db_session, "COC_ISSUED_OVER_EXCEPTIONS")
    assert len(rows) == 1
    assert rows[0].extra_data["reason"] == "Rework verified at final"
    assert rows[0].extra_data["release_exceptions"][0]["reference_id"] == ncr.id

    from app.models.shipping import CertificateOfConformance

    coc = db_session.query(CertificateOfConformance).filter_by(shipment_id=shipment.id).one()
    assert json.loads(coc.content_snapshot)["accepted_release_exceptions"][0]["code"] == "open_ncr"
    listed = {r["id"]: r for r in client.get("/api/v1/shipping/", headers=headers_for(quality)).json()}
    assert listed[shipment.id]["coc_withheld"] is False
    assert listed[shipment.id]["coc_number"] == coc.coc_number


def test_api_token_refused_on_issue(client: TestClient, db_session: Session):
    from app.models.shipping import ShipmentStatus
    from app.models.user import UserRole
    from tests.api.test_coc_generation_batch11c import _coc_rows

    quality = make_user(db_session, role=UserRole.QUALITY)
    wo = make_wo(db_session, make_part(db_session))
    shipment = make_shipment(db_session, wo, status_=ShipmentStatus.SHIPPED, ship_date=date.today())
    db_session.commit()
    resp = client.post(f"/api/v1/shipping/{shipment.id}/coc", headers=_token_headers(client, db_session, quality))
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    assert "API token" in resp.json()["detail"]
    assert _coc_rows(db_session, shipment.id) == []


def _ship_over(client: TestClient, db: Session, shipment, user):
    resp = _accept(client, db, shipment, user)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp


# ---------------------------------------------------------------------------
# Step 3: shipping over open items needs a named person's acknowledged reason.
# ---------------------------------------------------------------------------


def _shipment_status(db: Session, shipment):
    from app.models.shipping import Shipment

    db.expire_all()
    return db.get(Shipment, shipment.id).status


def test_ship_over_open_items_is_409_listing_them_and_writes_nothing(client: TestClient, db_session: Session):
    from app.models.shipping import ShipmentStatus
    from app.models.user import UserRole

    shipper = make_user(db_session, role=UserRole.SHIPPING)
    wo, ncr, shipment = _ship_with_open_ncr(db_session)
    resp = _ship(client, shipment, shipper)
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "release_exceptions_open"
    assert "accept them with a reason" in detail["message"]
    assert [e["reference_id"] for e in detail["release_exceptions"]] == [ncr.id]
    assert _shipment_status(db_session, shipment) == ShipmentStatus.PENDING
    assert _audit(db_session, "COC_WITHHELD") == []


def test_shipping_role_cannot_acknowledge(client: TestClient, db_session: Session):
    from app.models.shipping import ShipmentStatus
    from app.models.user import UserRole

    shipper = make_user(db_session, role=UserRole.SHIPPING)
    wo, ncr, shipment = _ship_with_open_ncr(db_session)
    resp = _accept(client, db_session, shipment, shipper)
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    assert "Admin, Manager or Quality" in resp.json()["detail"]
    assert _shipment_status(db_session, shipment) == ShipmentStatus.PENDING


def test_acknowledgment_needs_a_reason(client: TestClient, db_session: Session):
    from app.models.user import UserRole

    manager = make_user(db_session, role=UserRole.MANAGER)
    wo, ncr, shipment = _ship_with_open_ncr(db_session)
    resp = client.post(
        f"/api/v1/shipping/{shipment.id}/ship",
        json={"acknowledge_exceptions": True},
        headers=headers_for(manager),
    )
    assert resp.status_code == 422, resp.text


def test_api_token_cannot_acknowledge(client: TestClient, db_session: Session):
    from app.models.shipping import ShipmentStatus
    from app.models.user import UserRole

    manager = make_user(db_session, role=UserRole.MANAGER)
    wo, ncr, shipment = _ship_with_open_ncr(db_session)
    resp = client.post(
        f"/api/v1/shipping/{shipment.id}/ship",
        json={"acknowledge_exceptions": True, "reason": "Agent says fine"},
        headers=_token_headers(client, db_session, manager),
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    assert "API token" in resp.json()["detail"]
    assert _shipment_status(db_session, shipment) == ShipmentStatus.PENDING


def test_quality_accepts_and_ships_with_required_audit(client: TestClient, db_session: Session):
    from app.models.shipping import ShipmentStatus
    from app.models.user import UserRole

    quality = make_user(db_session, role=UserRole.QUALITY)
    wo, ncr, shipment = _ship_with_open_ncr(db_session)
    resp = _accept(client, db_session, shipment, quality)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert _shipment_status(db_session, shipment) == ShipmentStatus.SHIPPED
    rows = [r for r in _audit(db_session, "SHIPPED_OVER_QUALITY_EXCEPTIONS") if r.resource_id == shipment.id]
    assert len(rows) == 1
    assert rows[0].extra_data["reason"] == "Customer accepted per deviation D-1"
    assert rows[0].extra_data["release_exceptions"][0]["reference_id"] == ncr.id


def test_quality_cannot_ship_a_clean_job(client: TestClient, db_session: Session):
    from app.models.shipping import ShipmentStatus
    from app.models.user import UserRole

    quality = make_user(db_session, role=UserRole.QUALITY)
    wo = make_wo(db_session, make_part(db_session))
    shipment = make_shipment(db_session, wo)
    db_session.commit()
    resp = _ship(client, shipment, quality)
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    assert _shipment_status(db_session, shipment) == ShipmentStatus.PENDING


def test_api_token_still_ships_a_clean_job(client: TestClient, db_session: Session):
    from app.models.shipping import ShipmentStatus
    from app.models.user import UserRole

    shipper = make_user(db_session, role=UserRole.SHIPPING)
    wo = make_wo(db_session, make_part(db_session))
    shipment = make_shipment(db_session, wo)
    db_session.commit()
    resp = client.post(f"/api/v1/shipping/{shipment.id}/ship", headers=_token_headers(client, db_session, shipper))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert _shipment_status(db_session, shipment) == ShipmentStatus.SHIPPED


def test_required_audit_failure_rolls_the_ship_back(client: TestClient, db_session: Session):
    from unittest.mock import patch

    from app.models.shipping import ShipmentStatus
    from app.models.user import UserRole
    from app.services.audit_service import AuditWriteError

    manager = make_user(db_session, role=UserRole.MANAGER)
    wo, ncr, shipment = _ship_with_open_ncr(db_session)
    with patch("app.api.endpoints.shipping.record_ship_over_exceptions", side_effect=AuditWriteError("chain down")):
        resp = _accept(client, db_session, shipment, manager)
    assert resp.status_code == 503, resp.text
    assert "not shipped" in resp.json()["detail"]
    assert _shipment_status(db_session, shipment) == ShipmentStatus.PENDING
