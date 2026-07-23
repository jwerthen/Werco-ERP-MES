"""Purchase-order soft-delete + restore endpoint coverage.

Covers the delete/restore endpoints added to ``app/api/endpoints/purchasing.py``:

- ``DELETE /purchasing/purchase-orders/{id}`` soft-deletes (compliance invariant
  #3 — no hard delete): the row is hidden from ``list`` / ``get`` (404), ``is_deleted``
  / ``deleted_by`` are stamped, and a tamper-evident ``AuditLog`` DELETE row is written.
- The **received-material guardrail**: a PO with any line whose
  ``quantity_received > 0`` is refused with a 400 that directs the user to void the
  receipt(s) first, so voided receipts / inventory aren't stranded behind a deleted PO.
- RBAC: the gate is ``[ADMIN, MANAGER]`` — OPERATOR / VIEWER get 403, MANAGER 200.
- ``/restore`` un-deletes; a double delete is refused (400 "already deleted"); a
  restore of a live PO is refused (400 "not deleted"); tenant isolation holds (404).
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.purchasing import POStatus, PurchaseOrder
from app.models.user import UserRole
from tests.api.test_receiving_compliance import headers_for, make_po_line, make_user

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

PO_BASE = "/api/v1/purchasing/purchase-orders"


def _po_for(db: Session, *, company_id: int = 1) -> PurchaseOrder:
    """Return a live PO (SENT, one open line, nothing received) for the company."""
    line = make_po_line(db, company_id=company_id, quantity_ordered=10)
    return line.purchase_order


def _delete_rows(db: Session, po_id: int):
    return [
        log
        for log in db.query(AuditLog).all()
        if log.resource_type == "purchase_order" and log.action == "DELETE" and log.resource_id == po_id
    ]


# ---------------------------------------------------------------------------
# Happy path: soft delete + restore
# ---------------------------------------------------------------------------


def test_delete_soft_deletes_and_hides_row(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    po = _po_for(db_session, company_id=1)
    po_id = po.id

    resp = client.delete(f"{PO_BASE}/{po_id}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["can_restore"] is True

    # Row flagged, deleter stamped -- but NOT physically removed.
    db_session.expire_all()
    row = db_session.query(PurchaseOrder).filter(PurchaseOrder.id == po_id).one()
    assert row.is_deleted is True
    assert row.deleted_by == admin.id
    assert row.deleted_at is not None

    # Hidden from get (404) and from the default list.
    assert client.get(f"{PO_BASE}/{po_id}", headers=headers_for(admin)).status_code == status.HTTP_404_NOT_FOUND
    list_resp = client.get(PO_BASE, headers=headers_for(admin))
    assert list_resp.status_code == status.HTTP_200_OK
    assert all(p["id"] != po_id for p in list_resp.json())


def test_delete_writes_soft_delete_audit_row(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    po = _po_for(db_session, company_id=1)

    resp = client.delete(f"{PO_BASE}/{po.id}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    rows = _delete_rows(db_session, po.id)
    assert len(rows) == 1
    assert rows[0].extra_data.get("soft_delete") is True


def test_restore_undeletes_and_makes_visible_again(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    po = _po_for(db_session, company_id=1)
    po_id = po.id

    assert client.delete(f"{PO_BASE}/{po_id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK

    resp = client.post(f"{PO_BASE}/{po_id}/restore", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert "restored" in resp.json()["message"]

    db_session.expire_all()
    row = db_session.query(PurchaseOrder).filter(PurchaseOrder.id == po_id).one()
    assert row.is_deleted is False

    # Visible again from get + list.
    assert client.get(f"{PO_BASE}/{po_id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK
    list_resp = client.get(PO_BASE, headers=headers_for(admin))
    assert any(p["id"] == po_id for p in list_resp.json())


# ---------------------------------------------------------------------------
# Received-material guardrail
# ---------------------------------------------------------------------------


def test_delete_refused_when_line_has_received_material(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    po_id = line.purchase_order.id

    # Simulate a receipt having landed against the line.
    line.quantity_received = 5
    db_session.commit()

    resp = client.delete(f"{PO_BASE}/{po_id}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert "has received material" in resp.json()["detail"]
    assert "Void the receipt(s) first" in resp.json()["detail"]

    # Nothing changed: PO still live, no delete audit row.
    db_session.expire_all()
    assert db_session.query(PurchaseOrder).filter(PurchaseOrder.id == po_id).one().is_deleted is False
    assert _delete_rows(db_session, po_id) == []


# ---------------------------------------------------------------------------
# Idempotency / not-found / tenant isolation
# ---------------------------------------------------------------------------


def test_double_delete_returns_400(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    po = _po_for(db_session, company_id=1)

    first = client.delete(f"{PO_BASE}/{po.id}", headers=headers_for(admin))
    assert first.status_code == status.HTTP_200_OK, first.text

    second = client.delete(f"{PO_BASE}/{po.id}", headers=headers_for(admin))
    assert second.status_code == status.HTTP_400_BAD_REQUEST, second.text
    assert "already deleted" in second.json()["detail"]

    # The re-delete must not write a second DELETE audit row.
    assert len(_delete_rows(db_session, po.id)) == 1


def test_restore_non_deleted_returns_400(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    po = _po_for(db_session, company_id=1)

    resp = client.post(f"{PO_BASE}/{po.id}/restore", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert "not deleted" in resp.json()["detail"]


def test_delete_not_found_returns_404(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    resp = client.delete(f"{PO_BASE}/999999", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text


def test_restore_not_found_returns_404(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    resp = client.post(f"{PO_BASE}/999999/restore", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text


def test_delete_cross_company_po_is_404(client: TestClient, db_session: Session):
    admin1 = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    other = _po_for(db_session, company_id=2)

    resp = client.delete(f"{PO_BASE}/{other.id}", headers=headers_for(admin1))
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    # The company-2 PO is untouched.
    db_session.expire_all()
    assert db_session.query(PurchaseOrder).filter(PurchaseOrder.id == other.id).one().is_deleted is False


# ---------------------------------------------------------------------------
# RBAC: gate is [ADMIN, MANAGER]
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.VIEWER, UserRole.SUPERVISOR])
def test_delete_forbidden_for_roles_below_gate(client: TestClient, db_session: Session, role: UserRole):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    user = make_user(db_session, role=role, company_id=1)
    po = _po_for(db_session, company_id=1)

    resp = client.delete(f"{PO_BASE}/{po.id}", headers=headers_for(user))
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    # Positive control: the admin can still delete it.
    assert client.delete(f"{PO_BASE}/{po.id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK


def test_delete_allowed_for_manager(client: TestClient, db_session: Session):
    manager = make_user(db_session, role=UserRole.MANAGER, company_id=1)
    po = _po_for(db_session, company_id=1)

    resp = client.delete(f"{PO_BASE}/{po.id}", headers=headers_for(manager))
    assert resp.status_code == status.HTTP_200_OK, resp.text


@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.VIEWER])
def test_restore_forbidden_for_roles_below_gate(client: TestClient, db_session: Session, role: UserRole):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    user = make_user(db_session, role=role, company_id=1)
    po = _po_for(db_session, company_id=1)
    assert client.delete(f"{PO_BASE}/{po.id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK

    resp = client.post(f"{PO_BASE}/{po.id}/restore", headers=headers_for(user))
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text


def test_delete_allowed_when_only_closed_or_cancelled(client: TestClient, db_session: Session):
    """A CLOSED PO carries no live receiving activity — the received-material guard
    keys on the line's ``quantity_received`` (still 0 here), so delete is allowed."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    po = line.purchase_order
    po.status = POStatus.CLOSED
    db_session.commit()

    resp = client.delete(f"{PO_BASE}/{po.id}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
