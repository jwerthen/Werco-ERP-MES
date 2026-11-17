"""Evidence under a removed QMS clause or standard is frozen: its ``PUT`` / ``DELETE`` answer 404.

Removing a standard (``DELETE /qms-standards/{id}``) or a clause
(``DELETE /qms-standards/clauses/{id}``) soft-deletes only that row. The evidence links under
it keep ``is_deleted = false``; every QMS read hides them with their parent, and
``DELETE /documents/{id}`` counts them as removed (they keep their document). The evidence
``PUT`` and ``DELETE`` used to check only the evidence row's own flag, so an API caller could
still edit, verify or remove such a link: a compliance review set ``document_id`` to null on
evidence under a removed clause (200) and the document then deleted.

``_lock_live_evidence_or_404`` now also reads the clause and standard, after the evidence row
lock and inside the active company, and answers the same 404 for a removed one. These tests pin:

1. both verbs, under a removed clause and under a removed standard: 404 ``Evidence not found``
   and nothing written (the row's columns and the audit trail unchanged);
2. the review's reproduction end to end: the link cannot be cleared, and the document stays;
3. the parent flags are read from the database, not from a stale copy in the session;
4. a clause or standard that is not the active company's is not found;
5. the parent read comes after the row lock, is not itself a lock, and is tenant-scoped;
6. a live clause and standard are still editable (the check does not over-refuse).
"""

from typing import Any, Dict, List

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import event, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from app.api.endpoints import documents
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.document import Document
from app.models.qms_standard import QMSClause, QMSClauseEvidence, QMSStandard
from app.services.storage_service import LocalStorageBackend

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

BASE = "/api/v1/qms-standards"
COMPANY_A = 1
COMPANY_B = 2


def _ensure_company(db: Session, company_id: int) -> None:
    if db.query(Company).filter(Company.id == company_id).first() is None:
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True))
        db.commit()


def _chain(
    db: Session,
    *,
    standard_company: int = COMPANY_A,
    clause_company: int = COMPANY_A,
    document_id: Any = None,
) -> QMSClauseEvidence:
    """A standard, one clause and one evidence link; the evidence is always company A's."""
    for company_id in {standard_company, clause_company, COMPANY_A}:
        _ensure_company(db, company_id)
    standard = QMSStandard(company_id=standard_company, name="AS9100D", version="2016", is_active=True)
    db.add(standard)
    db.flush()
    clause = QMSClause(
        company_id=clause_company,
        standard_id=standard.id,
        clause_number="7.5.3",
        title="Control of documented information",
        compliance_status="not_assessed",
    )
    db.add(clause)
    db.flush()
    evidence = QMSClauseEvidence(
        company_id=COMPANY_A,
        clause_id=clause.id,
        evidence_type="document",
        title="Quality manual",
        document_id=document_id,
        is_verified=False,
    )
    db.add(evidence)
    db.commit()
    db.refresh(evidence)
    return evidence


def _remove_parent(client: TestClient, headers: dict, evidence: QMSClauseEvidence, parent: str) -> None:
    """Remove the clause or standard through its own route (Admin), as a user would."""
    clause = evidence.clause
    url = f"{BASE}/clauses/{clause.id}" if parent == "clause" else f"{BASE}/{clause.standard_id}"
    response = client.delete(url, headers=headers)
    assert response.status_code == status.HTTP_204_NO_CONTENT, response.text


def _call(client: TestClient, verb: str, evidence_id: int, headers: dict, body: Any = None):
    url = f"{BASE}/evidence/{evidence_id}"
    if verb == "put":
        return client.put(url, json=body or {"is_verified": True, "title": "Edited title"}, headers=headers)
    return client.delete(url, headers=headers)


def _columns(db: Session, evidence_id: int) -> Dict[str, Any]:
    db.expire_all()
    row = db.get(QMSClauseEvidence, evidence_id)
    return {c.key: getattr(row, c.key) for c in row.__table__.columns}


def _evidence_audit_rows(db: Session, evidence_id: int) -> List[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(AuditLog.resource_type == "qms_clause_evidence", AuditLog.resource_id == evidence_id)
        .all()
    )


def _assert_404_and_nothing_written(db: Session, response, evidence_id: int, before: Dict[str, Any]) -> None:
    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert response.json()["detail"] == "Evidence not found"
    db.rollback()
    assert _columns(db, evidence_id) == before
    assert _evidence_audit_rows(db, evidence_id) == []


# ---------------------------------------------------------------------------
# 1. Both verbs, under a removed clause and under a removed standard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("parent", ["clause", "standard"])
@pytest.mark.parametrize("verb", ["put", "delete"])
def test_evidence_under_a_removed_parent_is_404_and_writes_nothing(
    verb: str, parent: str, client: TestClient, db_session: Session, admin_headers: dict
):
    evidence = _chain(db_session)
    evidence_id = evidence.id
    _remove_parent(client, admin_headers, evidence, parent)
    before = _columns(db_session, evidence_id)
    assert before["is_deleted"] is False, "removing a parent leaves the evidence row's own flag clear"

    response = _call(client, verb, evidence_id, admin_headers)

    _assert_404_and_nothing_written(db_session, response, evidence_id, before)


@pytest.mark.parametrize("verb", ["put", "delete"])
def test_evidence_under_a_live_clause_and_standard_is_still_writable(
    verb: str, client: TestClient, db_session: Session, admin_headers: dict
):
    evidence = _chain(db_session)

    response = _call(client, verb, evidence.id, admin_headers)

    assert response.status_code in (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT), response.text
    after = _columns(db_session, evidence.id)
    if verb == "put":
        assert after["is_verified"] is True and after["title"] == "Edited title"
    else:
        assert after["is_deleted"] is True
    assert len(_evidence_audit_rows(db_session, evidence.id)) == 1


# ---------------------------------------------------------------------------
# 2. The compliance review's reproduction, end to end
# ---------------------------------------------------------------------------


@pytest.fixture
def local_files(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(documents, "get_storage", lambda: LocalStorageBackend())
    return tmp_path


@pytest.mark.parametrize("parent", ["clause", "standard"])
def test_link_under_a_removed_parent_cannot_be_cleared_so_its_document_stays(
    parent: str, client: TestClient, db_session: Session, admin_headers: dict, local_files
):
    uploaded = client.post(
        "/api/v1/documents/upload",
        headers=admin_headers,
        data={"title": "Quality manual", "document_type": "procedure"},
        files={"file": ("manual.pdf", b"synthetic-quality-manual", "application/pdf")},
    )
    assert uploaded.status_code == status.HTTP_200_OK, uploaded.text
    document_id = uploaded.json()["id"]
    evidence = _chain(db_session, document_id=document_id)
    evidence_id = evidence.id
    _remove_parent(client, admin_headers, evidence, parent)
    before = _columns(db_session, evidence_id)

    cleared = _call(client, "put", evidence_id, admin_headers, body={"document_id": None})
    _assert_404_and_nothing_written(db_session, cleared, evidence_id, before)
    assert before["document_id"] == document_id

    deleted = client.delete(f"/api/v1/documents/{document_id}", headers=admin_headers)
    assert deleted.status_code == status.HTTP_409_CONFLICT, deleted.text
    assert "named by a removed QMS standard, clause or evidence link" in deleted.json()["detail"]
    db_session.rollback()
    assert db_session.get(Document, document_id) is not None


# ---------------------------------------------------------------------------
# 3. The parent flags come from the database, not a stale session copy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("parent", ["clause", "standard"])
@pytest.mark.parametrize("verb", ["put", "delete"])
def test_parent_removed_behind_a_stale_session_copy_is_still_seen(
    verb: str, parent: str, client: TestClient, db_session: Session, admin_headers: dict
):
    """A removal the session has not seen (what a request meets when another one committed it).

    The ``client`` fixture shares ``db_session`` with the route, so the clause and standard
    loaded here sit in the route's identity map still marked live. A check that read them as
    entities would trust that copy and answer 200.
    """
    evidence = _chain(db_session)
    evidence_id = evidence.id
    clause = evidence.clause
    standard = clause.standard
    model, row = (QMSClause, clause) if parent == "clause" else (QMSStandard, standard)
    db_session.execute(
        update(model).where(model.id == row.id).values(is_deleted=True).execution_options(synchronize_session=False)
    )
    assert row.is_deleted is False, "the session's copy must still say live for this test to mean anything"
    before = {c.key: getattr(evidence, c.key) for c in evidence.__table__.columns}

    response = _call(client, verb, evidence_id, admin_headers)

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert response.json()["detail"] == "Evidence not found"
    # Nothing was flushed for the evidence row, and no audit row was written.
    assert {c.key: getattr(evidence, c.key) for c in evidence.__table__.columns} == before
    assert evidence not in db_session.dirty
    assert _evidence_audit_rows(db_session, evidence_id) == []


# ---------------------------------------------------------------------------
# 4. The clause and standard must be the active company's
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("foreign", ["clause", "standard"])
@pytest.mark.parametrize("verb", ["put", "delete"])
def test_parent_of_another_company_is_not_found(
    verb: str, foreign: str, client: TestClient, db_session: Session, admin_headers: dict
):
    """A company-A evidence row whose clause or standard is company B's (bad data) is not editable.

    The parent read joins inside the caller's company, so it does not report another tenant's
    row as live, and the answer is the same 404 as for a removed parent.
    """
    if foreign == "clause":
        evidence = _chain(db_session, standard_company=COMPANY_B, clause_company=COMPANY_B)
    else:
        evidence = _chain(db_session, standard_company=COMPANY_B)
    evidence_id = evidence.id
    before = _columns(db_session, evidence_id)

    response = _call(client, verb, evidence_id, admin_headers)

    _assert_404_and_nothing_written(db_session, response, evidence_id, before)


# ---------------------------------------------------------------------------
# 5. Statement shape: lock first, then an unlocked, tenant-scoped parent read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["put", "delete"])
def test_parent_read_follows_the_row_lock_and_takes_no_lock(
    verb: str, client: TestClient, db_session: Session, admin_headers: dict
):
    evidence = _chain(db_session)
    selects: List[Select] = []

    def record(conn, clauseelement, multiparams, params, execution_options):  # noqa: ARG001
        if isinstance(clauseelement, Select):
            selects.append(clauseelement)

    engine = db_session.get_bind()
    event.listen(engine, "before_execute", record)
    try:
        response = _call(client, verb, evidence.id, admin_headers)
    finally:
        event.remove(engine, "before_execute", record)
    assert response.status_code in (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT), response.text

    compiled = [(str(s.compile(dialect=postgresql.dialect())), s._for_update_arg is not None) for s in selects]
    lock_at = [i for i, (sql, locks) in enumerate(compiled) if locks and "FROM qms_clause_evidence" in sql]
    parent_at = [i for i, (sql, _) in enumerate(compiled) if "FROM qms_clauses JOIN qms_standards" in sql]
    assert len(lock_at) == 1, compiled
    assert len(parent_at) == 1, compiled
    assert lock_at[0] < parent_at[0], "the parent flags are read after the evidence row lock is held"

    parent_sql, parent_locks = compiled[parent_at[0]]
    assert not parent_locks, "the clause and standard must not be locked (see the helper's docstring)"
    assert "qms_clauses.company_id = " in parent_sql
    assert "qms_standards.company_id = " in parent_sql
