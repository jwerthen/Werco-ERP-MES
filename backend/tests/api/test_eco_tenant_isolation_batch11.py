"""Tenant-isolation, RBAC, and audit coverage for the ECO router (Batch 11A / G4-Fix1).

``app/api/endpoints/engineering_changes.py`` previously loaded ECOs (and their
child approvals/tasks) by id ALONE -- ``get_eco_or_404`` filtered only on
``EngineeringChangeOrder.id``. Every by-id endpoint (get / update / the status
transitions / affected-items / approvals / task update) was therefore
cross-tenant readable and mutable: a company-A user could read or drive a
company-B ECO just by guessing its integer id. The dashboard aggregates were
also unscoped (every tenant's ECOs counted together), ``get_affected_items``
resolved part/WO/document ids across ALL tenants (and surfaced soft-deleted
rows), and the affected-id lists accepted foreign ids on create/update. The
mutating endpoints also ran with no role gate and wrote no audit trail.

The G4-Fix1 contracts these tests lock:

- by-id endpoints scoped to the active company -> 404 cross-tenant, no mutation:
    GET  /eco/eco/{id}              PUT /eco/eco/{id}
    POST /eco/eco/{id}/submit       POST /eco/eco/{id}/approve  ... (transitions)
    GET  /eco/affected-items/{id}   GET /eco/eco/{id}/approvals
    PUT  /eco/eco/{eco_id}/tasks/{task_id}
- GET /eco/eco/dashboard counts ONLY the caller's company.
- get_affected_items resolves only same-company, non-soft-deleted Part/WO/Document.
- _validate_affected_ids_in_company: a foreign / nonexistent affected id -> 422.
- RBAC: a non-ADMIN/MANAGER (OPERATOR) on a mutating endpoint -> 403.
- audit: an ECO state transition writes a committed audit_log row for the resource.

NOTE on paths: the router is mounted at prefix ``/eco`` and every route inside
is ``/eco/...`` -- so the full paths are ``/api/v1/eco/eco/{id}`` etc. The
``affected-items`` route is ``/api/v1/eco/eco/affected-items/{id}``.

Rows for both companies are created directly in the shared ``db_session``
(tests/conftest.py); requests use a directly-minted token; the cross-tenant
invariant is asserted as 404 + the foreign rows surviving untouched.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.document import Document, DocumentType
from app.models.engineering_change import (
    ECOImplementationTask,
    ECOPriority,
    ECOStatus,
    ECOType,
    EngineeringChangeOrder,
)
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

API = "/api/v1/eco/eco"  # router prefix /eco + in-router /eco/...
COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens minted directly; never used for login
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True)
        db.add(company)
        db.commit()
    return company


def make_user(db: Session, *, company_id: int, role: UserRole = UserRole.MANAGER) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"eco-{n}@co{company_id}.test",
        employee_id=f"ECO-{n:05d}",
        first_name="Eco",
        last_name=f"C{company_id}",
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=True,
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


def make_eco(
    db: Session,
    *,
    company_id: int,
    requested_by: int,
    status_: ECOStatus = ECOStatus.DRAFT,
    eco_type: ECOType = ECOType.DESIGN_CHANGE,
    priority: ECOPriority = ECOPriority.MEDIUM,
    completed_date: date = None,
    created_at: datetime = None,
    affected_parts: str = None,
    affected_work_orders: str = None,
    affected_documents: str = None,
) -> EngineeringChangeOrder:
    _ensure_company(db, company_id)
    n = _next()
    eco = EngineeringChangeOrder(
        eco_number=f"ECO-T-{company_id}-{n:05d}",
        title=f"ECO {n}",
        description="eco-isolation fixture",
        eco_type=eco_type,
        priority=priority,
        status=status_,
        reason_for_change="because",
        requested_by=requested_by,
        completed_date=completed_date,
        affected_parts=affected_parts,
        affected_work_orders=affected_work_orders,
        affected_documents=affected_documents,
        company_id=company_id,
    )
    if created_at is not None:
        eco.created_at = created_at
    db.add(eco)
    db.commit()
    db.refresh(eco)
    return eco


def make_task(db: Session, *, eco: EngineeringChangeOrder, company_id: int) -> ECOImplementationTask:
    task = ECOImplementationTask(
        eco_id=eco.id,
        task_number=1,
        description="fixture task",
        status="pending",
        company_id=company_id,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def make_part(db: Session, *, company_id: int, is_deleted: bool = False) -> Part:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"ECO-P-{n}",
        name=f"Part {n}",
        description="eco affected fixture part",
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


def make_work_order(db: Session, *, company_id: int, is_deleted: bool = False) -> WorkOrder:
    part = make_part(db, company_id=company_id)
    n = _next()
    wo = WorkOrder(
        work_order_number=f"ECO-WO-{n:05d}",
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


def make_document(db: Session, *, company_id: int) -> Document:
    _ensure_company(db, company_id)
    n = _next()
    doc = Document(
        document_number=f"ECO-DOC-{n}",
        title=f"Doc {n}",
        document_type=DocumentType.DRAWING,
        company_id=company_id,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def _reload(db: Session, model, pk: int):
    db.expire_all()
    return db.get(model, pk)


def _committed_audit_rows(db: Session, *, resource_type: str, resource_id: int):
    """Audit rows that actually COMMITTED (rollback discards flush-only rows)."""
    db.rollback()
    db.expire_all()
    return db.query(AuditLog).filter(AuditLog.resource_type == resource_type, AuditLog.resource_id == resource_id).all()


# ===========================================================================
# Cross-tenant by-id endpoints -> 404, no mutation.
# ===========================================================================


def test_get_foreign_eco_is_404(client: TestClient, db_session: Session):
    a_user = make_user(db_session, company_id=COMPANY_A)
    b_user = make_user(db_session, company_id=COMPANY_B)
    eco_b = make_eco(db_session, company_id=COMPANY_B, requested_by=b_user.id)

    resp = client.get(f"{API}/{eco_b.id}", headers=headers_for(a_user))
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text


def test_update_foreign_eco_is_404_and_no_mutation(client: TestClient, db_session: Session):
    a_user = make_user(db_session, company_id=COMPANY_A)
    b_user = make_user(db_session, company_id=COMPANY_B)
    eco_b = make_eco(db_session, company_id=COMPANY_B, requested_by=b_user.id)
    original_title = eco_b.title

    resp = client.put(
        f"{API}/{eco_b.id}",
        headers=headers_for(a_user),
        json={"title": "HIJACKED TITLE"},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text

    eco_after = _reload(db_session, EngineeringChangeOrder, eco_b.id)
    assert eco_after.title == original_title, "foreign ECO title must be unchanged"


def test_submit_foreign_eco_is_404_and_no_mutation(client: TestClient, db_session: Session):
    a_user = make_user(db_session, company_id=COMPANY_A)
    b_user = make_user(db_session, company_id=COMPANY_B)
    eco_b = make_eco(db_session, company_id=COMPANY_B, requested_by=b_user.id, status_=ECOStatus.DRAFT)

    resp = client.post(f"{API}/{eco_b.id}/submit", headers=headers_for(a_user))
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text

    eco_after = _reload(db_session, EngineeringChangeOrder, eco_b.id)
    assert eco_after.status == ECOStatus.DRAFT, "foreign ECO must not be transitioned"


def test_approve_foreign_eco_is_404_and_no_mutation(client: TestClient, db_session: Session):
    a_user = make_user(db_session, company_id=COMPANY_A)
    b_user = make_user(db_session, company_id=COMPANY_B)
    eco_b = make_eco(db_session, company_id=COMPANY_B, requested_by=b_user.id, status_=ECOStatus.SUBMITTED)

    resp = client.post(
        f"{API}/{eco_b.id}/approve",
        headers=headers_for(a_user),
        json={"status": "approved"},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text

    eco_after = _reload(db_session, EngineeringChangeOrder, eco_b.id)
    assert eco_after.status == ECOStatus.SUBMITTED


def test_reject_foreign_eco_is_404_and_no_mutation(client: TestClient, db_session: Session):
    a_user = make_user(db_session, company_id=COMPANY_A)
    b_user = make_user(db_session, company_id=COMPANY_B)
    eco_b = make_eco(db_session, company_id=COMPANY_B, requested_by=b_user.id, status_=ECOStatus.SUBMITTED)

    resp = client.post(
        f"{API}/{eco_b.id}/reject",
        headers=headers_for(a_user),
        json={"status": "rejected", "comments": "no"},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text

    eco_after = _reload(db_session, EngineeringChangeOrder, eco_b.id)
    assert eco_after.status == ECOStatus.SUBMITTED


def test_implement_foreign_eco_is_404(client: TestClient, db_session: Session):
    a_user = make_user(db_session, company_id=COMPANY_A)
    b_user = make_user(db_session, company_id=COMPANY_B)
    eco_b = make_eco(db_session, company_id=COMPANY_B, requested_by=b_user.id, status_=ECOStatus.APPROVED)

    resp = client.post(f"{API}/{eco_b.id}/implement", headers=headers_for(a_user))
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text

    eco_after = _reload(db_session, EngineeringChangeOrder, eco_b.id)
    assert eco_after.status == ECOStatus.APPROVED


def test_affected_items_foreign_eco_is_404(client: TestClient, db_session: Session):
    a_user = make_user(db_session, company_id=COMPANY_A)
    b_user = make_user(db_session, company_id=COMPANY_B)
    eco_b = make_eco(db_session, company_id=COMPANY_B, requested_by=b_user.id)

    resp = client.get(f"{API}/affected-items/{eco_b.id}", headers=headers_for(a_user))
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text


def test_list_approvals_foreign_eco_is_404(client: TestClient, db_session: Session):
    a_user = make_user(db_session, company_id=COMPANY_A)
    b_user = make_user(db_session, company_id=COMPANY_B)
    eco_b = make_eco(db_session, company_id=COMPANY_B, requested_by=b_user.id)

    resp = client.get(f"{API}/{eco_b.id}/approvals", headers=headers_for(a_user))
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text


def test_update_task_on_foreign_eco_is_404_and_no_mutation(client: TestClient, db_session: Session):
    a_user = make_user(db_session, company_id=COMPANY_A)
    b_user = make_user(db_session, company_id=COMPANY_B)
    eco_b = make_eco(db_session, company_id=COMPANY_B, requested_by=b_user.id, status_=ECOStatus.IN_IMPLEMENTATION)
    task_b = make_task(db_session, eco=eco_b, company_id=COMPANY_B)

    resp = client.put(
        f"{API}/{eco_b.id}/tasks/{task_b.id}",
        headers=headers_for(a_user),
        json={"status": "completed"},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text

    task_after = _reload(db_session, ECOImplementationTask, task_b.id)
    assert task_after.status == "pending", "foreign ECO task must not be mutated"


# ===========================================================================
# Dashboard: counts ONLY the caller's company.
# ===========================================================================


def test_dashboard_counts_only_callers_company(client: TestClient, db_session: Session):
    a_user = make_user(db_session, company_id=COMPANY_A)
    b_user = make_user(db_session, company_id=COMPANY_B)

    # Company A: one SUBMITTED (pending_review), one IN_IMPLEMENTATION.
    make_eco(db_session, company_id=COMPANY_A, requested_by=a_user.id, status_=ECOStatus.SUBMITTED)
    make_eco(db_session, company_id=COMPANY_A, requested_by=a_user.id, status_=ECOStatus.IN_IMPLEMENTATION)
    # Company B: three SUBMITTED + two IN_IMPLEMENTATION -- must NOT leak into A's counts.
    for _ in range(3):
        make_eco(db_session, company_id=COMPANY_B, requested_by=b_user.id, status_=ECOStatus.SUBMITTED)
    for _ in range(2):
        make_eco(db_session, company_id=COMPANY_B, requested_by=b_user.id, status_=ECOStatus.IN_IMPLEMENTATION)

    resp = client.get(f"{API}/dashboard", headers=headers_for(a_user))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    data = resp.json()
    assert data["pending_review"] == 1, "only company A's submitted ECO should count"
    assert data["in_implementation"] == 1, "only company A's in-implementation ECO should count"
    assert data["total_active"] == 2, "only company A's active ECOs should count"


def test_dashboard_completed_this_month_only_callers_company(client: TestClient, db_session: Session):
    a_user = make_user(db_session, company_id=COMPANY_A)
    b_user = make_user(db_session, company_id=COMPANY_B)
    today = date.today()

    make_eco(
        db_session,
        company_id=COMPANY_A,
        requested_by=a_user.id,
        status_=ECOStatus.COMPLETED,
        completed_date=today,
        created_at=datetime.utcnow() - timedelta(days=5),
    )
    # Two completed-this-month for company B that must NOT count for A.
    for _ in range(2):
        make_eco(
            db_session,
            company_id=COMPANY_B,
            requested_by=b_user.id,
            status_=ECOStatus.COMPLETED,
            completed_date=today,
            created_at=datetime.utcnow() - timedelta(days=5),
        )

    resp = client.get(f"{API}/dashboard", headers=headers_for(a_user))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["completed_this_month"] == 1


# ===========================================================================
# get_affected_items: same-company, non-soft-deleted only.
# ===========================================================================


def test_affected_items_excludes_cross_tenant_and_soft_deleted(client: TestClient, db_session: Session):
    # Note: this also exercises the G4-Fix1 fix to get_affected_items' work_orders payload,
    # which previously used a nonexistent `w.wo_number` attribute (500); it now resolves
    # same-company, non-deleted WorkOrders via `w.work_order_number`.
    a_user = make_user(db_session, company_id=COMPANY_A)

    own_part = make_part(db_session, company_id=COMPANY_A)
    deleted_part = make_part(db_session, company_id=COMPANY_A, is_deleted=True)
    foreign_part = make_part(db_session, company_id=COMPANY_B)

    own_wo = make_work_order(db_session, company_id=COMPANY_A)
    deleted_wo = make_work_order(db_session, company_id=COMPANY_A, is_deleted=True)
    foreign_wo = make_work_order(db_session, company_id=COMPANY_B)

    own_doc = make_document(db_session, company_id=COMPANY_A)
    foreign_doc = make_document(db_session, company_id=COMPANY_B)

    import json

    eco_a = make_eco(
        db_session,
        company_id=COMPANY_A,
        requested_by=a_user.id,
        affected_parts=json.dumps([own_part.id, deleted_part.id, foreign_part.id]),
        affected_work_orders=json.dumps([own_wo.id, deleted_wo.id, foreign_wo.id]),
        affected_documents=json.dumps([own_doc.id, foreign_doc.id]),
    )

    resp = client.get(f"{API}/affected-items/{eco_a.id}", headers=headers_for(a_user))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    data = resp.json()

    part_ids = {p["id"] for p in data["parts"]}
    assert part_ids == {own_part.id}, "only the same-company, non-deleted part may resolve"

    wo_ids = {w["id"] for w in data["work_orders"]}
    assert wo_ids == {own_wo.id}, "only the same-company, non-deleted WO may resolve"

    doc_ids = {d["id"] for d in data["documents"]}
    assert doc_ids == {own_doc.id}, "only the same-company document may resolve"


# ===========================================================================
# _validate_affected_ids_in_company: foreign/nonexistent affected id -> 422.
# ===========================================================================


def test_create_eco_with_cross_tenant_affected_part_is_422(client: TestClient, db_session: Session):
    a_user = make_user(db_session, company_id=COMPANY_A)
    foreign_part = make_part(db_session, company_id=COMPANY_B)

    resp = client.post(
        f"{API}/",
        headers=headers_for(a_user),
        json={
            "title": "ECO X",
            "description": "valid description here",
            "eco_type": "design_change",
            "reason_for_change": "valid reason here",
            "affected_parts": [foreign_part.id],
        },
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text
    # No ECO leaked for company A.
    assert db_session.query(EngineeringChangeOrder).filter(EngineeringChangeOrder.company_id == COMPANY_A).count() == 0


def test_create_eco_with_nonexistent_affected_work_order_is_422(client: TestClient, db_session: Session):
    a_user = make_user(db_session, company_id=COMPANY_A)

    resp = client.post(
        f"{API}/",
        headers=headers_for(a_user),
        json={
            "title": "ECO Y",
            "description": "valid description here",
            "eco_type": "process_change",
            "reason_for_change": "valid reason here",
            "affected_work_orders": [999999],
        },
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text


def test_update_eco_with_cross_tenant_affected_document_is_422_and_no_mutation(client: TestClient, db_session: Session):
    a_user = make_user(db_session, company_id=COMPANY_A)
    eco_a = make_eco(db_session, company_id=COMPANY_A, requested_by=a_user.id)
    foreign_doc = make_document(db_session, company_id=COMPANY_B)

    resp = client.put(
        f"{API}/{eco_a.id}",
        headers=headers_for(a_user),
        json={"affected_documents": [foreign_doc.id]},
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text

    eco_after = _reload(db_session, EngineeringChangeOrder, eco_a.id)
    assert eco_after.affected_documents is None, "rejected affected-id update must not persist"


def test_create_eco_with_own_company_affected_ids_succeeds(client: TestClient, db_session: Session):
    """Control: same-company, live affected ids are accepted (the validator does not
    over-block legitimate references)."""
    a_user = make_user(db_session, company_id=COMPANY_A)
    own_part = make_part(db_session, company_id=COMPANY_A)
    own_wo = make_work_order(db_session, company_id=COMPANY_A)

    resp = client.post(
        f"{API}/",
        headers=headers_for(a_user),
        json={
            "title": "ECO OK",
            "description": "valid description here",
            "eco_type": "design_change",
            "reason_for_change": "valid reason here",
            "affected_parts": [own_part.id],
            "affected_work_orders": [own_wo.id],
        },
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text


# ===========================================================================
# RBAC: a non-ADMIN/MANAGER (OPERATOR) on a mutating endpoint -> 403.
# ===========================================================================


def test_operator_cannot_create_eco(client: TestClient, db_session: Session):
    operator = make_user(db_session, company_id=COMPANY_A, role=UserRole.OPERATOR)

    resp = client.post(
        f"{API}/",
        headers=headers_for(operator),
        json={
            "title": "ECO Z",
            "description": "valid description here",
            "eco_type": "design_change",
            "reason_for_change": "valid reason here",
        },
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text


def test_operator_cannot_submit_eco(client: TestClient, db_session: Session):
    manager = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    operator = make_user(db_session, company_id=COMPANY_A, role=UserRole.OPERATOR)
    eco = make_eco(db_session, company_id=COMPANY_A, requested_by=manager.id, status_=ECOStatus.DRAFT)

    resp = client.post(f"{API}/{eco.id}/submit", headers=headers_for(operator))
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text

    eco_after = _reload(db_session, EngineeringChangeOrder, eco.id)
    assert eco_after.status == ECOStatus.DRAFT, "RBAC reject must not transition the ECO"


def test_operator_cannot_update_task(client: TestClient, db_session: Session):
    manager = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    operator = make_user(db_session, company_id=COMPANY_A, role=UserRole.OPERATOR)
    eco = make_eco(db_session, company_id=COMPANY_A, requested_by=manager.id, status_=ECOStatus.IN_IMPLEMENTATION)
    task = make_task(db_session, eco=eco, company_id=COMPANY_A)

    resp = client.put(
        f"{API}/{eco.id}/tasks/{task.id}",
        headers=headers_for(operator),
        json={"status": "completed"},
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text


# ===========================================================================
# Audit: an ECO state transition writes a committed audit_log row.
# ===========================================================================


def test_submit_eco_writes_committed_audit_row(client: TestClient, db_session: Session):
    manager = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    eco = make_eco(db_session, company_id=COMPANY_A, requested_by=manager.id, status_=ECOStatus.DRAFT)

    resp = client.post(f"{API}/{eco.id}/submit", headers=headers_for(manager))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["status"] == ECOStatus.SUBMITTED.value

    rows = _committed_audit_rows(db_session, resource_type="engineering_change_order", resource_id=eco.id)
    assert rows, "ECO submit must write a committed audit_log row"
    actions = {r.action for r in rows}
    assert "STATUS_CHANGE" in actions, f"expected a STATUS_CHANGE audit row, got {actions}"
    status_row = next(r for r in rows if r.action == "STATUS_CHANGE")
    assert status_row.company_id == COMPANY_A
    assert status_row.new_values == {"status": ECOStatus.SUBMITTED.value}
    assert status_row.integrity_hash


def test_complete_eco_writes_committed_audit_row(client: TestClient, db_session: Session):
    manager = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    eco = make_eco(db_session, company_id=COMPANY_A, requested_by=manager.id, status_=ECOStatus.IN_IMPLEMENTATION)

    resp = client.post(f"{API}/{eco.id}/complete", headers=headers_for(manager))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["status"] == ECOStatus.COMPLETED.value

    rows = _committed_audit_rows(db_session, resource_type="engineering_change_order", resource_id=eco.id)
    status_rows = [r for r in rows if r.action == "STATUS_CHANGE"]
    assert status_rows, "ECO complete must write a committed STATUS_CHANGE audit row"
    assert status_rows[0].new_values == {"status": ECOStatus.COMPLETED.value}
