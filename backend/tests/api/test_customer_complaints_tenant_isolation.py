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
- the dashboard counts only the caller's company (every aggregate, including the
  resolution average), and no legacy cross-tenant part / customer / RMA link is shown;
- Viewer / Operator / Shipping -> 403 on every gated write with no side effect, and
  Supervisor -> 403 on the approver-tier verbs while keeping the author-tier ones;
  Manager and Quality keep every gated verb, and Viewer / Operator keep every read;
- the RMA from-state guards refuse out-of-order verbs with no side effect;
- every write audits; resolve / close, RMA approve / deny / dispose / complete and
  create-ncr / create-car fail closed (503, nothing saved) when a required audit row
  cannot be written;
- ``status`` (complaint) and ``disposition`` (RMA) are refused on PUT, and so is every
  field a workflow verb owns (investigate / resolve on a complaint, ``credit_amount``
  on an RMA), so an Author-tier PUT cannot rewrite an Approver's decision;
- PUT refuses (409) a closed / void complaint and a completed / denied RMA, an RMA
  ``quantity`` / ``reason`` change after REQUESTED, ``inspection_findings`` outside
  UNDER_INSPECTION, and a ``replacement_wo_id`` link on an RMA not dispositioned
  ``replace``;
- selected references must be live rows of the active company, except an RMA's
  customer / part copied from its own complaint, which only has to be ours;
- create-ncr / create-car produce tenant-correct, per-company-numbered records and
  emit the same events as the quality router's create paths, and a second tenant's
  same-day "-001" no longer collides (NCR/CAR numbers are unique per company, 116);
- complaint and RMA numbers are per-company sequences under a per-company lock, so
  two tenants may hold the same CC-/RMA- number.
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


def make_customer(db: Session, *, company_id: int = COMPANY_A, name: str = None, is_deleted: bool = False) -> Customer:
    _ensure_company(db, company_id)
    n = _next()
    customer = Customer(
        name=name or f"Customer {company_id}-{n}", code=f"CC-C-{n}", company_id=company_id, is_deleted=is_deleted
    )
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
        rma_number=fields.pop("rma_number", f"RMA-T-{company_id}-{n:05d}"),
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

    def test_legacy_cross_tenant_rma_links_are_not_disclosed(self, client: TestClient, db_session: Session, quality_a):
        # The RMA's own customer/part loads are scoped separately from the complaint's,
        # on both the detail and the list read.
        foreign_part = make_part(db_session, company_id=COMPANY_B)
        foreign_customer = make_customer(db_session, company_id=COMPANY_B, name="Bravo Secret Co")
        own_rma = make_rma(db_session, company_id=COMPANY_A, part_id=foreign_part.id, customer_id=foreign_customer.id)

        detail = client.get(f"{API}/rma/{own_rma.id}", headers=headers_for(quality_a)).json()
        listed = client.get(f"{API}/rma/", headers=headers_for(quality_a)).json()

        assert [row["id"] for row in listed] == [own_rma.id]
        for body in (detail, listed[0]):
            assert body["customer"] is None
            assert body["part"] is None
            assert "Bravo Secret Co" not in str(body)

    def test_dashboard_resolution_average_is_scoped(self, client: TestClient, db_session: Session, quality_a):
        # avg_resolution_days is its own query; the open-count / severity / satisfaction
        # assertions above cannot see whether it is scoped. Ours: 10 days. Unscoped: 55.
        today = date.today()
        make_complaint(
            db_session,
            company_id=COMPANY_A,
            status=ComplaintStatus.RESOLVED,
            date_received=today - timedelta(days=10),
            resolved_date=today,
        )
        make_complaint(
            db_session,
            company_id=COMPANY_B,
            status=ComplaintStatus.RESOLVED,
            date_received=today - timedelta(days=100),
            resolved_date=today,
        )

        response = client.get(f"{API}/complaints/dashboard", headers=headers_for(quality_a))

        assert response.status_code == 200, response.text
        assert response.json()["avg_resolution_days"] == 10.0

    READ_ROUTES = [
        ("dashboard", "/complaints/dashboard"),
        ("list_complaints", "/complaints/"),
        ("get_complaint", "/complaints/{complaint_id}"),
        ("get_8d_report", "/complaints/8d-report/{complaint_id}"),
        ("list_rmas", "/rma/"),
        ("get_rma", "/rma/{rma_id}"),
    ]

    @pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.OPERATOR])
    @pytest.mark.parametrize("name,path", READ_ROUTES, ids=[r[0] for r in READ_ROUTES])
    def test_reads_stay_open_to_view_only_roles(self, client: TestClient, db_session: Session, role, name, path):
        # The page is gated on quality:view, which Operator and Viewer hold; the write
        # gates must not leak onto the reads they still rely on.
        caller = make_user(db_session, company_id=COMPANY_A, role=role)
        complaint = make_complaint(db_session, company_id=COMPANY_A)
        rma = make_rma(db_session, company_id=COMPANY_A, complaint_id=complaint.id)

        response = client.get(
            f"{API}{path.format(complaint_id=complaint.id, rma_id=rma.id)}", headers=headers_for(caller)
        )

        assert response.status_code == 200, response.text


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

    # Nobody who held a verb before the gates moved may lose it: create-ncr / create-car
    # and the four RMA decisions were already Admin / Manager / Quality, and every
    # previously-open verb is in one of the two tiers both roles sit in. The tenancy
    # control above only exercises Admin.
    @pytest.mark.parametrize("role", [UserRole.MANAGER, UserRole.QUALITY])
    @pytest.mark.parametrize(
        "name,method,path,body,status,kind", GATED_BY_ID_ROUTES, ids=[r[0] for r in GATED_BY_ID_ROUTES]
    )
    def test_manager_and_quality_keep_every_gated_verb(
        self, client: TestClient, db_session: Session, role, name, method, path, body, status, kind
    ):
        caller = make_user(db_session, company_id=COMPANY_A, role=role)
        _model, own_id = _seed(db_session, kind, COMPANY_A, status)

        response = _call(client, method, path.format(id=own_id), body, headers_for(caller))

        assert response.status_code == 200, response.text
        assert response.json()["id"] == own_id

    @pytest.mark.parametrize("role", [UserRole.MANAGER, UserRole.QUALITY])
    @pytest.mark.parametrize("name,path,body", WRITE_CREATES, ids=[r[0] for r in WRITE_CREATES])
    def test_manager_and_quality_can_create(self, client: TestClient, db_session: Session, role, name, path, body):
        caller = make_user(db_session, company_id=COMPANY_A, role=role)

        response = client.post(f"{API}{path}", json=body, headers=headers_for(caller))

        assert response.status_code == 200, response.text
        model = CustomerComplaint if name == "create_complaint" else ReturnMaterialAuthorization
        assert db_session.get(model, response.json()["id"]).company_id == COMPANY_A


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

    def test_complaint_numbers_are_a_per_company_sequence(self, client: TestClient, db_session: Session, quality_a):
        # complaint_number is unique per company (migration 116), so each tenant numbers
        # from its own highest value: another tenant's CC-YYYY-0007 neither advances our
        # sequence nor blocks our own -0001, and our next number follows our own last one.
        year = f"{datetime.now():%Y}"
        make_complaint(db_session, company_id=COMPANY_B, complaint_number=f"CC-{year}-0007")
        payload = {"customer_name": "Acme", "title": "Dented", "description": "Dented flange"}

        first = client.post(f"{API}/complaints/", json=payload, headers=headers_for(quality_a))
        second = client.post(f"{API}/complaints/", json=payload, headers=headers_for(quality_a))

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json()["complaint_number"] == f"CC-{year}-0001"
        assert second.json()["complaint_number"] == f"CC-{year}-0002"
        # The other tenant's row is untouched and still holds its number.
        owned = db_session.query(CustomerComplaint).filter(CustomerComplaint.company_id == COMPANY_B).one()
        assert owned.complaint_number == f"CC-{year}-0007"

    def test_two_tenants_may_hold_the_same_complaint_number(self, client: TestClient, db_session: Session, quality_a):
        # The case the global unique turned into a 500: another tenant already holds -0001.
        year = f"{datetime.now():%Y}"
        make_complaint(db_session, company_id=COMPANY_B, complaint_number=f"CC-{year}-0001")

        response = client.post(
            f"{API}/complaints/",
            json={"customer_name": "Acme", "title": "Dented", "description": "Dented flange"},
            headers=headers_for(quality_a),
        )

        assert response.status_code == 200, response.text
        assert response.json()["complaint_number"] == f"CC-{year}-0001"

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
            ("rma", "/rma/{id}/approve", None, RMAStatus.REQUESTED, "status", RMAStatus.REQUESTED),
            ("rma", "/rma/{id}/deny", None, RMAStatus.REQUESTED, "status", RMAStatus.REQUESTED),
            # The NCR / CAR, its event and the complaint's link all roll back together.
            ("complaint", "/complaints/{id}/create-ncr", None, ComplaintStatus.RECEIVED, "ncr_id", None),
            ("complaint", "/complaints/{id}/create-car", None, ComplaintStatus.RECEIVED, "car_id", None),
        ],
        ids=["resolve", "close", "dispose", "complete", "approve", "deny", "create-ncr", "create-car"],
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
            f"{API}/complaints/{complaint.id}", json={"lot_number": "LOT-77"}, headers=headers_for(quality_a)
        )

        assert response.status_code == 200, response.text
        rows = _audit_rows(db_session, "customer_complaint", complaint.id)
        assert [row.action for row in rows] == ["UPDATE"]
        assert rows[0].extra_data["changes"] == {"lot_number": {"old": None, "new": "LOT-77"}}

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


class TestRMAWorkflow:
    # The RMA from-state guards were rewritten onto one shared ``_rma_transition``; the
    # lifecycle test above only walks the happy path, so nothing held them.
    @pytest.mark.parametrize(
        "verb,body,status",
        [
            ("approve", None, RMAStatus.APPROVED),
            ("approve", None, RMAStatus.DENIED),
            ("deny", None, RMAStatus.APPROVED),
            ("deny", None, RMAStatus.COMPLETED),
            ("receive", {"shipping_tracking": "1Z999"}, RMAStatus.REQUESTED),
            ("receive", {"shipping_tracking": "1Z999"}, RMAStatus.DENIED),
            ("inspect", {"inspection_findings": "Dent confirmed"}, RMAStatus.APPROVED),
            ("dispose", {"disposition": "scrap"}, RMAStatus.REQUESTED),
            ("dispose", {"disposition": "scrap"}, RMAStatus.APPROVED),
            ("dispose", {"disposition": "scrap"}, RMAStatus.DISPOSITION_DECIDED),
            ("complete", None, RMAStatus.UNDER_INSPECTION),
            ("complete", None, RMAStatus.COMPLETED),
        ],
        ids=lambda value: value.value if isinstance(value, RMAStatus) else None,
    )
    def test_out_of_order_rma_transitions_refused(
        self, client: TestClient, db_session: Session, quality_a, verb, body, status
    ):
        rma = make_rma(db_session, company_id=COMPANY_A, status=status)
        before_row = _state(db_session, ReturnMaterialAuthorization, rma.id)
        before_counts = _side_effect_counts(db_session)

        response = client.post(f"{API}/rma/{rma.id}/{verb}", json=body, headers=headers_for(quality_a))

        assert response.status_code == 400, response.text
        assert _state(db_session, ReturnMaterialAuthorization, rma.id) == before_row
        assert _side_effect_counts(db_session) == before_counts

    def test_dispose_is_allowed_straight_from_material_received(
        self, client: TestClient, db_session: Session, quality_a
    ):
        rma = make_rma(db_session, company_id=COMPANY_A, status=RMAStatus.MATERIAL_RECEIVED)

        response = client.post(
            f"{API}/rma/{rma.id}/dispose",
            json={"disposition": "credit", "credit_amount": 40},
            headers=headers_for(quality_a),
        )

        assert response.status_code == 200, response.text
        state = _state(db_session, ReturnMaterialAuthorization, rma.id)
        assert (state["status"], state["disposition"], state["credit_amount"]) == (
            RMAStatus.DISPOSITION_DECIDED,
            "credit",
            40,
        )
        rows = _audit_rows(db_session, "rma", rma.id)
        assert [(row.action, row.old_values["status"], row.new_values["status"]) for row in rows] == [
            ("STATUS_CHANGE", "material_received", "disposition_decided")
        ]
        assert rows[0].extra_data["changes"]["disposition"] == {"old": None, "new": "credit"}

    @pytest.mark.parametrize("verb,expected", [("approve", RMAStatus.APPROVED), ("deny", RMAStatus.DENIED)])
    def test_rma_decision_records_who_decided(self, client: TestClient, db_session: Session, verb, expected):
        decider = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
        rma = make_rma(db_session, company_id=COMPANY_A)

        response = client.post(f"{API}/rma/{rma.id}/{verb}", headers=headers_for(decider))

        assert response.status_code == 200, response.text
        state = _state(db_session, ReturnMaterialAuthorization, rma.id)
        assert state["status"] == expected
        assert state["authorized_by"] == decider.id
        assert state["authorized_date"] == date.today()
        rows = _audit_rows(db_session, "rma", rma.id)
        assert [(row.action, row.new_values["status"], row.user_id) for row in rows] == [
            ("STATUS_CHANGE", expected.value, decider.id)
        ]
        assert rows[0].extra_data["changes"]["authorized_by"] == {"old": None, "new": decider.id}

    def test_put_rma_audits_the_changed_fields(self, client: TestClient, db_session: Session, quality_a):
        rma = make_rma(db_session, company_id=COMPANY_A)

        response = client.put(
            f"{API}/rma/{rma.id}",
            json={"notes": "Customer asked for expedite", "lot_number": "LOT-9"},
            headers=headers_for(quality_a),
        )

        assert response.status_code == 200, response.text
        rows = _audit_rows(db_session, "rma", rma.id)
        assert [(row.action, row.company_id) for row in rows] == [("UPDATE", COMPANY_A)]
        assert rows[0].extra_data["changes"] == {
            "notes": {"old": None, "new": "Customer asked for expedite"},
            "lot_number": {"old": None, "new": "LOT-9"},
        }

    def test_rma_numbers_are_a_per_company_sequence(self, client: TestClient, db_session: Session, quality_a):
        # rma_number is unique per company like complaint_number (migration 116): another
        # tenant's -0001 and -0003 are invisible to our sequence, which follows our own -0001.
        year = f"{datetime.now():%Y}"
        make_rma(db_session, company_id=COMPANY_B, rma_number=f"RMA-{year}-0001")
        make_rma(db_session, company_id=COMPANY_B, rma_number=f"RMA-{year}-0003")
        make_rma(db_session, company_id=COMPANY_A, rma_number=f"RMA-{year}-0001")

        response = client.post(
            f"{API}/rma/",
            json={"customer_name": "Acme", "quantity": 1, "reason": "Return for inspection"},
            headers=headers_for(quality_a),
        )

        assert response.status_code == 200, response.text
        assert response.json()["rma_number"] == f"RMA-{year}-0002"

    def test_complaint_and_rma_numbers_are_allocated_under_the_generator_lock(
        self, client: TestClient, db_session: Session, quality_a, monkeypatch
    ):
        # SQLite cannot exercise the advisory lock itself; this holds that both
        # allocators still take it (the read-max-plus-one scan is racy without it), and
        # take it per company: the sequences are per company, so a global slot would
        # make every tenant's create wait on every other's for nothing.
        from app.services import customer_complaint_service

        taken = []
        monkeypatch.setattr(
            customer_complaint_service,
            "acquire_generator_lock",
            lambda db, namespace, company_id=None: taken.append((namespace, company_id)),
        )
        headers = headers_for(quality_a)

        complaint = client.post(
            f"{API}/complaints/",
            json={"customer_name": "Acme", "title": "Dented", "description": "Dented flange"},
            headers=headers,
        )
        rma = client.post(
            f"{API}/rma/", json={"customer_name": "Acme", "quantity": 1, "reason": "Return it now"}, headers=headers
        )

        assert complaint.status_code == 200 and rma.status_code == 200
        assert taken == [("complaint_number", COMPANY_A), ("rma_number", COMPANY_A)]


# ---------------------------------------------------------------- PUT vs the workflow verbs


class TestPutCannotBypassTheVerbs:
    # The update schemas used to carry the fields resolve and dispose decide, and PUT is
    # Author-tier with no state check. A Supervisor (refused on /resolve and /dispose)
    # could therefore set them anyway: a review probe got 200 on PUT credit_amount=99999
    # against a COMPLETED RMA, and on a rewrite of a CLOSED complaint's resolution.

    @pytest.mark.parametrize(
        "field,value",
        [
            ("resolution_description", "Rewritten resolution"),
            ("corrective_action", "Rewritten corrective action"),
            ("preventive_action", "Rewritten preventive action"),
            ("actual_cost", 0),
            ("investigation_findings", "Findings outside investigate"),
            ("root_cause", "Root cause outside investigate"),
            ("containment_action", "Containment outside investigate"),
        ],
    )
    def test_complaint_put_refuses_a_verb_owned_field(self, client: TestClient, db_session: Session, field, value):
        supervisor = make_user(db_session, company_id=COMPANY_A, role=UserRole.SUPERVISOR)
        complaint = make_complaint(db_session, company_id=COMPANY_A, status=ComplaintStatus.UNDER_INVESTIGATION)
        before_row = _state(db_session, CustomerComplaint, complaint.id)
        before_counts = _side_effect_counts(db_session)

        response = client.put(f"{API}/complaints/{complaint.id}", json={field: value}, headers=headers_for(supervisor))

        assert response.status_code == 422, response.text
        assert field in response.text
        assert _state(db_session, CustomerComplaint, complaint.id) == before_row
        assert _side_effect_counts(db_session) == before_counts

    def test_rma_put_refuses_credit_amount(self, client: TestClient, db_session: Session):
        supervisor = make_user(db_session, company_id=COMPANY_A, role=UserRole.SUPERVISOR)
        rma = make_rma(
            db_session, company_id=COMPANY_A, status=RMAStatus.COMPLETED, disposition="credit", credit_amount=40
        )
        before_row = _state(db_session, ReturnMaterialAuthorization, rma.id)
        before_counts = _side_effect_counts(db_session)

        response = client.put(f"{API}/rma/{rma.id}", json={"credit_amount": 99999}, headers=headers_for(supervisor))

        assert response.status_code == 422, response.text
        assert "credit_amount" in response.text
        assert _state(db_session, ReturnMaterialAuthorization, rma.id) == before_row
        assert _side_effect_counts(db_session) == before_counts

    @pytest.mark.parametrize("status", [ComplaintStatus.CLOSED, ComplaintStatus.VOID])
    def test_a_closed_complaint_cannot_be_edited(self, client: TestClient, db_session: Session, quality_a, status):
        # Once create-ncr has copied the lot, a later rewrite would make the closed
        # complaint and its NCR disagree about which lot was affected.
        complaint = make_complaint(db_session, company_id=COMPANY_A, status=status, lot_number="LOT-1")
        before_row = _state(db_session, CustomerComplaint, complaint.id)
        before_counts = _side_effect_counts(db_session)

        response = client.put(
            f"{API}/complaints/{complaint.id}", json={"lot_number": "LOT-2"}, headers=headers_for(quality_a)
        )

        assert response.status_code == 409, response.text
        assert "closed quality record" in response.json()["detail"]
        assert _state(db_session, CustomerComplaint, complaint.id) == before_row
        assert _side_effect_counts(db_session) == before_counts

    def test_a_resolved_complaint_is_still_editable(self, client: TestClient, db_session: Session, quality_a):
        # Control: the freeze is CLOSED / VOID only; RESOLVED still awaits close.
        complaint = make_complaint(db_session, company_id=COMPANY_A, status=ComplaintStatus.RESOLVED)

        response = client.put(
            f"{API}/complaints/{complaint.id}", json={"lot_number": "LOT-2"}, headers=headers_for(quality_a)
        )

        assert response.status_code == 200, response.text
        assert _state(db_session, CustomerComplaint, complaint.id)["lot_number"] == "LOT-2"

    @pytest.mark.parametrize("status", [RMAStatus.COMPLETED, RMAStatus.DENIED])
    def test_a_finished_rma_cannot_be_edited(self, client: TestClient, db_session: Session, quality_a, status):
        rma = make_rma(db_session, company_id=COMPANY_A, status=status)
        before_row = _state(db_session, ReturnMaterialAuthorization, rma.id)
        before_counts = _side_effect_counts(db_session)

        response = client.put(f"{API}/rma/{rma.id}", json={"notes": "Late note"}, headers=headers_for(quality_a))

        assert response.status_code == 409, response.text
        assert _state(db_session, ReturnMaterialAuthorization, rma.id) == before_row
        assert _side_effect_counts(db_session) == before_counts

    @pytest.mark.parametrize(
        "status,body,field",
        [
            # approve authorized this quantity and reason
            (RMAStatus.APPROVED, {"quantity": 50}, "quantity"),
            (RMAStatus.APPROVED, {"reason": "A different reason now"}, "reason"),
            (RMAStatus.UNDER_INSPECTION, {"quantity": 50}, "quantity"),
            # inspect writes the findings (from MATERIAL_RECEIVED); dispose decides on them
            (RMAStatus.REQUESTED, {"inspection_findings": "Findings before receipt"}, "inspection_findings"),
            (RMAStatus.MATERIAL_RECEIVED, {"inspection_findings": "Findings skipping inspect"}, "inspection_findings"),
            (RMAStatus.DISPOSITION_DECIDED, {"inspection_findings": "Findings after decision"}, "inspection_findings"),
        ],
        ids=["qty-approved", "reason-approved", "qty-inspecting", "find-requested", "find-received", "find-decided"],
    )
    def test_rma_put_refuses_a_field_outside_its_window(
        self, client: TestClient, db_session: Session, quality_a, status, body, field
    ):
        rma = make_rma(db_session, company_id=COMPANY_A, status=status)
        before_row = _state(db_session, ReturnMaterialAuthorization, rma.id)
        before_counts = _side_effect_counts(db_session)

        response = client.put(f"{API}/rma/{rma.id}", json=body, headers=headers_for(quality_a))

        assert response.status_code == 409, response.text
        assert response.json()["detail"].startswith(f"{field} can only be changed while the RMA is")
        assert _state(db_session, ReturnMaterialAuthorization, rma.id) == before_row
        assert _side_effect_counts(db_session) == before_counts

    @pytest.mark.parametrize(
        "status,body",
        [
            (RMAStatus.REQUESTED, {"quantity": 5, "reason": "Customer revised the count"}),
            (RMAStatus.UNDER_INSPECTION, {"inspection_findings": "Amended: dent and a scratch"}),
            # A full-object client resending the authorized values changes nothing locked.
            (RMAStatus.APPROVED, {"quantity": 2, "reason": "Return for inspection", "notes": "Expedite"}),
        ],
        ids=["requested", "under-inspection", "unchanged-after-approval"],
    )
    def test_rma_put_allows_a_field_inside_its_window(
        self, client: TestClient, db_session: Session, quality_a, status, body
    ):
        rma = make_rma(db_session, company_id=COMPANY_A, status=status)

        response = client.put(f"{API}/rma/{rma.id}", json=body, headers=headers_for(quality_a))

        assert response.status_code == 200, response.text
        state = _state(db_session, ReturnMaterialAuthorization, rma.id)
        assert {key: state[key] for key in body} == body

    @pytest.mark.parametrize(
        "status,disposition", [(RMAStatus.REQUESTED, None), (RMAStatus.DISPOSITION_DECIDED, "credit")]
    )
    def test_replacement_work_order_links_only_on_a_replace_disposition(
        self, client: TestClient, db_session: Session, quality_a, status, disposition
    ):
        wo = make_work_order(db_session, company_id=COMPANY_A)
        rma = make_rma(db_session, company_id=COMPANY_A, status=status, disposition=disposition)
        before_row = _state(db_session, ReturnMaterialAuthorization, rma.id)
        before_counts = _side_effect_counts(db_session)

        response = client.put(f"{API}/rma/{rma.id}", json={"replacement_wo_id": wo.id}, headers=headers_for(quality_a))

        assert response.status_code == 409, response.text
        assert "dispositioned 'replace'" in response.json()["detail"]
        assert _state(db_session, ReturnMaterialAuthorization, rma.id) == before_row
        assert _side_effect_counts(db_session) == before_counts

    @pytest.mark.parametrize("field", ["title", "description", "customer_name"])
    def test_complaint_put_refuses_blank_required_text(self, client: TestClient, db_session: Session, quality_a, field):
        # The same minimums create enforces; "" used to pass where null was refused.
        complaint = make_complaint(db_session, company_id=COMPANY_A)
        before_row = _state(db_session, CustomerComplaint, complaint.id)

        response = client.put(f"{API}/complaints/{complaint.id}", json={field: ""}, headers=headers_for(quality_a))

        assert response.status_code == 422, response.text
        assert _state(db_session, CustomerComplaint, complaint.id) == before_row


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

    # Customer is soft-deletable too (SoftDeleteMixin), so a retired customer is not selectable.
    @pytest.mark.parametrize("field", ["customer_id", "part_id", "work_order_id", "assigned_to"])
    def test_create_refuses_retired_reference(self, client: TestClient, db_session: Session, quality_a, field):
        retired = {
            "customer_id": lambda: make_customer(db_session, is_deleted=True).id,
            "part_id": lambda: make_part(db_session, is_deleted=True).id,
            "work_order_id": lambda: make_work_order(db_session, is_deleted=True).id,
            "assigned_to": lambda: make_user(db_session, active=False).id,
        }[field]()
        before_counts = _side_effect_counts(db_session)

        response = client.post(
            f"{API}/complaints/",
            json={"customer_name": "Acme", "title": "Dented", "description": "Dented flange", field: retired},
            headers=headers_for(quality_a),
        )

        assert response.status_code == 422, response.text
        assert field in response.json()["detail"]
        assert _side_effect_counts(db_session) == before_counts

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
            json={"part_id": part.id, "lot_number": "LOT-77"},
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

    @pytest.mark.parametrize("variant", ["foreign", "retired"])
    @pytest.mark.parametrize("field", ["work_order_id", "assigned_to"])
    def test_update_refuses_foreign_or_retired_reference(
        self, client: TestClient, db_session: Session, quality_a, field, variant
    ):
        target = {
            ("work_order_id", "foreign"): lambda: make_work_order(db_session, company_id=COMPANY_B).id,
            ("work_order_id", "retired"): lambda: make_work_order(db_session, is_deleted=True).id,
            ("assigned_to", "foreign"): lambda: make_user(db_session, company_id=COMPANY_B).id,
            ("assigned_to", "retired"): lambda: make_user(db_session, active=False).id,
        }[(field, variant)]()
        complaint = make_complaint(db_session, company_id=COMPANY_A)
        before_row = _state(db_session, CustomerComplaint, complaint.id)
        before_counts = _side_effect_counts(db_session)

        response = client.put(f"{API}/complaints/{complaint.id}", json={field: target}, headers=headers_for(quality_a))

        assert response.status_code == 422, response.text
        assert field in response.json()["detail"]
        assert _state(db_session, CustomerComplaint, complaint.id) == before_row
        assert _side_effect_counts(db_session) == before_counts

    @pytest.mark.parametrize("field", ["customer_id", "part_id"])
    def test_rma_create_refuses_foreign_customer_or_part(
        self, client: TestClient, db_session: Session, quality_a, field
    ):
        foreign = {
            "customer_id": lambda: make_customer(db_session, company_id=COMPANY_B).id,
            "part_id": lambda: make_part(db_session, company_id=COMPANY_B).id,
        }[field]()
        before_counts = _side_effect_counts(db_session)

        response = client.post(
            f"{API}/rma/",
            json={"customer_name": "Acme", "quantity": 1, "reason": "Return it now", field: foreign},
            headers=headers_for(quality_a),
        )

        assert response.status_code == 422, response.text
        assert field in response.json()["detail"]
        assert _side_effect_counts(db_session) == before_counts

    @pytest.mark.parametrize("variant", ["foreign", "retired"])
    def test_put_rma_refuses_foreign_or_retired_replacement_work_order(
        self, client: TestClient, db_session: Session, quality_a, variant
    ):
        wo = make_work_order(
            db_session,
            company_id=COMPANY_B if variant == "foreign" else COMPANY_A,
            is_deleted=variant == "retired",
        )
        # Dispositioned 'replace', so the link window is open and only the reference is wrong.
        rma = make_rma(db_session, company_id=COMPANY_A, status=RMAStatus.DISPOSITION_DECIDED, disposition="replace")
        before_row = _state(db_session, ReturnMaterialAuthorization, rma.id)
        before_counts = _side_effect_counts(db_session)

        response = client.put(f"{API}/rma/{rma.id}", json={"replacement_wo_id": wo.id}, headers=headers_for(quality_a))

        assert response.status_code == 422, response.text
        assert "replacement_wo_id" in response.json()["detail"]
        assert _state(db_session, ReturnMaterialAuthorization, rma.id) == before_row
        assert _side_effect_counts(db_session) == before_counts

    def test_put_rma_accepts_an_own_live_replacement_work_order(
        self, client: TestClient, db_session: Session, quality_a
    ):
        # The replacement job is often opened after the 'replace' decision; PUT links it.
        wo = make_work_order(db_session, company_id=COMPANY_A)
        rma = make_rma(db_session, company_id=COMPANY_A, status=RMAStatus.DISPOSITION_DECIDED, disposition="replace")

        response = client.put(f"{API}/rma/{rma.id}", json={"replacement_wo_id": wo.id}, headers=headers_for(quality_a))

        assert response.status_code == 200, response.text
        assert _state(db_session, ReturnMaterialAuthorization, rma.id)["replacement_wo_id"] == wo.id

    @pytest.mark.parametrize("field", ["customer_id", "part_id"])
    def test_rma_from_complaint_keeps_naming_its_retired_customer_or_part(
        self, client: TestClient, db_session: Session, quality_a, field
    ):
        # The page copies the complaint's customer_id / part_id into the RMA body. Retiring
        # either after the complaint was logged must not block the return (create-ncr
        # accepts the same retired part).
        target = make_customer(db_session) if field == "customer_id" else make_part(db_session)
        complaint = make_complaint(db_session, company_id=COMPANY_A, **{field: target.id})
        target.is_deleted = True
        db_session.commit()

        response = client.post(
            f"{API}/rma/",
            json={
                "complaint_id": complaint.id,
                "customer_name": "Acme",
                "quantity": 1,
                "reason": "Return dented",
                field: target.id,
            },
            headers=headers_for(quality_a),
        )

        assert response.status_code == 200, response.text
        assert _state(db_session, ReturnMaterialAuthorization, response.json()["id"])[field] == target.id

    def test_rma_from_complaint_still_refuses_a_different_retired_part(
        self, client: TestClient, db_session: Session, quality_a
    ):
        # Only the value the complaint names is a record; any other part is a new choice.
        complaint = make_complaint(db_session, company_id=COMPANY_A, part_id=make_part(db_session).id)
        other_retired = make_part(db_session, is_deleted=True)
        before_counts = _side_effect_counts(db_session)

        response = client.post(
            f"{API}/rma/",
            json={
                "complaint_id": complaint.id,
                "customer_name": "Acme",
                "quantity": 1,
                "reason": "Return dented",
                "part_id": other_retired.id,
            },
            headers=headers_for(quality_a),
        )

        assert response.status_code == 422, response.text
        assert response.json()["detail"] == "part_id does not reference a live part in your company"
        assert _side_effect_counts(db_session) == before_counts

    def test_rma_from_complaint_refuses_a_legacy_foreign_part_it_copied(
        self, client: TestClient, db_session: Session, quality_a
    ):
        # A copied value is still checked for ownership: a legacy cross-tenant link on the
        # complaint does not launder the other tenant's part into a new RMA.
        foreign_part = make_part(db_session, company_id=COMPANY_B)
        complaint = make_complaint(db_session, company_id=COMPANY_A, part_id=foreign_part.id)
        before_counts = _side_effect_counts(db_session)

        response = client.post(
            f"{API}/rma/",
            json={
                "complaint_id": complaint.id,
                "customer_name": "Acme",
                "quantity": 1,
                "reason": "Return dented",
                "part_id": foreign_part.id,
            },
            headers=headers_for(quality_a),
        )

        assert response.status_code == 422, response.text
        assert response.json()["detail"] == "part_id does not reference a part in your company"
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
        _ensure_company(db_session, COMPANY_B)
        existing = NonConformanceReport(
            ncr_number=f"NCR-{today}-001",
            title="Earlier NCR",
            description="Earlier",
            source=NCRSource.IN_PROCESS,
            company_id=COMPANY_A,
        )
        # The other tenant's higher same-day number is what makes "-002" prove per-company
        # allocation: a global scan would hand out -006. (The same-number case, B already
        # holding A's next "-001", is test_same_day_escalations_in_two_tenants_do_not_collide.)
        other_tenant = NonConformanceReport(
            ncr_number=f"NCR-{today}-005",
            title="Other tenant NCR",
            description="Other tenant",
            source=NCRSource.IN_PROCESS,
            company_id=COMPANY_B,
        )
        db_session.add_all([existing, other_tenant])
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

        before_again = _side_effect_counts(db_session)
        again = client.post(f"{API}/complaints/{complaint.id}/create-ncr", headers=headers_for(quality_a))
        assert again.status_code == 400, again.text
        assert _count(db_session, NonConformanceReport) == 3
        assert _side_effect_counts(db_session) == before_again
        assert _state(db_session, CustomerComplaint, complaint.id)["ncr_id"] == ncr.id

    def test_create_car_is_tenant_correct_numbered_evented_and_audited(
        self, client: TestClient, db_session: Session, quality_a
    ):
        complaint = make_complaint(db_session, company_id=COMPANY_A)
        today = f"{datetime.now():%Y%m%d}"
        _ensure_company(db_session, COMPANY_B)
        # Another tenant's same-day CAR: per-company allocation gives A -001, a global scan -005.
        db_session.add(
            CorrectiveActionRequest(
                car_number=f"CAR-{today}-004",
                title="Other tenant CAR",
                problem_description="Other tenant",
                company_id=COMPANY_B,
            )
        )
        db_session.commit()

        response = client.post(f"{API}/complaints/{complaint.id}/create-car", headers=headers_for(quality_a))

        assert response.status_code == 200, response.text
        car = db_session.get(CorrectiveActionRequest, response.json()["car_id"])
        assert car.company_id == COMPANY_A
        assert car.car_number == f"CAR-{today}-001"
        assert car.initiated_by == quality_a.id
        assert car.problem_description == complaint.description
        event = db_session.query(OperationalEvent).filter(OperationalEvent.event_type == "car_created").one()
        assert (event.company_id, event.entity_id) == (COMPANY_A, car.id)
        assert event.event_payload["complaint_number"] == complaint.complaint_number
        assert [row.action for row in _audit_rows(db_session, "car", car.id)] == ["CREATE"]
        complaint_rows = _audit_rows(db_session, "customer_complaint", complaint.id)
        assert [row.action for row in complaint_rows] == ["UPDATE"]
        assert complaint_rows[0].extra_data["changes"]["car_id"] == {"old": None, "new": car.id}

        before_again = _side_effect_counts(db_session)
        again = client.post(f"{API}/complaints/{complaint.id}/create-car", headers=headers_for(quality_a))
        assert again.status_code == 400, again.text
        assert _count(db_session, CorrectiveActionRequest) == 2
        assert _side_effect_counts(db_session) == before_again
        assert _state(db_session, CustomerComplaint, complaint.id)["car_id"] == car.id

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

    def test_create_ncr_refuses_a_legacy_cross_tenant_work_order(
        self, client: TestClient, db_session: Session, quality_a
    ):
        # The part check above cannot see whether the work-order check exists.
        foreign_wo = make_work_order(db_session, company_id=COMPANY_B)
        complaint = make_complaint(db_session, company_id=COMPANY_A, work_order_id=foreign_wo.id)
        before_counts = _side_effect_counts(db_session)

        response = client.post(f"{API}/complaints/{complaint.id}/create-ncr", headers=headers_for(quality_a))

        assert response.status_code == 409, response.text
        assert "work order" in response.json()["detail"]
        assert _state(db_session, CustomerComplaint, complaint.id)["ncr_id"] is None
        assert _side_effect_counts(db_session) == before_counts

    # quality_numbering allocates NCR / CAR numbers PER COMPANY, and since migration 116
    # ncrs.ncr_number and cars.car_number are unique per company, as (company_id, number).
    # Before that both were GLOBALLY unique, so the first same-day NCR/CAR of a second
    # tenant drew the "-001" the first tenant already held and the insert failed (a 500
    # from every NCR/CAR writer). Pinned here, at the escalation verbs, because this
    # router reaches both allocators.
    @pytest.mark.parametrize("verb", ["create-ncr", "create-car"])
    def test_same_day_escalations_in_two_tenants_do_not_collide(
        self, client: TestClient, db_session: Session, quality_a, verb
    ):
        today = f"{datetime.now():%Y%m%d}"
        _ensure_company(db_session, COMPANY_B)
        if verb == "create-ncr":
            db_session.add(
                NonConformanceReport(
                    ncr_number=f"NCR-{today}-001",
                    title="Other tenant NCR",
                    description="Other tenant",
                    source=NCRSource.IN_PROCESS,
                    company_id=COMPANY_B,
                )
            )
        else:
            db_session.add(
                CorrectiveActionRequest(
                    car_number=f"CAR-{today}-001",
                    title="Other tenant CAR",
                    problem_description="Other tenant",
                    company_id=COMPANY_B,
                )
            )
        db_session.commit()
        complaint = make_complaint(db_session, company_id=COMPANY_A)

        response = client.post(f"{API}/complaints/{complaint.id}/{verb}", headers=headers_for(quality_a))

        assert response.status_code == 200, response.text
        if verb == "create-ncr":
            ncr = db_session.get(NonConformanceReport, response.json()["ncr_id"])
            assert (ncr.company_id, ncr.ncr_number) == (COMPANY_A, f"NCR-{today}-001")
        else:
            car = db_session.get(CorrectiveActionRequest, response.json()["car_id"])
            assert (car.company_id, car.car_number) == (COMPANY_A, f"CAR-{today}-001")
