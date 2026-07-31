"""Vendor soft-delete + restore endpoint coverage, plus the PO-create vendor guard.

Covers the vendor lifecycle endpoints added to ``app/api/endpoints/purchasing.py``:

- ``DELETE /purchasing/vendors/{id}`` soft-deletes (compliance invariant #3): it sets
  ``is_deleted`` AND ``is_active=False``, hides the row from ``list`` / ``get`` (404),
  and writes a tamper-evident ``AuditLog`` DELETE row.
- The **active-PO guardrail**: a vendor referenced by any live (not CLOSED/CANCELLED,
  not soft-deleted) purchase order is refused with a 400.
- RBAC: the gate is ``[ADMIN, MANAGER]`` — OPERATOR / VIEWER get 403, MANAGER 200.
- ``/restore`` un-deletes AND re-activates; a double delete / a restore of a live
  vendor are refused (400); tenant isolation holds (404).
- The remediation to ``create_purchase_order``: a PO can no longer be opened against a
  soft-deleted or deactivated vendor (both 404 "Vendor not found").
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.purchasing import POStatus, Vendor
from app.models.user import UserRole
from tests.api.test_receiving_compliance import _ensure_company, _next, headers_for, make_po_line, make_user

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

VENDOR_BASE = "/api/v1/purchasing/vendors"
PO_BASE = "/api/v1/purchasing/purchase-orders"


def make_vendor(db: Session, *, company_id: int = 1, is_active: bool = True) -> Vendor:
    """A bare, PO-free vendor (so the active-PO guardrail doesn't block a delete)."""
    _ensure_company(db, company_id)
    n = _next()
    vendor = Vendor(
        code=f"VD{n:05d}",
        name=f"Standalone Vendor {n}",
        is_active=is_active,
        is_approved=True,
        company_id=company_id,
    )
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return vendor


def _delete_rows(db: Session, vendor_id: int):
    return [
        log
        for log in db.query(AuditLog).all()
        if log.resource_type == "vendor" and log.action == "DELETE" and log.resource_id == vendor_id
    ]


# ---------------------------------------------------------------------------
# Happy path: soft delete + restore
# ---------------------------------------------------------------------------


def test_delete_soft_deletes_and_deactivates(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = make_vendor(db_session, company_id=1)
    vendor_id = vendor.id

    resp = client.delete(f"{VENDOR_BASE}/{vendor_id}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["can_restore"] is True

    db_session.expire_all()
    row = db_session.query(Vendor).filter(Vendor.id == vendor_id).one()
    assert row.is_deleted is True
    assert row.is_active is False
    assert row.deleted_by == admin.id

    # Hidden from get (404) and from the list.
    assert client.get(f"{VENDOR_BASE}/{vendor_id}", headers=headers_for(admin)).status_code == status.HTTP_404_NOT_FOUND
    list_resp = client.get(VENDOR_BASE, headers=headers_for(admin))
    assert list_resp.status_code == status.HTTP_200_OK
    assert all(v["id"] != vendor_id for v in list_resp.json())


def test_delete_writes_soft_delete_audit_row(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = make_vendor(db_session, company_id=1)

    resp = client.delete(f"{VENDOR_BASE}/{vendor.id}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    rows = _delete_rows(db_session, vendor.id)
    assert len(rows) == 1
    assert rows[0].extra_data.get("soft_delete") is True


def test_restore_reactivates_and_makes_visible(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = make_vendor(db_session, company_id=1)
    vendor_id = vendor.id
    assert client.delete(f"{VENDOR_BASE}/{vendor_id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK

    resp = client.post(f"{VENDOR_BASE}/{vendor_id}/restore", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert "restored" in resp.json()["message"]

    db_session.expire_all()
    row = db_session.query(Vendor).filter(Vendor.id == vendor_id).one()
    assert row.is_deleted is False
    assert row.is_active is True

    list_resp = client.get(VENDOR_BASE, headers=headers_for(admin))
    assert any(v["id"] == vendor_id for v in list_resp.json())


# ---------------------------------------------------------------------------
# Active-PO guardrail
# ---------------------------------------------------------------------------


def test_delete_refused_with_active_po(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    # make_po_line creates a SENT (live) PO against a fresh vendor.
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    vendor_id = line.purchase_order.vendor_id

    resp = client.delete(f"{VENDOR_BASE}/{vendor_id}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert "active purchase order(s)" in resp.json()["detail"]

    db_session.expire_all()
    assert db_session.query(Vendor).filter(Vendor.id == vendor_id).one().is_deleted is False
    assert _delete_rows(db_session, vendor_id) == []


def test_delete_allowed_when_po_is_closed(client: TestClient, db_session: Session):
    """A CLOSED PO is not a 'live' reference — the guardrail counts only
    non-closed/cancelled, non-deleted POs, so the vendor can be deleted."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    vendor_id = line.purchase_order.vendor_id
    line.purchase_order.status = POStatus.CLOSED
    db_session.commit()

    resp = client.delete(f"{VENDOR_BASE}/{vendor_id}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text


# ---------------------------------------------------------------------------
# Idempotency / not-found / tenant isolation
# ---------------------------------------------------------------------------


def test_double_delete_returns_400(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = make_vendor(db_session, company_id=1)
    assert client.delete(f"{VENDOR_BASE}/{vendor.id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK

    second = client.delete(f"{VENDOR_BASE}/{vendor.id}", headers=headers_for(admin))
    assert second.status_code == status.HTTP_400_BAD_REQUEST, second.text
    assert "already deleted" in second.json()["detail"]
    assert len(_delete_rows(db_session, vendor.id)) == 1


def test_restore_non_deleted_returns_400(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = make_vendor(db_session, company_id=1)

    resp = client.post(f"{VENDOR_BASE}/{vendor.id}/restore", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert "not deleted" in resp.json()["detail"]


def test_delete_not_found_returns_404(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    assert client.delete(f"{VENDOR_BASE}/999999", headers=headers_for(admin)).status_code == status.HTTP_404_NOT_FOUND


def test_delete_cross_company_vendor_is_404(client: TestClient, db_session: Session):
    admin1 = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    other = make_vendor(db_session, company_id=2)

    resp = client.delete(f"{VENDOR_BASE}/{other.id}", headers=headers_for(admin1))
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    db_session.expire_all()
    assert db_session.query(Vendor).filter(Vendor.id == other.id).one().is_deleted is False


# ---------------------------------------------------------------------------
# RBAC: gate is [ADMIN, MANAGER]
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.VIEWER, UserRole.SUPERVISOR])
def test_delete_forbidden_for_roles_below_gate(client: TestClient, db_session: Session, role: UserRole):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    user = make_user(db_session, role=role, company_id=1)
    vendor = make_vendor(db_session, company_id=1)

    resp = client.delete(f"{VENDOR_BASE}/{vendor.id}", headers=headers_for(user))
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    assert client.delete(f"{VENDOR_BASE}/{vendor.id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK


def test_delete_allowed_for_manager(client: TestClient, db_session: Session):
    manager = make_user(db_session, role=UserRole.MANAGER, company_id=1)
    vendor = make_vendor(db_session, company_id=1)

    resp = client.delete(f"{VENDOR_BASE}/{vendor.id}", headers=headers_for(manager))
    assert resp.status_code == status.HTTP_200_OK, resp.text


def test_restore_forbidden_for_operator(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    operator = make_user(db_session, role=UserRole.OPERATOR, company_id=1)
    vendor = make_vendor(db_session, company_id=1)
    assert client.delete(f"{VENDOR_BASE}/{vendor.id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK

    resp = client.post(f"{VENDOR_BASE}/{vendor.id}/restore", headers=headers_for(operator))
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text


# ---------------------------------------------------------------------------
# create_purchase_order refuses a soft-deleted / inactive vendor
# ---------------------------------------------------------------------------


def _po_body(vendor_id: int, part_id: int) -> dict:
    return {
        "vendor_id": vendor_id,
        "lines": [{"part_id": part_id, "quantity_ordered": 10, "unit_price": 5.0}],
    }


def test_create_po_succeeds_with_active_vendor(client: TestClient, db_session: Session):
    """Positive control for the two refusal tests below."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)

    resp = client.post(
        PO_BASE,
        headers=headers_for(admin),
        json=_po_body(line.purchase_order.vendor_id, line.part_id),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text


def test_create_po_refuses_soft_deleted_vendor(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    vendor = line.purchase_order.vendor

    vendor.soft_delete(admin.id)
    db_session.commit()

    resp = client.post(PO_BASE, headers=headers_for(admin), json=_po_body(vendor.id, line.part_id))
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json()["detail"] == "Vendor not found"


def test_create_po_refuses_inactive_vendor(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    vendor = line.purchase_order.vendor

    vendor.is_active = False
    db_session.commit()

    resp = client.post(PO_BASE, headers=headers_for(admin), json=_po_body(vendor.id, line.part_id))
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json()["detail"] == "Vendor not found"
