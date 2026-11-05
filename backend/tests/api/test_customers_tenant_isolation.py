"""Two-company coverage for ``/api/v1/customers`` (invariants 1 and 2, plus the write gate).

What this file pins, each against the defect it closes:

* ``GET /customers/{id}/stats`` reads parts and work orders by the free-text
  ``customer_name``. Names are unique only PER COMPANY, so before the fix the child reads
  (no ``company_id`` predicate) returned another tenant's part numbers, work-order numbers
  and customer POs whenever two companies used the same customer name. The stats test
  seeds the SAME name in both companies, so it fails against the unscoped reads.
* ``DELETE /customers/{id}?hard_delete=true`` counted dependent parts across every tenant:
  a customer with no parts of its own could not be hard-deleted because ANOTHER company
  had parts filed under the same name -- and the 400 disclosed how many.
* ``customers.name`` / ``customers.code`` were globally unique (migration 115 makes them
  per-company). A second tenant creating "Acme" -- or getting the generated code
  ``ACM001`` -- hit the other tenant's row.
* Every wrong-tenant call is a 404 with no row delta and no audit delta.
* ``POST`` / ``PUT`` are Admin / Manager only and ``POST`` writes a committed CREATE row.

AuditLog rows are produced only by the endpoints and read back here (tamper-evident
chain -- never inserted directly).
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.customer import Customer
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
SHARED_NAME = "Acme Aerospace"

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True))
        db.commit()


def _make_user(db: Session, *, company_id: int, role: UserRole = UserRole.ADMIN) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"cust-tenant-{n}@co{company_id}.test",
        employee_id=f"CUSTTEN-{n:05d}",
        first_name=role.value.title(),
        last_name=f"C{company_id}",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",  # tokens are minted directly; never used for login
        role=role,
        is_active=True,
        is_superuser=False,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _headers(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _customer(db: Session, *, company_id: int, name: str = SHARED_NAME, code: str = "ACM001", **extra) -> Customer:
    _ensure_company(db, company_id)
    customer = Customer(company_id=company_id, name=name, code=code, is_active=True, **extra)
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def _part(db: Session, *, company_id: int, part_number: str, customer_name: str, part_type: str = "manufactured"):
    part = Part(
        part_number=part_number,
        name=f"{part_number} body",
        part_type=part_type,
        unit_of_measure="each",
        customer_name=customer_name,
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()
    return part


def _work_order(db: Session, *, company_id: int, number: str, part: Part, status_: WorkOrderStatus, **extra):
    wo = WorkOrder(
        work_order_number=number,
        part_id=part.id,
        quantity_ordered=5,
        status=status_,
        company_id=company_id,
        **extra,
    )
    db.add(wo)
    db.flush()
    return wo


def _counts(db: Session) -> tuple:
    db.expire_all()
    return db.query(Customer).count(), db.query(AuditLog).count()


# ---------------------------------------------------------------------------
# Stats: child reads are tenant-scoped
# ---------------------------------------------------------------------------


class TestCustomerStatsTenantScope:
    def test_stats_never_include_another_companys_parts_or_work_orders(self, client: TestClient, db_session: Session):
        user_a = _make_user(db_session, company_id=COMPANY_A)
        mine = _customer(db_session, company_id=COMPANY_A)
        _customer(db_session, company_id=COMPANY_B)  # same name AND code, other tenant

        my_part = _part(db_session, company_id=COMPANY_A, part_number="A-PRT-1", customer_name=SHARED_NAME)
        my_wo = _work_order(
            db_session, company_id=COMPANY_A, number="WO-A-1", part=my_part, status_=WorkOrderStatus.RELEASED
        )

        their_part = _part(db_session, company_id=COMPANY_B, part_number="B-SECRET-PRT", customer_name=SHARED_NAME)
        _work_order(
            db_session,
            company_id=COMPANY_B,
            number="WO-B-SECRET",
            part=their_part,
            status_=WorkOrderStatus.RELEASED,
            customer_po="PO-B-SECRET",
        )
        # Matched by customer_name alone (the OR branch), still the other tenant's.
        _work_order(
            db_session,
            company_id=COMPANY_B,
            number="WO-B-BYNAME",
            part=their_part,
            status_=WorkOrderStatus.COMPLETE,
            customer_name=SHARED_NAME,
        )
        db_session.commit()

        resp = client.get(f"/api/v1/customers/{mine.id}/stats", headers=_headers(user_a))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        data = resp.json()

        assert data["part_count"] == 1
        assert [p["part_number"] for p in data["parts"]] == ["A-PRT-1"]
        assert data["assemblies"] == []
        assert data["work_order_counts"] == {"total": 1, "by_status": {"released": 1}}
        assert [wo["id"] for wo in data["current_work_orders"]] == [my_wo.id]
        assert data["past_work_orders"] == []
        assert "SECRET" not in resp.text
        assert "WO-B-BYNAME" not in resp.text

    def test_stats_for_another_companys_customer_is_404(self, client: TestClient, db_session: Session):
        user_a = _make_user(db_session, company_id=COMPANY_A)
        theirs = _customer(db_session, company_id=COMPANY_B)
        resp = client.get(f"/api/v1/customers/{theirs.id}/stats", headers=_headers(user_a))
        assert resp.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# Hard delete: the dependency count is tenant-scoped
# ---------------------------------------------------------------------------


class TestHardDeleteDependencyCountTenantScope:
    def test_other_companys_parts_do_not_block_a_hard_delete(self, client: TestClient, db_session: Session):
        admin_a = _make_user(db_session, company_id=COMPANY_A)
        mine = _customer(db_session, company_id=COMPANY_A)
        _customer(db_session, company_id=COMPANY_B)
        _part(db_session, company_id=COMPANY_B, part_number="B-PRT-1", customer_name=SHARED_NAME)
        db_session.commit()
        mine_id = mine.id

        resp = client.delete(f"/api/v1/customers/{mine_id}", params={"hard_delete": True}, headers=_headers(admin_a))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        db_session.expire_all()
        assert db_session.query(Customer).filter(Customer.id == mine_id).first() is None

        deleted = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "customer", AuditLog.resource_id == mine_id, AuditLog.action == "DELETE")
            .one()
        )
        # The physically-removed row is preserved in the audit chain.
        assert deleted.old_values["name"] == SHARED_NAME
        assert deleted.company_id == COMPANY_A

    def test_own_parts_still_block_a_hard_delete(self, client: TestClient, db_session: Session):
        admin_a = _make_user(db_session, company_id=COMPANY_A)
        mine = _customer(db_session, company_id=COMPANY_A)
        _part(db_session, company_id=COMPANY_A, part_number="A-PRT-1", customer_name=SHARED_NAME)
        # Other tenant's parts must not inflate the number this tenant is told.
        _part(db_session, company_id=COMPANY_B, part_number="B-PRT-1", customer_name=SHARED_NAME)
        _part(db_session, company_id=COMPANY_B, part_number="B-PRT-2", customer_name=SHARED_NAME)
        db_session.commit()

        before = _counts(db_session)
        resp = client.delete(f"/api/v1/customers/{mine.id}", params={"hard_delete": True}, headers=_headers(admin_a))
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.json()["detail"] == "Cannot hard delete: Customer has 1 associated parts"
        assert _counts(db_session) == before


# ---------------------------------------------------------------------------
# Wrong tenant: 404, no row delta, no audit delta
# ---------------------------------------------------------------------------


class TestWrongTenantCustomerAccess:
    @pytest.mark.parametrize(
        "method, suffix, kwargs",
        [
            ("get", "", {}),
            ("get", "/stats", {}),
            ("put", "", {"json": {"phone": "555-0199"}}),
            ("delete", "", {}),
            ("delete", "", {"params": {"hard_delete": True}}),
            ("post", "/restore", {}),
        ],
    )
    def test_every_by_id_verb_is_404_with_no_side_effect(
        self, client: TestClient, db_session: Session, method: str, suffix: str, kwargs: dict
    ):
        admin_a = _make_user(db_session, company_id=COMPANY_A)
        theirs = _customer(db_session, company_id=COMPANY_B, phone="555-0100")
        if suffix == "/restore":
            theirs.is_deleted = True
            theirs.is_active = False
            db_session.commit()

        before = _counts(db_session)
        resp = getattr(client, method)(f"/api/v1/customers/{theirs.id}{suffix}", headers=_headers(admin_a), **kwargs)
        assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
        assert _counts(db_session) == before

        db_session.expire_all()
        row = db_session.query(Customer).filter(Customer.id == theirs.id).one()
        assert row.phone == "555-0100"
        assert row.is_deleted is (suffix == "/restore")


# ---------------------------------------------------------------------------
# Per-company uniqueness (migration 115) and per-company code generation
# ---------------------------------------------------------------------------


class TestCustomerUniquenessIsPerCompany:
    def test_second_company_can_create_the_same_name_and_code(self, client: TestClient, db_session: Session):
        _customer(db_session, company_id=COMPANY_A, name=SHARED_NAME, code="ACME-1")
        admin_b = _make_user(db_session, company_id=COMPANY_B)

        resp = client.post(
            "/api/v1/customers/", headers=_headers(admin_b), json={"name": SHARED_NAME, "code": "ACME-1"}
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["code"] == "ACME-1"

        db_session.expire_all()
        rows = db_session.query(Customer).filter(Customer.name == SHARED_NAME).order_by(Customer.company_id).all()
        assert [(r.company_id, r.code) for r in rows] == [(COMPANY_A, "ACME-1"), (COMPANY_B, "ACME-1")]

    def test_generated_code_is_per_company(self, client: TestClient, db_session: Session):
        _customer(db_session, company_id=COMPANY_A, name=SHARED_NAME, code="ACM001")
        admin_b = _make_user(db_session, company_id=COMPANY_B)

        # Before the fix this was a 500: B's per-company count said ACM001, the global
        # unique index said A already had it.
        resp = client.post("/api/v1/customers/", headers=_headers(admin_b), json={"name": SHARED_NAME})
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["code"] == "ACM001"

    def test_generated_code_steps_past_a_hand_keyed_code(self, client: TestClient, db_session: Session):
        admin_a = _make_user(db_session, company_id=COMPANY_A)
        _customer(db_session, company_id=COMPANY_A, name="Acme One", code="ACM002")

        resp = client.post("/api/v1/customers/", headers=_headers(admin_a), json={"name": "Acme Two"})
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["code"] == "ACM003"

    def test_duplicates_within_one_company_are_still_refused(self, client: TestClient, db_session: Session):
        admin_a = _make_user(db_session, company_id=COMPANY_A)
        existing = _customer(db_session, company_id=COMPANY_A, name=SHARED_NAME, code="ACME-1")
        existing.soft_delete(admin_a.id)  # a tombstone still owns its name and code
        db_session.commit()

        before = _counts(db_session)
        same_name = client.post("/api/v1/customers/", headers=_headers(admin_a), json={"name": SHARED_NAME})
        assert same_name.status_code == status.HTTP_400_BAD_REQUEST
        assert same_name.json()["detail"] == "Customer name already exists"

        same_code = client.post(
            "/api/v1/customers/", headers=_headers(admin_a), json={"name": "Different Co", "code": "ACME-1"}
        )
        assert same_code.status_code == status.HTTP_400_BAD_REQUEST
        assert same_code.json()["detail"] == "Customer code already exists"
        assert _counts(db_session) == before

    def test_rename_onto_another_companys_name_is_allowed(self, client: TestClient, db_session: Session):
        _customer(db_session, company_id=COMPANY_B, name=SHARED_NAME, code="B1")
        admin_a = _make_user(db_session, company_id=COMPANY_A)
        mine = _customer(db_session, company_id=COMPANY_A, name="Old Name", code="A1")

        resp = client.put(f"/api/v1/customers/{mine.id}", headers=_headers(admin_a), json={"name": SHARED_NAME})
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["name"] == SHARED_NAME


# ---------------------------------------------------------------------------
# Create is audited; create/update are Admin / Manager only
# ---------------------------------------------------------------------------


class TestCustomerWriteAuditAndRoles:
    def test_create_writes_a_committed_create_audit_row(self, client: TestClient, db_session: Session):
        manager_a = _make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
        resp = client.post(
            "/api/v1/customers/", headers=_headers(manager_a), json={"name": "Audited Co", "requires_fai": True}
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        new_id = resp.json()["id"]

        db_session.rollback()  # only a COMMITTED audit row survives this
        db_session.expire_all()
        rows = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "customer", AuditLog.resource_id == new_id, AuditLog.action == "CREATE")
            .all()
        )
        assert len(rows) == 1
        assert rows[0].company_id == COMPANY_A
        assert rows[0].user_id == manager_a.id
        assert rows[0].new_values["name"] == "Audited Co"
        assert rows[0].new_values["code"] == "AUD001"
        assert rows[0].new_values["requires_fai"] is True

    @pytest.mark.parametrize(
        "role", [UserRole.SUPERVISOR, UserRole.OPERATOR, UserRole.QUALITY, UserRole.SHIPPING, UserRole.VIEWER]
    )
    def test_non_office_roles_cannot_create_or_update(self, client: TestClient, db_session: Session, role: UserRole):
        user = _make_user(db_session, company_id=COMPANY_A, role=role)
        existing = _customer(db_session, company_id=COMPANY_A, name="Gated Co", code="GAT001", phone="111")

        before = _counts(db_session)
        created = client.post("/api/v1/customers/", headers=_headers(user), json={"name": "Not Allowed Co"})
        assert created.status_code == status.HTTP_403_FORBIDDEN
        updated = client.put(f"/api/v1/customers/{existing.id}", headers=_headers(user), json={"phone": "222"})
        assert updated.status_code == status.HTTP_403_FORBIDDEN
        assert _counts(db_session) == before

        db_session.expire_all()
        assert db_session.query(Customer).filter(Customer.id == existing.id).one().phone == "111"

    def test_reads_stay_open_to_every_role(self, client: TestClient, db_session: Session):
        operator = _make_user(db_session, company_id=COMPANY_A, role=UserRole.OPERATOR)
        mine = _customer(db_session, company_id=COMPANY_A)
        headers = _headers(operator)
        assert client.get("/api/v1/customers/", headers=headers).status_code == status.HTTP_200_OK
        assert client.get("/api/v1/customers/names", headers=headers).status_code == status.HTTP_200_OK
        assert client.get(f"/api/v1/customers/{mine.id}", headers=headers).status_code == status.HTTP_200_OK
        assert client.get(f"/api/v1/customers/{mine.id}/stats", headers=headers).status_code == status.HTTP_200_OK
