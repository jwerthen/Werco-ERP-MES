"""``PUT /eco/eco/{id}``: an edit that changes the ECO is saved only with its audit row.

The ECO update used to write its audit row best-effort (``AuditService.log_update``). A
failure that never reached the database (the integrity hash raising, a value the JSON column
cannot bind) was logged and swallowed, so the edit committed with no record of it. For the
affected-item lists that is the harm the document-delete guard exists to prevent: ``DELETE
/documents/{id}`` refuses a document a draft or submitted ECO lists, and sends the user to this
``PUT`` to take it out. A swallowed row there means the ECO silently lost an affected item.

The row is now required (``log_update(..., required=True)`` inside ``required_audit``) for every
edit that changes a stored value: no row, nothing saved, **503**. An edit that changes nothing
writes nothing and still answers 200 (``log_update`` returns ``None`` for "no changes", and that is
not a failure).

Rows are created in the shared ``db_session`` (the ``client`` fixture uses the same session), so
after a 503 the session is read exactly as the route left it: no rollback of the test's own, which
would hide a route that forgot to roll back.
"""

import json

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import app.services.audit_service as audit_module
from app.api.endpoints.engineering_changes import (
    ECO_TASK_UPDATE_AUDIT_UNAVAILABLE_DETAIL,
    ECO_UPDATE_AUDIT_UNAVAILABLE_DETAIL,
)
from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.engineering_change import ECOImplementationTask, ECOStatus, EngineeringChangeOrder
from app.models.user import UserRole
from app.services.audit_service import AuditService, AuditWriteError
from tests.api.test_eco_tenant_isolation_batch11 import (
    API,
    COMPANY_A,
    headers_for,
    make_document,
    make_eco,
    make_part,
    make_task,
    make_user,
    make_work_order,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

RESOURCE = "engineering_change_order"


def _eco_audit_rows(db: Session, eco_id: int):
    return (
        db.query(AuditLog)
        .filter(AuditLog.resource_type == RESOURCE, AuditLog.resource_id == eco_id)
        .order_by(AuditLog.id)
        .all()
    )


def _break_the_audit_write(monkeypatch) -> None:
    """Make every audit write fail the way ``log`` swallows: a Python-side error, not a DB one.

    ``compute_audit_hash`` raising is the failure ``AuditService.log`` logs and turns into ``None``
    (see ``test_a_python_side_audit_failure_stays_best_effort``). Under the old best-effort call the
    edit committed anyway; that is the path this guards.
    """

    def hash_fails(**_kwargs):
        raise TypeError("not JSON serializable")

    monkeypatch.setattr(settings, "AUDIT_HASH_CHAIN_ENABLED", True)
    monkeypatch.setattr(audit_module, "compute_audit_hash", hash_fails)


@pytest.fixture
def scoped_eco(db_session: Session):
    """A draft ECO in company A listing one part, one work order and two documents."""
    manager = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    part = make_part(db_session, company_id=COMPANY_A)
    work_order = make_work_order(db_session, company_id=COMPANY_A)
    kept = make_document(db_session, company_id=COMPANY_A)
    released = make_document(db_session, company_id=COMPANY_A)
    eco = make_eco(
        db_session,
        company_id=COMPANY_A,
        requested_by=manager.id,
        status_=ECOStatus.DRAFT,
        affected_parts=json.dumps([part.id]),
        affected_work_orders=json.dumps([work_order.id]),
        affected_documents=json.dumps([kept.id, released.id]),
    )
    return {
        "manager": manager,
        "eco": eco,
        "part": part,
        "work_order": work_order,
        "kept": kept,
        "released": released,
    }


# --------------------------------------------------------------------------- failure atomicity


@pytest.mark.parametrize(
    "field",
    ["affected_documents", "affected_parts", "affected_work_orders", "title"],
)
def test_an_audit_failure_on_an_eco_edit_answers_503_and_saves_nothing(
    client: TestClient, db_session: Session, monkeypatch, scoped_eco, field
):
    eco = scoped_eco["eco"]
    before = {
        "title": eco.title,
        "affected_parts": eco.affected_parts,
        "affected_work_orders": eco.affected_work_orders,
        "affected_documents": eco.affected_documents,
        "updated_at": eco.updated_at,
    }
    payload = {
        # The document-delete guard's own instruction: the list minus the document to release.
        "affected_documents": [scoped_eco["kept"].id],
        "affected_parts": [],
        "affected_work_orders": [],
        "title": "Edited while the audit store is down",
    }
    _break_the_audit_write(monkeypatch)

    response = client.put(f"{API}/{eco.id}", headers=headers_for(scoped_eco["manager"]), json={field: payload[field]})

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE, response.text
    assert response.json()["detail"] == ECO_UPDATE_AUDIT_UNAVAILABLE_DETAIL
    db_session.expire_all()
    after = db_session.get(EngineeringChangeOrder, eco.id)
    assert {key: getattr(after, key) for key in before} == before, "the edit must not be saved without its audit row"
    assert _eco_audit_rows(db_session, eco.id) == []


def test_a_database_error_from_the_audit_write_also_saves_nothing(
    client: TestClient, db_session: Session, monkeypatch, scoped_eco
):
    """A database error ``log`` re-raises (the chain tail read failing here) is ``AuditWriteError`` too.

    ``log_required`` converts it, so the route still answers its own 503 and rolls back, rather than
    the app-wide handler answering for it.
    """
    eco = scoped_eco["eco"]
    original = eco.affected_documents

    def tail_read_fails(self):
        raise OperationalError("SELECT audit_log tail", {}, Exception("connection lost"))

    monkeypatch.setattr(settings, "AUDIT_HASH_CHAIN_ENABLED", True)
    monkeypatch.setattr(AuditService, "_get_next_sequence_and_previous_hash", tail_read_fails)
    response = client.put(
        f"{API}/{eco.id}",
        headers=headers_for(scoped_eco["manager"]),
        json={"affected_documents": [scoped_eco["kept"].id]},
    )

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE, response.text
    assert response.json()["detail"] == ECO_UPDATE_AUDIT_UNAVAILABLE_DETAIL
    db_session.expire_all()
    assert db_session.get(EngineeringChangeOrder, eco.id).affected_documents == original
    assert _eco_audit_rows(db_session, eco.id) == []


# --------------------------------------------------------------------------- the normal path


def test_a_scope_edit_answers_200_with_its_audit_row(client: TestClient, db_session: Session, scoped_eco):
    eco = scoped_eco["eco"]
    kept, released = scoped_eco["kept"], scoped_eco["released"]

    response = client.put(
        f"{API}/{eco.id}",
        headers=headers_for(scoped_eco["manager"]),
        json={"affected_documents": [kept.id], "title": "Scope narrowed"},
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert json.loads(response.json()["affected_documents"]) == [kept.id]
    assert response.json()["title"] == "Scope narrowed"

    db_session.expire_all()
    assert json.loads(db_session.get(EngineeringChangeOrder, eco.id).affected_documents) == [kept.id]
    rows = _eco_audit_rows(db_session, eco.id)
    assert [row.action for row in rows] == ["UPDATE"]
    row = rows[0]
    assert row.company_id == COMPANY_A
    assert row.user_id == scoped_eco["manager"].id
    assert row.integrity_hash
    changes = row.extra_data["changes"]
    assert json.loads(changes["affected_documents"]["old"]) == [kept.id, released.id]
    assert json.loads(changes["affected_documents"]["new"]) == [kept.id]
    assert changes["title"]["new"] == "Scope narrowed"


def test_an_edit_that_changes_nothing_answers_200_and_writes_nothing(
    client: TestClient, db_session: Session, monkeypatch, scoped_eco
):
    """No change, no row, and no 503: ``None`` from ``log_update`` means "nothing to record".

    The audit write is broken on purpose, so a route that required a row even for a no-op save
    would answer 503 here.
    """
    eco = scoped_eco["eco"]
    _break_the_audit_write(monkeypatch)

    response = client.put(
        f"{API}/{eco.id}",
        headers=headers_for(scoped_eco["manager"]),
        json={"title": eco.title, "affected_documents": json.loads(eco.affected_documents)},
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    db_session.expire_all()
    assert _eco_audit_rows(db_session, eco.id) == []


# --------------------------------------------------------------------------- implementation tasks

TASK_RESOURCE = "eco_implementation_task"


def _task_audit_rows(db: Session, task_id: int):
    return (
        db.query(AuditLog)
        .filter(AuditLog.resource_type == TASK_RESOURCE, AuditLog.resource_id == task_id)
        .order_by(AuditLog.id)
        .all()
    )


@pytest.mark.parametrize("change", [{"status": "skipped"}, {"description": "Rewritten while audit is down"}])
def test_an_audit_failure_on_a_task_edit_answers_503_and_saves_nothing(
    client: TestClient, db_session: Session, monkeypatch, scoped_eco, change
):
    """Skipping a task is what lets ``complete_eco`` close the change with that work undone."""
    eco = scoped_eco["eco"]
    task = make_task(db_session, eco=eco, company_id=COMPANY_A)
    before = {"status": task.status, "description": task.description, "completed_date": task.completed_date}
    _break_the_audit_write(monkeypatch)

    response = client.put(f"{API}/{eco.id}/tasks/{task.id}", headers=headers_for(scoped_eco["manager"]), json=change)

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE, response.text
    assert response.json()["detail"] == ECO_TASK_UPDATE_AUDIT_UNAVAILABLE_DETAIL
    db_session.expire_all()
    after = db_session.get(ECOImplementationTask, task.id)
    assert {key: getattr(after, key) for key in before} == before
    assert _task_audit_rows(db_session, task.id) == []


def test_a_task_edit_answers_200_with_its_audit_row(client: TestClient, db_session: Session, scoped_eco):
    eco = scoped_eco["eco"]
    task = make_task(db_session, eco=eco, company_id=COMPANY_A)

    response = client.put(
        f"{API}/{eco.id}/tasks/{task.id}", headers=headers_for(scoped_eco["manager"]), json={"status": "skipped"}
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["status"] == "skipped"
    db_session.expire_all()
    rows = _task_audit_rows(db_session, task.id)
    assert [row.action for row in rows] == ["UPDATE"]
    assert rows[0].user_id == scoped_eco["manager"].id
    assert rows[0].extra_data["changes"]["status"] == {"old": "pending", "new": "skipped"}


def test_a_task_edit_that_changes_nothing_answers_200_and_writes_nothing(
    client: TestClient, db_session: Session, monkeypatch, scoped_eco
):
    eco = scoped_eco["eco"]
    task = make_task(db_session, eco=eco, company_id=COMPANY_A)
    _break_the_audit_write(monkeypatch)

    response = client.put(
        f"{API}/{eco.id}/tasks/{task.id}",
        headers=headers_for(scoped_eco["manager"]),
        json={"description": task.description},
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    db_session.expire_all()
    assert _task_audit_rows(db_session, task.id) == []


# --------------------------------------------------------------------------- the service option


def test_log_update_required_raises_only_when_a_change_has_no_row(db_session: Session, monkeypatch):
    audit = AuditService(db_session, company_id=COMPANY_A)

    # A real change: the row is written and returned.
    entry = audit.log_update(RESOURCE, 1, "ECO-1", old_values={"title": "a"}, new_values={"title": "b"}, required=True)
    assert entry is not None and entry.extra_data["changes"] == {"title": {"old": "a", "new": "b"}}

    # No change: nothing to record, so nothing is required.
    assert (
        audit.log_update(RESOURCE, 1, "ECO-1", old_values={"title": "b"}, new_values={"title": "b"}, required=True)
        is None
    )

    # A change whose row cannot be written: raises instead of returning None.
    _break_the_audit_write(monkeypatch)
    with pytest.raises(AuditWriteError):
        audit.log_update(RESOURCE, 1, "ECO-1", old_values={"title": "b"}, new_values={"title": "c"}, required=True)
    # The default stays best-effort for every other caller.
    assert audit.log_update(RESOURCE, 1, "ECO-1", old_values={"title": "b"}, new_values={"title": "c"}) is None
    db_session.rollback()
