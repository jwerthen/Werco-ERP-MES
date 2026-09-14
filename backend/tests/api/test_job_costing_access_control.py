"""HTTP regression coverage for active-company job costs, roles and atomic audit.

Fixtures deliberately include mismatched tenant/parent FKs: ordinary integer FKs
do not enforce tenant ownership, including for rows written before these guards.
"""

from datetime import date, datetime
from itertools import count

import pytest
from sqlalchemy import inspect

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.job_costing import CostEntry, CostEntryType, JobCost
from app.models.part import Part
from app.models.time_entry import TimeEntry
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder, WorkOrderOperation
from app.services.audit_service import AuditService

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

URL = "/api/v1/job-costs"
_ids = count(1)


def company(db, company_id):
    if db.get(Company, company_id) is None:
        db.add(Company(id=company_id, name=f"Job Company {company_id}", slug=f"job-company-{company_id}"))
        db.commit()


def user(db, company_id=1, role=UserRole.ADMIN, **values):
    company(db, company_id)
    n = next(_ids)
    row = User(
        company_id=company_id,
        email=f"job-cost-{n}@example.test",
        employee_id=f"JC-{n}",
        first_name="Job",
        last_name="Cost",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=role,
        is_active=True,
        is_superuser=values.pop("is_superuser", False),
        **values,
    )
    db.add(row)
    db.commit()
    return row


def headers(actor, active_company=None, read_only=False):
    token = create_access_token(subject=actor.id, company_id=active_company or actor.company_id, read_only=read_only)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def work_order(db, company_id=1, **values):
    company(db, company_id)
    n = next(_ids)
    row = WorkOrder(
        company_id=company_id,
        work_order_number=f"JC-WO-{n}",
        work_order_type="laser_cutting",
        quantity_ordered=1,
        customer_name=f"PRIVATE-CUSTOMER-{company_id}",
        **values,
    )
    db.add(row)
    db.commit()
    return row


def job(db, company_id=1, wo=None, **values):
    wo = wo or work_order(db, company_id)
    row = JobCost(company_id=company_id, work_order_id=wo.id, revenue=100, **values)
    db.add(row)
    db.commit()
    return row


def entry(db, jc, company_id=None, **values):
    row = CostEntry(
        company_id=company_id or jc.company_id,
        job_cost_id=jc.id,
        entry_type=CostEntryType.MATERIAL,
        description="PRIVATE-COST-ENTRY",
        quantity=2,
        unit_cost=5,
        total_cost=10,
        entry_date=date(2026, 9, 13),
        **values,
    )
    db.add(row)
    db.commit()
    return row


def payload(**values):
    return {
        "entry_type": "material",
        "description": "Manual material",
        "quantity": 2,
        "unit_cost": 5,
        "entry_date": "2026-09-13",
        **values,
    }


def stored_state(db):
    """Capture committed columns, including timestamps and all tenants' audit rows."""
    db.rollback()
    return {
        model.__tablename__: [
            tuple(getattr(row, column.key) for column in inspect(model).column_attrs)
            for row in db.query(model).order_by(model.id).all()
        ]
        for model in (JobCost, CostEntry, WorkOrder, WorkOrderOperation, TimeEntry, AuditLog)
    }


@pytest.mark.parametrize("suffix", ["", "/entries", "/variance-report"])
def test_viewer_cannot_read_foreign_job_cost(client, db_session, suffix):
    actor = user(db_session, role=UserRole.VIEWER)
    victim = job(db_session, 2)
    entry(db_session, victim)
    before = stored_state(db_session)

    response = client.get(f"{URL}/{victim.id}{suffix}", headers=headers(actor))
    missing = client.get(f"{URL}/999999{suffix}", headers=headers(actor))

    assert response.status_code == 404, response.text
    assert response.json() == missing.json()
    assert "PRIVATE" not in response.text
    assert stored_state(db_session) == before


@pytest.mark.parametrize("verb", ["update", "delete"])
def test_viewer_cannot_mutate_foreign_job_cost(client, db_session, verb):
    actor = user(db_session, role=UserRole.VIEWER)
    victim = job(db_session, 2)
    line = entry(db_session, victim)
    before = stored_state(db_session)

    if verb == "update":
        response = client.put(f"{URL}/{victim.id}", headers=headers(actor), json={"revenue": 999})
    else:
        response = client.delete(f"{URL}/{victim.id}/entries/{line.id}", headers=headers(actor))

    assert response.status_code == 403, response.text
    assert stored_state(db_session) == before


WRITES = ("create", "update", "add", "delete", "calculate")
READS = ("list", "summary", "detail", "entries", "variance")


def read(client, auth, verb, jc_id):
    suffix = {
        "list": "/",
        "summary": "/summary",
        "detail": f"/{jc_id}",
        "entries": f"/{jc_id}/entries",
        "variance": f"/{jc_id}/variance-report",
    }[verb]
    return client.get(URL + suffix, headers=auth)


def write(client, auth, verb, jc_id, wo_id, entry_id):
    if verb == "create":
        return client.post(URL + "/", headers=auth, json={"work_order_id": wo_id, "revenue": 200})
    if verb == "update":
        return client.put(f"{URL}/{jc_id}", headers=auth, json={"revenue": 250, "status": "reviewed"})
    if verb == "add":
        return client.post(f"{URL}/{jc_id}/entries", headers=auth, json=payload())
    if verb == "delete":
        return client.delete(f"{URL}/{jc_id}/entries/{entry_id}", headers=auth)
    return client.post(f"{URL}/{jc_id}/calculate", headers=auth)


def operation(db, wo, company_id=None):
    work_center = WorkCenter(
        company_id=company_id or wo.company_id,
        code=f"JC-WC-{next(_ids)}",
        name="Costed cell",
        work_center_type="milling",
        hourly_rate=100,
    )
    db.add(work_center)
    db.flush()
    row = WorkOrderOperation(
        company_id=company_id or wo.company_id,
        work_order_id=wo.id,
        sequence=10,
        name="Costed operation",
        work_center_id=work_center.id,
    )
    db.add(row)
    db.commit()
    return row


def labor(db, jc, actor, operation_id=None):
    row = TimeEntry(
        company_id=jc.company_id,
        work_order_id=jc.work_order_id,
        user_id=actor.id,
        operation_id=operation_id,
        clock_in=datetime(2026, 9, 13, 8),
        clock_out=datetime(2026, 9, 13, 10),
        duration_hours=2,
    )
    db.add(row)
    db.commit()
    return row


@pytest.mark.parametrize("role", list(UserRole))
@pytest.mark.parametrize("verb", READS)
def test_domain_reads_remain_available_in_active_company(client, db_session, role, verb):
    actor = user(db_session, role=role)
    jc = job(db_session)
    entry(db_session, jc)
    job(db_session, 2)
    before = stored_state(db_session)

    response = read(client, headers(actor), verb, jc.id)

    assert response.status_code == 200, response.text
    if verb == "list":
        assert [row["id"] for row in response.json()] == [jc.id]
    elif verb == "summary":
        assert response.json()["total_jobs"] == 1
    assert "PRIVATE-CUSTOMER-2" not in response.text
    assert stored_state(db_session) == before


@pytest.mark.parametrize(
    "role", [UserRole.SUPERVISOR, UserRole.OPERATOR, UserRole.QUALITY, UserRole.SHIPPING, UserRole.VIEWER]
)
@pytest.mark.parametrize("verb", WRITES)
def test_read_only_roles_cannot_write_even_with_local_ids(client, db_session, role, verb):
    actor = user(db_session, role=role)
    jc = job(db_session)
    line = entry(db_session, jc)
    new_wo = work_order(db_session)
    before = stored_state(db_session)

    response = write(client, headers(actor), verb, jc.id, new_wo.id, line.id)

    assert response.status_code == 403, response.text
    assert stored_state(db_session) == before


@pytest.mark.parametrize("verb", WRITES)
def test_writer_cannot_address_another_tenants_records(client, db_session, verb):
    actor = user(db_session, role=UserRole.MANAGER)
    foreign = job(db_session, 2)
    line = entry(db_session, foreign)
    new_wo = work_order(db_session, 2)
    before = stored_state(db_session)

    response = write(client, headers(actor), verb, foreign.id, new_wo.id, line.id)
    absent = write(client, headers(actor), verb, 999999, 999999, 999999)

    assert response.status_code == 404, response.text
    assert response.json() == absent.json()
    assert stored_state(db_session) == before


@pytest.mark.parametrize("bad_parent", ["foreign", "deleted"])
@pytest.mark.parametrize("verb", READS + WRITES)
def test_parent_tenant_is_required_and_deleted_work_orders_remain_readable(client, db_session, bad_parent, verb):
    actor = user(db_session)
    wo = work_order(db_session, 2 if bad_parent == "foreign" else 1, is_deleted=bad_parent == "deleted")
    malformed = job(db_session, 1, wo=wo)
    line = entry(db_session, malformed)
    before = stored_state(db_session)

    if verb in READS:
        response = read(client, headers(actor), verb, malformed.id)
        if bad_parent == "deleted":
            # Preserve historical financial reads after an authorized parent WO
            # is deleted; the live-parent rule applies to new writes only.
            assert response.status_code == 200, response.text
            if verb == "list":
                assert [row["id"] for row in response.json()] == [malformed.id]
            elif verb == "summary":
                assert response.json()["total_jobs"] == 1
        elif verb in ("list", "summary"):
            assert response.status_code == 200
            if verb == "list":
                assert response.json() == []
            else:
                assert response.json()["total_jobs"] == 0
        else:
            assert response.status_code == 404, response.text
    else:
        response = write(client, headers(actor), verb, malformed.id, wo.id, line.id)
        assert response.status_code == 404, response.text
    assert "PRIVATE-CUSTOMER-2" not in response.text
    assert stored_state(db_session) == before


@pytest.mark.parametrize("wrong_child", ["other_job", "foreign_child", "absent"])
def test_delete_resolves_entry_through_tenant_and_parent(client, db_session, wrong_child):
    actor = user(db_session)
    jc = job(db_session)
    if wrong_child == "other_job":
        child_id = entry(db_session, job(db_session)).id
    elif wrong_child == "foreign_child":
        company(db_session, 2)
        child_id = entry(db_session, jc, company_id=2).id
    else:
        child_id = 999999
    before = stored_state(db_session)

    response = client.delete(f"{URL}/{jc.id}/entries/{child_id}", headers=headers(actor))

    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "Cost entry not found"}
    assert stored_state(db_session) == before


@pytest.mark.parametrize("bad_operation", ["foreign", "other_work_order", "wrong_tenant_same_parent", "absent"])
def test_added_entry_operation_must_belong_to_same_live_work_order(client, db_session, bad_operation):
    actor = user(db_session)
    jc = job(db_session)
    if bad_operation == "absent":
        operation_id = 999999
    elif bad_operation == "wrong_tenant_same_parent":
        company(db_session, 2)
        operation_id = operation(db_session, db_session.get(WorkOrder, jc.work_order_id), company_id=2).id
    else:
        wo = work_order(db_session, 2 if bad_operation == "foreign" else 1)
        operation_id = operation(db_session, wo).id
    before = stored_state(db_session)

    response = client.post(
        f"{URL}/{jc.id}/entries",
        headers=headers(actor),
        json=payload(work_order_operation_id=operation_id),
    )

    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "Work order operation not found"}
    assert stored_state(db_session) == before


@pytest.mark.parametrize("verb", ["entries", "variance", "update", "add", "delete", "calculate"])
def test_foreign_children_never_leave_tenant_or_feed_recalculation(client, db_session, verb):
    actor = user(db_session)
    jc = job(db_session)
    local = entry(db_session, jc)
    company(db_session, 2)
    foreign = entry(db_session, jc, company_id=2)
    foreign.description = "FOREIGN-LINE-SECRET"
    foreign.total_cost = 99999
    db_session.commit()
    foreign_id = foreign.id
    # Poison the identity map deliberately; the resolver must replace unscoped
    # relationship state even when the same Session loaded it before the request.
    db_session.refresh(jc)
    assert len(jc.entries) == 2
    if verb in ("entries", "variance"):
        response = read(client, headers(actor), verb, jc.id)
    else:
        response = write(client, headers(actor), verb, jc.id, jc.work_order_id, local.id)
    assert response.status_code == 200, response.text
    assert "FOREIGN-LINE-SECRET" not in response.text
    db_session.rollback()
    assert db_session.get(CostEntry, foreign_id).total_cost == 99999
    if verb not in ("entries", "variance"):
        expected = {"update": 10, "add": 20, "delete": 0, "calculate": 10}[verb]
        assert db_session.get(JobCost, jc.id).actual_total_cost == expected
    for audit in db_session.query(AuditLog).all():
        assert "FOREIGN-LINE-SECRET" not in str(audit.old_values) + str(audit.new_values) + str(audit.extra_data)


@pytest.mark.parametrize(
    "relation", ["foreign_part", "deleted_part", "foreign_operation", "other_operation", "foreign_creator"]
)
def test_legacy_foreign_relationships_are_not_serialized(client, db_session, relation):
    actor = user(db_session)
    jc = job(db_session)
    line = entry(db_session, jc)
    if relation.endswith("part"):
        company(db_session, 2)
        part = Part(
            company_id=2 if relation == "foreign_part" else 1,
            part_number=f"SECRET-PART-{next(_ids)}",
            name="SECRET-PART-NAME",
            part_type="manufactured",
            is_deleted=relation == "deleted_part",
        )
        db_session.add(part)
        db_session.flush()
        db_session.get(WorkOrder, jc.work_order_id).part_id = part.id
    elif relation.endswith("operation"):
        line.work_order_operation_id = operation(
            db_session, work_order(db_session, 2 if relation == "foreign_operation" else 1)
        ).id
    else:
        line.created_by = user(db_session, 2).id
    db_session.commit()
    before = stored_state(db_session)

    if relation.endswith("part"):
        detail = read(client, headers(actor), "detail", jc.id)
        listing = read(client, headers(actor), "list", jc.id)
        assert detail.status_code == listing.status_code == 200
        if relation == "foreign_part":
            assert detail.json()["part_number"] is None
            assert detail.json()["part_name"] is None
            assert "SECRET-PART" not in listing.text
        else:
            # Deletion must not erase historical labels from an authorized
            # financial record. Tenant ownership still decides visibility.
            assert detail.json()["part_number"] == part.part_number
            assert detail.json()["part_name"] == part.name
            assert "SECRET-PART" in listing.text
    else:
        response = read(client, headers(actor), "entries", jc.id)
        assert response.status_code == 200, response.text
        key = "created_by" if relation == "foreign_creator" else "work_order_operation_id"
        assert response.json()[0][key] is None
    assert stored_state(db_session) == before


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER, UserRole.PLATFORM_ADMIN])
def test_allowed_lifecycle_commits_attributed_audits_and_correct_totals(client, db_session, role):
    actor = user(db_session, role=role)
    wo = work_order(db_session)
    op = operation(db_session, wo)
    auth = headers(actor)
    created = client.post(
        URL + "/",
        headers=auth,
        json={
            "work_order_id": wo.id,
            "estimated_material_cost": 4,
            "revenue": 100,
            "company_id": 2,
        },
    )
    assert created.status_code == 200, created.text
    jc_id = created.json()["id"]
    jc = db_session.get(JobCost, jc_id)
    assert jc.company_id == 1
    assert created.json()["estimated_total_cost"] == 4
    assert created.json()["margin_amount"] == 100
    assert created.json()["total_variance"] == 0  # preserve initial-record behavior
    added = client.post(f"{URL}/{jc_id}/entries", headers=auth, json=payload(work_order_operation_id=op.id))
    assert added.status_code == 200, added.text
    entry_id = added.json()["id"]
    assert added.json()["total_cost"] == 10
    assert added.json()["created_by"] == actor.id
    assert added.json()["work_order_operation_id"] == op.id
    assert db_session.get(CostEntry, entry_id).company_id == 1
    assert read(client, auth, "detail", jc_id).json()["actual_total_cost"] == 10
    updated = client.put(f"{URL}/{jc_id}", headers=auth, json={"revenue": 200, "status": "reviewed"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["margin_amount"] == 190
    labor(db_session, jc, actor, op.id)
    calculated = client.post(f"{URL}/{jc_id}/calculate", headers=auth)
    assert calculated.status_code == 200, calculated.text
    assert calculated.json()["actual_labor_cost"] == 200
    assert calculated.json()["actual_material_cost"] == 10
    removed = client.delete(f"{URL}/{jc_id}/entries/{entry_id}", headers=auth)
    assert removed.status_code == 200, removed.text
    assert read(client, auth, "detail", jc_id).json()["actual_material_cost"] == 0
    db_session.rollback()  # discard any merely flushed audit-after-commit rows
    audits = db_session.query(AuditLog).order_by(AuditLog.sequence_number).all()
    assert [(a.action, a.resource_type) for a in audits] == [
        ("CREATE", "job_cost"),
        ("CREATE", "cost_entry"),
        ("STATUS_CHANGE", "job_cost"),
        ("RECALCULATE", "job_cost"),
        ("DELETE", "cost_entry"),
    ]
    assert all(a.company_id == 1 and a.user_id == actor.id and a.integrity_hash for a in audits)
    assert audits[1].extra_data["job_cost_before"]["actual_total_cost"] == 0
    assert audits[1].extra_data["job_cost_after"]["actual_total_cost"] == 10
    assert audits[-1].old_values["id"] == entry_id
    assert db_session.get(CostEntry, entry_id) is None


@pytest.mark.parametrize("verb", WRITES)
def test_audit_failure_rolls_back_financial_mutation(client, db_session, monkeypatch, verb):
    actor = user(db_session)
    jc = job(db_session)
    line = entry(db_session, jc)
    new_wo = work_order(db_session)
    labor(db_session, jc, actor)
    before = stored_state(db_session)
    monkeypatch.setattr(AuditService, "log", lambda *args, **kwargs: None)

    response = write(client, headers(actor), verb, jc.id, new_wo.id, line.id)

    assert response.status_code == 503, response.text
    assert response.json() == {"detail": "Unable to save audit record"}
    assert stored_state(db_session) == before


def test_switched_platform_company_controls_reads_writes_and_audit(client, db_session):
    actor = user(db_session, role=UserRole.PLATFORM_ADMIN)
    home = job(db_session)
    target_wo = work_order(db_session, 2)
    auth = headers(actor, active_company=2)
    created = client.post(URL + "/", headers=auth, json={"work_order_id": target_wo.id})
    assert created.status_code == 200, created.text
    new_id = created.json()["id"]
    added = client.post(f"{URL}/{new_id}/entries", headers=auth, json=payload())
    assert added.status_code == 200, added.text
    assert added.json()["created_by"] == actor.id
    assert read(client, auth, "detail", home.id).status_code == 404
    assert [row["id"] for row in read(client, auth, "list", new_id).json()] == [new_id]
    db_session.rollback()
    assert db_session.get(JobCost, new_id).company_id == 2
    assert db_session.get(CostEntry, added.json()["id"]).company_id == 2
    assert all(a.company_id == 2 and a.user_id == actor.id for a in db_session.query(AuditLog).all())


@pytest.mark.parametrize("verb", WRITES)
def test_read_only_platform_context_cannot_write(client, db_session, verb):
    actor = user(db_session, role=UserRole.PLATFORM_ADMIN)
    jc = job(db_session, 2)
    line = entry(db_session, jc)
    new_wo = work_order(db_session, 2)
    before = stored_state(db_session)
    response = write(client, headers(actor, active_company=2, read_only=True), verb, jc.id, new_wo.id, line.id)
    assert response.status_code == 403, response.text
    assert stored_state(db_session) == before


@pytest.mark.parametrize("bad_operation", ["foreign", "other_work_order"])
def test_calculate_refuses_invalid_time_source_before_replacing_entries(client, db_session, bad_operation):
    actor = user(db_session)
    jc = job(db_session)
    entry(db_session, jc)
    op = operation(db_session, work_order(db_session, 2 if bad_operation == "foreign" else 1))
    labor(db_session, jc, actor, op.id)
    before = stored_state(db_session)
    response = client.post(f"{URL}/{jc.id}/calculate", headers=headers(actor))
    assert response.status_code == 404, response.text
    assert stored_state(db_session) == before


@pytest.mark.parametrize("values", [{"status": "invalid"}, {"status": None}, {"revenue": None}])
def test_invalid_header_updates_are_validation_errors_without_writes(client, db_session, values):
    actor = user(db_session)
    jc = job(db_session)
    before = stored_state(db_session)
    response = client.put(f"{URL}/{jc.id}", headers=headers(actor), json=values)
    assert response.status_code == 422, response.text
    assert stored_state(db_session) == before


@pytest.mark.parametrize("values", [{"entry_type": "invalid"}, {"source": "invalid"}])
def test_invalid_entry_enums_are_validation_errors_without_writes(client, db_session, values):
    actor = user(db_session)
    jc = job(db_session)
    before = stored_state(db_session)
    response = client.post(f"{URL}/{jc.id}/entries", headers=headers(actor), json=payload(**values))
    assert response.status_code == 422, response.text
    assert stored_state(db_session) == before


def test_legacy_foreign_header_occupying_work_order_key_cannot_be_disclosed_or_reassigned(client, db_session):
    actor = user(db_session)
    wo = work_order(db_session)
    company(db_session, 2)
    job(db_session, 2, wo=wo)
    before = stored_state(db_session)
    response = client.post(URL + "/", headers=headers(actor), json={"work_order_id": wo.id})
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": "Job cost could not be created"}
    assert stored_state(db_session) == before
