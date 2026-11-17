"""Required audit evidence on the QMS evidence routes.

``POST /qms-standards/clauses/{id}/evidence`` (Link), ``PUT /qms-standards/evidence/{id}`` (edit /
Verify) and ``DELETE /qms-standards/evidence/{id}`` (Remove) used to call ``audit.log_create`` /
``log_update`` / ``log_delete``, which are best-effort: a retry-exhausted sequence collision or a
Python-side failure inside ``AuditService.log`` dropped the audit row and the route still
answered 201 / 200 / 204. A Verify is a quality judgment whose only history is its audit row (the
evidence row keeps only the latest ``verified_by`` / ``verified_date``), so all three routes now
write their row through ``log_required`` inside ``app.api.audit_guard.required_audit``: no audit
row, no change -- rollback and 503.

Pinned here:

1. the happy path still writes exactly one committed row, JSON-ready: the CREATE row's
   ``new_values``, the UPDATE row's ``extra_data.changes`` diff and the DELETE row's
   ``extra_data.soft_delete`` + pre-delete ``old_values``; a re-Verify and an un-verify each
   write their own row (the history the row itself does not keep), and a row written under an API
   token carries the credential marker;
2. a PUT that changes nothing writes no row (and so cannot fail on one);
3. a failed audit write (``log`` returns ``None``, or raises a database error) answers 503 and
   leaves the evidence exactly as it was (or, for a Link, not created), with no audit row;
4. an evidence title longer than ``audit_log.resource_identifier`` (String(255)) is cut to fit
   in the identifier, so a long title cannot make every add, edit or removal of that evidence a
   503 on PostgreSQL.

Every read of the outcome rolls back first: the ``client`` fixture shares one session with the
routes, so a flushed-but-uncommitted row (or change) would otherwise look saved.
"""

from datetime import datetime
from typing import List

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.qms_standard import QMSClause, QMSClauseEvidence, QMSStandard
from app.models.user import User, UserRole
from app.services.audit_service import AuditService

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
API_TOKENS_URL = "/api/v1/api-tokens/"

POST_503_DETAIL = "The audit record for linking this evidence could not be saved; the evidence was not linked. Retry."
PUT_503_DETAIL = "The audit record for this evidence change could not be saved; nothing was changed. Retry."
DELETE_503_DETAIL = (
    "The audit record for removing this evidence could not be saved; the evidence was not removed. Retry."
)

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _make_user(db: Session, *, role: UserRole = UserRole.QUALITY) -> User:
    n = _next()
    user = User(
        email=f"qms-reqaudit-{n}@co{COMPANY_A}.test",
        employee_id=f"QMSRA-{n:05d}",
        first_name="Qms",
        last_name="Audit",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",  # tokens are minted directly; never used for login
        role=role,
        is_active=True,
        is_superuser=False,
        company_id=COMPANY_A,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _make_evidence(db: Session, *, title: str = "", notes: str = "") -> QMSClauseEvidence:
    n = _next()
    if db.query(Company).filter(Company.id == COMPANY_A).first() is None:
        db.add(Company(id=COMPANY_A, name=f"Company {COMPANY_A}", slug=f"company-{COMPANY_A}", is_active=True))
        db.commit()
    standard = QMSStandard(company_id=COMPANY_A, name=f"REQAUDIT-STD-{n}", version="2016", is_active=True)
    db.add(standard)
    db.flush()
    clause = QMSClause(
        company_id=COMPANY_A,
        standard_id=standard.id,
        clause_number=f"{n}.1",
        title=f"Clause {n}",
        compliance_status="not_assessed",
    )
    db.add(clause)
    db.flush()
    evidence = QMSClauseEvidence(
        company_id=COMPANY_A,
        clause_id=clause.id,
        evidence_type="module",
        title=title or f"Required-audit evidence {n}",
        verification_notes=notes or None,
        is_verified=False,
        is_auto_linked=False,
    )
    db.add(evidence)
    db.commit()
    db.refresh(evidence)
    return evidence


def _add_url(clause_id: int) -> str:
    return f"/api/v1/qms-standards/clauses/{clause_id}/evidence"


def _committed_links(db: Session, clause_id: int, title: str) -> List[QMSClauseEvidence]:
    """Evidence rows COMMITTED on the clause under this title (roll back first, then read)."""
    db.rollback()
    return (
        db.query(QMSClauseEvidence)
        .filter(QMSClauseEvidence.clause_id == clause_id, QMSClauseEvidence.title == title)
        .all()
    )


def _url(evidence_id: int) -> str:
    return f"/api/v1/qms-standards/evidence/{evidence_id}"


def _committed_audit_rows(db: Session, evidence_id: int, action: str) -> List[AuditLog]:
    """Audit rows that were COMMITTED for this evidence row (roll back first, then read)."""
    db.rollback()
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.resource_type == "qms_clause_evidence",
            AuditLog.resource_id == evidence_id,
            AuditLog.action == action,
        )
        .all()
    )


def _committed_evidence(db: Session, evidence_id: int) -> QMSClauseEvidence:
    db.rollback()
    db.expire_all()
    return db.get(QMSClauseEvidence, evidence_id)


def _audit_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """The best-effort failure ``log_required`` exists to catch: no row, no exception."""
    monkeypatch.setattr(AuditService, "log", lambda self, *args, **kwargs: None)


def _audit_raises_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A database error ``log`` re-raises (on PostgreSQL it has aborted the transaction)."""

    def boom(self, *args, **kwargs):
        raise OperationalError("INSERT INTO audit_log ...", {}, Exception("simulated audit write failure"))

    monkeypatch.setattr(AuditService, "log", boom)


# ---------------------------------------------------------------------------
# 1. Happy path: one committed, JSON-ready row
# ---------------------------------------------------------------------------


def test_verify_writes_one_required_update_row_with_the_changes_diff(client: TestClient, db_session: Session):
    user = _make_user(db_session)
    evidence = _make_evidence(db_session)
    evidence_id, title = evidence.id, evidence.title

    resp = client.put(_url(evidence_id), json={"is_verified": True}, headers=_headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["is_verified"] is True

    rows = _committed_audit_rows(db_session, evidence_id, "UPDATE")
    assert len(rows) == 1
    row = rows[0]
    assert row.company_id == COMPANY_A
    assert row.user_id == user.id
    assert row.resource_identifier == title
    assert row.description == f"Updated qms_clause_evidence: {title}"

    changes = row.extra_data["changes"]
    assert changes["is_verified"] == {"old": False, "new": True}
    assert changes["verified_by"] == {"old": None, "new": user.id}
    assert changes["verified_date"]["old"] is None
    # JSON-ready: the datetime is stored as an ISO string, not a bound datetime.
    assert isinstance(changes["verified_date"]["new"], str)
    datetime.fromisoformat(changes["verified_date"]["new"])
    # Columns the request did not touch are not in the diff.
    assert "title" not in changes
    assert "clause_id" not in changes

    # Full before / after images, as ``log_update`` stored them.
    assert row.old_values["is_verified"] is False
    assert row.old_values["title"] == title
    assert row.new_values["is_verified"] is True
    assert row.new_values["verified_by"] == user.id
    assert isinstance(row.new_values["created_at"], str)

    stored = _committed_evidence(db_session, evidence_id)
    assert stored.is_verified is True
    assert stored.verified_by == user.id


def test_notes_edit_writes_one_required_update_row(client: TestClient, db_session: Session):
    user = _make_user(db_session)
    evidence = _make_evidence(db_session, notes="first look")
    evidence_id = evidence.id

    resp = client.put(_url(evidence_id), json={"verification_notes": "second look"}, headers=_headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    rows = _committed_audit_rows(db_session, evidence_id, "UPDATE")
    assert len(rows) == 1
    assert rows[0].extra_data["changes"]["verification_notes"] == {"old": "first look", "new": "second look"}
    assert "is_verified" not in rows[0].extra_data["changes"]


def test_remove_writes_one_required_delete_row_with_the_pre_delete_image(client: TestClient, db_session: Session):
    user = _make_user(db_session)
    evidence = _make_evidence(db_session)
    evidence_id, title = evidence.id, evidence.title

    resp = client.delete(_url(evidence_id), headers=_headers_for(user))
    assert resp.status_code == status.HTTP_204_NO_CONTENT, resp.text

    rows = _committed_audit_rows(db_session, evidence_id, "DELETE")
    assert len(rows) == 1
    row = rows[0]
    assert row.company_id == COMPANY_A
    assert row.user_id == user.id
    assert row.resource_identifier == title
    assert row.description == f"Soft Deleted qms_clause_evidence: {title}"
    assert row.extra_data["soft_delete"] is True
    # The row as it stood BEFORE the delete, JSON-ready.
    assert row.old_values["is_deleted"] is False
    assert row.old_values["deleted_at"] is None
    assert row.old_values["deleted_by"] is None
    assert row.old_values["title"] == title
    assert isinstance(row.old_values["created_at"], str)
    assert row.new_values is None

    stored = _committed_evidence(db_session, evidence_id)
    assert stored.is_deleted is True
    assert stored.deleted_by == user.id


def test_reverify_writes_its_own_row_with_the_judgment_it_replaced(client: TestClient, db_session: Session):
    """The evidence row keeps only the latest Verify; the audit rows are the history of each one."""
    first = _make_user(db_session)
    second = _make_user(db_session)
    evidence = _make_evidence(db_session)
    evidence_id = evidence.id

    assert client.put(_url(evidence_id), json={"is_verified": True}, headers=_headers_for(first)).status_code == 200
    resp = client.put(_url(evidence_id), json={"is_verified": True}, headers=_headers_for(second))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    rows = sorted(_committed_audit_rows(db_session, evidence_id, "UPDATE"), key=lambda r: r.id)
    assert len(rows) == 2
    first_date = rows[0].extra_data["changes"]["verified_date"]["new"]
    changes = rows[1].extra_data["changes"]
    assert rows[1].user_id == second.id
    assert changes["verified_by"] == {"old": first.id, "new": second.id}
    assert changes["verified_date"]["old"] == first_date
    assert "is_verified" not in changes  # it was already true


def test_unverify_writes_a_row(client: TestClient, db_session: Session):
    user = _make_user(db_session)
    evidence = _make_evidence(db_session)
    evidence_id = evidence.id

    assert client.put(_url(evidence_id), json={"is_verified": True}, headers=_headers_for(user)).status_code == 200
    resp = client.put(_url(evidence_id), json={"is_verified": False}, headers=_headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    rows = sorted(_committed_audit_rows(db_session, evidence_id, "UPDATE"), key=lambda r: r.id)
    assert len(rows) == 2
    changes = rows[1].extra_data["changes"]
    assert changes["is_verified"] == {"old": True, "new": False}
    # An un-verify leaves who verified it, and when, on the row; the audit row says it was undone.
    assert "verified_by" not in changes
    assert rows[1].new_values["verified_by"] == user.id


@pytest.mark.parametrize("verb", ["put", "delete"])
def test_row_written_under_an_api_token_carries_the_credential_marker(
    verb: str, client: TestClient, db_session: Session
):
    admin = _make_user(db_session, role=UserRole.ADMIN)
    holder = _make_user(db_session)
    issued = client.post(API_TOKENS_URL, json={"user_id": holder.id, "label": "qms probe"}, headers=_headers_for(admin))
    assert issued.status_code == status.HTTP_201_CREATED, issued.text
    token_id = issued.json()["id"]
    token_headers = {"Authorization": f"Bearer {issued.json()['token']}", "X-Requested-With": "XMLHttpRequest"}
    evidence = _make_evidence(db_session)
    evidence_id = evidence.id

    if verb == "put":
        resp = client.put(_url(evidence_id), json={"is_verified": True}, headers=token_headers)
        action = "UPDATE"
    else:
        resp = client.delete(_url(evidence_id), headers=token_headers)
        action = "DELETE"
    assert resp.status_code in (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT), resp.text

    rows = _committed_audit_rows(db_session, evidence_id, action)
    assert len(rows) == 1
    assert rows[0].user_id == holder.id
    assert rows[0].extra_data["credential"]["kind"] == "api_token"
    assert rows[0].extra_data["credential"]["api_token_id"] == token_id
    # The route's own extra_data survives alongside the marker.
    if verb == "put":
        assert rows[0].extra_data["changes"]["is_verified"] == {"old": False, "new": True}
    else:
        assert rows[0].extra_data["soft_delete"] is True


# ---------------------------------------------------------------------------
# 2. A PUT that changes nothing writes no row, and so cannot fail on one
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="empty-body"),
        pytest.param({"is_verified": False}, id="unverify-an-unverified-row"),
        pytest.param({"verification_notes": "as it was"}, id="same-notes"),
    ],
)
def test_put_that_changes_nothing_writes_no_row(
    body: dict, client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    user = _make_user(db_session)
    evidence = _make_evidence(db_session, notes="as it was")
    evidence_id = evidence.id

    # If the route tried to write a row it would get None and answer 503: a 200 here proves it
    # skipped the row, as ``log_update`` did.
    with monkeypatch.context() as mp:
        _audit_returns_none(mp)
        resp = client.put(_url(evidence_id), json=body, headers=_headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    assert _committed_audit_rows(db_session, evidence_id, "UPDATE") == []
    stored = _committed_evidence(db_session, evidence_id)
    assert stored.is_verified is False
    assert stored.verification_notes == "as it was"


# ---------------------------------------------------------------------------
# 3. A failed audit write: 503, nothing saved, no row
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("break_audit", [_audit_returns_none, _audit_raises_db_error], ids=["none", "db-error"])
def test_verify_without_its_audit_row_is_503_and_nothing_is_saved(
    break_audit, client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    user = _make_user(db_session)
    evidence = _make_evidence(db_session, notes="before")
    evidence_id = evidence.id

    with monkeypatch.context() as mp:
        break_audit(mp)
        resp = client.put(
            _url(evidence_id),
            json={"is_verified": True, "verification_notes": "after"},
            headers=_headers_for(user),
        )
    assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE, resp.text
    assert resp.json()["detail"] == PUT_503_DETAIL

    stored = _committed_evidence(db_session, evidence_id)
    assert stored.is_verified is False
    assert stored.verified_by is None
    assert stored.verified_date is None
    assert stored.verification_notes == "before"
    assert _committed_audit_rows(db_session, evidence_id, "UPDATE") == []

    # Safe to retry: with the audit working again the same request goes through, once.
    retry = client.put(
        _url(evidence_id),
        json={"is_verified": True, "verification_notes": "after"},
        headers=_headers_for(user),
    )
    assert retry.status_code == status.HTTP_200_OK, retry.text
    rows = _committed_audit_rows(db_session, evidence_id, "UPDATE")
    assert len(rows) == 1
    assert rows[0].extra_data["changes"]["is_verified"] == {"old": False, "new": True}


@pytest.mark.parametrize("break_audit", [_audit_returns_none, _audit_raises_db_error], ids=["none", "db-error"])
def test_remove_without_its_audit_row_is_503_and_the_evidence_stays_linked(
    break_audit, client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    user = _make_user(db_session)
    evidence = _make_evidence(db_session)
    evidence_id = evidence.id

    with monkeypatch.context() as mp:
        break_audit(mp)
        resp = client.delete(_url(evidence_id), headers=_headers_for(user))
    assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE, resp.text
    assert resp.json()["detail"] == DELETE_503_DETAIL

    stored = _committed_evidence(db_session, evidence_id)
    assert stored.is_deleted is False
    assert stored.deleted_at is None
    assert stored.deleted_by is None
    assert _committed_audit_rows(db_session, evidence_id, "DELETE") == []

    # Safe to retry: with the audit working again the same request goes through.
    retry = client.delete(_url(evidence_id), headers=_headers_for(user))
    assert retry.status_code == status.HTTP_204_NO_CONTENT, retry.text
    assert len(_committed_audit_rows(db_session, evidence_id, "DELETE")) == 1


# ---------------------------------------------------------------------------
# 4. A long title is cut to fit the audit identifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["put", "delete"])
def test_title_longer_than_the_audit_identifier_is_cut_to_fit(verb: str, client: TestClient, db_session: Session):
    """``audit_log.resource_identifier`` is String(255); the evidence schemas allow a 500-char title.

    SQLite does not enforce the length, so this pins the cut itself: on PostgreSQL the uncut
    identifier fails the INSERT, which a required row turns into a 503 on every edit of the row.
    """
    user = _make_user(db_session)
    long_title = "L" * 300 + f" {_next()}"
    evidence = _make_evidence(db_session, title=long_title)
    evidence_id = evidence.id

    if verb == "put":
        resp = client.put(_url(evidence_id), json={"is_verified": True}, headers=_headers_for(user))
        action = "UPDATE"
    else:
        resp = client.delete(_url(evidence_id), headers=_headers_for(user))
        action = "DELETE"
    assert resp.status_code in (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT), resp.text

    rows = _committed_audit_rows(db_session, evidence_id, action)
    assert len(rows) == 1
    assert rows[0].resource_identifier == long_title[:255]
    assert len(rows[0].resource_identifier) == 255
    # The description and the before image keep the whole title.
    assert long_title in rows[0].description
    assert rows[0].old_values["title"] == long_title


@pytest.mark.parametrize("extra", [300, 0], ids=["long-title", "short-title"])
def test_add_evidence_cuts_the_audit_identifier_and_keeps_the_whole_title(
    extra: int, client: TestClient, db_session: Session
):
    """``POST /clauses/{id}/evidence`` takes a 500-char title; its CREATE row's identifier fits 255.

    On PostgreSQL an uncut identifier fails the audit INSERT, so adding a valid long-titled link
    answered 500 (and, with the row now required, would answer 503). SQLite does not enforce the
    length, so this pins the cut; the short-title case pins that nothing else moved.
    """
    user = _make_user(db_session)
    clause_id = _make_evidence(db_session).clause_id
    title = ("A" * extra) + f" new link {_next()}"

    resp = client.post(
        f"/api/v1/qms-standards/clauses/{clause_id}/evidence",
        json={"evidence_type": "module", "title": title},
        headers=_headers_for(user),
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    evidence_id = resp.json()["id"]
    assert resp.json()["title"] == title

    rows = _committed_audit_rows(db_session, evidence_id, "CREATE")
    assert len(rows) == 1
    assert rows[0].resource_identifier == title[:255]
    assert rows[0].description == f"Created qms_clause_evidence: {title}"
    assert rows[0].new_values["title"] == title


# ---------------------------------------------------------------------------
# 5. Link (POST): the CREATE row is required too
# ---------------------------------------------------------------------------


def test_link_writes_one_required_create_row(client: TestClient, db_session: Session):
    user = _make_user(db_session)
    clause_id = _make_evidence(db_session).clause_id
    title = f"Linked by hand {_next()}"

    resp = client.post(
        _add_url(clause_id),
        json={"evidence_type": "procedure", "title": title, "module_reference": "/quality/ncr"},
        headers=_headers_for(user),
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    evidence_id = resp.json()["id"]

    rows = _committed_audit_rows(db_session, evidence_id, "CREATE")
    assert len(rows) == 1
    row = rows[0]
    assert row.company_id == COMPANY_A
    assert row.user_id == user.id
    assert row.resource_identifier == title
    assert row.description == f"Created qms_clause_evidence: {title}"
    assert row.old_values is None
    # The row as created, JSON-ready (as ``log_create`` stored it): every column, no more, no less.
    assert set(row.new_values) == {attr.key for attr in sa_inspect(QMSClauseEvidence).mapper.column_attrs}
    assert row.new_values["id"] == evidence_id
    assert row.new_values["clause_id"] == clause_id
    assert row.new_values["title"] == title
    assert row.new_values["evidence_type"] == "procedure"
    assert row.new_values["module_reference"] == "/quality/ncr"
    assert row.new_values["created_by"] == user.id
    assert row.new_values["company_id"] == COMPANY_A
    assert isinstance(row.new_values["created_at"], str)
    datetime.fromisoformat(row.new_values["created_at"])
    assert not (row.extra_data or {}).get("credential")

    assert len(_committed_links(db_session, clause_id, title)) == 1


@pytest.mark.parametrize("break_audit", [_audit_returns_none, _audit_raises_db_error], ids=["none", "db-error"])
def test_link_without_its_audit_row_is_503_and_nothing_is_linked(
    break_audit, client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    user = _make_user(db_session)
    clause_id = _make_evidence(db_session).clause_id
    title = f"Never linked {_next()}"
    body = {"evidence_type": "other", "title": title}

    with monkeypatch.context() as mp:
        break_audit(mp)
        resp = client.post(_add_url(clause_id), json=body, headers=_headers_for(user))
    assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE, resp.text
    assert resp.json()["detail"] == POST_503_DETAIL

    assert _committed_links(db_session, clause_id, title) == []
    assert (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == "qms_clause_evidence", AuditLog.resource_identifier == title)
        .all()
        == []
    )

    # Safe to retry: with the audit working again the same request links it once, with its row.
    retry = client.post(_add_url(clause_id), json=body, headers=_headers_for(user))
    assert retry.status_code == status.HTTP_201_CREATED, retry.text
    links = _committed_links(db_session, clause_id, title)
    assert [link.id for link in links] == [retry.json()["id"]]
    assert len(_committed_audit_rows(db_session, links[0].id, "CREATE")) == 1


def test_link_under_an_api_token_carries_the_credential_marker(client: TestClient, db_session: Session):
    admin = _make_user(db_session, role=UserRole.ADMIN)
    holder = _make_user(db_session)
    issued = client.post(API_TOKENS_URL, json={"user_id": holder.id, "label": "qms link"}, headers=_headers_for(admin))
    assert issued.status_code == status.HTTP_201_CREATED, issued.text
    token_id = issued.json()["id"]
    token_headers = {"Authorization": f"Bearer {issued.json()['token']}", "X-Requested-With": "XMLHttpRequest"}
    clause_id = _make_evidence(db_session).clause_id

    resp = client.post(
        _add_url(clause_id),
        json={"evidence_type": "module", "title": f"Token link {_next()}"},
        headers=token_headers,
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text

    rows = _committed_audit_rows(db_session, resp.json()["id"], "CREATE")
    assert len(rows) == 1
    assert rows[0].user_id == holder.id
    assert rows[0].extra_data["credential"]["kind"] == "api_token"
    assert rows[0].extra_data["credential"]["api_token_id"] == token_id
