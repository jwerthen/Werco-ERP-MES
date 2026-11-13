"""P2.1 -- the CAR matches Werco's controlled form, records the Quality Manager approval, prints.

Covers, through the ``/api/v1/quality`` routes and the complaint escalation:

* the form fields (reason for initiation, RMA #, Date Submitted, part / customer / vendor +
  vendor PO, the four cost buckets, management action, vendor and customer approvals) are
  stored on create and update, committed on the required audit row, and read back with
  resolved names and a computed (never stored) cost total;
* ``Date Submitted`` defaults to today in Central and accepts a past date (paper back-entry);
* the reason ``other`` needs its text (422, nothing written);
* references are tenant-resolved before anything is written: a NEW part / customer must be a
  live row of the company (404 otherwise), a vendor need only be the company's -- an
  inactive or removed supplier is still a legitimate CAR subject (invariant 3) -- and an
  UNCHANGED reference is never re-checked;
* ``POST /quality/car/{id}/quality-approval`` stamps approver + instant once, refuses VOID
  and already-approved (409), API tokens (403) and roles outside Admin/Manager/Quality
  (403), is tenant-isolated (404), and its APPROVE row commits with the stamp or neither
  is saved (503);
* the NCR and complaint escalations prefill the form;
* ``GET /quality/car/{id}/pdf`` prints every form label and the recorded values, takes its
  header from the Company record, renders hostile values literally, and is tenant-isolated.
"""

import itertools
from datetime import date, datetime, timedelta
from io import BytesIO

import pytest
from pypdf import PdfReader
from sqlalchemy import inspect

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.customer import Customer
from app.models.customer_complaint import CustomerComplaint, ReturnMaterialAuthorization
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.purchasing import Vendor
from app.models.quality import (
    CARStatus,
    CorrectiveActionRequest,
    NCRDisposition,
    NCRSource,
    NCRStatus,
    NonConformanceReport,
)
from app.models.user import User, UserRole
from app.services.audit_service import AuditService
from app.services.quality_record_service import API_TOKEN_QUALITY_APPROVAL, central_today
from tests.api.export_audit_helpers import fresh_session

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

BASE = "/api/v1/quality"
API_TOKENS_URL = "/api/v1/api-tokens/"
PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens minted directly; never used for login
_seq = itertools.count(1)

FORM_LABELS = [
    "AS9100 PROCESS - Corrective Action Report",
    "Initiated by:",
    "RMA #:",
    "Corrective Action Number:",
    "Date Submitted:",
    "Date Due:",
    "Part number (if applicable):",
    "P.O number (if C/A applies to a vendor):",
    "Customer",
    "Reason for initiation",
    "Management Review",
    "Non Conformance Trend",
    "Customer Feedback",
    "Internal Audit",
    "Production Problems",
    "External Audit",
    "Continuous Improvement",
    "Other",
    "Describe the Problem",
    "Describe the Root Cause",
    "Describe the Corrective Action taken",
    "Corrective Action Cost",
    "Labor Cost",
    "Material",
    "Outside Services",
    "Shipping/Transportation",
    "Vendor Management Approval - signature and title (if C/A applies to a vendor)",
    "Management Action",
    "Customer Approval",
    "Quality Manager Approval",
]


# --------------------------------------------------------------------------- builders


def _company(db, company_id=1, **fields):
    company = db.query(Company).filter(Company.id == company_id).first()
    if company is None:
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"car-co-{company_id}", is_active=True)
        db.add(company)
    for key, value in fields.items():
        setattr(company, key, value)
    db.commit()
    return company


def _user(db, role=UserRole.QUALITY, company_id=1, first_name="Quinn"):
    _company(db, company_id)
    n = next(_seq)
    user = User(
        email=f"car-form-{n}@co{company_id}.test",
        employee_id=f"CARF-{n:05d}",
        first_name=first_name,
        last_name=f"Inspector{n}",
        hashed_password=PASSWORD_HASH,
        role=role,
        is_active=True,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _headers(user):
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _part(db, company_id=1, **extra):
    _company(db, company_id)
    n = next(_seq)
    part = Part(
        part_number=f"CARF-P-{company_id}-{n:04d}",
        name=f"Bracket {n}",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
        revision=extra.pop("revision", "C"),
        **extra,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def _customer(db, company_id=1, **extra):
    _company(db, company_id)
    n = next(_seq)
    customer = Customer(name=extra.pop("name", f"Customer {company_id}-{n}"), code=f"CARF-C-{n}", company_id=company_id)
    for key, value in extra.items():
        setattr(customer, key, value)
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def _vendor(db, company_id=1, **extra):
    _company(db, company_id)
    n = next(_seq)
    vendor = Vendor(code=f"CARF-V-{n}", name=extra.pop("name", f"Plating Vendor {n}"), company_id=company_id)
    for key, value in extra.items():
        setattr(vendor, key, value)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return vendor


def _car(db, company_id=1, **extra):
    _company(db, company_id)
    n = next(_seq)
    fields = {
        "car_number": f"CAR-F{company_id}-{n:04d}",
        "title": "Corrective action for edge burr",
        "problem_description": "Repeated edge burrs at final inspection.",
        "status": CARStatus.OPEN,
        "priority": 3,
        "company_id": company_id,
    }
    fields.update(extra)
    car = CorrectiveActionRequest(**fields)
    db.add(car)
    db.commit()
    db.refresh(car)
    return car


def _token_headers(client, db, user):
    admin = _user(db, role=UserRole.ADMIN, company_id=user.company_id)
    issued = client.post(API_TOKENS_URL, json={"user_id": user.id, "label": "car probe"}, headers=_headers(admin))
    assert issued.status_code == 201, issued.text
    return {"Authorization": f"Bearer {issued.json()['token']}", "X-Requested-With": "XMLHttpRequest"}


def _create_body(**overrides):
    body = {
        "title": "Burrs on flange edge at receiving",
        "problem_description": "Customer found burrs on 5 of 20 brackets at receiving inspection.",
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------- readers


def _committed(resource_id, action=None):
    with fresh_session() as verify:
        query = verify.query(AuditLog).filter(AuditLog.resource_type == "car", AuditLog.resource_id == resource_id)
        if action is not None:
            query = query.filter(AuditLog.action == action)
        return [
            {
                "action": row.action,
                "user_id": row.user_id,
                "company_id": row.company_id,
                "old_values": row.old_values,
                "new_values": row.new_values,
                "extra_data": row.extra_data,
            }
            for row in query.order_by(AuditLog.id).all()
        ]


def _snapshot(db):
    """Every CAR / NCR / audit / event row, for "nothing changed" assertions."""
    db.expire_all()
    return {
        model.__tablename__: [
            {column.key: getattr(row, column.key) for column in inspect(model).columns}
            for row in db.query(model).order_by(model.id).all()
        ]
        for model in (CorrectiveActionRequest, NonConformanceReport, AuditLog, OperationalEvent)
    }


def _pdf_text(content: bytes) -> str:
    reader = PdfReader(BytesIO(content))
    return " ".join(" ".join((page.extract_text() or "").split()) for page in reader.pages)


# ============================================================================ create


class TestCreateWithFormFields:
    def test_every_form_field_is_stored_audited_and_read_back(self, client, db_session):
        user = _user(db_session)
        part = _part(db_session, revision="D")
        customer = _customer(db_session, name="Acme Aerospace")
        vendor = _vendor(db_session, name="Tulsa Plating")
        body = _create_body(
            initiation_reason="customer_feedback",
            rma_number="CUST-RMA-77",
            part_id=part.id,
            customer_id=customer.id,
            vendor_id=vendor.id,
            vendor_po_number="PO-5521",
            cost_labor="1200.00",
            cost_material="45.50",
            cost_shipping="0",
            management_action="Reviewed at management review.",
            vendor_approval_name="Pat Smith",
            vendor_approval_title="QA Manager",
            vendor_approval_date="2026-11-10",
            customer_approval_name="Dana Buyer",
            customer_approval_date="2026-11-11",
        )
        resp = client.post(f"{BASE}/car", headers=_headers(user), json=body)
        assert resp.status_code == 200, resp.text
        car = resp.json()

        assert car["initiation_reason"] == "customer_feedback"
        assert car["rma_number"] == "CUST-RMA-77"
        assert car["submitted_date"] == central_today().isoformat()
        assert (car["part_id"], car["part_number"], car["part_revision"]) == (part.id, part.part_number, "D")
        assert (car["customer_id"], car["customer_name"]) == (customer.id, "Acme Aerospace")
        assert (car["vendor_id"], car["vendor_name"], car["vendor_po_number"]) == (
            vendor.id,
            "Tulsa Plating",
            "PO-5521",
        )
        # Blank is not zero: outside services was never entered and stays null; the total is
        # computed from what WAS entered (an explicit 0 included) and never stored.
        assert car["cost_outside_services"] is None
        assert float(car["cost_labor"]) == 1200.0 and float(car["cost_shipping"]) == 0.0
        assert float(car["cost_total"]) == 1245.5
        assert "cost_total" not in {column.key for column in inspect(CorrectiveActionRequest).columns}
        assert car["initiated_by"] == user.id and car["initiated_by_name"] == user.full_name
        assert car["quality_approved_by"] is None and car["quality_approved_at"] is None

        rows = _committed(car["id"])
        assert [row["action"] for row in rows] == ["CREATE"]
        new = rows[0]["new_values"]
        assert new["initiation_reason"] == "customer_feedback"
        assert new["customer_name"] == "Acme Aerospace" and new["vendor_id"] == vendor.id
        assert new["cost_labor"] == 1200.0 and new["submitted_date"] == central_today().isoformat()

    def test_a_past_submitted_date_back_enters_a_paper_car(self, client, db_session):
        user = _user(db_session)
        resp = client.post(f"{BASE}/car", headers=_headers(user), json=_create_body(submitted_date="2024-05-24"))
        assert resp.status_code == 200, resp.text
        assert resp.json()["submitted_date"] == "2024-05-24"

    def test_the_existing_due_date_rule_is_unchanged(self, client, db_session):
        user = _user(db_session)
        past = (date.today() - timedelta(days=3)).isoformat()
        resp = client.post(f"{BASE}/car", headers=_headers(user), json=_create_body(due_date=past))
        assert resp.status_code == 422, resp.text

    def test_other_reason_requires_its_text(self, client, db_session):
        user = _user(db_session)
        before = _snapshot(db_session)
        for other in (None, "   "):
            resp = client.post(
                f"{BASE}/car",
                headers=_headers(user),
                json=_create_body(initiation_reason="other", initiation_reason_other=other),
            )
            assert resp.status_code == 422, resp.text
            assert "initiation_reason_other" in resp.text
        assert _snapshot(db_session) == before

        ok = client.post(
            f"{BASE}/car",
            headers=_headers(user),
            json=_create_body(initiation_reason="other", initiation_reason_other="Supplier audit finding"),
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["initiation_reason_other"] == "Supplier audit finding"

    def test_unknown_reason_is_refused(self, client, db_session):
        resp = client.post(
            f"{BASE}/car", headers=_headers(_user(db_session)), json=_create_body(initiation_reason="hunch")
        )
        assert resp.status_code == 422, resp.text

    def test_quality_approval_cannot_be_sent_in_a_create_body(self, client, db_session):
        other = _user(db_session)
        resp = client.post(
            f"{BASE}/car",
            headers=_headers(_user(db_session)),
            json=_create_body(quality_approved_by=other.id, quality_approved_at="2026-01-01T00:00:00Z"),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["quality_approved_by"] is None and resp.json()["quality_approved_at"] is None

    @pytest.mark.parametrize(
        "field,case,detail",
        [
            ("part_id", "foreign", "Part not found"),
            ("part_id", "deleted", "Part not found"),
            ("customer_id", "foreign", "Customer not found"),
            ("customer_id", "deleted", "Customer not found"),
            ("vendor_id", "foreign", "Vendor not found"),
        ],
    )
    def test_bad_reference_is_404_and_nothing_is_written(self, client, db_session, field, case, detail):
        user = _user(db_session)
        company_id = 2 if case == "foreign" else 1
        if field == "part_id":
            ref = _part(db_session, company_id=company_id, is_deleted=case == "deleted")
        elif field == "customer_id":
            ref = _customer(db_session, company_id=company_id, is_deleted=case == "deleted")
        else:
            ref = _vendor(db_session, company_id=company_id)
        before = _snapshot(db_session)
        resp = client.post(f"{BASE}/car", headers=_headers(user), json=_create_body(**{field: ref.id}))
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == detail
        assert _snapshot(db_session) == before

    @pytest.mark.parametrize("state", ["inactive", "deleted"])
    def test_a_removed_or_inactive_own_vendor_is_still_a_legitimate_car_subject(self, client, db_session, state):
        # A supplier CAR is a record that may document the very removal (invariant 3).
        vendor = _vendor(db_session, is_active=False, **({"is_deleted": True} if state == "deleted" else {}))
        resp = client.post(f"{BASE}/car", headers=_headers(_user(db_session)), json=_create_body(vendor_id=vendor.id))
        assert resp.status_code == 200, resp.text
        assert resp.json()["vendor_id"] == vendor.id and resp.json()["vendor_name"] == vendor.name


# ============================================================================ update


class TestUpdateFormFields:
    def test_form_edit_is_audited_old_to_new(self, client, db_session):
        car = _car(db_session)
        part = _part(db_session)
        resp = client.put(
            f"{BASE}/car/{car.id}",
            headers=_headers(_user(db_session)),
            json={"version": 0, "part_id": part.id, "rma_number": "RMA-2026-0004", "cost_material": "12.25"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["part_number"] == part.part_number
        rows = _committed(car.id)
        assert [row["action"] for row in rows] == ["UPDATE"]
        assert rows[0]["old_values"] == {"part_id": None, "rma_number": None, "cost_material": None}
        assert rows[0]["new_values"] == {"part_id": part.id, "rma_number": "RMA-2026-0004", "cost_material": 12.25}

    def test_customer_change_resnapshots_its_name_and_clearing_clears_it(self, client, db_session):
        first = _customer(db_session, name="First Customer")
        second = _customer(db_session, name="Second Customer")
        car = _car(db_session, customer_id=first.id, customer_name="First Customer")
        headers = _headers(_user(db_session))

        moved = client.put(f"{BASE}/car/{car.id}", headers=headers, json={"version": 0, "customer_id": second.id})
        assert moved.status_code == 200, moved.text
        assert moved.json()["customer_name"] == "Second Customer"
        row = _committed(car.id)[-1]
        assert row["old_values"]["customer_name"] == "First Customer"
        assert row["new_values"]["customer_name"] == "Second Customer"

        cleared = client.put(f"{BASE}/car/{car.id}", headers=headers, json={"version": 0, "customer_id": None})
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["customer_id"] is None and cleared.json()["customer_name"] is None

    def test_an_unchanged_retired_reference_is_not_rechecked(self, client, db_session):
        # The detail screen re-sends every field: a part retired after the CAR named it is a
        # record, not a new selection, so re-saving it is not a 404.
        part = _part(db_session, is_deleted=True)
        customer = _customer(db_session, is_deleted=True)
        car = _car(db_session, part_id=part.id, customer_id=customer.id, customer_name=customer.name)
        resp = client.put(
            f"{BASE}/car/{car.id}",
            headers=_headers(_user(db_session)),
            json={"version": 0, "part_id": part.id, "customer_id": customer.id, "management_action": "Noted."},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["part_number"] == part.part_number  # a historical record keeps naming it

    @pytest.mark.parametrize("field", ["part_id", "customer_id", "vendor_id"])
    def test_a_foreign_new_reference_is_404_without_changes(self, client, db_session, field):
        car = _car(db_session)
        ref = {"part_id": _part, "customer_id": _customer, "vendor_id": _vendor}[field](db_session, company_id=2)
        before = _snapshot(db_session)
        resp = client.put(
            f"{BASE}/car/{car.id}", headers=_headers(_user(db_session)), json={"version": 0, field: ref.id}
        )
        assert resp.status_code == 404, resp.text
        assert _snapshot(db_session) == before

    def test_switching_to_other_without_text_is_422(self, client, db_session):
        car = _car(db_session, initiation_reason="internal_audit")
        before = _snapshot(db_session)
        resp = client.put(
            f"{BASE}/car/{car.id}",
            headers=_headers(_user(db_session)),
            json={"version": 0, "initiation_reason": "other"},
        )
        assert resp.status_code == 422, resp.text
        assert _snapshot(db_session) == before

    def test_closed_car_refuses_a_form_field_edit(self, client, db_session):
        car = _car(db_session, status=CARStatus.CLOSED)
        resp = client.put(
            f"{BASE}/car/{car.id}", headers=_headers(_user(db_session)), json={"version": 0, "rma_number": "X-1"}
        )
        assert resp.status_code == 409, resp.text


# ==================================================================== quality approval


class TestQualityApproval:
    def test_quality_role_records_it_once_with_a_committed_approve_row(self, client, db_session):
        car = _car(db_session)
        approver = _user(db_session, role=UserRole.QUALITY, first_name="Qa")
        resp = client.post(f"{BASE}/car/{car.id}/quality-approval", headers=_headers(approver))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["quality_approved_by"] == approver.id
        assert body["quality_approved_by_name"] == approver.full_name
        assert body["quality_approved_at"].endswith("Z")

        rows = _committed(car.id, "APPROVE")
        assert len(rows) == 1
        assert rows[0]["user_id"] == approver.id and rows[0]["company_id"] == 1
        assert rows[0]["old_values"] == {"quality_approved_by": None, "quality_approved_at": None}
        assert rows[0]["new_values"]["quality_approved_by"] == approver.id

        again = client.post(f"{BASE}/car/{car.id}/quality-approval", headers=_headers(_user(db_session)))
        assert again.status_code == 409, again.text
        db_session.expire_all()
        assert db_session.get(CorrectiveActionRequest, car.id).quality_approved_by == approver.id
        assert len(_committed(car.id, "APPROVE")) == 1

    @pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER])
    def test_admin_and_manager_may_record_it(self, client, db_session, role):
        car = _car(db_session)
        resp = client.post(f"{BASE}/car/{car.id}/quality-approval", headers=_headers(_user(db_session, role=role)))
        assert resp.status_code == 200, resp.text

    @pytest.mark.parametrize("role", [UserRole.SUPERVISOR, UserRole.OPERATOR, UserRole.VIEWER])
    def test_other_roles_are_forbidden_and_nothing_changes(self, client, db_session, role):
        car = _car(db_session)
        before = _snapshot(db_session)
        resp = client.post(f"{BASE}/car/{car.id}/quality-approval", headers=_headers(_user(db_session, role=role)))
        assert resp.status_code == 403, resp.text
        assert _snapshot(db_session) == before

    def test_void_car_is_refused(self, client, db_session):
        car = _car(db_session, status=CARStatus.VOID)
        before = _snapshot(db_session)
        resp = client.post(f"{BASE}/car/{car.id}/quality-approval", headers=_headers(_user(db_session)))
        assert resp.status_code == 409, resp.text
        assert _snapshot(db_session) == before

    def test_closed_car_may_still_be_approved_and_approval_does_not_gate_closure(self, client, db_session):
        closed = _car(db_session, status=CARStatus.CLOSED)
        resp = client.post(f"{BASE}/car/{closed.id}/quality-approval", headers=_headers(_user(db_session)))
        assert resp.status_code == 200, resp.text

        unapproved = _car(
            db_session,
            root_cause="Worn deburr fixture.",
            corrective_action="Replace and add fixture to PM.",
            verification_method="Inspect 3 consecutive lots.",
            verification_results="No burrs in 3 lots.",
        )
        closing = client.put(
            f"{BASE}/car/{unapproved.id}",
            headers=_headers(_user(db_session)),
            json={"version": 0, "status": "closed"},
        )
        assert closing.status_code == 200, closing.text

    def test_api_token_is_refused_and_nothing_changes(self, client, db_session):
        car = _car(db_session)
        holder = _user(db_session, role=UserRole.QUALITY)
        headers = _token_headers(client, db_session, holder)
        before = _snapshot(db_session)
        resp = client.post(f"{BASE}/car/{car.id}/quality-approval", headers=headers)
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == API_TOKEN_QUALITY_APPROVAL
        assert _snapshot(db_session) == before

    def test_foreign_tenant_car_is_404(self, client, db_session):
        car = _car(db_session, company_id=2)
        before = _snapshot(db_session)
        resp = client.post(f"{BASE}/car/{car.id}/quality-approval", headers=_headers(_user(db_session)))
        assert resp.status_code == 404, resp.text
        assert _snapshot(db_session) == before

    def test_put_cannot_set_the_approval_stamp(self, client, db_session):
        car = _car(db_session)
        resp = client.put(
            f"{BASE}/car/{car.id}",
            headers=_headers(_user(db_session)),
            json={"version": 0, "quality_approved_by": 1, "quality_approved_at": "2026-01-01T00:00:00Z"},
        )
        assert resp.status_code == 200, resp.text
        db_session.expire_all()
        row = db_session.get(CorrectiveActionRequest, car.id)
        assert row.quality_approved_by is None and row.quality_approved_at is None


# =========================================================================== fail-closed


@pytest.mark.parametrize("verb", ["create", "update", "approve"])
def test_lost_audit_row_rolls_the_form_write_back(client, db_session, monkeypatch, verb):
    headers = _headers(_user(db_session))
    part = _part(db_session)
    car = _car(db_session)
    if verb == "create":

        def call():
            return client.post(
                f"{BASE}/car", headers=headers, json=_create_body(part_id=part.id, initiation_reason="internal_audit")
            )

    elif verb == "update":

        def call():
            return client.put(
                f"{BASE}/car/{car.id}", headers=headers, json={"version": 0, "part_id": part.id, "cost_labor": "5"}
            )

    else:

        def call():
            return client.post(f"{BASE}/car/{car.id}/quality-approval", headers=headers)

    before = _snapshot(db_session)
    monkeypatch.setattr(AuditService, "log", lambda *args, **kwargs: None)
    resp = call()
    assert resp.status_code == 503, resp.text
    assert resp.json() == {"detail": "Unable to save audit record"}
    assert _snapshot(db_session) == before


# =========================================================================== escalations


class TestEscalationPrefill:
    def test_ncr_escalation_carries_part_and_po(self, client, db_session):
        part = _part(db_session)
        ncr = NonConformanceReport(
            ncr_number=f"NCR-F-{next(_seq):04d}",
            source=NCRSource.INCOMING_INSPECTION,
            title="Plating thickness under spec",
            description="Plating measured 0.0002 under the minimum on 4 pieces.",
            quantity_affected=4,
            quantity_rejected=0,
            status=NCRStatus.OPEN,
            disposition=NCRDisposition.PENDING,
            part_id=part.id,
            po_number="PO-7781",
            company_id=1,
        )
        db_session.add(ncr)
        db_session.commit()
        resp = client.post(f"{BASE}/ncr/{ncr.id}/create-car", headers=_headers(_user(db_session)))
        assert resp.status_code == 200, resp.text
        car = resp.json()
        assert car["part_id"] == part.id and car["vendor_po_number"] == "PO-7781"
        assert car["submitted_date"] == central_today().isoformat()
        create_row = _committed(car["id"], "CREATE")[0]
        assert create_row["new_values"]["part_id"] == part.id
        assert create_row["new_values"]["vendor_po_number"] == "PO-7781"

    def test_complaint_escalation_carries_part_customer_newest_rma_and_reason(self, client, db_session):
        part = _part(db_session)
        customer = _customer(db_session, name="Acme Aerospace")
        complaint = CustomerComplaint(
            complaint_number=f"CC-F-{next(_seq):05d}",
            customer_id=customer.id,
            customer_name="Acme Aerospace",
            part_id=part.id,
            title="Dented flange",
            description="Brackets arrived dented on one flange.",
            quantity_affected=3,
            date_received=date.today(),
            company_id=1,
        )
        db_session.add(complaint)
        db_session.commit()
        for number in ("RMA-2026-0001", "RMA-2026-0002"):
            db_session.add(
                ReturnMaterialAuthorization(
                    rma_number=number,
                    complaint_id=complaint.id,
                    customer_name="Acme Aerospace",
                    quantity=2,
                    reason="Return for inspection",
                    company_id=1,
                )
            )
            db_session.commit()

        resp = client.post(
            f"/api/v1/complaints/complaints/{complaint.id}/create-car", headers=_headers(_user(db_session))
        )
        assert resp.status_code == 200, resp.text
        db_session.expire_all()
        car = db_session.get(CorrectiveActionRequest, db_session.get(CustomerComplaint, complaint.id).car_id)
        assert car.part_id == part.id
        assert (car.customer_id, car.customer_name) == (customer.id, "Acme Aerospace")
        assert car.rma_number == "RMA-2026-0002"
        assert car.initiation_reason == "customer_feedback"
        assert car.submitted_date == central_today()


# ============================================================================ printable


class TestPrintableCar:
    def test_pdf_prints_every_form_label_and_the_recorded_values(self, client, db_session):
        _company(
            db_session,
            name="Werco Manufacturing",
            address="415 East Houston Street\nBroken Arrow, OK 74012",
            phone="918.251.6880",
        )
        approver = _user(db_session, first_name="Morgan")
        part = _part(db_session)
        vendor = _vendor(db_session, name="Tulsa Plating")
        car = _car(
            db_session,
            initiated_by=approver.id,
            rma_number="RMA-2026-0042",
            submitted_date=date(2026, 11, 2),
            due_date=date(2026, 12, 1),
            part_id=part.id,
            vendor_id=vendor.id,
            vendor_po_number="PO-5521",
            customer_name="Acme Aerospace",
            initiation_reason="internal_audit",
            root_cause="Worn deburr fixture.",
            corrective_action="Replaced fixture; added it to PM.",
            cost_labor=1200,
            cost_outside_services=5,
            management_action="Reviewed at management review.",
            vendor_approval_name="Pat Smith",
            vendor_approval_title="QA Manager",
            quality_approved_by=approver.id,
            # 2026-11-14 03:30 UTC is still 2026-11-13 in Central: the printed date is Central.
            quality_approved_at=datetime(2026, 11, 14, 3, 30),
        )
        resp = client.get(f"{BASE}/car/{car.id}/pdf", headers=_headers(_user(db_session, role=UserRole.VIEWER)))
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.headers["content-disposition"] == f'inline; filename="{car.car_number}.pdf"'
        text = _pdf_text(resp.content)

        for label in FORM_LABELS:
            assert label in text, f"form label {label!r} missing from the printed CAR"
        for value in (
            "Werco Manufacturing",
            "415 East Houston Street",
            "Broken Arrow, OK 74012",
            car.car_number,
            "RMA-2026-0042",
            "11/02/2026",
            "12/01/2026",
            part.part_number,
            "PO-5521",
            "Tulsa Plating",
            "Acme Aerospace",
            "[X] Internal Audit",
            "[ ] Management Review",
            "Worn deburr fixture.",
            "Replaced fixture; added it to PM.",
            "$ 1,200.00",
            "$ 5.00",
            "$ 1,205.00",
            "Pat Smith, QA Manager",
            "Reviewed at management review.",
            approver.full_name,
            "11/13/2026",
        ):
            assert value in text, f"{value!r} missing from the printed CAR"

    def test_header_comes_from_the_company_record_not_a_hard_coded_block(self, client, db_session):
        _company(db_session, 2, name="Second Shop LLC", address="1 Main St\nTulsa, OK 74103", phone="555-0100")
        car = _car(db_session, company_id=2)
        resp = client.get(f"{BASE}/car/{car.id}/pdf", headers=_headers(_user(db_session, company_id=2)))
        assert resp.status_code == 200, resp.text
        text = _pdf_text(resp.content)
        assert "Second Shop LLC" in text and "Tulsa, OK 74103" in text
        assert "Werco" not in text and "Broken Arrow" not in text

    def test_other_reason_prints_its_text(self, client, db_session):
        car = _car(db_session, initiation_reason="other", initiation_reason_other="Supplier survey")
        text = _pdf_text(client.get(f"{BASE}/car/{car.id}/pdf", headers=_headers(_user(db_session))).content)
        assert "[X] Other: Supplier survey" in text

    @pytest.mark.parametrize(
        "value", ["Acme <b>Corp</b>", "Check OD<ID before press", "R&D Group", "Dim is 2.500 <REF> per print"]
    )
    def test_hostile_values_print_literally(self, client, db_session, value):
        car = _car(
            db_session,
            title=f"Title {value}",
            problem_description=f"Problem {value}",
            root_cause=f"Cause {value}",
            rma_number=value[:50],
            customer_name=value,
            management_action=value,
        )
        resp = client.get(f"{BASE}/car/{car.id}/pdf", headers=_headers(_user(db_session)))
        assert resp.status_code == 200, resp.text
        text = _pdf_text(resp.content)
        assert f"Problem {value}" in text and f"Cause {value}" in text and value in text

    def test_long_text_flows_onto_more_pages_without_truncation(self, client, db_session):
        long_text = "Fixture wear let the edge burr through deburr. " * 120
        car = _car(db_session, problem_description=long_text, root_cause=long_text, corrective_action=long_text)
        resp = client.get(f"{BASE}/car/{car.id}/pdf", headers=_headers(_user(db_session)))
        assert resp.status_code == 200, resp.text
        reader = PdfReader(BytesIO(resp.content))
        assert len(reader.pages) >= 2
        assert "Page 2" in " ".join(page.extract_text() for page in reader.pages)
        assert _pdf_text(resp.content).count("Fixture wear let the edge burr through deburr.") == 360

    def test_foreign_tenant_car_is_404(self, client, db_session):
        car = _car(db_session, company_id=2)
        resp = client.get(f"{BASE}/car/{car.id}/pdf", headers=_headers(_user(db_session)))
        assert resp.status_code == 404, resp.text

    def test_pdf_writes_nothing(self, client, db_session):
        car = _car(db_session)
        before = _snapshot(db_session)
        assert client.get(f"{BASE}/car/{car.id}/pdf", headers=_headers(_user(db_session))).status_code == 200
        assert _snapshot(db_session) == before


def test_list_and_detail_resolve_names_tenant_scoped(client, db_session):
    user = _user(db_session)
    own_part = _part(db_session)
    foreign_part = _part(db_session, company_id=2)
    # A legacy row carrying another tenant's part id shows no part rather than theirs.
    legacy = _car(db_session, part_id=foreign_part.id)
    mine = _car(db_session, part_id=own_part.id, initiated_by=user.id)
    rows = {row["id"]: row for row in client.get(f"{BASE}/car", headers=_headers(user)).json()}
    assert rows[mine.id]["part_number"] == own_part.part_number
    assert rows[mine.id]["initiated_by_name"] == user.full_name
    assert rows[legacy.id]["part_number"] is None
    detail = client.get(f"{BASE}/car/{legacy.id}", headers=_headers(user)).json()
    assert detail["part_number"] is None and detail["part_id"] == foreign_part.id
