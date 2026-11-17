"""A QMS standard or evidence link may only name a document of the caller's active company.

``QMSStandard.document_id`` and ``QMSClauseEvidence.document_id`` are plain foreign keys to
``documents``, a table every tenant shares. The four routes that take the field from a request
body -- ``POST /qms-standards/`` (create standard), ``PUT /qms-standards/{id}`` (update
standard), ``POST /qms-standards/clauses/{id}/evidence`` (add evidence) and
``PUT /qms-standards/evidence/{id}`` (update evidence) -- wrote it unchecked. A nonexistent id
failed the FK at flush (a 500 on Postgres; SQLite, the test backend, does not enforce FKs and
answered 201/200), while any existing id, another company's included, answered 200/201 and was
linked: a cross-tenant existence oracle and a cross-tenant link (CLAUDE.md invariant 1).

Each route now resolves the id through ``_assert_company_document`` before it writes anything.
Proved here, per route:

* an own-company document links (and the write is audited as before);
* another company's document is refused 404 ``Document not found``, with no row written or
  changed and no audit row;
* a nonexistent id gets the byte-identical 404, so the two cannot be told apart;
* ``document_id: null`` on an update clears the link;
* an update that does not send ``document_id`` is not judged on the value already stored (the
  SPA's Verify sends ``{"is_verified": true}`` only).

"No audit row" is checked with no rollback first: the ``client`` fixture shares this one open
session with the endpoint, so a flushed-but-uncommitted audit row would be visible here too, and
the count must not move at all. "No write" is checked the same way: the audit query autoflushes
any attribute the route set, and ``refresh`` then reads what the database holds.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.document import Document, DocumentType
from app.models.qms_standard import QMSClause, QMSClauseEvidence, QMSStandard
from app.models.user import User, UserRole

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
REFUSAL = {"detail": "Document not found"}

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> None:
    if db.query(Company).filter(Company.id == company_id).first() is None:
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True))
        db.commit()


def _make_user(db: Session, *, company_id: int = COMPANY_A, role: UserRole = UserRole.QUALITY) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"qms-doclink-{n}@co{company_id}.test",
        employee_id=f"QMSDL-{n:05d}",
        first_name="Qms",
        last_name="DocLink",
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


def _headers_for(user: User, *, active_company_id: int = None) -> dict:
    """Auth headers. ``active_company_id`` mints the token for another company than the user's
    home one, the way a platform admin's switched context is simulated."""
    cid = active_company_id if active_company_id is not None else user.company_id
    token = create_access_token(subject=user.id, company_id=cid)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _make_document(db: Session, *, company_id: int) -> Document:
    _ensure_company(db, company_id)
    n = _next()
    document = Document(
        document_number=f"QMS-DOCLINK-{n}",
        title=f"Quality manual {n}",
        document_type=DocumentType.PROCEDURE,
        company_id=company_id,
    )
    db.add(document)
    db.commit()
    db.refresh(document)
    return document


def _missing_document_id(db: Session) -> int:
    """An id no document has, in any company."""
    return (db.query(func.max(Document.id)).scalar() or 0) + 100_000


def _make_standard(db: Session, *, company_id: int = COMPANY_A, document_id: int = None) -> QMSStandard:
    _ensure_company(db, company_id)
    n = _next()
    standard = QMSStandard(
        company_id=company_id,
        name=f"DOCLINK-STD-{n}",
        version="2016",
        is_active=True,
        document_id=document_id,
    )
    db.add(standard)
    db.commit()
    db.refresh(standard)
    return standard


def _make_clause(db: Session, *, company_id: int = COMPANY_A) -> QMSClause:
    standard = _make_standard(db, company_id=company_id)
    n = _next()
    clause = QMSClause(
        company_id=company_id,
        standard_id=standard.id,
        clause_number=f"{n}.1",
        title=f"Clause {n}",
        compliance_status="not_assessed",
    )
    db.add(clause)
    db.commit()
    db.refresh(clause)
    return clause


def _make_evidence(db: Session, *, company_id: int = COMPANY_A, document_id: int = None) -> QMSClauseEvidence:
    clause = _make_clause(db, company_id=company_id)
    n = _next()
    evidence = QMSClauseEvidence(
        company_id=company_id,
        clause_id=clause.id,
        evidence_type="document",
        title=f"Doc-link evidence {n}",
        is_verified=False,
        document_id=document_id,
    )
    db.add(evidence)
    db.commit()
    db.refresh(evidence)
    return evidence


def _audit_count(db: Session) -> int:
    """Every audit row the shared session can see, flushed or committed (no rollback first)."""
    return db.query(func.count(AuditLog.id)).scalar()


def _committed_audit_rows(db: Session, *, resource_type: str, resource_id: int, action: str):
    """Audit rows for one resource that were COMMITTED: the rollback drops flush-only rows."""
    db.rollback()
    db.expire_all()
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.resource_type == resource_type,
            AuditLog.resource_id == resource_id,
            AuditLog.action == action,
        )
        .all()
    )


# ---------------------------------------------------------------------------
# POST /qms-standards/clauses/{clause_id}/evidence
# ---------------------------------------------------------------------------


def test_add_evidence_links_own_company_document(client: TestClient, db_session: Session):
    clause = _make_clause(db_session)
    document = _make_document(db_session, company_id=COMPANY_A)
    user = _make_user(db_session)

    resp = client.post(
        f"/api/v1/qms-standards/clauses/{clause.id}/evidence",
        headers=_headers_for(user),
        json={"evidence_type": "document", "title": "Quality manual", "document_id": document.id},
    )

    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    body = resp.json()
    assert body["document_id"] == document.id
    assert _committed_audit_rows(
        db_session, resource_type="qms_clause_evidence", resource_id=body["id"], action="CREATE"
    ), "the link is audited as before"


@pytest.mark.parametrize("which", ["foreign", "missing"])
def test_add_evidence_refuses_foreign_or_missing_document(client: TestClient, db_session: Session, which: str):
    clause = _make_clause(db_session)
    user = _make_user(db_session)
    if which == "foreign":
        document_id = _make_document(db_session, company_id=COMPANY_B).id
    else:
        document_id = _missing_document_id(db_session)
    evidence_before = db_session.query(func.count(QMSClauseEvidence.id)).scalar()
    audit_before = _audit_count(db_session)

    resp = client.post(
        f"/api/v1/qms-standards/clauses/{clause.id}/evidence",
        headers=_headers_for(user),
        json={"evidence_type": "document", "title": "Cross-tenant link", "document_id": document_id},
    )

    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json() == REFUSAL
    assert _audit_count(db_session) == audit_before, "a refused link writes no audit row"
    assert db_session.query(func.count(QMSClauseEvidence.id)).scalar() == evidence_before, "no evidence row"


def test_add_evidence_foreign_and_missing_answers_are_identical(client: TestClient, db_session: Session):
    """The oracle the check closes: another company's id and an unused id read the same."""
    clause = _make_clause(db_session)
    user = _make_user(db_session)
    foreign_id = _make_document(db_session, company_id=COMPANY_B).id
    missing_id = _missing_document_id(db_session)

    answers = [
        client.post(
            f"/api/v1/qms-standards/clauses/{clause.id}/evidence",
            headers=_headers_for(user),
            json={"evidence_type": "document", "title": "Probe", "document_id": document_id},
        )
        for document_id in (foreign_id, missing_id)
    ]

    assert [a.status_code for a in answers] == [status.HTTP_404_NOT_FOUND] * 2
    assert answers[0].content == answers[1].content


def test_add_evidence_judges_the_active_company_not_the_home_one(client: TestClient, db_session: Session):
    """A platform admin whose home company is A, switched into B, links evidence on B's clause.

    The check must use the active company (``get_current_company_id``), never
    ``current_user.company_id``: A's document is refused there, B's is accepted. Every other test
    here has home == active, so a check reading the home company would pass all of them."""
    clause_b = _make_clause(db_session, company_id=COMPANY_B)
    document_a = _make_document(db_session, company_id=COMPANY_A)
    document_b = _make_document(db_session, company_id=COMPANY_B)
    platform_admin = _make_user(db_session, company_id=COMPANY_A, role=UserRole.PLATFORM_ADMIN)
    headers = _headers_for(platform_admin, active_company_id=COMPANY_B)
    audit_before = _audit_count(db_session)

    refused = client.post(
        f"/api/v1/qms-standards/clauses/{clause_b.id}/evidence",
        headers=headers,
        json={"evidence_type": "document", "title": "Home-company document", "document_id": document_a.id},
    )
    assert refused.status_code == status.HTTP_404_NOT_FOUND, refused.text
    assert refused.json() == REFUSAL
    assert _audit_count(db_session) == audit_before

    accepted = client.post(
        f"/api/v1/qms-standards/clauses/{clause_b.id}/evidence",
        headers=headers,
        json={"evidence_type": "document", "title": "Active-company document", "document_id": document_b.id},
    )
    assert accepted.status_code == status.HTTP_201_CREATED, accepted.text
    assert accepted.json()["document_id"] == document_b.id


# ---------------------------------------------------------------------------
# PUT /qms-standards/evidence/{evidence_id}
# ---------------------------------------------------------------------------


def test_update_evidence_links_own_company_document(client: TestClient, db_session: Session):
    evidence = _make_evidence(db_session)
    document = _make_document(db_session, company_id=COMPANY_A)
    user = _make_user(db_session)

    resp = client.put(
        f"/api/v1/qms-standards/evidence/{evidence.id}",
        headers=_headers_for(user),
        json={"document_id": document.id},
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["document_id"] == document.id
    assert _committed_audit_rows(
        db_session, resource_type="qms_clause_evidence", resource_id=evidence.id, action="UPDATE"
    )


@pytest.mark.parametrize("which", ["foreign", "missing", "zero"])
def test_update_evidence_refuses_foreign_or_missing_document(client: TestClient, db_session: Session, which: str):
    """Refused before any attribute is set: the Verify sent alongside it does not land either.

    ``zero``: ``QMSEvidenceUpdate.document_id`` carries no ``gt=0`` (the create schema does), so
    0 reaches the route and gets the same answer as any other id that names no document."""
    own_document = _make_document(db_session, company_id=COMPANY_A)
    evidence = _make_evidence(db_session, document_id=own_document.id)
    user = _make_user(db_session)
    if which == "foreign":
        document_id = _make_document(db_session, company_id=COMPANY_B).id
    elif which == "missing":
        document_id = _missing_document_id(db_session)
    else:
        document_id = 0
    audit_before = _audit_count(db_session)

    resp = client.put(
        f"/api/v1/qms-standards/evidence/{evidence.id}",
        headers=_headers_for(user),
        json={"document_id": document_id, "is_verified": True, "verification_notes": "looks right"},
    )

    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json() == REFUSAL
    assert _audit_count(db_session) == audit_before, "a refused edit writes no audit row"
    db_session.refresh(evidence)
    assert evidence.document_id == own_document.id
    assert evidence.is_verified is False
    assert evidence.verified_by is None
    assert evidence.verification_notes is None


def test_update_evidence_foreign_and_missing_answers_are_identical(client: TestClient, db_session: Session):
    evidence = _make_evidence(db_session)
    user = _make_user(db_session)
    foreign_id = _make_document(db_session, company_id=COMPANY_B).id
    missing_id = _missing_document_id(db_session)

    answers = [
        client.put(
            f"/api/v1/qms-standards/evidence/{evidence.id}",
            headers=_headers_for(user),
            json={"document_id": document_id},
        )
        for document_id in (foreign_id, missing_id)
    ]

    assert [a.status_code for a in answers] == [status.HTTP_404_NOT_FOUND] * 2
    assert answers[0].content == answers[1].content


def test_update_evidence_null_clears_the_link(client: TestClient, db_session: Session):
    document = _make_document(db_session, company_id=COMPANY_A)
    evidence = _make_evidence(db_session, document_id=document.id)
    user = _make_user(db_session)

    resp = client.put(
        f"/api/v1/qms-standards/evidence/{evidence.id}",
        headers=_headers_for(user),
        json={"document_id": None},
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["document_id"] is None
    db_session.refresh(evidence)
    assert evidence.document_id is None
    assert _committed_audit_rows(
        db_session, resource_type="qms_clause_evidence", resource_id=evidence.id, action="UPDATE"
    )


def test_update_evidence_without_document_id_is_not_rejudged(client: TestClient, db_session: Session):
    """Only a supplied ``document_id`` is judged. The SPA's Verify sends ``is_verified`` alone,
    so it is unaffected even on a row that holds a link the check would now refuse."""
    foreign_document = _make_document(db_session, company_id=COMPANY_B)
    evidence = _make_evidence(db_session, document_id=foreign_document.id)
    user = _make_user(db_session)

    resp = client.put(
        f"/api/v1/qms-standards/evidence/{evidence.id}",
        headers=_headers_for(user),
        json={"is_verified": True},
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["is_verified"] is True


# ---------------------------------------------------------------------------
# POST /qms-standards/ and PUT /qms-standards/{standard_id}
# ---------------------------------------------------------------------------


def test_create_standard_links_own_company_document(client: TestClient, db_session: Session):
    document = _make_document(db_session, company_id=COMPANY_A)
    user = _make_user(db_session)

    resp = client.post(
        "/api/v1/qms-standards/",
        headers=_headers_for(user),
        json={"name": f"DOCLINK-NEW-{_next()}", "version": "2016", "document_id": document.id},
    )

    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    assert resp.json()["document_id"] == document.id
    assert _committed_audit_rows(
        db_session, resource_type="qms_standard", resource_id=resp.json()["id"], action="CREATE"
    ), "the link is audited as before"


@pytest.mark.parametrize("which", ["foreign", "missing"])
def test_create_standard_refuses_foreign_or_missing_document(client: TestClient, db_session: Session, which: str):
    user = _make_user(db_session)
    if which == "foreign":
        document_id = _make_document(db_session, company_id=COMPANY_B).id
    else:
        document_id = _missing_document_id(db_session)
    name = f"DOCLINK-REFUSED-{_next()}"
    audit_before = _audit_count(db_session)

    resp = client.post(
        "/api/v1/qms-standards/",
        headers=_headers_for(user),
        json={"name": name, "version": "2016", "document_id": document_id},
    )

    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json() == REFUSAL
    assert _audit_count(db_session) == audit_before
    assert db_session.query(QMSStandard).filter(QMSStandard.name == name).count() == 0


@pytest.mark.parametrize("which", ["foreign", "missing"])
def test_update_standard_refuses_foreign_or_missing_document(client: TestClient, db_session: Session, which: str):
    standard = _make_standard(db_session)
    original_name = standard.name
    user = _make_user(db_session)
    if which == "foreign":
        document_id = _make_document(db_session, company_id=COMPANY_B).id
    else:
        document_id = _missing_document_id(db_session)
    audit_before = _audit_count(db_session)

    resp = client.put(
        f"/api/v1/qms-standards/{standard.id}",
        headers=_headers_for(user),
        json={"name": "Renamed alongside a cross-tenant link", "document_id": document_id},
    )

    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json() == REFUSAL
    assert _audit_count(db_session) == audit_before
    db_session.refresh(standard)
    assert standard.document_id is None
    assert standard.name == original_name


def test_update_standard_links_own_and_null_clears(client: TestClient, db_session: Session):
    standard = _make_standard(db_session)
    document = _make_document(db_session, company_id=COMPANY_A)
    user = _make_user(db_session)

    linked = client.put(
        f"/api/v1/qms-standards/{standard.id}",
        headers=_headers_for(user),
        json={"document_id": document.id},
    )
    assert linked.status_code == status.HTTP_200_OK, linked.text
    assert linked.json()["document_id"] == document.id

    cleared = client.put(
        f"/api/v1/qms-standards/{standard.id}",
        headers=_headers_for(user),
        json={"document_id": None},
    )
    assert cleared.status_code == status.HTTP_200_OK, cleared.text
    assert cleared.json()["document_id"] is None
