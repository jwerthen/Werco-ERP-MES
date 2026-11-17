"""Row lock and lock order on the QMS evidence edit routes.

``PUT /qms-standards/evidence/{id}`` (edit / Verify) and ``DELETE /qms-standards/evidence/{id}``
(Remove) used to read the evidence row without a lock and then call ``audit.log_update`` /
``audit.log_delete``. ``AuditService.log`` takes the global audit-chain advisory lock as its
first statement, before ``begin_nested`` flushes the caller's pending row UPDATE, so these
routes took the chain lock before the row lock. That is the opposite of the codebase order, and
of ``POST /{standard_id}/auto-link``, which writes the clause's auto-linked rows (and, once it
retires stale ones, reads them ``FOR UPDATE``) before its first audit row. Two consequences:

* a deadlock against a concurrent auto-link run (Postgres 40P01, the app-wide 409), during which
  this request held the global chain lock and stalled every tenant's audited writes;
* a Verify that read the row before a retirement (or another person's Remove) committed still
  marked it verified, leaving it verified AND deleted, and answered 200.

The routes now resolve the row through ``_lock_live_evidence_or_404`` (``FOR UPDATE`` +
``populate_existing``, ``is_deleted`` tested after the lock) and flush their change before the
audit call.

SQLite (the test backend) ignores ``FOR UPDATE`` and never blocks, so these tests pin the parts
that can be observed here:

1. the statement each route actually issues for the row compiles to ``FOR UPDATE`` on the
   PostgreSQL dialect, with the tenant filter in its ``WHERE``;
2. the tombstone is judged on the row as it stands after the lock: a row deleted in the database
   while the session still holds the pre-delete version (what a request sees once a retirement it
   waited on has committed) is a 404 and is not written;
3. the row's UPDATE reaches the database before the audit-chain lock is taken (a spy on
   ``AuditService._acquire_chain_lock``), and nothing is left pending for the audit's flush.
"""

from typing import Any, List

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import event, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select, Update

from app.core.config import settings
from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.qms_standard import QMSClause, QMSClauseEvidence, QMSStandard
from app.models.user import User, UserRole
from app.services.audit_service import AuditService

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
EVIDENCE_TABLE = QMSClauseEvidence.__tablename__

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _make_user(db: Session, *, role: UserRole = UserRole.QUALITY) -> User:
    n = _next()
    user = User(
        email=f"qms-lock-{n}@co{COMPANY_A}.test",
        employee_id=f"QMSLK-{n:05d}",
        first_name="Qms",
        last_name="Lock",
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


def _ensure_company(db: Session, company_id: int) -> None:
    if db.query(Company).filter(Company.id == company_id).first() is None:
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True))
        db.commit()


def _make_evidence(db: Session, *, is_auto_linked: bool = False, company_id: int = COMPANY_A) -> QMSClauseEvidence:
    n = _next()
    _ensure_company(db, company_id)
    standard = QMSStandard(company_id=company_id, name=f"LOCK-STD-{n}", version="2016", is_active=True)
    db.add(standard)
    db.flush()
    clause = QMSClause(
        company_id=company_id,
        standard_id=standard.id,
        clause_number=f"{n}.1",
        title=f"Clause {n}",
        compliance_status="not_assessed",
    )
    db.add(clause)
    db.flush()
    evidence = QMSClauseEvidence(
        company_id=company_id,
        clause_id=clause.id,
        evidence_type="module",
        title=f"Lock evidence {n}",
        is_verified=False,
        is_auto_linked=is_auto_linked,
        auto_link_query="calibration" if is_auto_linked else None,
    )
    db.add(evidence)
    db.commit()
    db.refresh(evidence)
    return evidence


def _call(client: TestClient, verb: str, evidence_id: int, user: User):
    url = f"/api/v1/qms-standards/evidence/{evidence_id}"
    if verb == "put":
        return client.put(url, json={"is_verified": True}, headers=_headers_for(user))
    return client.delete(url, headers=_headers_for(user))


def _touches_evidence(statement: Any) -> bool:
    return any(getattr(table, "name", None) == EVIDENCE_TABLE for table in statement.get_final_froms())


def _audit_rows(db: Session, evidence_id: int, action: str) -> List[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.resource_type == "qms_clause_evidence",
            AuditLog.resource_id == evidence_id,
            AuditLog.action == action,
        )
        .all()
    )


class _StatementLog:
    """Record, in order, the evidence-row lock reads, the evidence-row UPDATEs and the chain lock.

    ``before_execute`` sees the SQL construct before the dialect drops ``FOR UPDATE`` (SQLite has
    none), so a locked read is recognisable by its ``_for_update_arg``. The ORM's flush runs its
    UPDATE through the same hook.
    """

    def __init__(self) -> None:
        self.events: List[str] = []
        self.lock_reads: List[Select] = []
        self.pending_evidence_at_audit: List[bool] = []

    def before_execute(self, conn, clauseelement, multiparams, params, execution_options):  # noqa: ARG002
        if isinstance(clauseelement, Select) and clauseelement._for_update_arg is not None:
            if _touches_evidence(clauseelement):
                self.events.append("evidence_lock")
                self.lock_reads.append(clauseelement)
        elif isinstance(clauseelement, Update) and clauseelement.table.name == EVIDENCE_TABLE:
            self.events.append("evidence_update")


@pytest.fixture
def statement_log(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    log = _StatementLog()
    engine = db_session.get_bind()
    event.listen(engine, "before_execute", log.before_execute)

    # The chain lock is taken only while the hash chain is enabled (the default); pin it so the
    # spy below runs whatever a neighbouring test left the setting at.
    monkeypatch.setattr(settings, "AUDIT_HASH_CHAIN_ENABLED", True)
    original_chain_lock = AuditService._acquire_chain_lock
    original_log = AuditService.log

    def spy_chain_lock(self: AuditService) -> None:
        log.events.append("chain_lock")
        original_chain_lock(self)

    def spy_log(self: AuditService, *args: Any, **kwargs: Any):
        # Anything still pending here would be flushed by ``begin_nested`` AFTER the chain lock.
        log.pending_evidence_at_audit.append(
            any(isinstance(obj, QMSClauseEvidence) for obj in list(self.db.dirty) + list(self.db.new))
        )
        return original_log(self, *args, **kwargs)

    monkeypatch.setattr(AuditService, "_acquire_chain_lock", spy_chain_lock)
    monkeypatch.setattr(AuditService, "log", spy_log)
    try:
        yield log
    finally:
        event.remove(engine, "before_execute", log.before_execute)


# ---------------------------------------------------------------------------
# 1. The row is read FOR UPDATE, inside the tenant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["put", "delete"])
def test_route_reads_the_evidence_row_for_update(
    verb: str, client: TestClient, db_session: Session, statement_log: _StatementLog
):
    user = _make_user(db_session)
    evidence = _make_evidence(db_session)

    resp = _call(client, verb, evidence.id, user)
    assert resp.status_code in (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT), resp.text

    assert len(statement_log.lock_reads) == 1, statement_log.events
    sql = str(statement_log.lock_reads[0].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in sql
    assert f"{EVIDENCE_TABLE}.id = " in sql
    assert f"{EVIDENCE_TABLE}.company_id = " in sql, "the lock read must stay inside the caller's tenant"


@pytest.mark.parametrize("verb", ["put", "delete"])
def test_route_on_another_companys_evidence_is_404_and_writes_nothing(
    verb: str, client: TestClient, db_session: Session
):
    """The lock read is scoped by the caller's active company, not just by id."""
    user = _make_user(db_session)  # company A
    foreign = _make_evidence(db_session, company_id=COMPANY_B)
    foreign_id = foreign.id

    resp = _call(client, verb, foreign_id, user)
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json()["detail"] == "Evidence not found"

    db_session.rollback()
    row = db_session.get(QMSClauseEvidence, foreign_id)
    assert row.company_id == COMPANY_B
    assert row.is_deleted is False
    assert not row.is_verified
    assert row.deleted_by is None
    assert _audit_rows(db_session, foreign_id, "UPDATE") == []
    assert _audit_rows(db_session, foreign_id, "DELETE") == []


# ---------------------------------------------------------------------------
# 2. The tombstone is judged after the lock
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["put", "delete"])
def test_route_on_a_removed_row_is_404_and_writes_nothing(verb: str, client: TestClient, db_session: Session):
    user = _make_user(db_session)
    evidence = _make_evidence(db_session)
    evidence_id = evidence.id

    removed = client.delete(f"/api/v1/qms-standards/evidence/{evidence_id}", headers=_headers_for(user))
    assert removed.status_code == status.HTTP_204_NO_CONTENT, removed.text

    resp = _call(client, verb, evidence_id, user)
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json()["detail"] == "Evidence not found"

    db_session.rollback()
    row = db_session.get(QMSClauseEvidence, evidence_id)
    assert row.is_deleted is True
    assert not row.is_verified
    assert _audit_rows(db_session, evidence_id, "UPDATE") == []
    assert len(_audit_rows(db_session, evidence_id, "DELETE")) == 1


@pytest.mark.parametrize("verb", ["put", "delete"])
def test_row_retired_while_waiting_for_the_lock_is_404(verb: str, client: TestClient, db_session: Session):
    """The race the lock closes: auto-link retires the row after this request first saw it.

    On PostgreSQL the request's ``FOR UPDATE`` waits for auto-link's commit and then returns the
    retired version of the row. SQLite cannot block, so the test reproduces the state the request
    is in once that wait ends: the session still holds the pre-retirement row (loaded, not
    deleted), while the database row is already retired. The route must judge the row as the
    locked read returns it (``populate_existing``), not as the session last saw it, and refuse.

    This pins the post-lock check and ``populate_existing``, not the old bug: the old query
    filtered ``is_deleted`` in its ``WHERE`` and answered 404 here too. Its failure needed the
    retirement to land between its unlocked read and its write, which the lock now rules out and
    SQLite cannot show.
    """
    user = _make_user(db_session)
    retirer = _make_user(db_session, role=UserRole.ADMIN)
    evidence = _make_evidence(db_session, is_auto_linked=True)
    evidence_id = evidence.id
    assert evidence.is_deleted is False  # loaded into the session the route shares

    # Auto-link's retirement, written straight to the table so the session's copy stays stale.
    db_session.connection().execute(
        update(QMSClauseEvidence.__table__)
        .where(QMSClauseEvidence.__table__.c.id == evidence_id)
        .values(is_deleted=True, deleted_by=retirer.id)
    )
    assert evidence.is_deleted is False  # the stale pre-retirement view

    resp = _call(client, verb, evidence_id, user)
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text

    table = QMSClauseEvidence.__table__
    is_verified, is_deleted, deleted_by = (
        db_session.connection()
        .execute(select(table.c.is_verified, table.c.is_deleted, table.c.deleted_by).where(table.c.id == evidence_id))
        .one()
    )
    assert not is_verified, "a retired row must not be marked verified"
    assert is_deleted
    assert deleted_by == retirer.id, "a second remover must not overwrite who retired the row"
    assert _audit_rows(db_session, evidence_id, "UPDATE") == []
    assert _audit_rows(db_session, evidence_id, "DELETE") == []


# ---------------------------------------------------------------------------
# 3. Row lock, then row write, then the audit-chain lock
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["put", "delete"])
def test_row_write_is_flushed_before_the_audit_chain_lock(
    verb: str, client: TestClient, db_session: Session, statement_log: _StatementLog
):
    user = _make_user(db_session)
    evidence = _make_evidence(db_session)

    resp = _call(client, verb, evidence.id, user)
    assert resp.status_code in (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT), resp.text

    events = statement_log.events
    assert events.count("evidence_lock") == 1, events
    assert "evidence_update" in events, events
    assert "chain_lock" in events, events
    first_update = events.index("evidence_update")
    assert events.index("evidence_lock") < first_update < events.index("chain_lock"), events
    assert "evidence_update" not in events[events.index("chain_lock") :], events
    assert statement_log.pending_evidence_at_audit == [False]


def test_verify_still_writes_its_audit_row(client: TestClient, db_session: Session):
    """The audit row is unchanged in kind: one best-effort UPDATE row recording the verification."""
    user = _make_user(db_session)
    evidence = _make_evidence(db_session)

    resp = _call(client, "put", evidence.id, user)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["is_verified"] is True
    assert resp.json()["verified_by"] == user.id

    db_session.rollback()  # only a committed audit row survives
    rows = _audit_rows(db_session, evidence.id, "UPDATE")
    assert len(rows) == 1
    changes = rows[0].extra_data["changes"]
    assert changes["is_verified"] == {"old": False, "new": True}
    assert changes["verified_by"] == {"old": None, "new": user.id}
    assert rows[0].company_id == COMPANY_A


def test_remove_audit_row_records_the_row_before_the_delete(client: TestClient, db_session: Session):
    """The row is soft-deleted before the audit call now, so its old values are taken first."""
    user = _make_user(db_session)
    evidence = _make_evidence(db_session)

    resp = _call(client, "delete", evidence.id, user)
    assert resp.status_code == status.HTTP_204_NO_CONTENT, resp.text

    db_session.rollback()
    rows = _audit_rows(db_session, evidence.id, "DELETE")
    assert len(rows) == 1
    assert rows[0].old_values["is_deleted"] is False
    assert rows[0].old_values["deleted_by"] is None
    assert rows[0].old_values["title"] == evidence.title
    assert rows[0].extra_data["soft_delete"] is True
    row = db_session.get(QMSClauseEvidence, evidence.id)
    assert row.is_deleted is True
    assert row.deleted_by == user.id
