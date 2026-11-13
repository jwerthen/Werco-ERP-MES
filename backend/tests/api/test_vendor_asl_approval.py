"""The Approved Supplier List on the vendor record (AS9100D 8.4, QL 7.4): the approval verb,
the vendor PUT's is_approved guard, the overdue re-evaluation list, the supplier-approval
check, the PO warnings and the Disqualified refusal on every PO-create door, Part.supply_scope,
and the legacy-table read fixes.

The ASL import itself is covered in ``test_vendor_asl_import.py``.
"""

import io
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.time_utils import CENTRAL_TIME_ZONE
from app.models.audit_log import AuditLog
from app.models.part import Part
from app.models.purchasing import POStatus, PurchaseOrder, Vendor
from app.models.user import UserRole
from app.schemas.purchasing import POCreate, POLineCreate
from app.services.audit_service import AuditService, AuditWriteError
from app.services.erp_draft_commands import create_purchase_order_command
from app.services.supplier_approval_service import not_disqualified_clause
from tests.api.test_receiving_compliance import _ensure_company, _next, headers_for, make_user

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

VENDORS = "/api/v1/purchasing/vendors"
POS = "/api/v1/purchasing/purchase-orders"
CHECK = "/api/v1/purchasing/supplier-approval-check"
DISQUALIFIED_TEXT = "{} is Disqualified on the Approved Supplier List; a purchase order cannot be raised to it."


def central_today() -> date:
    return datetime.now(CENTRAL_TIME_ZONE).date()


def make_vendor(db: Session, *, company_id: int = 1, **fields) -> Vendor:
    _ensure_company(db, company_id)
    n = _next()
    values = dict(code=f"ASL{n:05d}", name=f"ASL Supplier {n}", is_active=True, is_approved=False)
    values.update(fields)
    vendor = Vendor(company_id=company_id, **values)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return vendor


def make_part(db: Session, *, company_id: int = 1, part_type: str = "purchased", **fields) -> Part:
    n = _next()
    part = Part(
        company_id=company_id,
        part_number=f"ASLP-{n:05d}",
        name=f"ASL part {n}",
        part_type=part_type,
        unit_of_measure="each",
        is_active=True,
        **fields,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def stamp(vendor: Vendor) -> str:
    return vendor.updated_at.replace(tzinfo=timezone.utc).isoformat()


def approval_body(vendor: Vendor, **fields) -> dict:
    body = {"approval_status": "approved", "reason": "Annual review complete", "expected_updated_at": stamp(vendor)}
    body.update(fields)
    return body


def vendor_audit_rows(db: Session, vendor_id: int):
    db.rollback()
    return (
        db.query(AuditLog)
        .filter(AuditLog.resource_type == "vendor", AuditLog.resource_id == vendor_id)
        .order_by(AuditLog.id)
        .all()
    )


def api_token_headers(client: TestClient, admin_headers: dict, user) -> dict:
    issued = client.post("/api/v1/api-tokens/", json={"user_id": user.id, "label": "asl probe"}, headers=admin_headers)
    assert issued.status_code == 201, issued.text
    return {"Authorization": f"Bearer {issued.json()['token']}", "X-Requested-With": "XMLHttpRequest"}


# =========================================================================== approval verb


def test_quality_records_an_approval_with_labels_normalized_and_a_required_audit_row(client, db_session):
    quality = make_user(db_session, role=UserRole.QUALITY, company_id=1)
    vendor = make_vendor(db_session, notes="General buyer notes")
    response = client.put(
        f"{VENDORS}/{vendor.id}/approval",
        headers=headers_for(quality),
        json=approval_body(
            vendor,
            approved_scopes=["Raw Material – Metalics", "Hardware "],
            approval_bases=["AS9100 Cert"],
            reevaluation_due="2027-11-13",
            last_performance_evaluation="2026-10-01",
            approval_notes="Mill certs on file",
        ),
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["approval_status"] == "approved" and body["is_approved"] is True
    assert body["approved_scopes"] == ["hardware", "raw_material_metallics"]
    assert body["approval_bases"] == ["as9100"]
    assert body["reevaluation_due"] == "2027-11-13" and body["last_performance_evaluation"] == "2026-10-01"
    assert body["approval_notes"] == "Mill certs on file"
    # Becoming approved with no approval_date sent records today's (Central) date.
    assert body["approval_date"] == central_today().isoformat()

    db_session.expire_all()
    row = db_session.get(Vendor, vendor.id)
    assert row.notes == "General buyer notes"  # the general notes are never touched
    (audit,) = vendor_audit_rows(db_session, vendor.id)
    assert audit.action == "UPDATE" and audit.user_id == quality.id and audit.company_id == 1
    assert audit.extra_data == {"source": "approval", "reason": "Annual review complete"}
    assert audit.old_values["approval_status"] is None and audit.new_values["approval_status"] == "approved"
    assert audit.old_values["is_approved"] is False and audit.new_values["is_approved"] is True
    assert "notes" not in audit.new_values and "name" not in audit.new_values


@pytest.mark.parametrize("status_label, expected", [("Conditional", False), ("Disqualified", False)])
def test_conditional_and_disqualified_derive_not_approved(client, db_session, status_label, expected):
    manager = make_user(db_session, role=UserRole.MANAGER, company_id=1)
    vendor = make_vendor(db_session, is_approved=True)
    response = client.put(
        f"{VENDORS}/{vendor.id}/approval",
        headers=headers_for(manager),
        json=approval_body(vendor, approval_status=status_label),
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["is_approved"] is expected


def test_taking_a_vendor_off_the_asl_leaves_is_approved_and_omitted_fields_alone(client, db_session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = make_vendor(db_session, is_approved=True, approval_status="approved", reevaluation_due=date(2027, 1, 1))
    response = client.put(
        f"{VENDORS}/{vendor.id}/approval",
        headers=headers_for(admin),
        json={"approval_status": None, "reason": "Pending re-qualification", "expected_updated_at": stamp(vendor)},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["approval_status"] is None and body["is_approved"] is True
    assert body["reevaluation_due"] == "2027-01-01"


def test_a_no_change_request_writes_nothing(client, db_session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = make_vendor(db_session, approval_status="conditional", is_approved=False)
    response = client.put(
        f"{VENDORS}/{vendor.id}/approval",
        headers=headers_for(admin),
        json=approval_body(vendor, approval_status="conditional"),
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    assert vendor_audit_rows(db_session, vendor.id) == []


@pytest.mark.parametrize("role", [UserRole.SUPERVISOR, UserRole.OPERATOR, UserRole.VIEWER])
def test_other_roles_are_refused(client, db_session, role):
    user = make_user(db_session, role=role, company_id=1)
    vendor = make_vendor(db_session)
    response = client.put(f"{VENDORS}/{vendor.id}/approval", headers=headers_for(user), json=approval_body(vendor))
    assert response.status_code == status.HTTP_403_FORBIDDEN
    db_session.expire_all()
    assert db_session.get(Vendor, vendor.id).approval_status is None


def test_a_deleted_or_foreign_vendor_is_404(client, db_session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    deleted = make_vendor(db_session)
    deleted.soft_delete(admin.id)
    db_session.commit()
    foreign = make_vendor(db_session, company_id=2)
    for vendor in (deleted, foreign):
        response = client.put(f"{VENDORS}/{vendor.id}/approval", headers=headers_for(admin), json=approval_body(vendor))
        assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    db_session.expire_all()
    assert db_session.get(Vendor, foreign.id).approval_status is None


def test_a_stale_expected_updated_at_is_409_and_writes_nothing(client, db_session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = make_vendor(db_session)
    stale = (vendor.updated_at - timedelta(minutes=5)).replace(tzinfo=timezone.utc).isoformat()
    response = client.put(
        f"{VENDORS}/{vendor.id}/approval",
        headers=headers_for(admin),
        json=approval_body(vendor, expected_updated_at=stale),
    )
    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert vendor_audit_rows(db_session, vendor.id) == []
    assert db_session.get(Vendor, vendor.id).approval_status is None


@pytest.mark.parametrize(
    "fields",
    [
        {"approval_status": "Probationary"},
        {"approved_scopes": ["Unobtainium"]},
        {"approval_bases": ["Handshake"]},
        {"reason": "   "},
        {"is_approved": True},  # not a field of this verb
    ],
)
def test_bad_bodies_are_422(client, db_session, fields):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = make_vendor(db_session)
    response = client.put(
        f"{VENDORS}/{vendor.id}/approval", headers=headers_for(admin), json=approval_body(vendor, **fields)
    )
    assert response.status_code == 422, response.text


def test_an_api_token_is_refused_403(client, db_session, admin_headers):
    quality = make_user(db_session, role=UserRole.QUALITY, company_id=1)
    headers = api_token_headers(client, admin_headers, quality)
    vendor = make_vendor(db_session)
    response = client.put(f"{VENDORS}/{vendor.id}/approval", headers=headers, json=approval_body(vendor))
    assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
    assert "person" in response.json()["detail"]
    db_session.expire_all()
    assert db_session.get(Vendor, vendor.id).approval_status is None


def test_a_lost_required_audit_rolls_the_approval_back_503(client, db_session, monkeypatch):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = make_vendor(db_session)

    def refusing(self, *args, **kwargs):
        raise AuditWriteError("simulated")

    monkeypatch.setattr(AuditService, "log_required", refusing)
    response = client.put(f"{VENDORS}/{vendor.id}/approval", headers=headers_for(admin), json=approval_body(vendor))
    assert response.status_code == 503, response.text
    db_session.rollback()
    row = db_session.get(Vendor, vendor.id)
    assert row.approval_status is None and row.is_approved is False


# =========================================================================== vendor PUT guard


def test_vendor_put_cannot_contradict_the_asl_derived_approval(client, db_session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = make_vendor(db_session, approval_status="disqualified", is_approved=False)
    refused = client.put(f"{VENDORS}/{vendor.id}", headers=headers_for(admin), json={"version": 0, "is_approved": True})
    assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
    assert "Disqualified" in refused.json()["detail"] and "/approval" in refused.json()["detail"]
    db_session.expire_all()
    assert db_session.get(Vendor, vendor.id).is_approved is False

    # Re-sending the derived value is a harmless no-op.
    same = client.put(f"{VENDORS}/{vendor.id}", headers=headers_for(admin), json={"version": 0, "is_approved": False})
    assert same.status_code == status.HTTP_200_OK, same.text


def test_vendor_put_still_flips_is_approved_for_a_vendor_not_on_the_asl(client, db_session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = make_vendor(db_session)
    response = client.put(
        f"{VENDORS}/{vendor.id}", headers=headers_for(admin), json={"version": 0, "is_approved": True}
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["is_approved"] is True and response.json()["approval_status"] is None


# =========================================================================== re-evaluation due


def test_reevaluation_due_list_filters_and_orders(client, db_session):
    user = make_user(db_session, role=UserRole.VIEWER, company_id=1)
    today = central_today()
    overdue_old = make_vendor(
        db_session, name="Alpha Castings", approval_status="approved", reevaluation_due=today - timedelta(days=40)
    )
    overdue_conditional = make_vendor(
        db_session, name="Beta Bolts", approval_status="conditional", reevaluation_due=today - timedelta(days=1)
    )
    overdue_directed = make_vendor(
        db_session,
        name="Gamma Composites",
        approval_status="approved_customer_directed",
        reevaluation_due=today - timedelta(days=3),
    )
    due_today = make_vendor(db_session, name="Delta Labs", approval_status="approved", reevaluation_due=today)
    upcoming = make_vendor(
        db_session, name="Echo Paints", approval_status="approved", reevaluation_due=today + timedelta(days=20)
    )
    make_vendor(db_session, name="Foxtrot", approval_status="disqualified", reevaluation_due=today - timedelta(days=9))
    make_vendor(db_session, name="Golf", approval_status=None, reevaluation_due=today - timedelta(days=9))
    inactive = make_vendor(
        db_session,
        name="Hotel Metals",
        approval_status="approved",
        reevaluation_due=today - timedelta(days=5),
        is_active=False,
    )
    deleted = make_vendor(
        db_session, name="India", approval_status="approved", reevaluation_due=today - timedelta(days=5)
    )
    deleted.soft_delete(user.id)
    db_session.commit()
    make_vendor(
        db_session, company_id=2, name="Foreign", approval_status="approved", reevaluation_due=today - timedelta(days=5)
    )

    response = client.get(f"{VENDORS}/reevaluation-due", headers=headers_for(user))
    assert response.status_code == status.HTTP_200_OK, response.text  # not shadowed by /vendors/{vendor_id}
    rows = response.json()
    assert [row["id"] for row in rows] == [overdue_old.id, overdue_directed.id, overdue_conditional.id]
    assert [row["days_overdue"] for row in rows] == [40, 3, 1]

    widened = client.get(
        f"{VENDORS}/reevaluation-due", params={"within_days": 30, "include_inactive": True}, headers=headers_for(user)
    ).json()
    ids = [row["id"] for row in widened]
    assert ids == [overdue_old.id, inactive.id, overdue_directed.id, overdue_conditional.id, due_today.id, upcoming.id]
    assert widened[-1]["days_overdue"] == -20 and widened[-2]["days_overdue"] == 0


# =========================================================================== approval check


def test_supplier_approval_check_reports_warnings_and_ignores_foreign_parts(client, db_session):
    user = make_user(db_session, role=UserRole.SUPERVISOR, company_id=1)
    vendor = make_vendor(db_session, approval_status="approved", is_approved=True, approved_scopes=["hardware"])
    hardware = make_part(db_session, part_type="hardware")
    composite = make_part(db_session, part_type="raw_material", supply_scope="raw_material_composite")
    foreign = make_part(db_session, company_id=2, part_type="raw_material", supply_scope="raw_material_composite")
    response = client.post(
        CHECK,
        headers=headers_for(user),
        json={"vendor_id": vendor.id, "part_ids": [hardware.id, composite.id, foreign.id]},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["refused"] is False and body["approval_status"] == "approved"
    assert [(w["code"], w["part_id"]) for w in body["warnings"]] == [("scope_not_approved", composite.id)]
    assert foreign.part_number not in response.text


def test_supplier_approval_check_names_the_disqualified_refusal(client, db_session):
    user = make_user(db_session, role=UserRole.SUPERVISOR, company_id=1)
    vendor = make_vendor(db_session, approval_status="disqualified")
    body = client.post(CHECK, headers=headers_for(user), json={"vendor_id": vendor.id}).json()
    assert body["refused"] is True and body["refusal"] == DISQUALIFIED_TEXT.format(vendor.name)
    foreign = make_vendor(db_session, company_id=2)
    assert client.post(CHECK, headers=headers_for(user), json={"vendor_id": foreign.id}).status_code == 404


# =========================================================================== PO-create doors


def _po_body(vendor, *parts):
    return {
        "vendor_id": vendor.id,
        "lines": [{"part_id": part.id, "quantity_ordered": 2, "unit_price": 5} for part in parts],
    }


def test_po_create_refuses_a_disqualified_vendor_and_writes_nothing(client, db_session):
    manager = make_user(db_session, role=UserRole.MANAGER, company_id=1)
    vendor = make_vendor(db_session, approval_status="disqualified")
    part = make_part(db_session)
    response = client.post(POS, headers=headers_for(manager), json=_po_body(vendor, part))
    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert response.json()["detail"] == DISQUALIFIED_TEXT.format(vendor.name)
    db_session.rollback()
    assert db_session.query(PurchaseOrder).count() == 0


def test_po_create_records_and_returns_the_asl_warnings(client, db_session):
    manager = make_user(db_session, role=UserRole.MANAGER, company_id=1)
    vendor = make_vendor(
        db_session,
        approval_status="conditional",
        approved_scopes=["machining"],
        reevaluation_due=central_today() - timedelta(days=2),
    )
    part = make_part(db_session, part_type="hardware")
    response = client.post(POS, headers=headers_for(manager), json=_po_body(vendor, part))
    assert response.status_code == status.HTTP_200_OK, response.text
    codes = ["vendor_conditional", "reevaluation_overdue", "scope_unverified"]
    assert [w["code"] for w in response.json()["supplier_approval_warnings"]] == codes
    po_id = response.json()["id"]

    db_session.rollback()
    (create_row,) = (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == "purchase_order", AuditLog.resource_id == po_id, AuditLog.action == "CREATE")
        .all()
    )
    assert [w["code"] for w in create_row.extra_data["supplier_approval_warnings"]] == codes

    fetched = client.get(f"{POS}/{po_id}", headers=headers_for(manager))
    assert [w["code"] for w in fetched.json()["supplier_approval_warnings"]] == codes


def test_the_shared_command_refuses_for_hank_and_the_ai_applier(db_session):
    """``create_purchase_order_command`` is the door the Purchasing screen, Hank and the AI
    draft applier share; it refuses before writing anything."""
    manager = make_user(db_session, role=UserRole.MANAGER, company_id=1)
    vendor = make_vendor(db_session, approval_status="disqualified")
    part = make_part(db_session)
    po_in = POCreate(vendor_id=vendor.id, lines=[POLineCreate(part_id=part.id, quantity_ordered=1, unit_price=1)])
    with pytest.raises(HTTPException) as refused:
        create_purchase_order_command(db_session, po_in, manager, 1, AuditService(db_session, manager))
    assert refused.value.status_code == 409
    db_session.rollback()
    assert db_session.query(PurchaseOrder).count() == 0


def test_hank_refuses_at_prepare_and_again_at_execute(client, db_session, auth_headers):
    part = make_part(db_session)
    disqualified = make_vendor(db_session, approval_status="disqualified")

    def propose(vendor):
        return client.post(
            "/api/v1/hank/tasks",
            headers=auth_headers,
            json={
                "expected_company_id": 1,
                "request_key": str(uuid4()),
                "kind": "draft_purchase_order",
                "input": {
                    "vendor_id": vendor.id,
                    "lines": [{"part_id": part.id, "quantity_ordered": 1, "unit_price": 2}],
                },
            },
        )

    refused = propose(disqualified)
    assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
    assert refused.json()["detail"] == DISQUALIFIED_TEXT.format(disqualified.name)

    later = make_vendor(db_session, approval_status="approved", is_approved=True)
    proposed = propose(later)
    assert proposed.status_code == 200, proposed.text
    task = proposed.json()
    db_session.expire_all()
    vendor = db_session.get(Vendor, later.id)
    vendor.approval_status = "disqualified"
    vendor.is_approved = False
    db_session.commit()
    executed = client.post(
        f"/api/v1/hank/tasks/{task['id']}/execute",
        headers=auth_headers,
        json={"expected_company_id": 1, "expected_version": task["version"]},
    )
    assert executed.status_code == status.HTTP_409_CONFLICT, executed.text
    db_session.rollback()
    assert db_session.query(PurchaseOrder).count() == 0


def test_po_upload_refuses_a_disqualified_vendor(client, db_session):
    manager = make_user(db_session, role=UserRole.MANAGER, company_id=1)
    vendor = make_vendor(db_session, approval_status="disqualified")
    part = make_part(db_session)
    payload = {
        "po_number": f"PO-ASL-{_next():05d}",
        "vendor_id": vendor.id,
        "create_vendor": False,
        "line_items": [{"part_id": part.id, "part_number": part.part_number, "quantity_ordered": 1, "unit_price": 1.0}],
        "create_parts": [],
        "pdf_path": "",
    }
    response = client.post("/api/v1/po-upload/create-from-upload", headers=headers_for(manager), json=payload)
    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert response.json()["detail"] == DISQUALIFIED_TEXT.format(vendor.name)
    db_session.rollback()
    assert db_session.query(PurchaseOrder).count() == 0


def test_po_upload_returns_and_records_warnings(client, db_session):
    manager = make_user(db_session, role=UserRole.MANAGER, company_id=1)
    vendor = make_vendor(db_session)  # not on the ASL, not approved
    part = make_part(db_session)
    payload = {
        "po_number": f"PO-ASL-{_next():05d}",
        "vendor_id": vendor.id,
        "line_items": [{"part_id": part.id, "part_number": part.part_number, "quantity_ordered": 1, "unit_price": 1.0}],
        "pdf_path": "",
    }
    response = client.post("/api/v1/po-upload/create-from-upload", headers=headers_for(manager), json=payload)
    assert response.status_code == status.HTTP_200_OK, response.text
    assert [w["code"] for w in response.json()["supplier_approval_warnings"]] == ["vendor_not_on_asl"]
    db_session.rollback()
    row = db_session.query(AuditLog).filter(AuditLog.action == "PO_CREATE_FROM_UPLOAD").one()
    assert row.extra_data["supplier_approval_warnings"][0]["code"] == "vendor_not_on_asl"


def test_open_po_loader_reports_a_disqualified_vendor_as_the_row_error(client, db_session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = make_vendor(db_session, approval_status="disqualified")
    part = make_part(db_session)
    csv = f"vendor_code,part_number,quantity,unit_price\n{vendor.code},{part.part_number},3,1.50\n".encode()
    response = client.post(
        f"{POS}/import",
        headers=headers_for(admin),
        files={"file": ("open-pos.csv", io.BytesIO(csv), "text/csv")},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    reasons = [error["reason"] for error in response.json()["errors"]]
    assert DISQUALIFIED_TEXT.format(vendor.name) in reasons
    db_session.rollback()
    assert db_session.query(PurchaseOrder).count() == 0


def test_automatic_vendor_pickers_skip_disqualified_vendors(db_session):
    on_list = make_vendor(db_session, approval_status="approved")
    off_list = make_vendor(db_session)
    make_vendor(db_session, approval_status="disqualified")
    picked = {v.id for v in db_session.query(Vendor).filter(Vendor.company_id == 1, not_disqualified_clause())}
    assert picked == {on_list.id, off_list.id}


# =========================================================================== Part.supply_scope


def test_supply_scope_is_settable_through_both_part_doors(client, db_session):
    manager = make_user(db_session, role=UserRole.MANAGER, company_id=1)
    part = make_part(db_session, part_type="raw_material")
    via_parts = client.put(
        f"/api/v1/parts/{part.id}",
        headers=headers_for(manager),
        json={"version": 0, "supply_scope": "Raw Material – Metalics"},
    )
    assert via_parts.status_code == status.HTTP_200_OK, via_parts.text
    assert via_parts.json()["supply_scope"] == "raw_material_metallics"
    via_materials = client.put(
        f"/api/v1/materials/{part.id}", headers=headers_for(manager), json={"version": 0, "supply_scope": "Hardware "}
    )
    assert via_materials.status_code == status.HTTP_200_OK, via_materials.text
    assert via_materials.json()["supply_scope"] == "hardware"
    cleared = client.put(
        f"/api/v1/parts/{part.id}", headers=headers_for(manager), json={"version": 0, "supply_scope": None}
    )
    assert cleared.json()["supply_scope"] is None
    bad = client.put(
        f"/api/v1/parts/{part.id}", headers=headers_for(manager), json={"version": 0, "supply_scope": "Unobtainium"}
    )
    assert bad.status_code == 422


def test_materials_import_takes_an_optional_supply_scope_column(client, db_session):
    manager = make_user(db_session, role=UserRole.MANAGER, company_id=1)
    n = _next()
    csv = (
        "part_number,name,part_type,supply_scope\n"
        f"RM-ASL-{n},Sheet stock,raw_material,Raw Material – Metalics\n"
        f"HW-ASL-{n},Locknut,hardware,\n"
        f"CON-ASL-{n},Tape,consumable,Unobtainium\n"
    ).encode()
    response = client.post(
        "/api/v1/materials/import-csv",
        headers=headers_for(manager),
        files={"file": ("materials.csv", io.BytesIO(csv), "text/csv")},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["imported_count"] == 2 and len(body["errors"]) == 1
    assert "Unknown scope of supply" in body["errors"][0]["reason"]
    db_session.expire_all()
    scopes = {
        p.part_number: p.supply_scope
        for p in db_session.query(Part).filter(Part.part_number.in_([f"RM-ASL-{n}", f"HW-ASL-{n}"]))
    }
    assert scopes == {f"RM-ASL-{n}": "raw_material_metallics", f"HW-ASL-{n}": None}


# =========================================================================== legacy-table reads


def test_scorecard_reviews_due_counts_the_vendor_reevaluation_dates(client, db_session):
    user = make_user(db_session, role=UserRole.VIEWER, company_id=1)
    today = date.today()
    make_vendor(db_session, approval_status="approved", reevaluation_due=today + timedelta(days=10))
    make_vendor(db_session, approval_status="conditional", reevaluation_due=today - timedelta(days=10))
    make_vendor(db_session, approval_status="approved", reevaluation_due=today + timedelta(days=90))
    make_vendor(db_session, approval_status="disqualified", reevaluation_due=today)
    make_vendor(db_session, company_id=2, approval_status="approved", reevaluation_due=today)
    body = client.get("/api/v1/supplier-scorecards/supplier-scorecards/dashboard", headers=headers_for(user)).json()
    assert body["reviews_due_30_days"] == 2


# =========================================================================== approval-verb field semantics


def test_an_explicit_approval_date_is_kept_and_a_reapproval_never_restamps_it(client, db_session):
    """Only BECOMING approved with no date sent records today. A date the auditor sends is
    the record; and re-saving an already-approved vendor (say, to set its next re-evaluation)
    must not rewrite when it was approved -- that would falsify the approval history."""
    quality = make_user(db_session, role=UserRole.QUALITY, company_id=1)
    conditional = make_vendor(db_session, approval_status="conditional", is_approved=False)
    sent = client.put(
        f"{VENDORS}/{conditional.id}/approval",
        headers=headers_for(quality),
        json=approval_body(conditional, approval_date="2025-06-01"),
    )
    assert sent.status_code == status.HTTP_200_OK, sent.text
    assert sent.json()["approval_date"] == "2025-06-01" and sent.json()["is_approved"] is True

    approved = make_vendor(db_session, approval_status="approved", is_approved=True, approval_date=date(2020, 1, 15))
    resaved = client.put(
        f"{VENDORS}/{approved.id}/approval",
        headers=headers_for(quality),
        json=approval_body(approved, reevaluation_due="2028-01-15"),
    )
    assert resaved.status_code == status.HTTP_200_OK, resaved.text
    assert resaved.json()["approval_date"] == "2020-01-15" and resaved.json()["reevaluation_due"] == "2028-01-15"
    (audit,) = vendor_audit_rows(db_session, approved.id)
    assert set(audit.new_values) == {"reevaluation_due"}


def test_an_explicit_null_clears_a_field_and_an_omitted_one_is_kept(client, db_session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = make_vendor(
        db_session,
        approval_status="approved",
        is_approved=True,
        reevaluation_due=date(2027, 1, 1),
        approval_notes="Keep me",
        approved_scopes=["hardware"],
    )
    response = client.put(
        f"{VENDORS}/{vendor.id}/approval",
        headers=headers_for(admin),
        json=approval_body(vendor, reevaluation_due=None, approved_scopes=[]),
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["reevaluation_due"] is None and body["approved_scopes"] is None
    assert body["approval_notes"] == "Keep me"
    (audit,) = vendor_audit_rows(db_session, vendor.id)
    assert audit.new_values == {"approved_scopes": None, "reevaluation_due": None}


# =========================================================================== PO responses


def test_po_update_and_supplier_confirmation_responses_carry_current_warnings(client, db_session):
    """The warnings are computed from the vendor's CURRENT ASL entry on every PO response, not
    stored: a PO raised while the vendor was fine shows the warning once it goes Conditional."""
    manager = make_user(db_session, role=UserRole.MANAGER, company_id=1)
    vendor = make_vendor(db_session, approval_status="approved", is_approved=True, approved_scopes=["hardware"])
    part = make_part(db_session, part_type="hardware")
    created = client.post(POS, headers=headers_for(manager), json=_po_body(vendor, part))
    assert created.status_code == status.HTTP_200_OK, created.text
    assert created.json()["supplier_approval_warnings"] == []
    po_id = created.json()["id"]

    db_session.expire_all()
    stored_vendor = db_session.get(Vendor, vendor.id)
    stored_vendor.approval_status = "conditional"
    stored_vendor.is_approved = False
    db_session.commit()

    updated = client.put(f"{POS}/{po_id}", headers=headers_for(manager), json={"version": 0, "notes": "Expedite"})
    assert updated.status_code == status.HTTP_200_OK, updated.text
    assert [w["code"] for w in updated.json()["supplier_approval_warnings"]] == ["vendor_conditional"]

    db_session.expire_all()
    po = db_session.get(PurchaseOrder, po_id)
    po.status = POStatus.SENT
    db_session.commit()
    db_session.refresh(po)
    confirmed = client.put(
        f"{POS}/{po_id}/supplier-confirmation",
        headers=headers_for(manager),
        json={
            "expected_updated_at": po.updated_at.replace(tzinfo=timezone.utc).isoformat(),
            "acknowledged": True,
            "supplier_confirmation_note": "Supplier acknowledged by phone",
        },
    )
    assert confirmed.status_code == status.HTTP_200_OK, confirmed.text
    assert [w["code"] for w in confirmed.json()["supplier_approval_warnings"]] == ["vendor_conditional"]
