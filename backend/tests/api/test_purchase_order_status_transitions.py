"""``PUT /purchasing/purchase-orders/{id}`` holds ``status`` to a transition whitelist.

The handler used to ``setattr(po, "status", POStatus(value))`` for any enum member, so a
Supervisor -- or an agent -- could mark a PO SENT without the Admin/Manager send verb and its
``order_date`` stamp, declare it RECEIVED with nothing received, or reopen a CLOSED one; and
an unknown string reached ``POStatus(value)`` as a 500. ``assert_po_status_edit_allowed``
now runs before the first ``setattr``:

    DRAFT / PENDING_APPROVAL / APPROVED  ->  each other, or CANCELLED
    SENT                                 ->  CANCELLED
    PARTIAL / RECEIVED                   ->  CLOSED
    CLOSED / CANCELLED                   ->  nothing (terminal)

Everything else is a 409 naming the verb that owns the move, and leaves the row (and the
audit chain) untouched. Echoing the current status is always allowed.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.purchasing import POStatus, PurchaseOrder, Vendor
from app.models.user import User, UserRole

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

PO_BASE = "/api/v1/purchasing/purchase-orders"
FIXTURE_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
_seq = {"n": 0}

ALLOWED = {
    POStatus.DRAFT: {POStatus.PENDING_APPROVAL, POStatus.APPROVED, POStatus.CANCELLED},
    POStatus.PENDING_APPROVAL: {POStatus.DRAFT, POStatus.APPROVED, POStatus.CANCELLED},
    POStatus.APPROVED: {POStatus.DRAFT, POStatus.PENDING_APPROVAL, POStatus.CANCELLED},
    POStatus.SENT: {POStatus.CANCELLED},
    POStatus.PARTIAL: {POStatus.CLOSED},
    POStatus.RECEIVED: {POStatus.CLOSED},
    POStatus.CLOSED: set(),
    POStatus.CANCELLED: set(),
}

# The verb a refusal must point at, keyed by what made the move illegal.
TERMINAL_HINT = "Raise a new purchase order"
RECEIVING_HINT = "/receiving/receive"


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _user(db: Session, role: UserRole) -> User:
    n = _next()
    user = User(
        email=f"po-transition-{n}@werco.test",
        employee_id=f"POTR-{n:05d}",
        first_name="Po",
        last_name="Transition",
        hashed_password=FIXTURE_PASSWORD_HASH,
        role=role,
        is_active=True,
        company_id=1,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _headers(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _po(db: Session, po_status: POStatus) -> PurchaseOrder:
    n = _next()
    vendor = Vendor(code=f"VND-TR-{n:05d}", name=f"Transition Vendor {n}", is_active=True, company_id=1)
    db.add(vendor)
    db.flush()
    po = PurchaseOrder(po_number=f"PO-TR-{n:05d}", vendor_id=vendor.id, status=po_status, company_id=1)
    db.add(po)
    db.commit()
    db.refresh(po)
    return po


def _reload(db: Session, po_id: int) -> PurchaseOrder:
    db.rollback()
    db.expire_all()
    return db.get(PurchaseOrder, po_id)


def _committed_po_rows(db: Session, po_id: int):
    db.rollback()
    db.expire_all()
    return (
        db.query(AuditLog)
        .filter(AuditLog.resource_type == "purchase_order", AuditLog.resource_id == po_id)
        .order_by(AuditLog.sequence_number)
        .all()
    )


CASES = [(current, target) for current in POStatus for target in POStatus if target != current]


@pytest.mark.parametrize("current, target", CASES, ids=[f"{c.value}->{t.value}" for c, t in CASES])
def test_put_status_transition_matrix(client: TestClient, db_session: Session, current, target):
    supervisor = _user(db_session, UserRole.SUPERVISOR)
    po = _po(db_session, current)

    response = client.put(
        f"{PO_BASE}/{po.id}", headers=_headers(supervisor), json={"version": 0, "status": target.value}
    )

    if target in ALLOWED[current]:
        assert response.status_code == status.HTTP_200_OK, response.text
        assert _reload(db_session, po.id).status == target
        (row,) = _committed_po_rows(db_session, po.id)
        assert row.extra_data["changes"]["status"] == {"old": current.value, "new": target.value}
        return

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    detail = response.json()["detail"]
    assert po.po_number in detail
    if current in (POStatus.CLOSED, POStatus.CANCELLED):
        assert TERMINAL_HINT in detail
    elif target == POStatus.SENT:
        assert f"/purchase-orders/{po.id}/send" in detail
    elif target in (POStatus.PARTIAL, POStatus.RECEIVED):
        assert RECEIVING_HINT in detail
    # A refusal is raised before the first setattr: row and chain untouched.
    reloaded = _reload(db_session, po.id)
    assert reloaded.status == current
    assert reloaded.order_date is None
    assert _committed_po_rows(db_session, po.id) == []


def test_refused_status_rejects_the_whole_edit(client: TestClient, db_session: Session):
    """A refused status takes the rest of the body down with it -- no half-applied save."""
    po = _po(db_session, POStatus.DRAFT)
    response = client.put(
        f"{PO_BASE}/{po.id}",
        headers=_headers(_user(db_session, UserRole.ADMIN)),
        json={"version": 0, "notes": "should not land", "status": "received"},
    )
    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    reloaded = _reload(db_session, po.id)
    assert reloaded.notes is None and reloaded.status == POStatus.DRAFT


def test_echoing_the_current_status_is_allowed(client: TestClient, db_session: Session):
    po = _po(db_session, POStatus.SENT)
    response = client.put(
        f"{PO_BASE}/{po.id}",
        headers=_headers(_user(db_session, UserRole.SUPERVISOR)),
        json={"version": 0, "status": "sent", "notes": "expedite"},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    reloaded = _reload(db_session, po.id)
    assert reloaded.status == POStatus.SENT and reloaded.notes == "expedite"


@pytest.mark.parametrize("bad", ["shipped", "SENT", "", None])
def test_unknown_or_null_status_is_422_not_500(client: TestClient, db_session: Session, bad):
    po = _po(db_session, POStatus.DRAFT)
    response = client.put(
        f"{PO_BASE}/{po.id}",
        headers=_headers(_user(db_session, UserRole.ADMIN)),
        json={"version": 0, "status": bad},
    )
    assert response.status_code == 422, response.text
    assert _reload(db_session, po.id).status == POStatus.DRAFT
    assert _committed_po_rows(db_session, po.id) == []


def test_send_verb_is_still_the_way_to_sent(client: TestClient, db_session: Session):
    po = _po(db_session, POStatus.APPROVED)
    response = client.post(f"{PO_BASE}/{po.id}/send", headers=_headers(_user(db_session, UserRole.MANAGER)))
    assert response.status_code == status.HTTP_200_OK, response.text
    reloaded = _reload(db_session, po.id)
    assert reloaded.status == POStatus.SENT and reloaded.order_date is not None


@pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.OPERATOR])
def test_read_only_roles_cannot_put(client: TestClient, db_session: Session, role):
    po = _po(db_session, POStatus.DRAFT)
    response = client.put(
        f"{PO_BASE}/{po.id}", headers=_headers(_user(db_session, role)), json={"version": 0, "status": "cancelled"}
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
    assert _reload(db_session, po.id).status == POStatus.DRAFT
    assert _committed_po_rows(db_session, po.id) == []
