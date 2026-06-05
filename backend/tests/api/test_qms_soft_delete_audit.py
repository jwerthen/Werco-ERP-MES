"""Soft-delete + audit-logging coverage for the QMS standards/clauses/evidence endpoints.

Locks in the hardening on branch qa/full-pass-2026-06-04 that makes
``app.api.endpoints.qms_standards`` compliant after ``QMSStandard``, ``QMSClause``
and ``QMSClauseEvidence`` gained ``SoftDeleteMixin`` (``is_deleted`` /
``deleted_at`` / ``deleted_by``) on top of the existing ``TenantMixin``.

Headline invariants proved here:

Soft delete (not hard delete):
- DELETE of a standard / clause / evidence returns its success status (204), a
  subsequent GET is 404 (filtered out of every read), but the row STILL EXISTS
  in the DB with ``is_deleted == True`` and ``deleted_by == <caller id>``. We
  prove persistence by querying the model directly through the db session.
- Nested filtering via ``with_loader_criteria``: a soft-deleted clause is absent
  from ``GET /{standard_id}``'s nested ``clauses``; a soft-deleted evidence link
  is absent from ``GET /{standard_id}/clauses``' nested ``evidence_links``.

Audit logging (queried from the ``AuditLog`` model; rows are written by the
endpoints via ``AuditService`` in the same session/transaction):
- CREATE of a standard/clause/evidence emits an ``action="CREATE"`` row with the
  right ``resource_type`` (``qms_standard`` / ``qms_clause`` / ``qms_clause_evidence``)
  and ``resource_id``.
- DELETE emits ``action="DELETE"``.
- ``PUT /clauses/{clause_id}`` that changes ``compliance_status`` emits a
  ``STATUS_CHANGE`` row carrying old + new status -- the headline compliance
  behavior.
- Every audit row is tenant-tagged with the caller's active ``company_id``.

We do NOT insert ``AuditLog`` rows directly (they carry a tamper-evident hash
chain); they are produced by the endpoints and only read back here. The fixture
shape mirrors tests/api/test_qms_standards_tenant_isolation.py. The default
seeded company is id=1 (tests/conftest.py).
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.qms_standard import QMSClause, QMSClauseEvidence, QMSStandard
from app.models.user import User, UserRole

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1

# Module-level counter so every fixture row gets a globally unique natural key,
# even across tests sharing a worker DB under -n auto.
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _make_user(db: Session, *, company_id: int = COMPANY_A, role: UserRole = UserRole.ADMIN) -> User:
    n = _next()
    user = User(
        email=f"qms-sd-{n}@co{company_id}.test",
        employee_id=f"QMSSD-{n:05d}",
        first_name="Qms",
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


def _headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _make_standard(db: Session, *, company_id: int = COMPANY_A) -> QMSStandard:
    n = _next()
    standard = QMSStandard(
        company_id=company_id,
        name=f"STD-{n}",
        version="2015",
        standard_body="ISO",
        is_active=True,
    )
    db.add(standard)
    db.commit()
    db.refresh(standard)
    return standard


def _make_clause(db: Session, *, standard: QMSStandard, compliance_status: str = "not_assessed") -> QMSClause:
    n = _next()
    clause = QMSClause(
        company_id=standard.company_id,
        standard_id=standard.id,
        clause_number=f"{n}.0",
        title=f"Clause {n}",
        description="requirement text",
        compliance_status=compliance_status,
    )
    db.add(clause)
    db.commit()
    db.refresh(clause)
    return clause


def _make_evidence(db: Session, *, clause: QMSClause) -> QMSClauseEvidence:
    n = _next()
    evidence = QMSClauseEvidence(
        company_id=clause.company_id,
        clause_id=clause.id,
        evidence_type="document",
        title=f"Evidence {n}",
        description="how this satisfies the clause",
        is_verified=False,
    )
    db.add(evidence)
    db.commit()
    db.refresh(evidence)
    return evidence


def _audit_rows(db: Session, *, resource_type: str, resource_id: int, action: str = None):
    """Fetch AuditLog rows for a resource, newest first, optionally filtered by action.

    ``expire_all`` first so rows committed through the endpoint's session (the
    same ``db_session`` the client overrides ``get_db`` with) are reloaded
    instead of served stale from the identity map.
    """
    db.expire_all()
    q = db.query(AuditLog).filter(
        AuditLog.resource_type == resource_type,
        AuditLog.resource_id == resource_id,
    )
    if action is not None:
        q = q.filter(AuditLog.action == action)
    return q.order_by(AuditLog.sequence_number.desc()).all()


def _committed_audit_rows(db: Session, *, resource_type: str, resource_id: int, action: str = None):
    """Fetch AuditLog rows that were actually COMMITTED, not merely flushed.

    This is the real guard against the production bug. The ``client`` fixture
    overrides ``get_db`` to yield this one shared, never-closed ``db_session``,
    so the endpoint and the test share a single open transaction. ``AuditService.log()``
    only ``flush()``es; the handler is responsible for the ``commit()``. If a handler
    called the audit helper AFTER ``db.commit()`` (the bug that was just fixed), the
    audit row would be flushed into a fresh, never-committed transaction -- yet a
    plain ``db.query(AuditLog)`` in the test would still SEE it, because the read
    happens inside that same open transaction. So a naive assertion passes against
    broken code.

    Rolling back BEFORE querying closes that loophole: a committed audit row
    survives the rollback (commit already ended its transaction), while a
    flushed-but-uncommitted one is discarded. The entity itself was committed by
    the handler in both the buggy and fixed versions, so only the audit row's
    durability is being probed here.
    """
    db.rollback()
    return _audit_rows(db, resource_type=resource_type, resource_id=resource_id, action=action)


# ---------------------------------------------------------------------------
# 1. Soft delete: success status, filtered from reads, row persists in DB
# ---------------------------------------------------------------------------


def test_delete_standard_is_soft_delete(client: TestClient, db_session: Session):
    """DELETE /qms-standards/{id}: 204, then GET 404, but the row remains in the
    DB flagged is_deleted=True with deleted_by set to the caller -- proving it is
    soft- not hard-deleted."""
    admin = _make_user(db_session, role=UserRole.ADMIN)
    standard = _make_standard(db_session)

    resp = client.delete(f"/api/v1/qms-standards/{standard.id}", headers=_headers_for(admin))
    assert resp.status_code == status.HTTP_204_NO_CONTENT, resp.text

    # Filtered out of reads.
    get_resp = client.get(f"/api/v1/qms-standards/{standard.id}", headers=_headers_for(admin))
    assert get_resp.status_code == status.HTTP_404_NOT_FOUND, get_resp.text

    # ...but the row STILL EXISTS in the DB, soft-flagged and attributed.
    db_session.expire_all()
    row = db_session.query(QMSStandard).filter(QMSStandard.id == standard.id).first()
    assert row is not None, "standard was hard-deleted -- expected a soft delete"
    assert row.is_deleted is True
    assert row.deleted_by == admin.id
    assert row.deleted_at is not None


def test_delete_clause_is_soft_delete(client: TestClient, db_session: Session):
    """DELETE /qms-standards/clauses/{id}: 204, GET (via list) no longer shows it,
    but the row persists with is_deleted=True / deleted_by=<caller>."""
    admin = _make_user(db_session, role=UserRole.ADMIN)
    standard = _make_standard(db_session)
    clause = _make_clause(db_session, standard=standard)

    resp = client.delete(f"/api/v1/qms-standards/clauses/{clause.id}", headers=_headers_for(admin))
    assert resp.status_code == status.HTTP_204_NO_CONTENT, resp.text

    # Gone from the standard's clause list.
    list_resp = client.get(f"/api/v1/qms-standards/{standard.id}/clauses", headers=_headers_for(admin))
    assert list_resp.status_code == status.HTTP_200_OK, list_resp.text
    assert clause.id not in {c["id"] for c in list_resp.json()}

    # Row persists, soft-flagged.
    db_session.expire_all()
    row = db_session.query(QMSClause).filter(QMSClause.id == clause.id).first()
    assert row is not None, "clause was hard-deleted -- expected a soft delete"
    assert row.is_deleted is True
    assert row.deleted_by == admin.id
    assert row.deleted_at is not None


def test_delete_evidence_is_soft_delete(client: TestClient, db_session: Session):
    """DELETE /qms-standards/evidence/{id}: 204, and the row persists with
    is_deleted=True / deleted_by=<caller>."""
    admin = _make_user(db_session, role=UserRole.ADMIN)
    standard = _make_standard(db_session)
    clause = _make_clause(db_session, standard=standard)
    evidence = _make_evidence(db_session, clause=clause)

    resp = client.delete(f"/api/v1/qms-standards/evidence/{evidence.id}", headers=_headers_for(admin))
    assert resp.status_code == status.HTTP_204_NO_CONTENT, resp.text

    db_session.expire_all()
    row = db_session.query(QMSClauseEvidence).filter(QMSClauseEvidence.id == evidence.id).first()
    assert row is not None, "evidence was hard-deleted -- expected a soft delete"
    assert row.is_deleted is True
    assert row.deleted_by == admin.id
    assert row.deleted_at is not None


# ---------------------------------------------------------------------------
# 2. Nested filtering: with_loader_criteria excludes soft-deleted children
# ---------------------------------------------------------------------------


def test_soft_deleted_clause_absent_from_standard_nested_payload(client: TestClient, db_session: Session):
    """After soft-deleting one of two clauses, GET /qms-standards/{id} returns the
    surviving clause only in its nested ``clauses`` -- guarding the
    with_loader_criteria fix on the eager-loaded children."""
    admin = _make_user(db_session, role=UserRole.ADMIN)
    standard = _make_standard(db_session)
    kept = _make_clause(db_session, standard=standard)
    deleted = _make_clause(db_session, standard=standard)

    del_resp = client.delete(f"/api/v1/qms-standards/clauses/{deleted.id}", headers=_headers_for(admin))
    assert del_resp.status_code == status.HTTP_204_NO_CONTENT, del_resp.text

    resp = client.get(f"/api/v1/qms-standards/{standard.id}", headers=_headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    nested_ids = {c["id"] for c in resp.json()["clauses"]}
    assert kept.id in nested_ids
    assert deleted.id not in nested_ids


def test_soft_deleted_evidence_absent_from_clauses_nested_payload(client: TestClient, db_session: Session):
    """After soft-deleting one of two evidence links, GET /qms-standards/{id}/clauses
    returns only the surviving evidence in the clause's nested ``evidence_links``
    -- guarding the with_loader_criteria fix on the nested evidence."""
    admin = _make_user(db_session, role=UserRole.ADMIN)
    standard = _make_standard(db_session)
    clause = _make_clause(db_session, standard=standard)
    kept = _make_evidence(db_session, clause=clause)
    deleted = _make_evidence(db_session, clause=clause)

    del_resp = client.delete(f"/api/v1/qms-standards/evidence/{deleted.id}", headers=_headers_for(admin))
    assert del_resp.status_code == status.HTTP_204_NO_CONTENT, del_resp.text

    resp = client.get(f"/api/v1/qms-standards/{standard.id}/clauses", headers=_headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    rows = resp.json()
    target = next(c for c in rows if c["id"] == clause.id)
    evidence_ids = {e["id"] for e in target["evidence_links"]}
    assert kept.id in evidence_ids
    assert deleted.id not in evidence_ids


# ---------------------------------------------------------------------------
# 3. Audit logging on CREATE
# ---------------------------------------------------------------------------


def test_create_standard_emits_create_audit(client: TestClient, db_session: Session):
    """POST /qms-standards/ emits a CREATE AuditLog row for resource_type
    'qms_standard', tagged with the caller's company."""
    admin = _make_user(db_session, role=UserRole.ADMIN)

    resp = client.post(
        "/api/v1/qms-standards/",
        headers=_headers_for(admin),
        json={"name": "AS9100D", "version": "Rev D"},
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    new_id = resp.json()["id"]

    # Require the audit row to be COMMITTED (rollback-then-query), not merely flushed.
    rows = _committed_audit_rows(db_session, resource_type="qms_standard", resource_id=new_id, action="CREATE")
    assert len(rows) == 1, "expected exactly one COMMITTED CREATE audit row for the new standard"
    assert rows[0].action == "CREATE"
    assert rows[0].resource_id == new_id
    assert rows[0].company_id == COMPANY_A


def test_create_clause_emits_create_audit(client: TestClient, db_session: Session):
    """POST /qms-standards/{id}/clauses emits a CREATE AuditLog row for
    resource_type 'qms_clause'."""
    admin = _make_user(db_session, role=UserRole.ADMIN)
    standard = _make_standard(db_session)

    resp = client.post(
        f"/api/v1/qms-standards/{standard.id}/clauses",
        headers=_headers_for(admin),
        json={"clause_number": "8.5.2", "title": "Identification and traceability"},
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    clause_id = resp.json()["id"]

    # Require the audit row to be COMMITTED (rollback-then-query), not merely flushed.
    rows = _committed_audit_rows(db_session, resource_type="qms_clause", resource_id=clause_id, action="CREATE")
    assert len(rows) == 1
    assert rows[0].action == "CREATE"
    assert rows[0].company_id == COMPANY_A


def test_add_evidence_emits_create_audit(client: TestClient, db_session: Session):
    """POST /qms-standards/clauses/{id}/evidence emits a CREATE AuditLog row for
    resource_type 'qms_clause_evidence'."""
    admin = _make_user(db_session, role=UserRole.ADMIN)
    standard = _make_standard(db_session)
    clause = _make_clause(db_session, standard=standard)

    resp = client.post(
        f"/api/v1/qms-standards/clauses/{clause.id}/evidence",
        headers=_headers_for(admin),
        json={"evidence_type": "document", "title": "Procedure QP-7.5"},
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    evidence_id = resp.json()["id"]

    # Require the audit row to be COMMITTED (rollback-then-query), not merely flushed.
    rows = _committed_audit_rows(
        db_session, resource_type="qms_clause_evidence", resource_id=evidence_id, action="CREATE"
    )
    assert len(rows) == 1
    assert rows[0].action == "CREATE"
    assert rows[0].company_id == COMPANY_A


# ---------------------------------------------------------------------------
# 4. Audit logging on DELETE
# ---------------------------------------------------------------------------


def test_delete_standard_emits_delete_audit(client: TestClient, db_session: Session):
    """DELETE /qms-standards/{id} emits a DELETE AuditLog row, company-tagged."""
    admin = _make_user(db_session, role=UserRole.ADMIN)
    standard = _make_standard(db_session)

    resp = client.delete(f"/api/v1/qms-standards/{standard.id}", headers=_headers_for(admin))
    assert resp.status_code == status.HTTP_204_NO_CONTENT, resp.text

    # Require the audit row to be COMMITTED (rollback-then-query), not merely flushed.
    rows = _committed_audit_rows(db_session, resource_type="qms_standard", resource_id=standard.id, action="DELETE")
    assert len(rows) == 1
    assert rows[0].action == "DELETE"
    assert rows[0].company_id == COMPANY_A


def test_delete_clause_emits_delete_audit(client: TestClient, db_session: Session):
    """DELETE /qms-standards/clauses/{id} emits a DELETE AuditLog row."""
    admin = _make_user(db_session, role=UserRole.ADMIN)
    standard = _make_standard(db_session)
    clause = _make_clause(db_session, standard=standard)

    resp = client.delete(f"/api/v1/qms-standards/clauses/{clause.id}", headers=_headers_for(admin))
    assert resp.status_code == status.HTTP_204_NO_CONTENT, resp.text

    # Require the audit row to be COMMITTED (rollback-then-query), not merely flushed.
    rows = _committed_audit_rows(db_session, resource_type="qms_clause", resource_id=clause.id, action="DELETE")
    assert len(rows) == 1
    assert rows[0].action == "DELETE"
    assert rows[0].company_id == COMPANY_A


def test_delete_evidence_emits_delete_audit(client: TestClient, db_session: Session):
    """DELETE /qms-standards/evidence/{id} emits a DELETE AuditLog row."""
    admin = _make_user(db_session, role=UserRole.ADMIN)
    standard = _make_standard(db_session)
    clause = _make_clause(db_session, standard=standard)
    evidence = _make_evidence(db_session, clause=clause)

    resp = client.delete(f"/api/v1/qms-standards/evidence/{evidence.id}", headers=_headers_for(admin))
    assert resp.status_code == status.HTTP_204_NO_CONTENT, resp.text

    # Require the audit row to be COMMITTED (rollback-then-query), not merely flushed.
    rows = _committed_audit_rows(
        db_session, resource_type="qms_clause_evidence", resource_id=evidence.id, action="DELETE"
    )
    assert len(rows) == 1
    assert rows[0].action == "DELETE"
    assert rows[0].company_id == COMPANY_A


# ---------------------------------------------------------------------------
# 5. STATUS_CHANGE on a compliance_status transition (headline behavior)
# ---------------------------------------------------------------------------


def test_update_clause_compliance_status_emits_status_change_audit(client: TestClient, db_session: Session):
    """PUT /qms-standards/clauses/{id} that moves compliance_status from
    not_assessed -> compliant emits a STATUS_CHANGE AuditLog row carrying the old
    and new status, company-tagged. This is the headline compliance behavior."""
    admin = _make_user(db_session, role=UserRole.ADMIN)
    standard = _make_standard(db_session)
    clause = _make_clause(db_session, standard=standard, compliance_status="not_assessed")

    resp = client.put(
        f"/api/v1/qms-standards/clauses/{clause.id}",
        headers=_headers_for(admin),
        json={"compliance_status": "compliant"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["compliance_status"] == "compliant"

    # Require the audit row to be COMMITTED (rollback-then-query), not merely flushed.
    rows = _committed_audit_rows(db_session, resource_type="qms_clause", resource_id=clause.id, action="STATUS_CHANGE")
    assert len(rows) == 1, "expected exactly one COMMITTED STATUS_CHANGE audit row"
    row = rows[0]
    assert row.action == "STATUS_CHANGE"
    assert row.company_id == COMPANY_A
    # old/new status are captured in the audit payload (log_status_change shape).
    assert row.old_values == {"status": "not_assessed"}
    assert row.new_values == {"status": "compliant"}


def test_update_clause_without_status_change_emits_no_status_change_audit(client: TestClient, db_session: Session):
    """A clause update that does NOT touch compliance_status produces no
    STATUS_CHANGE row (the status-change audit is gated on an actual transition)."""
    admin = _make_user(db_session, role=UserRole.ADMIN)
    standard = _make_standard(db_session)
    clause = _make_clause(db_session, standard=standard, compliance_status="not_assessed")

    resp = client.put(
        f"/api/v1/qms-standards/clauses/{clause.id}",
        headers=_headers_for(admin),
        json={"compliance_notes": "reviewed, no status change"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    status_rows = _audit_rows(db_session, resource_type="qms_clause", resource_id=clause.id, action="STATUS_CHANGE")
    assert status_rows == [], "no compliance_status transition occurred -- expected no STATUS_CHANGE row"


def test_update_clause_same_status_emits_no_status_change_audit(client: TestClient, db_session: Session):
    """Re-asserting the SAME compliance_status (compliant -> compliant) is not a
    transition and must not emit a STATUS_CHANGE row."""
    admin = _make_user(db_session, role=UserRole.ADMIN)
    standard = _make_standard(db_session)
    clause = _make_clause(db_session, standard=standard, compliance_status="compliant")

    resp = client.put(
        f"/api/v1/qms-standards/clauses/{clause.id}",
        headers=_headers_for(admin),
        json={"compliance_status": "compliant"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    status_rows = _audit_rows(db_session, resource_type="qms_clause", resource_id=clause.id, action="STATUS_CHANGE")
    assert status_rows == [], "status did not change -- expected no STATUS_CHANGE row"
