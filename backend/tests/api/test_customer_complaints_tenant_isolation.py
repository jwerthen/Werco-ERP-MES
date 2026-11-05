"""Tenant isolation, RBAC and audit coverage for the Customer Complaints / RMA router.

``app/api/endpoints/customer_complaints.py`` previously resolved every by-id complaint
and RMA route by ``id`` alone: any authenticated user (or agent token) of company A
could read company B's 8D data and drive its complaints and RMAs through their
workflow. The dashboard aggregated every tenant. investigate / resolve / close and RMA
receive / inspect had no role gate, no write in the module produced an audit row, and
create-ncr / create-car built tenant rows with no ``company_id`` (a NOT NULL 500).
Complaint creation always 500'd on a duplicate ``date_received`` keyword.

What these tests lock (router prefix ``/complaints`` + in-router ``/complaints/...`` and
``/rma/...``):

- every by-id route and verb -> 404 for another tenant's id, with no row change, no
  audit row, no NCR/CAR and no event; the owning tenant's own call on the same row,
  state and body SUCCEEDS, so the 404 is tenancy and nothing else;
- the dashboard counts only the caller's company;
- Viewer / Operator / Shipping -> 403 on every gated write with no side effect, and
  Supervisor -> 403 on the approver-tier verbs while keeping the author-tier ones;
- every write audits; resolve / close / RMA dispose / RMA complete fail closed (503,
  nothing saved) when the required audit row cannot be written;
- ``status`` (complaint) and ``disposition`` (RMA) are refused on PUT;
- selected references must be live rows of the active company;
- create-ncr / create-car produce tenant-correct, per-company-numbered records and
  emit the same events as the quality router's create paths.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.customer import Customer
from app.models.customer_complaint import (
    ComplaintStatus,
    CustomerComplaint,
    ReturnMaterialAuthorization,
    RMAStatus,
)
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.quality import CorrectiveActionRequest, NCRSource, NonConformanceReport
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.services.audit_service import AuditService

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

API = "/api/v1/complaints"
COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens minted directly; never used for login
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


# ---------------------------------------------------------------- fixtures / factories


def _ensure_company(db: Session, company_id: int) -> None:
    if db.query(Company).filter(Company.id == company_id).first() is None:
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True))
        db.commit()


def make_user(db: Session, *, company_id: int = COMPANY_A, role: UserRole = UserRole.QUALITY, active=True) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"cc-{n}@co{company_id}.test",
        employee_id=f"CC-{n:05d}",
        first_name="Cc",
        last_name=f"C{company_id}",
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=active,
        is_superuser=False,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def make_part(db: Session, *, company_id: int = COMPANY_A, is_deleted: bool = False) -> Part:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"CC-P-{n}",
        name=f"Complaint part {n}",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        is_deleted=is_deleted,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_customer(db: Session, *, company_id: int = COMPANY_A, name: str = None) -> Customer:
    _ensure_company(db, company_id)
    n = _next()
    customer = Customer(name=name or f"Customer {company_id}-{n}", code=f"CC-C-{n}", company_id=company_id)
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def make_work_order(db: Session, *, company_id: int = COMPANY_A, is_deleted: bool = False) -> WorkOrder:
    part = make_part(db, company_id=company_id)
    n = _next()
    wo = WorkOrder(
        work_order_number=f"CC-WO-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=10,
        status=WorkOrderStatus.IN_PROGRESS,
        priority=5,
        due_date=date.today() + timedelta(days=30),
        is_deleted=is_deleted,
        company_id=company_id,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def make_complaint(
    db: Session,
    *,
    company_id: int = COMPANY_A,
    status: ComplaintStatus = ComplaintStatus.RECEIVED,
    customer_name: str = "Acme Aerospace",
    **fields,
) -> CustomerComplaint:
    _ensure_company(db, company_id)
    n = _next()
    complaint = CustomerComplaint(
        complaint_number=fields.pop("complaint_number", f"CC-T-{company_id}-{n:05d}"),
        customer_name=customer_name,
        title=fields.pop("title", f"Complaint {n}"),
        description="Brackets arrived dented on one flange.",
        quantity_affected=3,
        status=status,
        date_received=fields.pop("date_received", date.today()),
        company_id=company_id,
        **fields,
    )
    db.add(complaint)
    db.commit()
    db.refresh(complaint)
    return complaint


def make_rma(
    db: Session, *, company_id: int = COMPANY_A, status: RMAStatus = RMAStatus.REQUESTED, **fields
) -> ReturnMaterialAuthorization:
    _ensure_company(db, company_id)
    n = _next()
    rma = ReturnMaterialAuthorization(
        rma_number=f"RMA-T-{company_id}-{n:05d}",
        customer_name="Acme Aerospace",
        quantity=2,
        reason="Return for inspection",
        status=status,
        company_id=company_id,
        **fields,
    )
    db.add(rma)
    db.commit()
    db.refresh(rma)
    return rma


def _state(db: Session, model, pk: int) -> dict:
    db.expire_all()
    row = db.get(model, pk)
    return {column.key: getattr(row, column.key) for column in model.__table__.columns}


def _count(db: Session, model) -> int:
    db.expire_all()
    return db.query(func.count(model.id)).scalar()


def _side_effect_counts(db: Session) -> dict:
    return {
        model.__name__: _count(db, model)
        for model in (
            AuditLog,
            CustomerComplaint,
            ReturnMaterialAuthorization,
            NonConformanceReport,
            CorrectiveActionRequest,
            OperationalEvent,
        )
    }


def _audit_rows(db: Session, resource_type: str, resource_id: int):
    db.expire_all()
    return (
        db.query(AuditLog)
        .filter(AuditLog.resource_type == resource_type, AuditLog.resource_id == resource_id)
        .order_by(AuditLog.sequence_number)
        .all()
    )


@pytest.fixture
def quality_a(db_session: Session) -> User:
    return make_user(db_session, company_id=COMPANY_A, role=UserRole.QUALITY)


# ---------------------------------------------------------------- route table
#
# (name, method, path, body, kind, the status that makes the call legal for its owner)
COMPLAINT_ROUTES = [
    ("get_complaint", "GET", "/complaints/{id}", None, ComplaintStatus.RECEIVED),
    ("get_8d_report", "GET", "/complaints/8d-report/{id}", None, ComplaintStatus.RECEIVED),
    ("update_complaint", "PUT", "/complaints/{id}", {"title": "Rewritten title"}, ComplaintStatus.RECEIVED),
    (
        "investigate",
        "POST",
        "/complaints/{id}/investigate",
        {"investigation_findings": "Flange dented in packing"},
        ComplaintStatus.RECEIVED,
    ),
    (
        "resolve",
        "POST",
        "/complaints/{id}/resolve",
        {"resolution_description": "Packing changed to foam"},
        ComplaintStatus.UNDER_INVESTIGATION,
    ),
    ("close", "POST", "/complaints/{id}/close", {"customer_satisfied": True}, ComplaintStatus.RESOLVED),
    ("create_ncr", "POST", "/complaints/{id}/create-ncr", None, ComplaintStatus.RECEIVED),
    ("create_car", "POST", "/complaints/{id}/create-car", None, ComplaintStatus.RECEIVED),
]
RMA_ROUTES = [
    ("get_rma", "GET", "/rma/{id}", None, RMAStatus.REQUESTED),
    ("update_rma", "PUT", "/rma/{id}", {"notes": "Rewritten notes"}, RMAStatus.REQUESTED),
    ("approve_rma", "POST", "/rma/{id}/approve", None, RMAStatus.REQUESTED),
    ("deny_rma", "POST", "/rma/{id}/deny", None, RMAStatus.REQUESTED),
    ("receive_rma", "POST", "/rma/{id}/receive", {"shipping_tracking": "1Z999"}, RMAStatus.APPROVED),
    (
        "inspect_rma",
        "POST",
        "/rma/{id}/inspect",
        {"inspection_findings": "Dent confirmed on flange"},
        RMAStatus.MATERIAL_RECEIVED,
    ),
    ("dispose_rma", "POST", "/rma/{id}/dispose", {"disposition": "scrap"}, RMAStatus.UNDER_INSPECTION),
    ("complete_rma", "POST", "/rma/{id}/complete", None, RMAStatus.DISPOSITION_DECIDED),
]
BY_ID_ROUTES = [(*route, "complaint") for route in COMPLAINT_ROUTES] + [(*route, "rma") for route in RMA_ROUTES]

AUTHOR_TIER = {"update_complaint", "investigate", "update_rma", "receive_rma", "inspect_rma"}
APPROVER_TIER = {
    "resolve",
    "close",
    "create_ncr",
    "create_car",
    "approve_rma",
    "deny_rma",
    "dispose_rma",
    "complete_rma",
}
GATED_BY_ID_ROUTES = [route for route in BY_ID_ROUTES if route[0] in AUTHOR_TIER | APPROVER_TIER]


def _seed(db: Session, kind: str, company_id: int, status):
    if kind == "complaint":
        row = make_complaint(db, company_id=company_id, status=status)
        return CustomerComplaint, row.id
    row = make_rma(db, company_id=company_id, status=status)
    return ReturnMaterialAuthorization, row.id


def _call(client: TestClient, method: str, path: str, body, headers):
    return client.request(method, f"{API}{path}", json=body, headers=headers)


# ---------------------------------------------------------------- tenancy


class TestWrongTenantByIdRoutes:
    @pytest.mark.parametrize("name,method,path,body,status,kind", BY_ID_ROUTES, ids=[r[0] for r in BY_ID_ROUTES])
    def test_foreign_id_is_404_with_no_side_effect(
        self, client: TestClient, db_session: Session, name, method, path, body, status, kind
    ):
        caller = make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)
        model, foreign_id = _seed(db_session, kind, COMPANY_B, status)
        before_row = _state(db_session, model, foreign_id)
        before_counts = _side_effect_counts(db_session)

        response = _call(client, method, path.format(id=foreign_id), body, headers_for(caller))

        assert response.status_code == 404, response.text
        assert _state(db_session, model, foreign_id) == before_row
        assert _side_effect_counts(db_session) == before_counts

    @pytest.mark.parametrize("name,method,path,body,status,kind", BY_ID_ROUTES, ids=[r[0] for r in BY_ID_ROUTES])
    def test_owning_tenant_succeeds_on_the_same_call(
        self, client: TestClient, db_session: Session, name, method, path, body, status, kind
    ):
        # Non-vacuity control: the identical row state and body succeed for the owner,
        # so the 404 above can only come from tenancy.
        owner = make_user(db_session, company_id=COMPANY_B, role=UserRole.ADMIN)
        _model, own_id = _seed(db_session, kind, COMPANY_B, status)

        response = _call(client, method, path.format(id=own_id), body, headers_for(owner))

        assert response.status_code == 200, response.text
        payload = response.json()
        assert (payload["complaint"] if name == "get_8d_report" else payload)["id"] == own_id


class TestScopedReads:
    def test_dashboard_counts_only_the_active_company(self, client: TestClient, db_session: Session, quality_a):
        make_complaint(db_session, company_id=COMPANY_A, customer_name="Alpha Co")
        make_complaint(
            db_session,
            company_id=COMPANY_A,
            status=ComplaintStatus.CLOSED,
            customer_name="Alpha Co",
            customer_satisfied=True,
        )
        for _ in range(3):
            make_complaint(db_session, company_id=COMPANY_B, customer_name="Bravo Secret Co", customer_satisfied=False)
        make_rma(db_session, company_id=COMPANY_B, status=RMAStatus.APPROVED)

        response = client.get(f"{API}/complaints/dashboard", headers=headers_for(quality_a))

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["open_complaints"] == 1
        assert body["by_customer"] == [{"customer": "Alpha Co", "count": 2}]
        assert body["by_severity"] == {"minor": 1}
        assert body["satisfaction_rate"] == 100.0
        assert sum(point["count"] for point in body["trend"]) == 2
        assert body["open_rmas"] == 0

    def test_lists_return_only_the_active_company(self, client: TestClient, db_session: Session, quality_a):
        own = make_complaint(db_session, company_id=COMPANY_A)
        make_complaint(db_session, company_id=COMPANY_B)
        own_rma = make_rma(db_session, company_id=COMPANY_A)
        make_rma(db_session, company_id=COMPANY_B)

        complaints = client.get(f"{API}/complaints/", headers=headers_for(quality_a)).json()
        rmas = client.get(f"{API}/rma/", headers=headers_for(quality_a)).json()

        assert [row["id"] for row in complaints] == [own.id]
        assert [row["id"] for row in rmas] == [own_rma.id]

    def test_legacy_cross_tenant_links_are_not_disclosed(self, client: TestClient, db_session: Session, quality_a):
        # Rows written before references were validated may point across tenants.
        foreign_part = make_part(db_session, company_id=COMPANY_B)
        foreign_customer = make_customer(db_session, company_id=COMPANY_B, name="Bravo Secret Co")
        complaint = make_complaint(
            db_session, company_id=COMPANY_A, part_id=foreign_part.id, customer_id=foreign_customer.id
        )
        make_rma(db_session, company_id=COMPANY_B, complaint_id=complaint.id)
        own_rma = make_rma(db_session, company_id=COMPANY_A, complaint_id=complaint.id, part_id=foreign_part.id)

        detail = client.get(f"{API}/complaints/{complaint.id}", headers=headers_for(quality_a)).json()
        listed = client.get(f"{API}/complaints/", headers=headers_for(quality_a)).json()[0]
        rma = client.get(f"{API}/rma/{own_rma.id}", headers=headers_for(quality_a)).json()

        for body in (detail, listed):
            assert body["part"] is None
            assert body["customer"] is None
            assert [row["id"] for row in body["rmas"]] == [own_rma.id]
        assert rma["part"] is None


# ---------------------------------------------------------------- roles


class TestRoleGates:
    WRITE_CREATES = [
        (
            "create_complaint",
            "/complaints/",
            {"customer_name": "Acme", "title": "Dented", "description": "Dented flange"},
        ),
        ("create_rma", "/rma/", {"customer_name": "Acme", "quantity": 1, "reason": "Return for inspection"}),
    ]

    @pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.OPERATOR, UserRole.SHIPPING])
    @pytest.mark.parametrize(
        "name,method,path,body,status,kind", GATED_BY_ID_ROUTES, ids=[r[0] for r in GATED_BY_ID_ROUTES]
    )
    def test_view_only_roles_refused_on_every_gated_verb(
        self, client: TestClient, db_session: Session, role, name, method, path, body, status, kind
    ):
        caller = make_user(db_session, company_id=COMPANY_A, role=role)
        model, own_id = _seed(db_session, kind, COMPANY_A, status)
        before_row = _state(db_session, model, own_id)
        before_counts = _side_effect_counts(db_session)

        response = _call(client, method, path.format(id=own_id), body, headers_for(caller))

        assert response.status_code == 403, response.text
        assert _state(db_session, model, own_id) == before_row
        assert _side_effect_counts(db_session) == before_counts

    @pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.OPERATOR, UserRole.SHIPPING])
    @pytest.mark.parametrize("name,path,body", WRITE_CREATES, ids=[r[0] for r in WRITE_CREATES])
    def test_view_only_roles_cannot_create(self, client: TestClient, db_session: Session, role, name, path, body):
        caller = make_user(db_session, company_id=COMPANY_A, role=role)
        before_counts = _side_effect_counts(db_session)

        response = client.post(f"{API}{path}", json=body, headers=headers_for(caller))

        assert response.status_code == 403, response.text
        assert _side_effect_counts(db_session) == before_counts

    @pytest.mark.parametrize(
        "name,method,path,body,status,kind",
        [r for r in GATED_BY_ID_ROUTES if r[0] in APPROVER_TIER],
        ids=[r[0] for r in GATED_BY_ID_ROUTES if r[0] in APPROVER_TIER],
    )
    def test_supervisor_refused_on_approver_verbs(
        self, client: TestClient, db_session: Session, name, method, path, body, status, kind
    ):
        caller = make_user(db_session, company_id=COMPANY_A, role=UserRole.SUPERVISOR)
        model, own_id = _seed(db_session, kind, COMPANY_A, status)
        before_row = _state(db_session, model, own_id)
        before_counts = _side_effect_counts(db_session)

        response = _call(client, method, path.format(id=own_id), body, headers_for(caller))

        assert response.status_code == 403, response.text
        assert _state(db_session, model, own_id) == before_row
        assert _side_effect_counts(db_session) == before_counts

    @pytest.mark.parametrize(
        "name,method,path,body,status,kind",
        [r for r in GATED_BY_ID_ROUTES if r[0] in AUTHOR_TIER],
        ids=[r[0] for r in GATED_BY_ID_ROUTES if r[0] in AUTHOR_TIER],
    )
    def test_supervisor_keeps_author_verbs(
        self, client: TestClient, db_session: Session, name, method, path, body, status, kind
    ):
        caller = make_user(db_session, company_id=COMPANY_A, role=UserRole.SUPERVISOR)
        _model, own_id = _seed(db_session, kind, COMPANY_A, status)

        response = _call(client, method, path.format(id=own_id), body, headers_for(caller))

        assert response.status_code == 200, response.text

    @pytest.mark.parametrize("name,path,body", WRITE_CREATES, ids=[r[0] for r in WRITE_CREATES])
    def test_supervisor_can_create(self, client: TestClient, db_session: Session, name, path, body):
        caller = make_user(db_session, company_id=COMPANY_A, role=UserRole.SUPERVISOR)

        response = client.post(f"{API}{path}", json=body, headers=headers_for(caller))

        assert response.status_code == 200, response.text


# ---------------------------------------------------------------- workflow + audit


class TestComplaintWorkflowAudit:
    def test_create_records_tenant_number_and_audit(self, client: TestClient, db_session: Session, quality_a):
        response = client.post(
            f"{API}/complaints/",
            json={
                "customer_name": "Acme",
                "title": "Dented",
                "description": "Dented flange",
                "date_received": "2026-10-01",
            },
            headers=headers_for(quality_a),
        )

        assert response.status_code == 200, response.text
        body = response.json()
        complaint = db_session.get(CustomerComplaint, body["id"])
        assert complaint.company_id == COMPANY_A
        assert complaint.received_by == quality_a.id
        assert complaint.date_received == date(2026, 10, 1)
        assert body["complaint_number"] == f"CC-{datetime.now():%Y}-0001"
        rows = _audit_rows(db_session, "customer_complaint", complaint.id)
        assert [(row.action, row.company_id) for row in rows] == [("CREATE", COMPANY_A)]

    def test_complaint_numbers_are_one_global_sequence(self, client: TestClient, db_session: Session, quality_a):
        # complaint_number is globally unique, so the sequence is global (per-company
        # numbering needs a migration first); the lock keeps it collision-free.
        make_complaint(db_session, company_id=COMPANY_B, complaint_number=f"CC-{datetime.now():%Y}-0007")

        response = client.post(
            f"{API}/complaints/",
            json={"customer_name": "Acme", "title": "Dented", "description": "Dented flange"},
            headers=headers_for(quality_a),
        )

        assert response.status_code == 200, response.text
        assert response.json()["complaint_number"] == f"CC-{datetime.now():%Y}-0008"

    def test_full_workflow_writes_one_status_change_row_per_verb(
        self, client: TestClient, db_session: Session, quality_a
    ):
        complaint = make_complaint(db_session, company_id=COMPANY_A)
        headers = headers_for(quality_a)
        steps = [
            ("investigate", {"investigation_findings": "Flange dented in packing", "root_cause": "Loose packing"}),
            ("resolve", {"resolution_description": "Packing changed to foam", "actual_cost": 120}),
            ("close", {"customer_satisfied": True, "satisfaction_notes": "Accepted"}),
        ]
        for verb, body in steps:
            response = client.post(f"{API}/complaints/{complaint.id}/{verb}", json=body, headers=headers)
            assert response.status_code == 200, response.text

        rows = _audit_rows(db_session, "customer_complaint", complaint.id)
        assert [(row.action, row.old_values["status"], row.new_values["status"]) for row in rows] == [
            ("STATUS_CHANGE", "received", "under_investigation"),
            ("STATUS_CHANGE", "under_investigation", "resolved"),
            ("STATUS_CHANGE", "resolved", "closed"),
        ]
        assert rows[0].extra_data["changes"]["root_cause"] == {"old": None, "new": "Loose packing"}
        assert all(row.company_id == COMPANY_A for row in rows)
        final = _state(db_session, CustomerComplaint, complaint.id)
        assert final["status"] == ComplaintStatus.CLOSED
        assert final["resolved_date"] == date.today() and final["closed_date"] == date.today()

    @pytest.mark.parametrize(
        "verb,body,status",
        [
            ("resolve", {"resolution_description": "Skipped investigation"}, ComplaintStatus.RECEIVED),
            ("close", {}, ComplaintStatus.UNDER_INVESTIGATION),
            ("close", {}, ComplaintStatus.RECEIVED),
            ("investigate", {"investigation_findings": "Reopened findings"}, ComplaintStatus.CLOSED),
            ("investigate", {"investigation_findings": "Reopened findings"}, ComplaintStatus.RESOLVED),
            ("resolve", {"resolution_description": "Second resolution"}, ComplaintStatus.CLOSED),
        ],
    )
    def test_out_of_order_transitions_refused(
        self, client: TestClient, db_session: Session, quality_a, verb, body, status
    ):
        complaint = make_complaint(db_session, company_id=COMPANY_A, status=status)
        before_row = _state(db_session, CustomerComplaint, complaint.id)
        before_counts = _side_effect_counts(db_session)

        response = client.post(f"{API}/complaints/{complaint.id}/{verb}", json=body, headers=headers_for(quality_a))

        assert response.status_code == 400, response.text
        assert _state(db_session, CustomerComplaint, complaint.id) == before_row
        assert _side_effect_counts(db_session) == before_counts

    @pytest.mark.parametrize(
        "kind,path,body,status,field,expected",
        [
            (
                "complaint",
                "/complaints/{id}/resolve",
                {"resolution_description": "Packing changed"},
                ComplaintStatus.UNDER_INVESTIGATION,
                "status",
                ComplaintStatus.UNDER_INVESTIGATION,
            ),
            ("complaint", "/complaints/{id}/close", {}, ComplaintStatus.RESOLVED, "status", ComplaintStatus.RESOLVED),
            (
                "rma",
                "/rma/{id}/dispose",
                {"disposition": "credit", "credit_amount": 50},
                RMAStatus.UNDER_INSPECTION,
                "status",
                RMAStatus.UNDER_INSPECTION,
            ),
            ("rma", "/rma/{id}/complete", None, RMAStatus.DISPOSITION_DECIDED, "status", RMAStatus.DISPOSITION_DECIDED),
        ],
        ids=["resolve", "close", "dispose", "complete"],
    )
    def test_required_audit_failure_rolls_back(
        self, client: TestClient, db_session: Session, quality_a, monkeypatch, kind, path, body, status, field, expected
    ):
        model, own_id = _seed(db_session, kind, COMPANY_A, status)
        before_counts = _side_effect_counts(db_session)
        monkeypatch.setattr(AuditService, "log", lambda *args, **kwargs: None)

        response = client.post(f"{API}{path.format(id=own_id)}", json=body, headers=headers_for(quality_a))

        assert response.status_code == 503, response.text
        assert _state(db_session, model, own_id)[field] == expected
        assert _side_effect_counts(db_session) == before_counts

    def test_put_refuses_status(self, client: TestClient, db_session: Session, quality_a):
        complaint = make_complaint(db_session, company_id=COMPANY_A)
        before_row = _state(db_session, CustomerComplaint, complaint.id)

        response = client.put(
            f"{API}/complaints/{complaint.id}", json={"status": "closed"}, headers=headers_for(quality_a)
        )

        assert response.status_code == 422, response.text
        assert _state(db_session, CustomerComplaint, complaint.id) == before_row

    def test_put_rma_refuses_disposition(self, client: TestClient, db_session: Session, quality_a):
        rma = make_rma(db_session, company_id=COMPANY_A)
        before_row = _state(db_session, ReturnMaterialAuthorization, rma.id)

        response = client.put(f"{API}/rma/{rma.id}", json={"disposition": "scrap"}, headers=headers_for(quality_a))

        assert response.status_code == 422, response.text
        assert _state(db_session, ReturnMaterialAuthorization, rma.id) == before_row

    def test_put_refuses_clearing_a_required_field(self, client: TestClient, db_session: Session, quality_a):
        complaint = make_complaint(db_session, company_id=COMPANY_A)

        response = client.put(f"{API}/complaints/{complaint.id}", json={"title": None}, headers=headers_for(quality_a))

        assert response.status_code == 422, response.text
        assert _state(db_session, CustomerComplaint, complaint.id)["title"] == complaint.title

    def test_put_audits_the_changed_fields(self, client: TestClient, db_session: Session, quality_a):
        complaint = make_complaint(db_session, company_id=COMPANY_A)

        response = client.put(
            f"{API}/complaints/{complaint.id}", json={"root_cause": "Loose packing"}, headers=headers_for(quality_a)
        )

        assert response.status_code == 200, response.text
        rows = _audit_rows(db_session, "customer_complaint", complaint.id)
        assert [row.action for row in rows] == ["UPDATE"]
        assert rows[0].extra_data["changes"] == {"root_cause": {"old": None, "new": "Loose packing"}}

    def test_rma_lifecycle_audits_every_step(self, client: TestClient, db_session: Session, quality_a):
        headers = headers_for(quality_a)
        complaint = make_complaint(db_session, company_id=COMPANY_A)
        created = client.post(
            f"{API}/rma/",
            json={"complaint_id": complaint.id, "customer_name": "Acme", "quantity": 2, "reason": "Return dented"},
            headers=headers,
        )
        assert created.status_code == 200, created.text
        rma_id = created.json()["id"]
        assert db_session.get(ReturnMaterialAuthorization, rma_id).company_id == COMPANY_A
        for verb, body in [
            ("approve", None),
            ("receive", {"shipping_tracking": "1Z999"}),
            ("inspect", {"inspection_findings": "Dent confirmed"}),
            ("dispose", {"disposition": "replace"}),
            ("complete", None),
        ]:
            response = client.post(f"{API}/rma/{rma_id}/{verb}", json=body, headers=headers)
            assert response.status_code == 200, response.text

        rows = _audit_rows(db_session, "rma", rma_id)
        assert [row.action for row in rows] == ["CREATE"] + ["STATUS_CHANGE"] * 5
        assert [row.new_values["status"] for row in rows[1:]] == [
            "approved",
            "material_received",
            "under_inspection",
            "disposition_decided",
            "completed",
        ]
        assert all(row.company_id == COMPANY_A for row in rows)

    def test_dispose_rejects_unknown_disposition(self, client: TestClient, db_session: Session, quality_a):
        rma = make_rma(db_session, company_id=COMPANY_A, status=RMAStatus.UNDER_INSPECTION)
        before_counts = _side_effect_counts(db_session)

        response = client.post(
            f"{API}/rma/{rma.id}/dispose", json={"disposition": "keep_it"}, headers=headers_for(quality_a)
        )

        assert response.status_code == 400, response.text
        assert _state(db_session, ReturnMaterialAuthorization, rma.id)["status"] == RMAStatus.UNDER_INSPECTION
        assert _side_effect_counts(db_session) == before_counts


# ---------------------------------------------------------------- references


class TestReferenceValidation:
    @pytest.mark.parametrize("field", ["customer_id", "part_id", "work_order_id", "assigned_to"])
    def test_create_refuses_foreign_reference(self, client: TestClient, db_session: Session, quality_a, field):
        foreign = {
            "customer_id": lambda: make_customer(db_session, company_id=COMPANY_B).id,
            "part_id": lambda: make_part(db_session, company_id=COMPANY_B).id,
            "work_order_id": lambda: make_work_order(db_session, company_id=COMPANY_B).id,
            "assigned_to": lambda: make_user(db_session, company_id=COMPANY_B).id,
        }[field]()
        before_counts = _side_effect_counts(db_session)

        response = client.post(
            f"{API}/complaints/",
            json={"customer_name": "Acme", "title": "Dented", "description": "Dented flange", field: foreign},
            headers=headers_for(quality_a),
        )

        assert response.status_code == 422, response.text
        assert field in response.json()["detail"]
        assert _side_effect_counts(db_session) == before_counts

    @pytest.mark.parametrize("field", ["part_id", "work_order_id", "assigned_to"])
    def test_create_refuses_retired_reference(self, client: TestClient, db_session: Session, quality_a, field):
        retired = {
            "part_id": lambda: make_part(db_session, is_deleted=True).id,
            "work_order_id": lambda: make_work_order(db_session, is_deleted=True).id,
            "assigned_to": lambda: make_user(db_session, active=False).id,
        }[field]()

        response = client.post(
            f"{API}/complaints/",
            json={"customer_name": "Acme", "title": "Dented", "description": "Dented flange", field: retired},
            headers=headers_for(quality_a),
        )

        assert response.status_code == 422, response.text

    def test_create_accepts_own_live_references(self, client: TestClient, db_session: Session, quality_a):
        part = make_part(db_session)
        customer = make_customer(db_session)
        wo = make_work_order(db_session)

        response = client.post(
            f"{API}/complaints/",
            json={
                "customer_name": customer.name,
                "customer_id": customer.id,
                "part_id": part.id,
                "work_order_id": wo.id,
                "assigned_to": quality_a.id,
                "title": "Dented",
                "description": "Dented flange",
            },
            headers=headers_for(quality_a),
        )

        assert response.status_code == 200, response.text
        assert response.json()["part"]["part_number"] == part.part_number
        assert response.json()["customer"]["name"] == customer.name

    def test_update_refuses_foreign_part(self, client: TestClient, db_session: Session, quality_a):
        complaint = make_complaint(db_session, company_id=COMPANY_A)
        foreign_part = make_part(db_session, company_id=COMPANY_B)
        before_row = _state(db_session, CustomerComplaint, complaint.id)

        response = client.put(
            f"{API}/complaints/{complaint.id}", json={"part_id": foreign_part.id}, headers=headers_for(quality_a)
        )

        assert response.status_code == 422, response.text
        assert _state(db_session, CustomerComplaint, complaint.id) == before_row

    def test_update_keeps_an_unchanged_reference_to_a_retired_part(
        self, client: TestClient, db_session: Session, quality_a
    ):
        # An unchanged reference is a record, not a new selection.
        part = make_part(db_session)
        complaint = make_complaint(db_session, company_id=COMPANY_A, part_id=part.id)
        part.is_deleted = True
        db_session.commit()

        response = client.put(
            f"{API}/complaints/{complaint.id}",
            json={"part_id": part.id, "root_cause": "Loose packing"},
            headers=headers_for(quality_a),
        )

        assert response.status_code == 200, response.text

    def test_rma_refuses_foreign_complaint(self, client: TestClient, db_session: Session, quality_a):
        foreign_complaint = make_complaint(db_session, company_id=COMPANY_B)
        before_counts = _side_effect_counts(db_session)

        response = client.post(
            f"{API}/rma/",
            json={"complaint_id": foreign_complaint.id, "customer_name": "Acme", "quantity": 1, "reason": "Return it"},
            headers=headers_for(quality_a),
        )

        assert response.status_code == 422, response.text
        assert _side_effect_counts(db_session) == before_counts

    def test_dispose_refuses_foreign_replacement_work_order(self, client: TestClient, db_session: Session, quality_a):
        rma = make_rma(db_session, company_id=COMPANY_A, status=RMAStatus.UNDER_INSPECTION)
        foreign_wo = make_work_order(db_session, company_id=COMPANY_B)
        before_row = _state(db_session, ReturnMaterialAuthorization, rma.id)
        before_counts = _side_effect_counts(db_session)

        response = client.post(
            f"{API}/rma/{rma.id}/dispose",
            json={"disposition": "replace", "replacement_wo_id": foreign_wo.id},
            headers=headers_for(quality_a),
        )

        assert response.status_code == 422, response.text
        assert _state(db_session, ReturnMaterialAuthorization, rma.id) == before_row
        assert _side_effect_counts(db_session) == before_counts


# ---------------------------------------------------------------- NCR / CAR escalation


class TestEscalation:
    def test_create_ncr_is_tenant_correct_numbered_evented_and_audited(
        self, client: TestClient, db_session: Session, quality_a
    ):
        part = make_part(db_session)
        wo = make_work_order(db_session)
        complaint = make_complaint(
            db_session, company_id=COMPANY_A, part_id=part.id, work_order_id=wo.id, lot_number="LOT-42"
        )
        today = f"{datetime.now():%Y%m%d}"
        existing = NonConformanceReport(
            ncr_number=f"NCR-{today}-001",
            title="Earlier NCR",
            description="Earlier",
            source=NCRSource.IN_PROCESS,
            company_id=COMPANY_A,
        )
        db_session.add(existing)
        db_session.commit()

        response = client.post(f"{API}/complaints/{complaint.id}/create-ncr", headers=headers_for(quality_a))

        assert response.status_code == 200, response.text
        ncr = db_session.get(NonConformanceReport, response.json()["ncr_id"])
        assert ncr.company_id == COMPANY_A
        assert ncr.ncr_number == f"NCR-{today}-002"
        assert ncr.source == NCRSource.CUSTOMER_RETURN
        assert (ncr.part_id, ncr.work_order_id, ncr.lot_number) == (part.id, wo.id, "LOT-42")
        assert ncr.detected_by == quality_a.id
        event = db_session.query(OperationalEvent).filter(OperationalEvent.event_type == "ncr_created").one()
        assert (event.company_id, event.entity_id, event.work_order_id) == (COMPANY_A, ncr.id, wo.id)
        assert event.event_payload["complaint_number"] == complaint.complaint_number
        assert [row.action for row in _audit_rows(db_session, "ncr", ncr.id)] == ["CREATE"]
        complaint_rows = _audit_rows(db_session, "customer_complaint", complaint.id)
        assert [row.action for row in complaint_rows] == ["UPDATE"]
        assert complaint_rows[0].extra_data["changes"]["ncr_id"] == {"old": None, "new": ncr.id}

        again = client.post(f"{API}/complaints/{complaint.id}/create-ncr", headers=headers_for(quality_a))
        assert again.status_code == 400, again.text
        assert _count(db_session, NonConformanceReport) == 2

    def test_create_car_is_tenant_correct_numbered_evented_and_audited(
        self, client: TestClient, db_session: Session, quality_a
    ):
        complaint = make_complaint(db_session, company_id=COMPANY_A)

        response = client.post(f"{API}/complaints/{complaint.id}/create-car", headers=headers_for(quality_a))

        assert response.status_code == 200, response.text
        car = db_session.get(CorrectiveActionRequest, response.json()["car_id"])
        assert car.company_id == COMPANY_A
        assert car.car_number == f"CAR-{datetime.now():%Y%m%d}-001"
        assert car.initiated_by == quality_a.id
        assert car.problem_description == complaint.description
        event = db_session.query(OperationalEvent).filter(OperationalEvent.event_type == "car_created").one()
        assert (event.company_id, event.entity_id) == (COMPANY_A, car.id)
        assert [row.action for row in _audit_rows(db_session, "car", car.id)] == ["CREATE"]

        again = client.post(f"{API}/complaints/{complaint.id}/create-car", headers=headers_for(quality_a))
        assert again.status_code == 400, again.text
        assert _count(db_session, CorrectiveActionRequest) == 1

    @pytest.mark.parametrize("verb,field", [("create-ncr", "ncr_id"), ("create-car", "car_id")])
    def test_long_titles_fit_the_record(self, client: TestClient, db_session: Session, quality_a, verb, field):
        complaint = make_complaint(db_session, company_id=COMPANY_A, title="T" * 255)

        response = client.post(f"{API}/complaints/{complaint.id}/{verb}", headers=headers_for(quality_a))

        assert response.status_code == 200, response.text
        model = NonConformanceReport if field == "ncr_id" else CorrectiveActionRequest
        assert len(db_session.get(model, response.json()[field]).title) == 255

    def test_create_ncr_refuses_a_legacy_cross_tenant_part(self, client: TestClient, db_session: Session, quality_a):
        foreign_part = make_part(db_session, company_id=COMPANY_B)
        complaint = make_complaint(db_session, company_id=COMPANY_A, part_id=foreign_part.id)
        before_counts = _side_effect_counts(db_session)

        response = client.post(f"{API}/complaints/{complaint.id}/create-ncr", headers=headers_for(quality_a))

        assert response.status_code == 409, response.text
        assert _state(db_session, CustomerComplaint, complaint.id)["ncr_id"] is None
        assert _side_effect_counts(db_session) == before_counts

    def test_create_ncr_keeps_naming_a_retired_part(self, client: TestClient, db_session: Session, quality_a):
        # The NCR records what the complaint names; a soft-deleted part is still ours.
        part = make_part(db_session, is_deleted=True)
        complaint = make_complaint(db_session, company_id=COMPANY_A, part_id=part.id)

        response = client.post(f"{API}/complaints/{complaint.id}/create-ncr", headers=headers_for(quality_a))

        assert response.status_code == 200, response.text
        assert db_session.get(NonConformanceReport, response.json()["ncr_id"]).part_id == part.id
