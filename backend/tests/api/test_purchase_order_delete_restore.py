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
- **Discovery**: ``GET /purchasing/purchase-orders?deleted_only=true`` -- the only way
  anything can SEE a soft-deleted PO, and therefore the only way the restore verb can
  ever be offered. Covered below: the unset parameter is provably inert, the two views
  partition the same fixture set, the default closed/cancelled exclusion is deliberately
  NOT applied to the deleted view (a CANCELLED-then-deleted PO is exactly what somebody
  wants back), an explicit ``?status=`` still narrows, tenancy holds, and the read gate
  stays ``get_current_user`` while restore stays ADMIN/MANAGER.
- **A deleted PO is not workable**: ``PUT``, ``/send`` and ``/lines`` all 404 on one, so
  the archive cannot be edited or mailed to a vendor. Those verbs never filtered
  ``is_deleted``; harmless while nothing could hand out a deleted PO's id, and no longer
  harmless now that the deleted view hands it to any authenticated reader.
- **Restore refuses onto a deleted vendor** (400 naming it), because ``delete_vendor``
  does not count soft-deleted POs as blocking -- so the delete-PO-then-delete-vendor
  sequence could otherwise resurrect a live order against a vendor that is gone.
"""

from datetime import datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.purchasing import POStatus, PurchaseOrder, PurchaseOrderLine
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


# ===========================================================================
# Discovery: GET /purchase-orders?deleted_only=true (the restore view)
#
# The restore endpoint has existed since this file was written, but nothing could
# reach it: the list endpoint hard-filtered ``is_deleted == False`` with no override,
# so a deleted PO was invisible to every API caller and you could not restore what you
# could not list. ``deleted_only`` is that missing half. The tests below pin both
# halves of the contract -- the flag ON does what it says, and the flag UNSET changes
# nothing.
# ===========================================================================


# A fixed base instant so every fixture PO in a test gets a DISTINCT, KNOWN created_at.
# The endpoint orders by ``created_at DESC``; two POs flushed in the same microsecond
# would make an order assertion flap, and "same order" is one of the things the default
# path must provably preserve.
_BASE_CREATED_AT = datetime(2026, 1, 1, 12, 0, 0)


def _po_with(
    db: Session,
    *,
    company_id: int = 1,
    po_status: POStatus = POStatus.SENT,
    minutes: int = 0,
) -> PurchaseOrder:
    """A live PO in the given status, with a deterministic ``created_at``.

    ``minutes`` places it on the ordering axis: higher = newer = earlier in the
    ``created_at DESC`` list.
    """
    line = make_po_line(db, company_id=company_id, quantity_ordered=10, status_=po_status)
    po = line.purchase_order
    po.created_at = _BASE_CREATED_AT + timedelta(minutes=minutes)
    db.commit()
    db.refresh(po)
    return po


def _list(client: TestClient, user, **params) -> list:
    resp = client.get(PO_BASE, headers=headers_for(user), params=params)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()


def _numbers(rows: list) -> list:
    """PO numbers in response order. Asserting on the natural key, never on a count."""
    return [r["po_number"] for r in rows]


def _restore_rows(db: Session, po_id: int):
    # AuditService.log_update(action="restore") stores the verb upper-cased.
    return [
        log
        for log in db.query(AuditLog).all()
        if log.resource_type == "purchase_order" and log.action == "RESTORE" and log.resource_id == po_id
    ]


# ---------------------------------------------------------------------------
# Both directions, one fixture set
# ---------------------------------------------------------------------------


def test_deleted_only_partitions_the_same_fixture_set(client: TestClient, db_session: Session):
    """The two views must be complementary, asserted against ONE fixture set.

    Checking only that ``deleted_only=true`` returns the deleted PO would pass for an
    endpoint that ignores the flag entirely and returns everything. So both directions
    are asserted here: each PO appears in exactly one of the two views.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    live = _po_with(db_session, minutes=0)
    doomed = _po_with(db_session, minutes=10)
    live_number, doomed_number = live.po_number, doomed.po_number

    assert client.delete(f"{PO_BASE}/{doomed.id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK

    default_view = _numbers(_list(client, admin))
    deleted_view = _numbers(_list(client, admin, deleted_only=True))

    # Live PO: default only.
    assert live_number in default_view
    assert live_number not in deleted_view
    # Deleted PO: deleted view only.
    assert doomed_number in deleted_view
    assert doomed_number not in default_view
    # Disjoint, and between them they account for both fixtures.
    assert set(default_view) & set(deleted_view) == set()


def test_deleted_only_false_is_identical_to_unset(client: TestClient, db_session: Session):
    """The parameter must be provably inert when not set (frozen contract).

    ``deleted_only=false`` and no parameter at all must produce the SAME response body
    -- not "the same rows", the same bytes -- so the flag cannot have introduced a
    second code path that merely happens to agree today.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    _po_with(db_session, minutes=0)
    _po_with(db_session, minutes=10)
    doomed = _po_with(db_session, minutes=20)
    assert client.delete(f"{PO_BASE}/{doomed.id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK

    unset = client.get(PO_BASE, headers=headers_for(admin))
    explicit_false = client.get(PO_BASE, headers=headers_for(admin), params={"deleted_only": False})

    assert unset.status_code == status.HTTP_200_OK, unset.text
    assert explicit_false.status_code == status.HTTP_200_OK, explicit_false.text
    assert unset.json() == explicit_false.json()


# ---------------------------------------------------------------------------
# THE CARVE-OUT: the default closed/cancelled exclusion must NOT reach the deleted view
# ---------------------------------------------------------------------------


def test_deleted_view_does_not_apply_the_default_closed_cancelled_exclusion(client: TestClient, db_session: Session):
    """A CANCELLED-then-deleted and a CLOSED-then-deleted PO must BOTH be listed by
    ``deleted_only=true`` with no ``status`` argument.

    This is the whole reason the carve-out exists. The default list excludes CLOSED and
    CANCELLED when no status is given; a soft-deleted PO can sit in ANY status, and
    "cancelled it, then deleted it, then wanted it back" is one of the likeliest restore
    cases there is. If someone "tidies" the ``elif not deleted_only`` back into a plain
    ``else``, those POs vanish from the ONLY list that can see them, the restore control
    can never be offered for them, and nothing else in the API reaches them either --
    this test fails the moment that happens.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    cancelled = _po_with(db_session, po_status=POStatus.CANCELLED, minutes=0)
    closed = _po_with(db_session, po_status=POStatus.CLOSED, minutes=10)
    sent = _po_with(db_session, po_status=POStatus.SENT, minutes=20)
    cancelled_number, closed_number, sent_number = cancelled.po_number, closed.po_number, sent.po_number

    for po in (cancelled, closed, sent):
        assert client.delete(f"{PO_BASE}/{po.id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK

    deleted_view = _numbers(_list(client, admin, deleted_only=True))
    assert cancelled_number in deleted_view, "a CANCELLED-then-deleted PO is unreachable -- restore cannot find it"
    assert closed_number in deleted_view, "a CLOSED-then-deleted PO is unreachable -- restore cannot find it"
    assert sent_number in deleted_view


def test_default_view_still_excludes_live_closed_and_cancelled(client: TestClient, db_session: Session):
    """Control for the carve-out: the exclusion was skipped on the deleted view ONLY.

    Without this, a change that simply dropped the closed/cancelled exclusion outright
    would satisfy the carve-out test above while silently widening the normal PO list.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    live_open = _po_with(db_session, po_status=POStatus.SENT, minutes=0)
    live_closed = _po_with(db_session, po_status=POStatus.CLOSED, minutes=10)
    live_cancelled = _po_with(db_session, po_status=POStatus.CANCELLED, minutes=20)

    default_view = _numbers(_list(client, admin))
    assert live_open.po_number in default_view
    assert live_closed.po_number not in default_view
    assert live_cancelled.po_number not in default_view


def test_explicit_status_still_narrows_within_the_deleted_view(client: TestClient, db_session: Session):
    """``?status=`` is shared by both views -- the carve-out only skips the DEFAULT
    exclusion, it does not disable explicit filtering."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    cancelled = _po_with(db_session, po_status=POStatus.CANCELLED, minutes=0)
    sent = _po_with(db_session, po_status=POStatus.SENT, minutes=10)
    cancelled_number, sent_number = cancelled.po_number, sent.po_number

    for po in (cancelled, sent):
        assert client.delete(f"{PO_BASE}/{po.id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK

    narrowed = _numbers(_list(client, admin, deleted_only=True, status="cancelled"))
    assert cancelled_number in narrowed
    assert sent_number not in narrowed

    # And the live view filtered to the same status still cannot see the deleted row --
    # ``?status=`` narrows within a view, it never crosses between them.
    live_cancelled = _numbers(_list(client, admin, status="cancelled"))
    assert cancelled_number not in live_cancelled


# ---------------------------------------------------------------------------
# The default path is unchanged: same rows, same order, same exclusions
# ---------------------------------------------------------------------------


def test_default_path_rows_and_order_unchanged(client: TestClient, db_session: Session):
    """A zero-argument request returns exactly the pre-feature result set, in order.

    Five POs spanning every axis the endpoint filters on. The deleted one is
    deliberately the NEWEST, so a broken soft-delete predicate would put it first and
    fail loudly rather than hide at the tail.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    open_old = _po_with(db_session, po_status=POStatus.SENT, minutes=0)
    open_new = _po_with(db_session, po_status=POStatus.APPROVED, minutes=10)
    closed = _po_with(db_session, po_status=POStatus.CLOSED, minutes=20)
    cancelled = _po_with(db_session, po_status=POStatus.CANCELLED, minutes=30)
    doomed = _po_with(db_session, po_status=POStatus.SENT, minutes=40)
    open_old_number, open_new_number = open_old.po_number, open_new.po_number

    assert client.delete(f"{PO_BASE}/{doomed.id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK

    rows = _list(client, admin)
    # Exact list, exact order: created_at DESC, closed/cancelled/deleted excluded.
    assert _numbers(rows) == [open_new_number, open_old_number]
    assert closed.po_number not in _numbers(rows)
    assert cancelled.po_number not in _numbers(rows)

    # The three new fields exist on the default path but assert nothing there.
    for row in rows:
        assert row["is_deleted"] is None
        assert row["deleted_at"] is None
        assert row["deleted_by_name"] is None
        # Nothing else about the row shape moved.
        assert row["line_count"] == 1
        assert row["vendor_name"]


# ---------------------------------------------------------------------------
# The new response fields
# ---------------------------------------------------------------------------


def test_deleted_view_populates_provenance_fields(client: TestClient, db_session: Session):
    """``is_deleted`` / ``deleted_at`` / ``deleted_by_name`` are what make the restore
    decision possible: WHEN it went and WHO sent it there."""
    manager = make_user(db_session, role=UserRole.MANAGER, company_id=1)
    po = _po_with(db_session)
    po_number = po.po_number

    assert client.delete(f"{PO_BASE}/{po.id}", headers=headers_for(manager)).status_code == status.HTTP_200_OK

    row = next(r for r in _list(client, manager, deleted_only=True) if r["po_number"] == po_number)
    assert row["is_deleted"] is True
    assert row["deleted_by_name"] == f"{manager.first_name} {manager.last_name}"

    # UTCModel contract: served as UTC ISO-8601 with a trailing 'Z'. SoftDeleteMixin
    # writes a NAIVE utcnow(), so without ensure_utc stamping tzinfo this would ship
    # without its Z and the frontend would render it in the viewer's timezone.
    assert isinstance(row["deleted_at"], str)
    assert row["deleted_at"].endswith("Z"), row["deleted_at"]
    parsed = datetime.fromisoformat(row["deleted_at"].replace("Z", "+00:00"))
    assert parsed.utcoffset().total_seconds() == 0


def test_deleted_by_name_is_none_when_the_user_row_is_gone(client: TestClient, db_session: Session):
    """Contract: ``deleted_by_name`` is None if the deleter's user row no longer
    resolves -- the row must still be listable and restorable without a name."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    po = _po_with(db_session)
    po_number = po.po_number
    assert client.delete(f"{PO_BASE}/{po.id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK

    # Point deleted_by at a user id that does not exist (deleted_by is a bare Integer
    # column with no FK, which is exactly why this state is representable).
    db_session.expire_all()
    row = db_session.query(PurchaseOrder).filter(PurchaseOrder.id == po.id).one()
    row.deleted_by = 999999
    db_session.commit()

    listed = next(r for r in _list(client, admin, deleted_only=True) if r["po_number"] == po_number)
    assert listed["deleted_by_name"] is None
    assert listed["is_deleted"] is True
    assert listed["deleted_at"] is not None


# ---------------------------------------------------------------------------
# Tenant isolation (invariant 1) on the new view
# ---------------------------------------------------------------------------


def test_deleted_view_is_tenant_scoped_both_ways(client: TestClient, db_session: Session):
    """A new view over previously-invisible rows is exactly where a tenancy leak would
    hide, so both directions are asserted."""
    admin1 = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    admin2 = make_user(db_session, role=UserRole.ADMIN, company_id=2)
    po1 = _po_with(db_session, company_id=1)
    po2 = _po_with(db_session, company_id=2)
    po1_number, po2_number = po1.po_number, po2.po_number

    assert client.delete(f"{PO_BASE}/{po1.id}", headers=headers_for(admin1)).status_code == status.HTTP_200_OK
    assert client.delete(f"{PO_BASE}/{po2.id}", headers=headers_for(admin2)).status_code == status.HTTP_200_OK

    view1 = _numbers(_list(client, admin1, deleted_only=True))
    view2 = _numbers(_list(client, admin2, deleted_only=True))

    assert po1_number in view1 and po2_number not in view1
    assert po2_number in view2 and po1_number not in view2


# ---------------------------------------------------------------------------
# Round trip: delete -> discover -> restore -> gone from the restore view
# ---------------------------------------------------------------------------


def test_delete_discover_restore_round_trip(client: TestClient, db_session: Session):
    """The full loop the feature exists to make possible, plus the audit row for the
    restore (invariant 2 -- the restore is a state change and must be recorded)."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    po = _po_with(db_session, po_status=POStatus.CANCELLED)
    po_id, po_number = po.id, po.po_number

    # 1. Delete: leaves the default list, enters the restore view.
    assert client.delete(f"{PO_BASE}/{po_id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK
    assert po_number in _numbers(_list(client, admin, deleted_only=True))

    # 2. Restore.
    resp = client.post(f"{PO_BASE}/{po_id}/restore", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    # 3. Gone from the restore view; back in the app. (It is CANCELLED, so the default
    #    no-status list still excludes it -- ``?status=cancelled`` is where it lands.)
    assert po_number not in _numbers(_list(client, admin, deleted_only=True))
    assert po_number in _numbers(_list(client, admin, status="cancelled"))
    assert client.get(f"{PO_BASE}/{po_id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK

    # 4. Audited.
    rows = _restore_rows(db_session, po_id)
    assert len(rows) == 1
    assert rows[0].resource_identifier == po_number

    # 5. restore() clears the provenance, so a restored PO carries no stale deleter.
    db_session.expire_all()
    restored = db_session.query(PurchaseOrder).filter(PurchaseOrder.id == po_id).one()
    assert restored.is_deleted is False
    assert restored.deleted_at is None
    assert restored.deleted_by is None


def test_restored_po_can_be_deleted_and_rediscovered(client: TestClient, db_session: Session):
    """The loop is repeatable -- restore does not consume the ability to delete again."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    po = _po_with(db_session)
    po_id, po_number = po.id, po.po_number

    assert client.delete(f"{PO_BASE}/{po_id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK
    assert client.post(f"{PO_BASE}/{po_id}/restore", headers=headers_for(admin)).status_code == status.HTTP_200_OK
    assert client.delete(f"{PO_BASE}/{po_id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK

    assert po_number in _numbers(_list(client, admin, deleted_only=True))


# ---------------------------------------------------------------------------
# Role posture: reading the restore view is NOT gated; restoring is
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.VIEWER, UserRole.SUPERVISOR])
def test_deleted_view_readable_by_roles_below_the_restore_gate(client: TestClient, db_session: Session, role: UserRole):
    """Deliberate posture: the LIST endpoint stays on ``get_current_user``.

    ``deleted_only`` discloses rows the same reader could already see before the delete,
    so no new gate. The privileged act is the restore verb -- which the same user is
    still refused below. Documented so nobody "hardens" the read and breaks the UI's
    ability to show why a PO vanished.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    reader = make_user(db_session, role=role, company_id=1)
    po = _po_with(db_session)
    po_id, po_number = po.id, po.po_number
    assert client.delete(f"{PO_BASE}/{po_id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK

    assert po_number in _numbers(_list(client, reader, deleted_only=True))
    # ...but they cannot act on it.
    assert (
        client.post(f"{PO_BASE}/{po_id}/restore", headers=headers_for(reader)).status_code == status.HTTP_403_FORBIDDEN
    )


def test_restore_forbidden_for_supervisor(client: TestClient, db_session: Session):
    """SUPERVISOR sits below the [ADMIN, MANAGER] restore gate, matching its delete twin."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR, company_id=1)
    po = _po_with(db_session)
    assert client.delete(f"{PO_BASE}/{po.id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK

    resp = client.post(f"{PO_BASE}/{po.id}/restore", headers=headers_for(supervisor))
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text


def test_restore_allowed_for_manager(client: TestClient, db_session: Session):
    """MANAGER is inside the gate -- the positive half of the RBAC pair."""
    manager = make_user(db_session, role=UserRole.MANAGER, company_id=1)
    po = _po_with(db_session)
    po_id, po_number = po.id, po.po_number
    assert client.delete(f"{PO_BASE}/{po_id}", headers=headers_for(manager)).status_code == status.HTTP_200_OK

    resp = client.post(f"{PO_BASE}/{po_id}/restore", headers=headers_for(manager))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert po_number not in _numbers(_list(client, manager, deleted_only=True))


def test_restore_cross_company_po_is_404_and_invisible(client: TestClient, db_session: Session):
    """Company 1 can neither see nor restore company 2's deleted PO."""
    admin1 = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    admin2 = make_user(db_session, role=UserRole.ADMIN, company_id=2)
    other = _po_with(db_session, company_id=2)
    other_id, other_number = other.id, other.po_number
    assert client.delete(f"{PO_BASE}/{other_id}", headers=headers_for(admin2)).status_code == status.HTTP_200_OK

    assert other_number not in _numbers(_list(client, admin1, deleted_only=True))
    resp = client.post(f"{PO_BASE}/{other_id}/restore", headers=headers_for(admin1))
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text

    # Untouched: still deleted, still restorable by its own tenant.
    db_session.expire_all()
    assert db_session.query(PurchaseOrder).filter(PurchaseOrder.id == other_id).one().is_deleted is True


# ---------------------------------------------------------------------------
# A deleted PO is a RECORD, not a workable order: every write verb 404s on it
# ---------------------------------------------------------------------------
#
# The reads all filtered ``is_deleted`` already; the WRITES did not. That was
# theoretical while nothing could hand out the id of a deleted PO -- and it stopped
# being theoretical the moment ``deleted_only=true`` shipped, because that view is open
# to any authenticated reader in the tenant, including the roles deliberately kept BELOW
# ``require_role([ADMIN, MANAGER])`` on restore. A SUPERVISOR who can read the archive
# could otherwise add lines to a deleted DRAFT PO, and an admin could mail one to a
# vendor. One test per verb, each asserting the row did not move, not merely the status
# code -- a 404 that still wrote is the failure worth catching.


def test_update_of_deleted_po_is_404_and_changes_nothing(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    po = _po_with(db_session)
    po_id, before_notes = po.id, po.notes
    assert client.delete(f"{PO_BASE}/{po_id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK

    resp = client.put(
        f"{PO_BASE}/{po_id}",
        headers=headers_for(admin),
        json={"version": 0, "notes": "edited after delete", "status": "closed"},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text

    db_session.expire_all()
    row = db_session.query(PurchaseOrder).filter(PurchaseOrder.id == po_id).one()
    assert row.is_deleted is True
    assert row.notes == before_notes
    assert row.status == POStatus.SENT


def test_send_of_deleted_po_is_404_and_does_not_issue_it(client: TestClient, db_session: Session):
    """The worst of the three: /send stamps order_date and flips status to SENT, i.e. it
    issues an order to a vendor while ``is_deleted`` stays true."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    po = _po_with(db_session, po_status=POStatus.DRAFT)
    po_id = po.id
    assert client.delete(f"{PO_BASE}/{po_id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK

    resp = client.post(f"{PO_BASE}/{po_id}/send", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text

    db_session.expire_all()
    row = db_session.query(PurchaseOrder).filter(PurchaseOrder.id == po_id).one()
    assert row.is_deleted is True
    assert row.status == POStatus.DRAFT


def test_add_line_to_deleted_po_is_404_and_adds_no_line(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10, status_=POStatus.DRAFT)
    po = line.purchase_order
    po_id, part_id = po.id, line.part_id
    lines_before = db_session.query(PurchaseOrderLine).filter(PurchaseOrderLine.purchase_order_id == po_id).count()
    assert client.delete(f"{PO_BASE}/{po_id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK

    resp = client.post(
        f"{PO_BASE}/{po_id}/lines",
        headers=headers_for(admin),
        json={"part_id": part_id, "quantity_ordered": 5, "unit_price": 12.5},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text

    db_session.expire_all()
    assert (
        db_session.query(PurchaseOrderLine).filter(PurchaseOrderLine.purchase_order_id == po_id).count() == lines_before
    )


def test_live_po_write_verbs_still_work(client: TestClient, db_session: Session):
    """Control for the three above: the same calls succeed on a PO that is NOT deleted, so
    the 404s prove the delete filter and not a broken fixture."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10, status_=POStatus.DRAFT)
    po = line.purchase_order

    assert (
        client.put(f"{PO_BASE}/{po.id}", headers=headers_for(admin), json={"version": 0, "notes": "edited"}).status_code
        == status.HTTP_200_OK
    )
    assert (
        client.post(
            f"{PO_BASE}/{po.id}/lines",
            headers=headers_for(admin),
            json={"part_id": line.part_id, "quantity_ordered": 5, "unit_price": 12.5},
        ).status_code
        == status.HTTP_200_OK
    )
    assert client.post(f"{PO_BASE}/{po.id}/send", headers=headers_for(admin)).status_code == status.HTTP_200_OK


# ---------------------------------------------------------------------------
# Restore must not resurrect a PO onto a deleted vendor
# ---------------------------------------------------------------------------
#
# ``delete_vendor`` counts blocking POs with ``is_deleted == False``, so a soft-deleted
# PO does not hold its vendor open. Delete the PO, then the vendor, and an unguarded
# restore brings a live SENT order back against a vendor that no longer exists --
# receivable through ``GET /receiving/open-pos`` (status + is_deleted, no vendor check)
# and a state ``POST /purchase-orders`` refuses outright to create.


def test_restore_refused_when_vendor_is_deleted(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    po = _po_with(db_session)
    po_id, vendor_id = po.id, po.vendor_id
    assert client.delete(f"{PO_BASE}/{po_id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK
    # Allowed precisely BECAUSE the PO is soft-deleted -- that is the sequence this guards.
    assert (
        client.delete(f"/api/v1/purchasing/vendors/{vendor_id}", headers=headers_for(admin)).status_code
        == status.HTTP_200_OK
    )

    resp = client.post(f"{PO_BASE}/{po_id}/restore", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert "vendor" in resp.json()["detail"].lower()

    db_session.expire_all()
    assert db_session.query(PurchaseOrder).filter(PurchaseOrder.id == po_id).one().is_deleted is True


def test_restore_succeeds_once_the_vendor_is_restored(client: TestClient, db_session: Session):
    """The refusal is a sequencing rule, not a dead end: restore the vendor, then the PO."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    po = _po_with(db_session)
    po_id, vendor_id = po.id, po.vendor_id
    assert client.delete(f"{PO_BASE}/{po_id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK
    assert (
        client.delete(f"/api/v1/purchasing/vendors/{vendor_id}", headers=headers_for(admin)).status_code
        == status.HTTP_200_OK
    )
    assert (
        client.post(f"/api/v1/purchasing/vendors/{vendor_id}/restore", headers=headers_for(admin)).status_code
        == status.HTTP_200_OK
    )

    resp = client.post(f"{PO_BASE}/{po_id}/restore", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    db_session.expire_all()
    assert db_session.query(PurchaseOrder).filter(PurchaseOrder.id == po_id).one().is_deleted is False


def test_restore_allowed_when_vendor_is_merely_inactive(client: TestClient, db_session: Session):
    """Deliberately NOT mirroring the create path's ``is_active`` half: a live PO against a
    deactivated vendor is already representable (``PUT /vendors/{id}`` deactivates with no
    PO guard), so refusing here would be stricter than the invariant the router keeps."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    po = _po_with(db_session)
    po_id = po.id
    vendor = po.vendor
    assert client.delete(f"{PO_BASE}/{po_id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK
    vendor.is_active = False
    db_session.commit()

    resp = client.post(f"{PO_BASE}/{po_id}/restore", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
