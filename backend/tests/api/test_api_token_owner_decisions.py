"""Four writes a FULL-scope API token may make -- owner decision 2026-11-20.

The owner of the shop ruled (2026-11-20) that a standing API token minted ``scope = "full"`` MAY:

1. edit the purchase-order quality flow-down settings and the supplier quality clause catalog
   (``PUT /purchasing/flowdown-profile``, ``POST`` / ``PUT /purchasing/quality-clauses``, #367);
2. record or change who owns stock -- the change-of-ownership verb
   (``POST /inventory/items/{id}/ownership``) and ownership recorded at receipt, through every
   receipt door (``POST /inventory/receive``, and a PO receipt through ``POST /receiving/receive``
   or ``POST /receiving/deliveries``), #368;
3. verify QMS clause evidence (``PUT /qms-standards/evidence/{id}`` with ``is_verified``);
4. move a PM schedule's ``next_due_date`` (``PUT /maintenance/schedules/{id}``).

None of the four needed a code change: no route refuses a full token. This module pins that the
four stay open, in one place, and that every such write stays attributed: the stored row and the
audit row name the token's BOUND user, and the audit row carries ``extra_data.credential``
(``kind`` ``api_token``, the token's id and label) so a token's writes stay distinguishable from
the person's own. Every token here is minted the way an Admin mints one (``POST /api-tokens/``),
and each test first proves the row it minted really is full-scope and not AI-external.

Not covered here, on purpose:

* **Read-only and AI-external tokens stay refused** all four writes, unchanged by the decision.
  ``tests/test_api_token_read_only.py`` walks every non-GET route in ``app.routes`` with a
  read-only token and expects 403; an AI-external token is read-only whatever its scope
  (``tests/api/test_ai_external_token.py``).
* The required-audit 503 paths, role gates and refusals of each route live in that route's own
  module (``test_po_quality_flowdown.py``, ``test_owned_property.py``,
  ``test_qms_evidence_required_audit.py``, ``test_maintenance_pm_roll_forward.py`` /
  ``test_maintenance_checklist_completion.py``). Decision (3)'s positive case was already pinned
  by ``test_qms_evidence_required_audit.py::test_row_written_under_an_api_token_carries_the_credential_marker``;
  it is repeated here so all four decisions sit together.
* The one token refusal on the schedule ``PUT`` -- taking checklist steps off a schedule with an
  open work order (owner decision 2026-11-17) -- is ``test_maintenance_checklist_completion.py``'s.
  It is not a due-date refusal: case (4) moves the date with a PM work order open too.

Every name here is invented.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional
from unittest.mock import Mock

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.api_token import ApiToken
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.inventory import InventoryItem, InventoryLocation, InventoryTransaction, TransactionType
from app.models.maintenance import MaintenanceSchedule, MaintenanceStatus, MaintenanceWorkOrder
from app.models.part import Part
from app.models.po_flowdown import POFlowdownProfile, POQualityClause
from app.models.purchasing import POReceipt
from app.models.qms_standard import QMSClause, QMSClauseEvidence, QMSStandard
from app.models.user import User, UserRole
from app.services.company_time import company_today
from tests.api.export_control_helpers import bearer, issue_token
from tests.api.test_maintenance_pm_roll_forward import make_schedule
from tests.api.test_receiving_compliance import _next, headers_for, make_location, make_po_line, make_user

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_ID = 1  # seeded by the db_session fixture

FLOWDOWN_PROFILE_URL = "/api/v1/purchasing/flowdown-profile"
QUALITY_CLAUSES_URL = "/api/v1/purchasing/quality-clauses"
RECEIVE_URL = "/api/v1/inventory/receive"
PO_RECEIVE_URL = "/api/v1/receiving/receive"
DELIVERY_URL = "/api/v1/receiving/deliveries"
SCHEDULES_URL = "/api/v1/maintenance/schedules"
MAINTENANCE_WORK_ORDERS_URL = "/api/v1/maintenance/work-orders"


# ---------------------------------------------------------------------------
# The token, and what "attributed" means
# ---------------------------------------------------------------------------


@dataclass
class FullToken:
    id: int
    label: str
    holder: User
    headers: Dict[str, str]


def _mint_full_token(client: TestClient, admin_headers: dict, db: Session, holder: User) -> FullToken:
    """A FULL, non-AI-external token bound to ``holder``, minted through ``POST /api-tokens/``.

    The row is read back so a test can never pass on a token that was quietly narrower than the
    decision it pins (a read-only one would be refused 403 at the method gate anyway, but an
    unchecked mint default is exactly what a later change could flip).
    """
    token_id, token = issue_token(client, admin_headers, holder, scope="full")
    db.expire_all()
    row = db.get(ApiToken, token_id)
    assert (row.scope, row.ai_external, row.revoked) == ("full", False, False)
    assert row.user_id == holder.id
    return FullToken(id=token_id, label=row.label, holder=holder, headers=bearer(token))


def _assert_written_under(row: AuditLog, token: FullToken) -> None:
    """The audit row is the bound person's, and says a token -- this token -- wrote it."""
    assert row.user_id == token.holder.id
    assert row.company_id == COMPANY_ID
    credential = (row.extra_data or {}).get("credential")
    assert credential is not None, f"{row.resource_type} {row.action} row carries no credential marker"
    assert credential["kind"] == "api_token"
    assert credential["api_token_id"] == token.id
    assert credential["label"] == token.label
    assert credential["jti_prefix"]


def _audit_rows(db: Session, resource_type: str, resource_id: int, action: Optional[str] = None) -> List[AuditLog]:
    """COMMITTED audit rows for one resource (roll back first: the client shares this session)."""
    db.rollback()
    query = db.query(AuditLog).filter(AuditLog.resource_type == resource_type, AuditLog.resource_id == resource_id)
    if action is not None:
        query = query.filter(AuditLog.action == action)
    return query.order_by(AuditLog.id).all()


# ---------------------------------------------------------------------------
# 1. PO quality flow-down settings and clause catalog (#367)
# ---------------------------------------------------------------------------


def test_a_quality_users_full_token_saves_the_flowdown_profile_and_edits_the_clause_catalog(
    client: TestClient, db_session: Session, admin_headers: dict
):
    holder = make_user(db_session, role=UserRole.QUALITY, company_id=COMPANY_ID)
    token = _mint_full_token(client, admin_headers, db_session, holder)

    # The profile: the first save creates it (version 0 -> 1), a second save updates it (1 -> 2).
    created = client.put(
        FLOWDOWN_PROFILE_URL,
        json={
            "enabled": True,
            "heading": "Supplier Quality Terms (token test)",
            "document_reference": "SQR-TOKEN Rev A",
            "body": "Invented flow-down paragraph.",
            "version": 0,
        },
        headers=token.headers,
    )
    assert created.status_code == status.HTTP_200_OK, created.text
    assert (created.json()["enabled"], created.json()["version"]) == (True, 1)
    profile_id = created.json()["id"]

    updated = client.put(
        FLOWDOWN_PROFILE_URL,
        json={
            "enabled": False,
            "heading": "Supplier Quality Terms (token test)",
            "document_reference": "SQR-TOKEN Rev B",
            "body": "Invented flow-down paragraph, revised.",
            "version": 1,
        },
        headers=token.headers,
    )
    assert updated.status_code == status.HTTP_200_OK, updated.text
    assert (updated.json()["enabled"], updated.json()["version"]) == (False, 2)

    db_session.expire_all()
    profile = db_session.get(POFlowdownProfile, profile_id)
    assert (profile.enabled, profile.document_reference, profile.version) == (False, "SQR-TOKEN Rev B", 2)
    assert profile.updated_by == holder.id

    profile_rows = _audit_rows(db_session, "po_flowdown_profile", profile_id)
    assert [row.action for row in profile_rows] == ["CREATE", "UPDATE"]
    for row in profile_rows:
        _assert_written_under(row, token)
    assert profile_rows[1].extra_data["changes"]["enabled"] == {"old": True, "new": False}

    # The clause catalog: add a clause, then edit it.
    added = client.post(
        QUALITY_CLAUSES_URL,
        json={"code": "QTK1", "title": "Invented token clause", "body": "Invented clause text.", "sort_order": 5},
        headers=token.headers,
    )
    assert added.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), added.text
    clause_id = added.json()["id"]
    assert added.json()["created_by"] == holder.id

    edited = client.put(
        f"{QUALITY_CLAUSES_URL}/{clause_id}",
        json={"title": "Invented token clause, retitled", "default_selected": True},
        headers=token.headers,
    )
    assert edited.status_code == status.HTTP_200_OK, edited.text

    db_session.expire_all()
    clause = db_session.get(POQualityClause, clause_id)
    assert (clause.code, clause.title, clause.default_selected) == ("QTK1", "Invented token clause, retitled", True)
    assert (clause.created_by, clause.updated_by) == (holder.id, holder.id)

    clause_rows = _audit_rows(db_session, "po_quality_clause", clause_id)
    assert [row.action for row in clause_rows] == ["CREATE", "UPDATE"]
    for row in clause_rows:
        _assert_written_under(row, token)
    assert clause_rows[1].extra_data["changes"]["title"] == {
        "old": "Invented token clause",
        "new": "Invented token clause, retitled",
    }


# ---------------------------------------------------------------------------
# 2. Stock ownership: the change-of-ownership verb and ownership at receipt (#368)
# ---------------------------------------------------------------------------


def _part(db: Session) -> Part:
    n = _next()
    part = Part(
        part_number=f"TOK-OWN-{n:05d}",
        name=f"Invented token ownership part {n}",
        part_type="purchased",
        unit_of_measure="each",
        is_active=True,
        company_id=COMPANY_ID,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def _location(db: Session) -> InventoryLocation:
    n = _next()
    location = InventoryLocation(
        code=f"TOK-LOC-{n:05d}", name=f"Invented rack {n}", warehouse="MAIN", is_active=True, company_id=COMPANY_ID
    )
    db.add(location)
    db.commit()
    db.refresh(location)
    return location


def _customer(db: Session) -> Customer:
    n = _next()
    customer = Customer(name=f"Invented Rotor Works {n}", code=f"IRW{n:05d}", company_id=COMPANY_ID)
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def _company_stock(db: Session, part: Part, *, qty: float, lot: str) -> InventoryItem:
    item = InventoryItem(
        part_id=part.id,
        location="RAW-TOK",
        warehouse="MAIN",
        quantity_on_hand=qty,
        quantity_allocated=0.0,
        quantity_available=qty,
        lot_number=lot,
        unit_cost=2.0,
        received_date=datetime(2026, 3, 1, 12, 0, 0),
        status="available",
        is_active=True,
        company_id=COMPANY_ID,
        ownership="company",
        owner_customer_id=None,
        owner_vendor_id=None,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def test_a_supervisors_full_token_re_owns_a_stock_row(client: TestClient, db_session: Session, admin_headers: dict):
    holder = make_user(db_session, role=UserRole.SUPERVISOR, company_id=COMPANY_ID)
    token = _mint_full_token(client, admin_headers, db_session, holder)
    part = _part(db_session)
    item = _company_stock(db_session, part, qty=8, lot="TOK-HEAT-1")
    customer = _customer(db_session)

    response = client.post(
        f"/api/v1/inventory/items/{item.id}/ownership",
        json={
            "ownership": "customer",
            "owner_customer_id": customer.id,
            "reason": "  Customer-furnished bar stock, found on the rack  ",
        },
        headers=token.headers,
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert (response.json()["ownership"], response.json()["owner_customer_id"]) == ("customer", customer.id)
    db_session.expire_all()
    stored = db_session.get(InventoryItem, item.id)
    assert (stored.ownership, stored.owner_customer_id, stored.owner_vendor_id) == ("customer", customer.id, None)
    assert stored.quantity_on_hand == 8.0  # ownership is not a quantity movement

    [row] = [
        row
        for row in _audit_rows(db_session, "inventory", item.id, action="UPDATE")
        if (row.extra_data or {}).get("change") == "ownership"
    ]
    _assert_written_under(row, token)
    assert row.extra_data["reason"] == "Customer-furnished bar stock, found on the rack"
    assert (row.old_values["ownership"], row.new_values["ownership"]) == ("company", "customer")
    assert row.new_values["owner_customer_id"] == customer.id


def test_a_supervisors_full_token_records_ownership_at_receipt(
    client: TestClient, db_session: Session, admin_headers: dict
):
    holder = make_user(db_session, role=UserRole.SUPERVISOR, company_id=COMPANY_ID)
    token = _mint_full_token(client, admin_headers, db_session, holder)
    part = _part(db_session)
    location = _location(db_session)
    customer = _customer(db_session)

    response = client.post(
        RECEIVE_URL,
        json={
            "part_id": part.id,
            "location_code": location.code,
            "quantity": 6,
            "lot_number": "TOK-CFM-1",
            "ownership": "customer",
            "owner_customer_id": customer.id,
        },
        headers=token.headers,
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    item_id = response.json()["inventory_item_id"]
    db_session.expire_all()
    stored = db_session.get(InventoryItem, item_id)
    assert (stored.ownership, stored.owner_customer_id, stored.quantity_on_hand) == ("customer", customer.id, 6.0)

    [txn] = (
        db_session.query(InventoryTransaction)
        .filter(
            InventoryTransaction.inventory_item_id == item_id,
            InventoryTransaction.transaction_type == TransactionType.RECEIVE,
        )
        .all()
    )
    assert txn.created_by == holder.id
    [row] = _audit_rows(db_session, "inventory", txn.id, action="CREATE")
    _assert_written_under(row, token)
    assert row.description.endswith("property"), row.description  # names whose property it is


@pytest.mark.parametrize("door", ["receive", "deliveries"])
def test_a_supervisors_full_token_records_ownership_on_a_po_receipt(
    client: TestClient, db_session: Session, admin_headers: dict, monkeypatch, door: str
):
    """The PO receipt doors pin the same decision: one PO line received dock-to-stock as
    customer-owned material, through ``POST /receiving/receive`` or as a one-line batch through
    ``POST /receiving/deliveries``. The owner lands on the receipt and on the stock row, and every
    audit row the receipt writes is the bound person's and carries the token's credential marker.
    """
    monkeypatch.setattr("app.api.endpoints.receiving.enqueue_job_best_effort", Mock())  # no label worker
    holder = make_user(db_session, role=UserRole.SUPERVISOR, company_id=COMPANY_ID)
    token = _mint_full_token(client, admin_headers, db_session, holder)
    line = make_po_line(db_session, company_id=COMPANY_ID, quantity_ordered=10)
    line_id, po_id, part_id = line.id, line.purchase_order_id, line.part_id
    location = make_location(db_session, company_id=COMPANY_ID)
    location_code = location.code
    customer = _customer(db_session)
    customer_id, customer_name = customer.id, customer.name
    receipt_line = {
        "po_line_id": line_id,
        "quantity_received": 5,
        "lot_number": f"TOK-PO-CFM-{_next():05d}",
        "location_id": location.id,
        "requires_inspection": False,
        "ownership": "customer",
        "owner_customer_id": customer_id,
    }

    batch_id: Optional[int] = None
    if door == "receive":
        response = client.post(PO_RECEIVE_URL, json=receipt_line, headers=token.headers)
        assert response.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), response.text
        receipt_body = response.json()
    else:
        response = client.post(
            DELIVERY_URL,
            json={
                "idempotency_key": f"tok-own-dlv-{_next():05d}",
                "purchase_order_id": po_id,
                "lines": [receipt_line],
            },
            headers=token.headers,
        )
        assert response.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), response.text
        [receipt_body] = response.json()["receipts"]
        batch_id = response.json()["batch_id"]

    owner_key = ("customer", customer_id, None)
    assert (receipt_body["ownership"], receipt_body["owner_customer_id"], receipt_body["owner_vendor_id"]) == owner_key
    receipt_id = receipt_body["id"]

    db_session.expire_all()
    receipt = db_session.get(POReceipt, receipt_id)
    assert (receipt.ownership, receipt.owner_customer_id, receipt.owner_vendor_id) == owner_key
    assert receipt.received_by == holder.id

    [stored] = db_session.query(InventoryItem).filter(InventoryItem.part_id == part_id).all()
    item_id = stored.id
    assert (stored.ownership, stored.owner_customer_id, stored.owner_vendor_id) == owner_key
    assert (stored.location, stored.quantity_on_hand) == (location_code, 5.0)
    [txn] = (
        db_session.query(InventoryTransaction)
        .filter(
            InventoryTransaction.inventory_item_id == item_id,
            InventoryTransaction.transaction_type == TransactionType.RECEIVE,
        )
        .all()
    )
    assert txn.created_by == holder.id

    [receipt_row] = _audit_rows(db_session, "receipt", receipt_id, action="CREATE")
    _assert_written_under(receipt_row, token)
    [stock_row] = _audit_rows(db_session, "inventory", item_id, action="CREATE")
    _assert_written_under(stock_row, token)
    assert stock_row.description.endswith(f"as customer-owned ({customer_name}) property"), stock_row.description
    [po_row] = _audit_rows(db_session, "purchase_order", po_id, action="STATUS_CHANGE")  # SENT -> PARTIAL
    _assert_written_under(po_row, token)
    if batch_id is not None:
        [batch_row] = _audit_rows(db_session, "receiving_delivery", batch_id, action="CREATE")
        _assert_written_under(batch_row, token)


# ---------------------------------------------------------------------------
# 3. Verify QMS clause evidence
# ---------------------------------------------------------------------------


def _unverified_evidence(db: Session) -> QMSClauseEvidence:
    n = _next()
    standard = QMSStandard(company_id=COMPANY_ID, name=f"TOKEN-STD-{n}", version="2016", is_active=True)
    db.add(standard)
    db.flush()
    clause = QMSClause(
        company_id=COMPANY_ID,
        standard_id=standard.id,
        clause_number=f"{n}.4",
        title=f"Invented clause {n}",
        compliance_status="not_assessed",
    )
    db.add(clause)
    db.flush()
    evidence = QMSClauseEvidence(
        company_id=COMPANY_ID,
        clause_id=clause.id,
        evidence_type="module",
        title=f"Invented evidence {n}",
        is_verified=False,
        is_auto_linked=False,
    )
    db.add(evidence)
    db.commit()
    db.refresh(evidence)
    return evidence


def test_a_quality_users_full_token_verifies_qms_evidence(client: TestClient, db_session: Session, admin_headers: dict):
    holder = make_user(db_session, role=UserRole.QUALITY, company_id=COMPANY_ID)
    token = _mint_full_token(client, admin_headers, db_session, holder)
    evidence = _unverified_evidence(db_session)
    evidence_id = evidence.id

    response = client.put(
        f"/api/v1/qms-standards/evidence/{evidence_id}", json={"is_verified": True}, headers=token.headers
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["is_verified"] is True
    db_session.expire_all()
    stored = db_session.get(QMSClauseEvidence, evidence_id)
    assert stored.is_verified is True
    # The Verify is stamped with the token's bound person: the audit row is what says a token did it.
    assert stored.verified_by == holder.id
    assert stored.verified_date is not None

    [row] = _audit_rows(db_session, "qms_clause_evidence", evidence_id, action="UPDATE")
    _assert_written_under(row, token)
    assert row.extra_data["changes"]["is_verified"] == {"old": False, "new": True}
    assert row.extra_data["changes"]["verified_by"]["new"] == holder.id


# ---------------------------------------------------------------------------
# 4. Move a PM schedule's next_due_date
# ---------------------------------------------------------------------------


def _open_pm_work_order(client: TestClient, db: Session, schedule: MaintenanceSchedule, person: User) -> int:
    """Start PM the way the page does (a person, not the token): an open work order on the schedule."""
    created = client.post(
        MAINTENANCE_WORK_ORDERS_URL,
        json={"schedule_id": schedule.id, "title": "Invented PM"},
        headers=headers_for(person),
    )
    assert created.status_code == status.HTTP_200_OK, created.text
    db.expire_all()
    work_order = db.get(MaintenanceWorkOrder, created.json()["id"])
    assert work_order.status not in (MaintenanceStatus.COMPLETED, MaintenanceStatus.CANCELLED)
    return work_order.id


@pytest.mark.parametrize("open_work_order", [False, True], ids=["idle-schedule", "pm-work-order-open"])
def test_a_supervisors_full_token_moves_a_pm_schedules_next_due_date(
    client: TestClient, db_session: Session, admin_headers: dict, open_work_order: bool
):
    holder = make_user(db_session, role=UserRole.SUPERVISOR, company_id=COMPANY_ID)
    token = _mint_full_token(client, admin_headers, db_session, holder)
    due: date = company_today(db_session, COMPANY_ID)
    moved_to = due + timedelta(days=30)
    schedule = make_schedule(db_session, next_due=due)
    schedule_id = schedule.id
    if open_work_order:
        _open_pm_work_order(client, db_session, schedule, holder)
    rows_before = len(_audit_rows(db_session, "maintenance_schedule", schedule_id, action="UPDATE"))

    response = client.put(
        f"{SCHEDULES_URL}/{schedule_id}", json={"next_due_date": moved_to.isoformat()}, headers=token.headers
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["next_due_date"] == moved_to.isoformat()
    db_session.expire_all()
    assert db_session.get(MaintenanceSchedule, schedule_id).next_due_date == moved_to

    rows = _audit_rows(db_session, "maintenance_schedule", schedule_id, action="UPDATE")
    assert len(rows) == rows_before + 1
    row = rows[-1]
    _assert_written_under(row, token)
    changes = row.extra_data["changes"]
    assert set(changes["next_due_date"]) == {"old", "new"}
    assert str(changes["next_due_date"]["old"]).startswith(due.isoformat())
    assert str(changes["next_due_date"]["new"]).startswith(moved_to.isoformat())
