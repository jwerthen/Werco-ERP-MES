"""Auto-link's evidence row locks: judged as locked, and taken before the audit-chain lock.

``POST /qms-standards/{standard_id}/auto-link`` writes three kinds of evidence change in one
transaction: it RETIRES (soft-deletes) auto-linked rows whose rule no longer matches their
clause, REFRESHES the rows whose rule still matches, and CREATES rows for newly matched rules.
``_retire_unmatched_auto_links`` reads each clause's candidate rows ``FOR UPDATE``; the evidence
``PUT`` / ``DELETE`` routes lock the same rows (``_lock_live_evidence_or_404``). Two properties
keep those writers correct against each other, and this file pins both for auto-link (the
evidence routes' half is ``tests/api/test_qms_evidence_row_lock.py``):

1. The locked read re-reads the row (``populate_existing()``). Without it the ORM returns the
   copy already in the session's identity map for any row loaded earlier in the request, so a
   Verify or a document / notes annotation that committed while auto-link waited on the lock
   would be discarded and the row retired anyway -- a person's judgment overruled, and a row
   left verified AND deleted. Nothing earlier in an auto-link request loads a clause's evidence
   today, so this guards the day something does.

2. Lock order: every evidence write (the retirement UPDATEs, the refresh UPDATEs and the create
   INSERTs) reaches the database before ``audit.log_required`` / ``audit.log_create`` take the
   global audit-chain lock. ``AuditService.log`` takes that lock as its first statement and only
   then flushes the caller's pending writes, so a write still pending at the first audit call
   would take its row lock under the chain lock -- the inverted order that deadlocks against an
   evidence ``PUT`` (40P01, the app-wide 409) while stalling every tenant's audited writes.

SQLite (the test backend) ignores ``FOR UPDATE`` and never blocks, so:

* (1) is shown behaviourally in the state a locked read is in once its wait ends: the session
  holds the pre-Verify row (loaded, unverified) while the database row is already verified. The
  change is written straight to the table on the session's own connection, as
  ``test_row_retired_while_waiting_for_the_lock_is_404`` does. A second session committing it
  is not used: the suite's engine is a ``StaticPool`` over one shared-cache connection, so a
  second session's COMMIT would also commit the first session's transaction and blur exactly
  the boundary under test. The same state is reproduced through the route, with the row loaded
  into the session the route shares.
* (2) is a statement log: ``before_execute`` sees each SQL construct before the SQLite dialect
  drops ``FOR UPDATE``, and a spy on ``AuditService._acquire_chain_lock`` marks the chain lock.
  Every locked read is also compiled on the PostgreSQL dialect, where it must say ``FOR UPDATE``
  and carry the tenant filter.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session
from sqlalchemy.sql import Insert, Select, Update

from app.api.endpoints.qms_standards import _retire_unmatched_auto_links
from app.core.config import settings
from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.document import Document, DocumentType
from app.models.qms_standard import QMSClause, QMSClauseEvidence, QMSStandard
from app.models.user import User, UserRole
from app.services.audit_service import AuditService
from app.services.auto_evidence_service import matched_rule_ids

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
EVIDENCE_TABLE = QMSClauseEvidence.__tablename__
STALE_TIME = datetime(2026, 1, 5, 12, 0, 0)
VERIFIED_TIME = datetime(2026, 2, 9, 8, 30, 0)

# Clause wording, paraphrased test text. Each test asserts which rules its clause matches, so a
# keyword edit cannot quietly turn a "retire" clause into a "refresh" one.
UNMATCHED_TITLE = "Scope of the system"  # matches no rule: a stored auto-link on it is stale
CALIBRATION_TITLE = "Calibration"  # matches only the "calibration" rule
# Matches the "audit" (change-log) and "calibration" rules, and not "spc".
AUDIT_TRAIL_TITLE = "Audit trail"
MEASURING_DESCRIPTION = "Calibration of measuring equipment"

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _ensure_company(db: Session, company_id: int) -> None:
    if db.query(Company).filter(Company.id == company_id).first() is None:
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True))
        db.commit()


def _make_user(db: Session, *, role: UserRole = UserRole.QUALITY) -> User:
    n = _next()
    user = User(
        email=f"qms-autolink-lock-{n}@co{COMPANY_A}.test",
        employee_id=f"QALK-{n:05d}",
        first_name="Autolink",
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


def _make_standard(db: Session) -> QMSStandard:
    _ensure_company(db, COMPANY_A)
    standard = QMSStandard(company_id=COMPANY_A, name=f"AUTOLINK-LOCK-STD-{_next()}", version="2016", is_active=True)
    db.add(standard)
    db.commit()
    db.refresh(standard)
    return standard


def _make_clause(
    db: Session, standard: QMSStandard, title: str, expected_rules: set, description: str = ""
) -> QMSClause:
    clause = QMSClause(
        company_id=COMPANY_A,
        standard_id=standard.id,
        clause_number=f"{_next()}.1",
        title=title,
        description=description,
        compliance_status="not_assessed",
    )
    db.add(clause)
    db.commit()
    db.refresh(clause)
    assert matched_rule_ids(clause) == expected_rules, f"clause {title!r} matches {matched_rule_ids(clause)}"
    return clause


def _make_auto_link(db: Session, clause: QMSClause, *, rule_id: str = "audit") -> QMSClauseEvidence:
    """An auto-linked evidence row as an earlier auto-link run left it."""
    evidence = QMSClauseEvidence(
        company_id=COMPANY_A,
        clause_id=clause.id,
        evidence_type="module",
        title=f"Earlier run evidence {_next()}",
        description="earlier run",
        module_reference="/audit",
        record_type="module",
        is_auto_linked=True,
        auto_link_query=rule_id,
        live_count=42,
        last_refreshed=STALE_TIME,
        is_verified=False,
    )
    db.add(evidence)
    db.commit()
    db.refresh(evidence)
    return evidence


def _make_document(db: Session) -> Document:
    n = _next()
    document = Document(
        document_number=f"QMS-AUTOLINK-LOCK-{n}",
        title=f"Calibration procedure {n}",
        document_type=DocumentType.PROCEDURE,
        company_id=COMPANY_A,
    )
    db.add(document)
    db.commit()
    db.refresh(document)
    return document


def _db_row(db: Session, evidence_id: int):
    """The evidence row as the DATABASE holds it, bypassing the session's identity map."""
    table = QMSClauseEvidence.__table__
    return (
        db.connection()
        .execute(
            select(
                table.c.is_deleted,
                table.c.deleted_by,
                table.c.is_verified,
                table.c.document_id,
                table.c.verification_notes,
            ).where(table.c.id == evidence_id)
        )
        .one()
    )


def _committed_audit_rows(db: Session, *, resource_type: str, resource_id: int, action: str) -> List[AuditLog]:
    db.rollback()  # the client shares this session: only a COMMITTED row survives
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


# The person's change, committed in the database while auto-link waited on the row lock.
def _review_values(kind: str, db: Session, reviewer: User) -> Dict[str, Any]:
    if kind == "verified":
        return {"is_verified": True, "verified_by": reviewer.id, "verified_date": VERIFIED_TIME}
    if kind == "document":
        return {"document_id": _make_document(db).id}
    assert kind == "notes"
    return {"verification_notes": "Checked against the calibration recall list."}


EXPECTED_REASON = {"verified": "verified", "document": "annotated", "notes": "annotated"}


_REVIEW_COLUMNS = {"is_verified", "document_id", "verification_notes"}


def _apply_review_behind_the_session(db: Session, evidence: QMSClauseEvidence, values: Dict[str, Any]) -> None:
    """Write the review straight to the table, so the session's loaded copy stays the stale one.

    The copy is (re)loaded first: a COMMIT earlier in the test expires every instance, and an
    expired attribute would simply be re-read from the database -- after the review -- the next
    time anything touched it, which is not the state under test.
    """
    db.refresh(evidence)  # the session's copy, loaded before the review lands
    evidence_id = evidence.id
    db.connection().execute(
        update(QMSClauseEvidence.__table__).where(QMSClauseEvidence.__table__.c.id == evidence_id).values(**values)
    )
    # The precondition the test depends on: the session holds a LOADED (not expired, so reading it
    # issues no SELECT) and un-reviewed copy, while the database row is reviewed.
    assert _REVIEW_COLUMNS.isdisjoint(sa_inspect(evidence).unloaded)
    assert evidence.is_verified is False
    assert evidence.document_id is None
    assert evidence.verification_notes is None
    reviewed = _db_row(db, evidence_id)
    assert any(getattr(reviewed, column) for column in _REVIEW_COLUMNS)


# ---------------------------------------------------------------------------
# 1. The locked read judges the row as the database holds it (populate_existing)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["verified", "document", "notes"])
def test_review_committed_while_waiting_keeps_the_row_despite_a_stale_session_copy(kind: str, db_session: Session):
    """The row is in the session (stale, un-reviewed) when the locked read runs; the database row
    was reviewed meanwhile. The decision must follow the database: kept for review, not retired."""
    standard = _make_standard(db_session)
    clause = _make_clause(db_session, standard, UNMATCHED_TITLE, set())
    retirer = _make_user(db_session, role=UserRole.ADMIN)
    reviewer = _make_user(db_session)
    evidence = _make_auto_link(db_session, clause)  # loaded into the session's identity map
    evidence_id = evidence.id

    _apply_review_behind_the_session(db_session, evidence, _review_values(kind, db_session, reviewer))

    retired, kept = _retire_unmatched_auto_links(db_session, clause, COMPANY_A, retirer.id)
    db_session.flush()

    assert retired == [], "a row a person reviewed must not be retired"
    assert kept == [
        {
            "evidence_id": evidence_id,
            "clause_id": clause.id,
            "clause_number": clause.clause_number,
            "rule_id": "audit",
            "reason": EXPECTED_REASON[kind],
        }
    ]
    row = _db_row(db_session, evidence_id)
    assert not row.is_deleted
    assert row.deleted_by is None
    # The person's review survives, read from the database.
    if kind == "verified":
        assert row.is_verified
    elif kind == "document":
        assert row.document_id is not None
    else:
        assert row.verification_notes


def test_unreviewed_stale_row_is_still_retired(db_session: Session):
    """Control for the test above: the same setup with no review is retired, so the kept rows
    there are kept because of the review the locked read saw, not because the row was never a
    retirement candidate."""
    standard = _make_standard(db_session)
    clause = _make_clause(db_session, standard, UNMATCHED_TITLE, set())
    retirer = _make_user(db_session, role=UserRole.ADMIN)
    evidence = _make_auto_link(db_session, clause)
    evidence_id = evidence.id

    retired, kept = _retire_unmatched_auto_links(db_session, clause, COMPANY_A, retirer.id)
    db_session.flush()

    assert kept == []
    assert [entry["evidence_id"] for entry in retired] == [evidence_id]
    row = _db_row(db_session, evidence_id)
    assert row.is_deleted
    assert row.deleted_by == retirer.id


def test_route_keeps_a_row_verified_behind_a_preloaded_session_copy(client: TestClient, db_session: Session):
    """The same race through ``POST /{standard_id}/auto-link``: the stale row is loaded into the
    session the route shares, as a future pre-load in the request would leave it."""
    standard = _make_standard(db_session)
    clause = _make_clause(db_session, standard, UNMATCHED_TITLE, set())
    user = _make_user(db_session)
    reviewer = _make_user(db_session)
    evidence = _make_auto_link(db_session, clause)
    evidence_id, clause_id, clause_number, standard_id = evidence.id, clause.id, clause.clause_number, standard.id

    _apply_review_behind_the_session(db_session, evidence, _review_values("verified", db_session, reviewer))

    resp = client.post(f"/api/v1/qms-standards/{standard_id}/auto-link", headers=_headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()

    assert body["total_evidence_retired"] == 0
    assert body["evidence_kept_reviewed"] == [
        {
            "evidence_id": evidence_id,
            "clause_id": clause_id,
            "clause_number": clause_number,
            "rule_id": "audit",
            "reason": "verified",
        }
    ]
    assert (
        _committed_audit_rows(db_session, resource_type="qms_clause_evidence", resource_id=evidence_id, action="DELETE")
        == []
    )
    row = _db_row(db_session, evidence_id)
    assert not row.is_deleted
    assert row.is_verified


# ---------------------------------------------------------------------------
# 2. Lock order: evidence locks and writes, then the audit-chain lock
# ---------------------------------------------------------------------------


def _touches_evidence(statement: Any) -> bool:
    return any(getattr(table, "name", None) == EVIDENCE_TABLE for table in statement.get_final_froms())


class _StatementLog:
    """In order: evidence lock reads, evidence INSERTs / UPDATEs, and audit-chain lock requests."""

    def __init__(self) -> None:
        self.events: List[str] = []
        self.lock_reads: List[Select] = []
        self.pending_evidence_at_audit: List[bool] = []

    def clear(self) -> None:
        """Forget the fixture rows' own INSERTs: record only what the auto-link run does."""
        self.events.clear()
        self.lock_reads.clear()
        self.pending_evidence_at_audit.clear()

    def before_execute(self, conn, clauseelement, multiparams, params, execution_options):  # noqa: ARG002
        if isinstance(clauseelement, Select) and clauseelement._for_update_arg is not None:
            if _touches_evidence(clauseelement):
                self.events.append("evidence_lock")
                self.lock_reads.append(clauseelement)
        elif isinstance(clauseelement, Insert) and clauseelement.table.name == EVIDENCE_TABLE:
            self.events.append("evidence_insert")
        elif isinstance(clauseelement, Update) and clauseelement.table.name == EVIDENCE_TABLE:
            self.events.append("evidence_update")


@pytest.fixture
def statement_log(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    log = _StatementLog()
    engine = db_session.get_bind()
    event.listen(engine, "before_execute", log.before_execute)

    # The chain lock is taken only while the hash chain is enabled (the default); pin it so the
    # spy runs whatever a neighbouring test left the setting at.
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


def _seed_retire_refresh_create(db: Session) -> Dict[str, Any]:
    """One standard whose next auto-link run creates, refreshes AND retires.

    Two clauses, in id order (the run's order):

    * a calibration clause with no stored row: a CREATE, which the second clause's rule
      savepoint (``discover_evidence_for_clause``'s ``begin_nested``) flushes;
    * the LAST clause, which matches the change-log and calibration rules and stores a change-log
      row (REFRESH) and a stale SPC auto-link no rule of it matches (RETIRE), and gets a new
      calibration row (CREATE). It is last on purpose: no later savepoint flushes its three
      writes, so only the run's own flush before its audit rows can put them ahead of the
      audit-chain lock.
    """
    standard = _make_standard(db)
    first_clause = _make_clause(db, standard, CALIBRATION_TITLE, {"calibration"})
    last_clause = _make_clause(
        db, standard, AUDIT_TRAIL_TITLE, {"audit", "calibration"}, description=MEASURING_DESCRIPTION
    )
    refreshed = _make_auto_link(db, last_clause, rule_id="audit")
    stale = _make_auto_link(db, last_clause, rule_id="spc")
    db.expire_all()  # nothing pre-loaded: the run reads every row itself
    return {
        "standard_id": standard.id,
        "clause_ids": [first_clause.id, last_clause.id],
        "refreshed_id": refreshed.id,
        "stale_id": stale.id,
    }


def _run(client: TestClient, standard_id: int, user: User) -> dict:
    resp = client.post(f"/api/v1/qms-standards/{standard_id}/auto-link", headers=_headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()


def test_every_evidence_write_reaches_the_database_before_the_audit_chain_lock(
    client: TestClient, db_session: Session, statement_log: _StatementLog
):
    seeded = _seed_retire_refresh_create(db_session)
    user = _make_user(db_session)
    statement_log.clear()

    body = _run(client, seeded["standard_id"], user)

    # The run did all three kinds of write -- otherwise the order below proves less than it claims.
    assert body["total_evidence_created"] == 2
    assert body["total_evidence_updated"] == 1
    assert body["total_evidence_retired"] == 1
    assert body["evidence_kept_reviewed"] == []

    events = statement_log.events
    assert events.count("evidence_lock") == len(seeded["clause_ids"]), events  # one locked read per clause
    assert events.count("evidence_insert") == 2, events  # one create per clause
    # One UPDATE for the refresh and one for the retirement: they set different columns.
    assert events.count("evidence_update") == 2, events
    assert "chain_lock" in events, events

    first_chain_lock = events.index("chain_lock")
    after_chain_lock = events[first_chain_lock:]
    for kind in ("evidence_lock", "evidence_insert", "evidence_update"):
        assert kind not in after_chain_lock, f"{kind} after the audit-chain lock: {events}"
    # Nothing was left pending for an audit call's ``begin_nested`` to flush under the chain lock.
    assert statement_log.pending_evidence_at_audit, "auto-link wrote no audit row"
    assert not any(statement_log.pending_evidence_at_audit), statement_log.pending_evidence_at_audit


def test_every_evidence_write_is_on_the_database(client: TestClient, db_session: Session):
    """The writes the order test watches are the real ones: committed create, refresh and retire,
    with the retirement's required DELETE audit row."""
    seeded = _seed_retire_refresh_create(db_session)
    user = _make_user(db_session)

    _run(client, seeded["standard_id"], user)

    db_session.rollback()
    db_session.expire_all()
    created = (
        db_session.query(QMSClauseEvidence)
        .filter(
            QMSClauseEvidence.clause_id.in_(seeded["clause_ids"]),
            QMSClauseEvidence.auto_link_query == "calibration",
        )
        .all()
    )
    assert sorted(row.clause_id for row in created) == sorted(seeded["clause_ids"])
    assert all(row.is_deleted is False and row.is_auto_linked for row in created)
    refreshed = db_session.get(QMSClauseEvidence, seeded["refreshed_id"])
    assert refreshed.is_deleted is False and refreshed.last_refreshed > STALE_TIME
    stale = db_session.get(QMSClauseEvidence, seeded["stale_id"])
    assert stale.is_deleted is True and stale.deleted_by == user.id
    deletes = _committed_audit_rows(
        db_session, resource_type="qms_clause_evidence", resource_id=seeded["stale_id"], action="DELETE"
    )
    assert len(deletes) == 1 and deletes[0].extra_data["retired_by"] == "auto_link"


def test_every_locked_evidence_read_is_for_update_inside_the_tenant(
    client: TestClient, db_session: Session, statement_log: _StatementLog
):
    seeded = _seed_retire_refresh_create(db_session)
    user = _make_user(db_session)
    statement_log.clear()

    _run(client, seeded["standard_id"], user)

    # The log collects only SELECTs built with ``with_for_update()``, so this count is what fails
    # when a lock read loses its FOR UPDATE; the compiled-SQL checks below pin the tenant scope.
    assert len(statement_log.lock_reads) == len(seeded["clause_ids"]), statement_log.events
    for statement in statement_log.lock_reads:
        sql = str(statement.compile(dialect=postgresql.dialect()))
        assert "FOR UPDATE" in sql, sql
        assert f"{EVIDENCE_TABLE}.company_id = " in sql, f"the lock read must stay inside the tenant: {sql}"
        assert f"{EVIDENCE_TABLE}.clause_id = " in sql, sql


def _clause_ids_locked(statement_log: _StatementLog) -> List[Optional[int]]:
    ids = []
    for statement in statement_log.lock_reads:
        compiled = statement.compile(dialect=postgresql.dialect())
        ids.append(next((v for k, v in compiled.params.items() if k.startswith("clause_id")), None))
    return ids


def test_clauses_are_locked_in_id_order(client: TestClient, db_session: Session, statement_log: _StatementLog):
    """Two concurrent runs on one standard take the clauses' evidence locks in the same order."""
    seeded = _seed_retire_refresh_create(db_session)
    user = _make_user(db_session)
    statement_log.clear()

    _run(client, seeded["standard_id"], user)

    assert _clause_ids_locked(statement_log) == sorted(seeded["clause_ids"])
