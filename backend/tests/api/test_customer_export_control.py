"""Customer export-control endpoints (Claude-plugin brief P0.3; owner decision 2026-11-13).

``GET /customers/{id}/export-control``, ``POST /customers/{id}/contracts`` and
``PUT /customers/{id}/contracts/{contract_id}`` are the only writer of
``customer_contracts``. Pinned: ADMIN only; never through an API token (403, nothing
written), whatever the token's role; a required ``reason`` on every write, on a REQUIRED
audit row (plus a customer STATUS_CHANGE row when control flips) -- an audit failure is 503
with nothing saved; tenant-scoped (404 across companies); a soft-deleted customer can still
be read and marked; no delete -- un-marking is a PUT; and the mark takes effect for an
AI-external token on its very next request.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.endpoints.customers import EXPORT_CONTROL_API_TOKEN_DETAIL, EXPORT_CONTROL_AUDIT_UNAVAILABLE_DETAIL
from app.core.export_control import REDACTED_VALUE
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.governance import CustomerContract
from app.models.user import User, UserRole
from app.services.audit_service import AuditService
from tests.api.export_control_helpers import (
    bearer,
    contains_canary,
    issue_token,
    make_viewer,
    seed_world,
)
from tests.api.kiosk_test_helpers import ensure_company, make_user, user_headers

pytestmark = pytest.mark.api

API = "/api/v1/customers"


@pytest.fixture
def plain_customer(db_session: Session) -> Customer:
    row = Customer(name="Northwind Aero", code="NWA", company_id=1)
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _mark(client: TestClient, headers: dict, customer_id: int, **body):
    payload = {"contract_name": "Program X", "itar_controlled": True, "reason": "DD254 received", **body}
    return client.post(f"{API}/{customer_id}/contracts", json=payload, headers=headers)


def _audit_rows(db: Session, resource_type: str, resource_id: int):
    db.expire_all()
    return (
        db.query(AuditLog)
        .filter(AuditLog.resource_type == resource_type, AuditLog.resource_id == resource_id)
        .order_by(AuditLog.id)
        .all()
    )


class TestRead:
    def test_an_admin_reads_the_derived_standing(self, client: TestClient, admin_headers: dict, plain_customer):
        before = client.get(f"{API}/{plain_customer.id}/export-control", headers=admin_headers)
        assert before.status_code == 200, before.text
        assert before.json() == {
            "customer_id": plain_customer.id,
            "customer_name": "Northwind Aero",
            "customer_is_deleted": False,
            "controlled": False,
            "data_classification": None,
            "controlling_contract_id": None,
            "contracts": [],
        }
        created = _mark(client, admin_headers, plain_customer.id)
        assert created.status_code == 201, created.text
        after = client.get(f"{API}/{plain_customer.id}/export-control", headers=admin_headers).json()
        assert after["controlled"] is True
        assert after["data_classification"] == "itar_export_controlled"
        assert after["controlling_contract_id"] == after["contracts"][0]["id"]
        assert after["contracts"][0]["controlling"] is True
        assert after["contracts"][0]["created_at"].endswith("Z")

    def test_another_companys_customer_is_404(self, client: TestClient, db_session: Session, admin_headers: dict):
        ensure_company(db_session, 2)
        foreign = Customer(name="Foreign", code="FRN", company_id=2)
        db_session.add(foreign)
        db_session.commit()
        assert client.get(f"{API}/{foreign.id}/export-control", headers=admin_headers).status_code == 404
        assert _mark(client, admin_headers, foreign.id).status_code == 404
        assert db_session.query(CustomerContract).count() == 0


class TestWho:
    @pytest.mark.parametrize(
        "role", [UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.QUALITY, UserRole.OPERATOR, UserRole.VIEWER]
    )
    def test_only_an_admin(self, client: TestClient, db_session: Session, plain_customer, role):
        headers = user_headers(make_user(db_session, company_id=1, role=role))
        assert client.get(f"{API}/{plain_customer.id}/export-control", headers=headers).status_code == 403
        assert _mark(client, headers, plain_customer.id).status_code == 403
        assert db_session.query(CustomerContract).count() == 0

    @pytest.mark.parametrize("scope", ["full", "read_only"])
    def test_never_through_an_api_token(
        self, client: TestClient, db_session: Session, admin_headers: dict, admin_user: User, plain_customer, scope
    ):
        """Even an ADMIN's full token: marking a customer is a person's judgment in the app."""
        extra = {"scope": scope} if scope == "full" else {"scope": scope, "ai_external": False}
        _id, token = issue_token(client, admin_headers, admin_user, **extra)
        audit_before = db_session.query(AuditLog).count()

        read = client.get(f"{API}/{plain_customer.id}/export-control", headers=bearer(token))
        assert read.status_code == 403
        if scope == "full":
            assert read.json()["detail"] == EXPORT_CONTROL_API_TOKEN_DETAIL
            created = _mark(client, bearer(token), plain_customer.id)
            assert created.status_code == 403 and created.json()["detail"] == EXPORT_CONTROL_API_TOKEN_DETAIL
        else:
            assert _mark(client, bearer(token), plain_customer.id).status_code == 403
        db_session.expire_all()
        assert db_session.query(CustomerContract).count() == 0
        assert db_session.query(AuditLog).count() == audit_before


class TestWhoMayEdit:
    """The PUT twin of ``TestWho``: un-marking a customer is the same judgment as marking it."""

    @pytest.fixture
    def contract_id(self, client: TestClient, admin_headers: dict, plain_customer) -> int:
        created = _mark(client, admin_headers, plain_customer.id)
        assert created.status_code == 201, created.text
        return created.json()["contracts"][0]["id"]

    def _unmark(self, client: TestClient, headers: dict, customer_id: int, contract_id: int):
        return client.put(
            f"{API}/{customer_id}/contracts/{contract_id}",
            json={"itar_controlled": False, "reason": "declassified"},
            headers=headers,
        )

    def _still_marked(self, db: Session, contract_id: int, audit_before: int) -> None:
        db.expire_all()
        assert db.get(CustomerContract, contract_id).itar_controlled is True
        assert db.query(AuditLog).count() == audit_before

    @pytest.mark.parametrize(
        "role", [UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.QUALITY, UserRole.OPERATOR, UserRole.VIEWER]
    )
    def test_only_an_admin_may_unmark(self, client: TestClient, db_session: Session, plain_customer, contract_id, role):
        audit_before = db_session.query(AuditLog).count()
        headers = user_headers(make_user(db_session, company_id=1, role=role))
        assert self._unmark(client, headers, plain_customer.id, contract_id).status_code == 403
        self._still_marked(db_session, contract_id, audit_before)

    def test_never_through_an_admins_full_api_token(
        self,
        client: TestClient,
        db_session: Session,
        admin_headers: dict,
        admin_user: User,
        plain_customer,
        contract_id,
    ):
        _id, token = issue_token(client, admin_headers, admin_user)
        audit_before = db_session.query(AuditLog).count()
        response = self._unmark(client, bearer(token), plain_customer.id, contract_id)
        assert response.status_code == 403
        assert response.json()["detail"] == EXPORT_CONTROL_API_TOKEN_DETAIL
        self._still_marked(db_session, contract_id, audit_before)

    def test_another_companys_admin_gets_404(
        self, client: TestClient, db_session: Session, plain_customer, contract_id
    ):
        ensure_company(db_session, 2)
        foreign_admin = make_user(db_session, company_id=2, role=UserRole.ADMIN)
        audit_before = db_session.query(AuditLog).count()
        headers = user_headers(foreign_admin)
        assert self._unmark(client, headers, plain_customer.id, contract_id).status_code == 404
        assert client.get(f"{API}/{plain_customer.id}/export-control", headers=headers).status_code == 404
        self._still_marked(db_session, contract_id, audit_before)

    def test_an_audit_failure_on_unmark_saves_nothing(
        self, client: TestClient, db_session: Session, admin_headers: dict, plain_customer, contract_id, monkeypatch
    ):
        audit_before = db_session.query(AuditLog).count()
        monkeypatch.setattr(AuditService, "log", lambda self, *args, **kwargs: None)
        response = self._unmark(client, admin_headers, plain_customer.id, contract_id)
        assert response.status_code == 503
        assert response.json()["detail"] == EXPORT_CONTROL_AUDIT_UNAVAILABLE_DETAIL
        self._still_marked(db_session, contract_id, audit_before)


class TestWrites:
    def test_marking_is_audited_with_the_reason(
        self, client: TestClient, db_session: Session, admin_headers: dict, admin_user: User, plain_customer
    ):
        response = _mark(client, admin_headers, plain_customer.id, contract_number="DD-254-01")
        assert response.status_code == 201, response.text
        contract = db_session.query(CustomerContract).one()
        assert (contract.company_id, contract.customer_id, contract.itar_controlled) == (1, plain_customer.id, True)
        assert contract.created_by == admin_user.id

        create = _audit_rows(db_session, "customer_contract", contract.id)
        assert [row.action for row in create] == ["CREATE"]
        assert create[0].extra_data["reason"] == "DD254 received"
        assert create[0].extra_data["customer_controlled_before"] is False
        assert create[0].extra_data["customer_controlled_after"] is True
        assert create[0].new_values["itar_controlled"] is True
        assert create[0].user_id == admin_user.id

        status = [
            row for row in _audit_rows(db_session, "customer", plain_customer.id) if row.action == "STATUS_CHANGE"
        ]
        assert len(status) == 1
        assert status[0].old_values == {"export_control": None}
        assert status[0].new_values == {"export_control": "itar_export_controlled"}

    def test_unmarking_is_a_put_with_a_reason(
        self, client: TestClient, db_session: Session, admin_headers: dict, plain_customer
    ):
        contract_id = _mark(client, admin_headers, plain_customer.id).json()["contracts"][0]["id"]
        response = client.put(
            f"{API}/{plain_customer.id}/contracts/{contract_id}",
            json={"itar_controlled": False, "reason": "contract closed out; program declassified by the customer"},
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["controlled"] is False
        rows = _audit_rows(db_session, "customer_contract", contract_id)
        assert [row.action for row in rows] == ["CREATE", "UPDATE"]
        assert rows[1].old_values == {"itar_controlled": True} and rows[1].new_values == {"itar_controlled": False}
        assert rows[1].extra_data["reason"].startswith("contract closed out")
        flips = [row for row in _audit_rows(db_session, "customer", plain_customer.id) if row.action == "STATUS_CHANGE"]
        assert [row.new_values for row in flips] == [
            {"export_control": "itar_export_controlled"},
            {"export_control": None},
        ]

    def test_a_put_that_changes_nothing_writes_nothing(
        self, client: TestClient, db_session: Session, admin_headers: dict, plain_customer
    ):
        contract_id = _mark(client, admin_headers, plain_customer.id).json()["contracts"][0]["id"]
        before = db_session.query(AuditLog).count()
        response = client.put(
            f"{API}/{plain_customer.id}/contracts/{contract_id}",
            json={"itar_controlled": True, "reason": "no-op"},
            headers=admin_headers,
        )
        assert response.status_code == 200
        db_session.expire_all()
        assert db_session.query(AuditLog).count() == before

    @pytest.mark.parametrize("reason", [None, "", "   "])
    def test_a_reason_is_required(
        self, client: TestClient, db_session: Session, admin_headers: dict, plain_customer, reason
    ):
        body = {"contract_name": "Program X", "itar_controlled": True}
        if reason is not None:
            body["reason"] = reason
        assert client.post(f"{API}/{plain_customer.id}/contracts", json=body, headers=admin_headers).status_code == 422
        assert db_session.query(CustomerContract).count() == 0

    def test_validation(self, client: TestClient, db_session: Session, admin_headers: dict, plain_customer):
        assert (
            _mark(client, admin_headers, plain_customer.id, default_data_classification="top_secret").status_code == 422
        )
        assert _mark(client, admin_headers, plain_customer.id, surprise=True).status_code == 422
        contract_id = _mark(client, admin_headers, plain_customer.id).json()["contracts"][0]["id"]
        null_flag = client.put(
            f"{API}/{plain_customer.id}/contracts/{contract_id}",
            json={"itar_controlled": None, "reason": "x"},
            headers=admin_headers,
        )
        assert null_flag.status_code == 422

    def test_a_null_contract_name_is_422_not_a_misleading_409(
        self, client: TestClient, db_session: Session, admin_headers: dict, plain_customer
    ):
        """Code review / compliance review P0, finding 6/8: an explicit null on the NOT NULL
        column used to reach the flush and come back as 409 "A contract with that number
        already exists"."""
        contract_id = _mark(client, admin_headers, plain_customer.id).json()["contracts"][0]["id"]
        response = client.put(
            f"{API}/{plain_customer.id}/contracts/{contract_id}",
            json={"contract_name": None, "reason": "r"},
            headers=admin_headers,
        )
        assert response.status_code == 422, response.text
        assert "already exists" not in response.text
        db_session.expire_all()
        assert db_session.get(CustomerContract, contract_id).contract_name == "Program X"
        # Omitting the field still leaves it alone.
        kept = client.put(
            f"{API}/{plain_customer.id}/contracts/{contract_id}",
            json={"active": False, "reason": "r"},
            headers=admin_headers,
        )
        assert kept.status_code == 200 and kept.json()["contracts"][0]["contract_name"] == "Program X"

    def test_a_duplicate_contract_number_is_409(
        self, client: TestClient, db_session: Session, admin_headers: dict, plain_customer
    ):
        assert _mark(client, admin_headers, plain_customer.id, contract_number="N-1").status_code == 201
        duplicate = _mark(client, admin_headers, plain_customer.id, contract_number="N-1")
        assert duplicate.status_code == 409
        db_session.expire_all()
        assert db_session.query(CustomerContract).count() == 1

    def test_a_contract_of_another_customer_is_404(
        self, client: TestClient, db_session: Session, admin_headers: dict, plain_customer
    ):
        other = Customer(name="Other", code="OTH", company_id=1)
        db_session.add(other)
        db_session.commit()
        contract_id = _mark(client, admin_headers, plain_customer.id).json()["contracts"][0]["id"]
        response = client.put(
            f"{API}/{other.id}/contracts/{contract_id}",
            json={"itar_controlled": False, "reason": "x"},
            headers=admin_headers,
        )
        assert response.status_code == 404
        db_session.expire_all()
        assert db_session.get(CustomerContract, contract_id).itar_controlled is True

    def test_an_audit_failure_saves_nothing(
        self, client: TestClient, db_session: Session, admin_headers: dict, plain_customer, monkeypatch
    ):
        monkeypatch.setattr(AuditService, "log", lambda self, *args, **kwargs: None)
        response = _mark(client, admin_headers, plain_customer.id)
        assert response.status_code == 503
        assert response.json()["detail"] == EXPORT_CONTROL_AUDIT_UNAVAILABLE_DETAIL
        db_session.expire_all()
        assert db_session.query(CustomerContract).count() == 0

    def test_a_soft_deleted_customer_can_still_be_marked(
        self, client: TestClient, db_session: Session, admin_headers: dict, plain_customer, admin_user: User
    ):
        """Its historical records stay controlled; an Admin must be able to say so."""
        plain_customer.soft_delete(admin_user.id)
        db_session.commit()
        response = _mark(client, admin_headers, plain_customer.id)
        assert response.status_code == 201
        assert response.json()["customer_is_deleted"] is True and response.json()["controlled"] is True


class TestEffect:
    def test_marking_takes_effect_for_an_ai_external_token_on_the_next_request(
        self, client: TestClient, db_session: Session, admin_headers: dict
    ):
        world = seed_world(db_session, controlled=False)
        viewer = make_viewer(db_session)
        _id, token = issue_token(client, admin_headers, viewer, scope="read_only")
        url = f"/api/v1/work-orders/{world.itar_wo.id}"
        assert contains_canary(client.get(url, headers=bearer(token)).content), "not controlled yet"

        marked = _mark(client, admin_headers, world.itar_customer.id, contains_cui=True, itar_controlled=False)
        assert marked.status_code == 201 and marked.json()["data_classification"] == "cui"
        body = client.get(url, headers=bearer(token))
        assert not contains_canary(body.content)
        assert body.json()["notes"] == REDACTED_VALUE

        contract_id = marked.json()["controlling_contract_id"]
        cleared = client.put(
            f"{API}/{world.itar_customer.id}/contracts/{contract_id}",
            json={"contains_cui": False, "reason": "flag entered in error"},
            headers=admin_headers,
        )
        assert cleared.status_code == 200 and cleared.json()["controlled"] is False
        # Only the seed's own (uncontrolled) contract and this one are left; neither controls.
        assert contains_canary(client.get(url, headers=bearer(token)).content)


def test_the_routes_are_tenant_scoped_through_the_dependency():
    """``get_current_company_id`` is in every route's dependency tree (invariant 1)."""
    from fastapi.routing import APIRoute

    from app.api.deps import get_current_company_id
    from app.main import app

    targets = {
        ("GET", "/api/v1/customers/{customer_id}/export-control"),
        ("POST", "/api/v1/customers/{customer_id}/contracts"),
        ("PUT", "/api/v1/customers/{customer_id}/contracts/{contract_id}"),
    }
    found = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods:
            if (method, route.path_format) in targets:
                stack, calls = [route.dependant], []
                while stack:
                    current = stack.pop()
                    calls.append(current.call)
                    stack.extend(current.dependencies)
                assert get_current_company_id in calls
                found.add((method, route.path_format))
    assert found == targets
