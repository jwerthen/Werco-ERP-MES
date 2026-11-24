"""The issued, numbered Certificate of Conformance is served on the shipment reads.

shipping/1: ``GET /shipping/`` and ``GET /shipping/{id}`` carry ``coc_number`` and
``coc_issued_at`` (one batched query for the list), and ``POST /shipping/{id}/coc``
issues only for a shipment that has shipped (409 otherwise).
"""

from datetime import date

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.shipping import ShipmentStatus
from app.models.user import UserRole
from app.services.audit_service import AuditService
from app.services.coc_service import generate_coc_for_shipment
from tests.api.test_coc_generation_batch11c import (
    COMPANY_A,
    COMPANY_B,
    _coc_rows,
    headers_for,
    make_part,
    make_shipment,
    make_user,
    make_wo,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]


def test_list_and_detail_carry_issued_coc_number(client: TestClient, db_session: Session):
    user = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part)
    issued = make_shipment(db_session, wo, status_=ShipmentStatus.SHIPPED, ship_date=date.today())
    bare = make_shipment(db_session, wo, quantity_shipped=1)
    coc = generate_coc_for_shipment(
        db_session, shipment=issued, company_id=COMPANY_A, user_id=user.id, audit=AuditService(db_session, user)
    )
    db_session.commit()

    rows = {r["id"]: r for r in client.get("/api/v1/shipping/", headers=headers_for(user)).json()}
    assert rows[issued.id]["coc_number"] == coc.coc_number
    assert rows[issued.id]["coc_issued_at"].endswith("Z")
    assert rows[bare.id]["coc_number"] is None
    assert rows[bare.id]["coc_issued_at"] is None

    detail = client.get(f"/api/v1/shipping/{issued.id}", headers=headers_for(user)).json()
    assert detail["coc_number"] == coc.coc_number
    assert detail["coc_issued_at"].endswith("Z")
    assert client.get(f"/api/v1/shipping/{bare.id}", headers=headers_for(user)).json()["coc_number"] is None


def test_list_coc_number_is_tenant_scoped(client: TestClient, db_session: Session):
    user_b = make_user(db_session, company_id=COMPANY_B)
    part_b = make_part(db_session, company_id=COMPANY_B)
    wo_b = make_wo(db_session, part_b, company_id=COMPANY_B)
    ship_b = make_shipment(
        db_session, wo_b, status_=ShipmentStatus.SHIPPED, ship_date=date.today(), company_id=COMPANY_B
    )
    generate_coc_for_shipment(
        db_session, shipment=ship_b, company_id=COMPANY_B, user_id=user_b.id, audit=AuditService(db_session, user_b)
    )
    db_session.commit()
    user_a = make_user(db_session)
    ids = [r["id"] for r in client.get("/api/v1/shipping/", headers=headers_for(user_a)).json()]
    assert ship_b.id not in ids


def test_issue_refused_409_before_ship(client: TestClient, db_session: Session):
    quality = make_user(db_session, role=UserRole.QUALITY)
    part = make_part(db_session)
    wo = make_wo(db_session, part)
    shipment = make_shipment(db_session, wo)
    db_session.commit()

    resp = client.post(f"/api/v1/shipping/{shipment.id}/coc", headers=headers_for(quality))
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert "has shipped" in resp.json()["detail"]
    assert _coc_rows(db_session, shipment.id) == []


def test_detail_carries_order_facts_for_the_packing_slip(client: TestClient, db_session: Session):
    user = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part, serial_numbers='["SN-1", "SN-2"]')
    wo.po_line_item = "3"
    wo.unit_number = "U-12"
    shipment = make_shipment(db_session, wo)
    shipment.ship_to_city = "Springfield"
    shipment.ship_to_state = "IL"
    shipment.ship_to_zip = "62701"
    db_session.commit()

    detail = client.get(f"/api/v1/shipping/{shipment.id}", headers=headers_for(user)).json()
    assert detail["po_line_item"] == "3"
    assert detail["unit_number"] == "U-12"
    assert detail["part_revision"] == part.revision
    assert detail["serial_numbers"] == ["SN-1", "SN-2"]
    assert (detail["ship_to_city"], detail["ship_to_state"], detail["ship_to_zip"]) == ("Springfield", "IL", "62701")
