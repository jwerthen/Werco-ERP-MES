"""shipping/1 + shipping/2: the refusals, rollbacks and read-purity around the CoC and the ship.

Complements ``test_shipment_release_exceptions.py`` (the rule matrix and the happy paths)
with the cases a reviewer asks about next:

* another company's shipment is 404 on ship and on issue, and nothing is written;
* Supervisor / Viewer cannot accept open items, a kiosk-scoped token cannot reach the
  ship verb, and an API token is refused on issue even when a certificate exists;
* malformed acknowledgments (extra key, over-long reason, ``acknowledge_exceptions:
  false``) change nothing;
* both required audits (COC_WITHHELD on ship, COC_ISSUED_OVER_EXCEPTIONS on issue) roll
  the WHOLE unit of work back -- no shipped status, no closed work order, no FG ledger
  row, no certificate;
* the three shipment reads write nothing, and the withheld lookup is tenant-scoped.
"""

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.inventory import InventoryTransaction
from app.models.operational_event import OperationalEvent
from app.models.quality import FAIStatus, NCRDisposition, NCRStatus
from app.models.shipping import CertificateOfConformance, Shipment, ShipmentStatus
from app.models.user import UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.services.audit_service import AuditService, AuditWriteError
from app.services.coc_service import withheld_shipment_ids
from app.services.quality_gate_service import latest_fais_by_work_order
from app.services.shipment_release_service import (
    can_acknowledge_release_exceptions,
    is_quality_only_shipper,
)
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
from tests.api.test_shipment_release_exceptions import (
    _token_headers,
    make_blocker,
    make_fai,
    make_ncr,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

ACK = {"acknowledge_exceptions": True, "reason": "Customer accepted per deviation D-1"}
RELEASE_ACTIONS = ("COC_WITHHELD", "SHIPPED_OVER_QUALITY_EXCEPTIONS", "COC_ISSUED_OVER_EXCEPTIONS")


def _ship_url(shipment) -> str:
    return f"/api/v1/shipping/{shipment.id}/ship"


def _coc_url(shipment) -> str:
    return f"/api/v1/shipping/{shipment.id}/coc"


def _job_with_open_ncr(db: Session, *, company_id: int = COMPANY_A, cert: bool = True, **shipment_kwargs):
    wo = make_wo(db, make_part(db, company_id=company_id), company_id=company_id)
    ncr = make_ncr(db, wo, status_=NCRStatus.OPEN, disposition=NCRDisposition.PENDING)
    shipment = make_shipment(db, wo, cert_of_conformance=cert, company_id=company_id, **shipment_kwargs)
    db.commit()
    return wo, ncr, shipment


def _fresh(db: Session, model, row_id):
    db.expire_all()
    return db.get(model, row_id)


def _release_audit_rows(db: Session, company_id: int | None = None):
    db.expire_all()
    q = db.query(AuditLog).filter(AuditLog.action.in_(RELEASE_ACTIONS))
    if company_id is not None:
        q = q.filter(AuditLog.company_id == company_id)
    return q.all()


def _counts(db: Session, company_id: int = COMPANY_A) -> tuple:
    db.expire_all()
    return (
        db.query(AuditLog).filter(AuditLog.company_id == company_id).count(),
        db.query(OperationalEvent).filter(OperationalEvent.company_id == company_id).count(),
        db.query(CertificateOfConformance).filter(CertificateOfConformance.company_id == company_id).count(),
        db.query(InventoryTransaction).filter(InventoryTransaction.company_id == company_id).count(),
    )


# ---------------------------------------------------------------------------
# Wrong tenant: 404, and nothing written under either company.
# ---------------------------------------------------------------------------


def test_ship_another_companys_shipment_is_404_and_writes_nothing(client: TestClient, db_session: Session):
    manager_a = make_user(db_session, role=UserRole.MANAGER)
    wo_b, _ncr, shipment_b = _job_with_open_ncr(db_session, company_id=COMPANY_B)
    before = _counts(db_session, COMPANY_B)

    resp = client.post(_ship_url(shipment_b), json=ACK, headers=headers_for(manager_a))

    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert _fresh(db_session, Shipment, shipment_b.id).status == ShipmentStatus.PENDING
    assert _fresh(db_session, WorkOrder, wo_b.id).status == WorkOrderStatus.COMPLETE
    assert _counts(db_session, COMPANY_B) == before
    assert _release_audit_rows(db_session) == []


def test_issue_on_another_companys_shipment_is_404_and_mints_nothing(client: TestClient, db_session: Session):
    quality_a = make_user(db_session, role=UserRole.QUALITY)
    _wo_b, _ncr, shipment_b = _job_with_open_ncr(
        db_session, company_id=COMPANY_B, status_=ShipmentStatus.SHIPPED, ship_date=date.today()
    )

    resp = client.post(_coc_url(shipment_b), json=ACK, headers=headers_for(quality_a))

    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert _coc_rows(db_session, shipment_b.id, company_id=COMPANY_B) == []
    assert _release_audit_rows(db_session) == []


# ---------------------------------------------------------------------------
# Who may accept open items.
# ---------------------------------------------------------------------------


def test_supervisor_cannot_acknowledge_open_items(client: TestClient, db_session: Session):
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    wo, _ncr, shipment = _job_with_open_ncr(db_session)

    resp = client.post(_ship_url(shipment), json=ACK, headers=headers_for(supervisor))

    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    assert resp.json()["detail"] == "Only Admin, Manager or Quality can accept open quality items on a job."
    assert _fresh(db_session, Shipment, shipment.id).status == ShipmentStatus.PENDING
    assert _release_audit_rows(db_session) == []


def test_supervisor_without_acknowledgment_gets_the_409_list(client: TestClient, db_session: Session):
    """The list comes before the role answer, so the floor sees WHY it cannot ship."""
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    _wo, ncr, shipment = _job_with_open_ncr(db_session)

    resp = client.post(_ship_url(shipment), headers=headers_for(supervisor))

    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert [e["reference_id"] for e in resp.json()["detail"]["release_exceptions"]] == [ncr.id]


@pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.OPERATOR])
def test_roles_outside_the_gate_are_refused_on_ship_and_issue(client: TestClient, db_session: Session, role):
    user = make_user(db_session, role=role)
    _wo, _ncr, shipment = _job_with_open_ncr(db_session)

    ship = client.post(_ship_url(shipment), json=ACK, headers=headers_for(user))
    issue = client.post(_coc_url(shipment), json=ACK, headers=headers_for(user))

    assert ship.status_code == status.HTTP_403_FORBIDDEN, ship.text
    assert issue.status_code == status.HTTP_403_FORBIDDEN, issue.text
    assert _fresh(db_session, Shipment, shipment.id).status == ShipmentStatus.PENDING
    assert _coc_rows(db_session, shipment.id) == []


def test_kiosk_scoped_token_cannot_ship_or_accept(client: TestClient, db_session: Session):
    """A badge-minted kiosk token is fenced to shop-floor paths, even an Admin's."""
    admin = make_user(db_session, role=UserRole.ADMIN)
    _wo, _ncr, shipment = _job_with_open_ncr(db_session)
    token = create_access_token(subject=admin.id, company_id=COMPANY_A, scope="kiosk")
    headers = {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}

    resp = client.post(_ship_url(shipment), json=ACK, headers=headers)

    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    assert _fresh(db_session, Shipment, shipment.id).status == ShipmentStatus.PENDING
    assert _release_audit_rows(db_session) == []


def test_quality_acknowledging_a_clean_job_is_still_refused(client: TestClient, db_session: Session):
    """An acknowledgment body does not turn Quality into a general shipper."""
    quality = make_user(db_session, role=UserRole.QUALITY)
    wo = make_wo(db_session, make_part(db_session))
    shipment = make_shipment(db_session, wo)
    db_session.commit()

    resp = client.post(_ship_url(shipment), json=ACK, headers=headers_for(quality))

    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    assert "only to accept open quality items" in resp.json()["detail"]
    assert _fresh(db_session, Shipment, shipment.id).status == ShipmentStatus.PENDING


def test_admin_acknowledges_and_the_stored_reason_is_trimmed(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN)
    _wo, _ncr, shipment = _job_with_open_ncr(db_session)

    resp = client.post(
        _ship_url(shipment) + "?tracking_number=1Z-FICTION-1",
        json={"acknowledge_exceptions": True, "reason": "   Deviation D-9 approved   "},
        headers=headers_for(admin),
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    shipped = _fresh(db_session, Shipment, shipment.id)
    assert shipped.status == ShipmentStatus.SHIPPED
    assert shipped.tracking_number == "1Z-FICTION-1"  # the query parameter still works beside the body
    rows = [r for r in _release_audit_rows(db_session) if r.action == "SHIPPED_OVER_QUALITY_EXCEPTIONS"]
    assert len(rows) == 1
    assert rows[0].extra_data["reason"] == "Deviation D-9 approved"
    assert rows[0].user_id == admin.id


def test_platform_principals_may_acknowledge_but_are_never_quality_only():
    superuser = SimpleNamespace(is_superuser=True, role=UserRole.VIEWER)
    platform = SimpleNamespace(is_superuser=False, role=UserRole.PLATFORM_ADMIN)
    supervisor = SimpleNamespace(is_superuser=False, role=UserRole.SUPERVISOR)
    shipping = SimpleNamespace(is_superuser=False, role=UserRole.SHIPPING)
    quality = SimpleNamespace(is_superuser=False, role=UserRole.QUALITY)

    assert can_acknowledge_release_exceptions(superuser)
    assert can_acknowledge_release_exceptions(platform)
    assert can_acknowledge_release_exceptions(quality)
    assert not can_acknowledge_release_exceptions(supervisor)
    assert not can_acknowledge_release_exceptions(shipping)
    assert is_quality_only_shipper(quality)
    assert not is_quality_only_shipper(SimpleNamespace(is_superuser=True, role=UserRole.QUALITY))
    assert not is_quality_only_shipper(supervisor)


# ---------------------------------------------------------------------------
# Malformed acknowledgments change nothing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body, expected",
    [
        ({"acknowledge_exceptions": False, "reason": "Not really"}, status.HTTP_409_CONFLICT),
        ({"acknowledge_exceptions": True, "reason": "ok"}, 422),
        ({"acknowledge_exceptions": True, "reason": "x" * 1001}, 422),
        ({"acknowledge_exceptions": True, "reason": "Fine", "override": True}, 422),
    ],
)
def test_malformed_acknowledgment_ships_nothing(client: TestClient, db_session: Session, body, expected):
    manager = make_user(db_session, role=UserRole.MANAGER)
    wo, _ncr, shipment = _job_with_open_ncr(db_session)
    before = _counts(db_session)

    resp = client.post(_ship_url(shipment), json=body, headers=headers_for(manager))

    assert resp.status_code == expected, resp.text
    assert _fresh(db_session, Shipment, shipment.id).status == ShipmentStatus.PENDING
    assert _fresh(db_session, WorkOrder, wo.id).status == WorkOrderStatus.COMPLETE
    assert _counts(db_session) == before


def test_json_null_body_is_no_acknowledgment(client: TestClient, db_session: Session):
    """The SPA sends a JSON ``null`` body when nothing is acknowledged; a clean job ships."""
    shipper = make_user(db_session, role=UserRole.SHIPPING)
    wo = make_wo(db_session, make_part(db_session))
    shipment = make_shipment(db_session, wo)
    db_session.commit()

    resp = client.post(
        _ship_url(shipment),
        content="null",
        headers={**headers_for(shipper), "Content-Type": "application/json"},
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert _fresh(db_session, Shipment, shipment.id).status == ShipmentStatus.SHIPPED


def test_first_article_and_quality_hold_alone_also_stop_the_ship(client: TestClient, db_session: Session):
    shipper = make_user(db_session, role=UserRole.SHIPPING)
    wo = make_wo(db_session, make_part(db_session))
    fai = make_fai(db_session, wo, FAIStatus.FAILED)
    hold = make_blocker(db_session, wo)
    shipment = make_shipment(db_session, wo)
    db_session.commit()

    resp = client.post(_ship_url(shipment), headers=headers_for(shipper))

    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    listed = [(e["code"], e["reference_type"], e["reference_id"]) for e in resp.json()["detail"]["release_exceptions"]]
    assert listed == [("fai_not_passed", "fai", fai.id), ("open_blocker", "work_order_blocker", hold.id)]


def test_ship_over_open_items_when_no_coc_is_required(client: TestClient, db_session: Session):
    """Acceptance is still recorded; there is no certificate to withhold, so no COC_WITHHELD."""
    manager = make_user(db_session, role=UserRole.MANAGER)
    _wo, _ncr, shipment = _job_with_open_ncr(db_session, cert=False)

    resp = client.post(_ship_url(shipment), json=ACK, headers=headers_for(manager))

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["coc_withheld"] is False
    actions = sorted(r.action for r in _release_audit_rows(db_session))
    assert actions == ["SHIPPED_OVER_QUALITY_EXCEPTIONS"]
    assert _coc_rows(db_session, shipment.id) == []
    listed = {r["id"]: r for r in client.get("/api/v1/shipping/", headers=headers_for(manager)).json()}
    assert listed[shipment.id]["coc_withheld"] is False


# ---------------------------------------------------------------------------
# Required audits roll the whole unit of work back.
# ---------------------------------------------------------------------------


def test_coc_withheld_audit_failure_rolls_back_ship_close_and_ledger(client: TestClient, db_session: Session):
    manager = make_user(db_session, role=UserRole.MANAGER)
    wo, _ncr, shipment = _job_with_open_ncr(db_session, quantity_shipped=10)  # ships the whole order
    before = _counts(db_session)

    with patch("app.api.endpoints.shipping.record_coc_withheld", side_effect=AuditWriteError("chain down")):
        resp = client.post(_ship_url(shipment), json=ACK, headers=headers_for(manager))

    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == (
        "The record of the withheld Certificate of Conformance could not be saved; the shipment was not shipped. Retry."
    )
    assert _fresh(db_session, Shipment, shipment.id).status == ShipmentStatus.PENDING
    # The WO close, its audit row, the shipped event, the SHIPPED_OVER row: all gone.
    assert _fresh(db_session, WorkOrder, wo.id).status == WorkOrderStatus.COMPLETE
    assert _counts(db_session) == before
    assert _release_audit_rows(db_session) == []


def test_issue_over_exceptions_audit_failure_mints_no_certificate(client: TestClient, db_session: Session):
    manager = make_user(db_session, role=UserRole.MANAGER)
    quality = make_user(db_session, role=UserRole.QUALITY)
    _wo, _ncr, shipment = _job_with_open_ncr(db_session)
    shipped = client.post(_ship_url(shipment), json=ACK, headers=headers_for(manager))
    assert shipped.status_code == status.HTTP_200_OK, shipped.text
    flag_before = _fresh(db_session, Shipment, shipment.id).cert_of_conformance
    before = _counts(db_session)

    with patch.object(AuditService, "log_required", side_effect=AuditWriteError("chain down")):
        resp = client.post(_coc_url(shipment), json=ACK, headers=headers_for(quality))

    assert resp.status_code == 503, resp.text
    assert "no certificate was issued" in resp.json()["detail"]
    assert _coc_rows(db_session, shipment.id) == []
    assert _fresh(db_session, Shipment, shipment.id).cert_of_conformance == flag_before
    assert _counts(db_session) == before
    # Still waiting on Quality.
    listed = {r["id"]: r for r in client.get("/api/v1/shipping/", headers=headers_for(quality)).json()}
    assert listed[shipment.id]["coc_withheld"] is True


# ---------------------------------------------------------------------------
# Issue: API token refused even when a certificate exists; delivered is shipped.
# ---------------------------------------------------------------------------


def test_api_token_refused_on_issue_even_when_a_certificate_exists(client: TestClient, db_session: Session):
    quality = make_user(db_session, role=UserRole.QUALITY)
    wo = make_wo(db_session, make_part(db_session))
    shipment = make_shipment(db_session, wo, status_=ShipmentStatus.SHIPPED, ship_date=date.today())
    db_session.commit()
    first = client.post(_coc_url(shipment), headers=headers_for(quality))
    assert first.status_code == status.HTTP_200_OK, first.text

    resp = client.post(_coc_url(shipment), headers=_token_headers(client, db_session, quality))

    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    assert len(_coc_rows(db_session, shipment.id)) == 1


def test_issue_allowed_on_a_delivered_shipment(client: TestClient, db_session: Session):
    quality = make_user(db_session, role=UserRole.QUALITY)
    wo = make_wo(db_session, make_part(db_session))
    shipment = make_shipment(db_session, wo, status_=ShipmentStatus.DELIVERED, ship_date=date.today())
    db_session.commit()

    resp = client.post(_coc_url(shipment), headers=headers_for(quality))

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["coc_number"]
    assert _release_audit_rows(db_session) == []  # nothing open, nothing accepted


@pytest.mark.parametrize("state", [ShipmentStatus.PACKED, ShipmentStatus.CANCELLED])
def test_issue_refused_on_unshipped_states(client: TestClient, db_session: Session, state):
    quality = make_user(db_session, role=UserRole.QUALITY)
    wo = make_wo(db_session, make_part(db_session))
    shipment = make_shipment(db_session, wo, status_=state)
    db_session.commit()

    resp = client.post(_coc_url(shipment), headers=headers_for(quality))

    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert _coc_rows(db_session, shipment.id) == []


# ---------------------------------------------------------------------------
# Pure reads, and the tenant-scoped withheld lookup.
# ---------------------------------------------------------------------------


def test_shipment_reads_write_nothing(client: TestClient, db_session: Session):
    manager = make_user(db_session, role=UserRole.MANAGER)
    _wo, _ncr, withheld = _job_with_open_ncr(db_session)
    assert client.post(_ship_url(withheld), json=ACK, headers=headers_for(manager)).status_code == 200
    _wo2, _ncr2, pending = _job_with_open_ncr(db_session)
    before = _counts(db_session)

    for url in (
        "/api/v1/shipping/",
        "/api/v1/shipping/ready-to-ship",
        f"/api/v1/shipping/{withheld.id}",
        f"/api/v1/shipping/{pending.id}",
    ):
        resp = client.get(url, headers=headers_for(manager))
        assert resp.status_code == status.HTTP_200_OK, f"{url}: {resp.text}"

    assert _counts(db_session) == before
    assert _fresh(db_session, Shipment, pending.id).status == ShipmentStatus.PENDING


def test_withheld_lookup_is_tenant_scoped(client: TestClient, db_session: Session):
    manager = make_user(db_session, role=UserRole.MANAGER)
    _wo, _ncr, shipment = _job_with_open_ncr(db_session)
    assert client.post(_ship_url(shipment), json=ACK, headers=headers_for(manager)).status_code == 200

    assert withheld_shipment_ids(db_session, COMPANY_A, [shipment.id]) == {shipment.id}
    assert withheld_shipment_ids(db_session, COMPANY_B, [shipment.id]) == set()


# ---------------------------------------------------------------------------
# inspection_fai/2: ties on created_at break by id.
# ---------------------------------------------------------------------------


def test_latest_first_article_ties_break_by_id(db_session: Session):
    from app.models.quality import FirstArticleInspection

    wo = make_wo(db_session, make_part(db_session))
    stamp = datetime(2026, 11, 2, 9, 0)
    rows = []
    for status_ in (FAIStatus.FAILED, FAIStatus.PASSED):
        fai = FirstArticleInspection(
            fai_number=f"TIE-FAI-{status_.value}-{wo.id}",
            part_id=wo.part_id,
            work_order_id=wo.id,
            status=status_,
            created_at=stamp,
            company_id=COMPANY_A,
        )
        db_session.add(fai)
        db_session.flush()
        rows.append(fai)

    latest = latest_fais_by_work_order(db_session, COMPANY_A, [wo.id])[wo.id]
    assert [f.id for f in latest] == [rows[1].id]
