"""HTTP coverage: internal-audit / management-review clauses are not tracked, and auto-link
retires the change-log links it used to make on them.

Companion to ``tests/services/test_auto_evidence_internal_audit_routing.py`` (keyword routing
and the roll-up). Drives two live routes:

    GET  /api/v1/qms-standards/clauses/{clause_id}/auto-evidence
    POST /api/v1/qms-standards/{standard_id}/auto-link

The GET lists the not-tracked ``internal_audit_review`` row with every ``_``-prefixed internal
key stripped, and no change-log row. (``AutoEvidenceResult`` ignores unknown keys, so the
no-``_``-key assertion is defense in depth: it holds the payload shape if the schema is ever
loosened to pass extra keys through.)

Auto-link:
* never persists a not-tracked row, and does not count its clause as a clause with evidence;
* retires (soft-deletes, one REQUIRED audit DELETE row each, written by the endpoint) its own
  unverified evidence rows whose rule no longer matches the clause -- e.g. the change-log
  ("audit") link it used to make on 9.2 / 9.3;
* keeps a stale row a person reviewed (verified, or annotated with notes) and lists it in the
  response and on the run's summary audit row;
* never touches a manual row, a row whose rule still matches, a row whose matched rule's
  query failed this run, or another company's rows.

Audit rows are read back from ``AuditLog`` after a ``rollback()`` so only COMMITTED rows count
(the same guard as ``tests/api/test_qms_soft_delete_audit.py``). Change-log rows are seeded
through ``AuditService``, never inserted directly. Clause text is paraphrased test wording.
"""

from datetime import datetime
from typing import Optional

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.qms_standard import QMSClause, QMSClauseEvidence, QMSStandard
from app.models.user import User, UserRole
from app.schemas.qms_standard import AutoEvidenceResult
from app.services.audit_service import AuditService
from app.services.auto_evidence_service import EVIDENCE_MAPPING_RULES, INTERNAL_AUDIT_REVIEW_TITLE

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2

AUDIT_LOG_TITLE = "Audit Trail (Immutable Hash-Chain Log)"
STALE_TIME = datetime(2026, 1, 5, 12, 0, 0)

# Module-level counter so every fixture row gets a globally unique natural key, even across
# tests sharing a worker DB under -n auto.
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True)
        db.add(company)
        db.commit()
    return company


def _make_user(db: Session, *, company_id: int = COMPANY_A, role: UserRole = UserRole.QUALITY) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"iar-api-{n}@co{company_id}.test",
        employee_id=f"IARA-{n:05d}",
        first_name="Evidence",
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


def _seed_change_log(db: Session, *, company_id: int = COMPANY_A, count: int = 3) -> None:
    """Populate the company's system change log through ``AuditService`` (hash-chained)."""
    writer = _make_user(db, company_id=company_id)
    svc = AuditService(db, writer)
    assert svc.company_id == company_id
    for _ in range(count):
        row = svc.log(action="CREATE", resource_type="part", resource_id=_next(), resource_identifier="IAR-API-PART")
        assert row is not None and row.company_id == company_id
    db.commit()


def _make_standard(db: Session, *, company_id: int = COMPANY_A) -> QMSStandard:
    _ensure_company(db, company_id)
    standard = QMSStandard(
        company_id=company_id, name=f"STD-IAR-API-{_next()}", version="2016", standard_body="SAE", is_active=True
    )
    db.add(standard)
    db.commit()
    db.refresh(standard)
    return standard


def _make_clause(db: Session, *, standard: QMSStandard, clause_number: str, title: str, description: str = ""):
    clause = QMSClause(
        company_id=standard.company_id,
        standard_id=standard.id,
        clause_number=clause_number,
        title=title,
        description=description,
    )
    db.add(clause)
    db.commit()
    db.refresh(clause)
    return clause


def _make_evidence(
    db: Session,
    *,
    clause: QMSClause,
    company_id: Optional[int] = None,
    auto_linked: bool = True,
    rule_id: Optional[str] = "audit",
    verified: bool = False,
    title: str = AUDIT_LOG_TITLE,
    verification_notes: Optional[str] = None,
) -> QMSClauseEvidence:
    """An evidence row as an earlier auto-link run (or a person, when ``auto_linked`` is False)
    left it. The defaults are the change-log link auto-link made on 9.2 / 9.3 before the fix."""
    evidence = QMSClauseEvidence(
        company_id=company_id if company_id is not None else clause.company_id,
        clause_id=clause.id,
        evidence_type="module" if auto_linked else "document",
        title=title,
        description="earlier run",
        module_reference="/audit" if auto_linked else None,
        record_type="module" if auto_linked else None,
        is_auto_linked=auto_linked,
        auto_link_query=rule_id if auto_linked else None,
        live_count=42 if auto_linked else None,
        last_refreshed=STALE_TIME if auto_linked else None,
        is_verified=verified,
        verified_date=STALE_TIME if verified else None,
        verification_notes=verification_notes,
    )
    db.add(evidence)
    db.commit()
    db.refresh(evidence)
    return evidence


def _evidence(db: Session, evidence_id: int) -> QMSClauseEvidence:
    db.expire_all()
    row = db.query(QMSClauseEvidence).filter(QMSClauseEvidence.id == evidence_id).first()
    assert row is not None, f"evidence {evidence_id} was hard-deleted"
    return row


def _assert_live(db: Session, evidence_id: int) -> QMSClauseEvidence:
    row = _evidence(db, evidence_id)
    assert row.is_deleted is False, f"evidence {evidence_id} was retired"
    assert row.deleted_at is None
    assert row.deleted_by is None
    return row


def _committed_audit_rows(db: Session, *, resource_type: str, resource_id: int, action: str):
    """Committed ``AuditLog`` rows for one resource, oldest first.

    ``rollback()`` first: the client shares this session, so a row merely flushed by the
    endpoint would otherwise be visible here. A committed row survives the rollback.
    """
    db.rollback()
    db.expire_all()
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.resource_type == resource_type,
            AuditLog.resource_id == resource_id,
            AuditLog.action == action,
        )
        .order_by(AuditLog.sequence_number.asc())
        .all()
    )


def _auto_link(client: TestClient, standard_id: int, user: User) -> dict:
    resp = client.post(f"/api/v1/qms-standards/{standard_id}/auto-link", headers=_headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()


def _run_summary_row(db: Session, standard_id: int) -> AuditLog:
    rows = _committed_audit_rows(db, resource_type="qms_standard", resource_id=standard_id, action="CREATE")
    assert rows, "auto-link wrote no summary audit row"
    return rows[-1]


# ---------------------------------------------------------------------------
# GET auto-evidence
# ---------------------------------------------------------------------------


def test_get_auto_evidence_internal_audit_clause_lists_not_tracked_row_only(client: TestClient, db_session: Session):
    """With the change log populated, an "Internal audit" clause shows the not-tracked row,
    no change-log row, no internal keys, and an overall ``not_assessed`` (was ``compliant``)."""
    _seed_change_log(db_session, count=3)
    standard = _make_standard(db_session)
    clause = _make_clause(db_session, standard=standard, clause_number="9.2", title="Internal audit")
    user = _make_user(db_session)

    resp = client.get(f"/api/v1/qms-standards/clauses/{clause.id}/auto-evidence", headers=_headers_for(user))

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["clause_id"] == clause.id
    assert body["overall_suggested_compliance"] == "not_assessed"

    rows = body["discovered_evidence"]
    assert [r["title"] for r in rows] == [INTERNAL_AUDIT_REVIEW_TITLE]
    assert not any(r["title"] == AUDIT_LOG_TITLE for r in rows)

    row = rows[0]
    assert row["suggested_compliance"] == "not_assessed"
    assert row["health_status"] == "no_data"
    assert (row["total_count"], row["recent_count"], row["examples"]) == (0, 0, [])
    for r in rows:
        assert not any(k.startswith("_") for k in r), r
        assert "_not_tracked" not in r and "_rule_id" not in r
        assert set(r) == set(AutoEvidenceResult.model_fields)


# ---------------------------------------------------------------------------
# POST auto-link
# ---------------------------------------------------------------------------


def test_auto_link_persists_no_not_tracked_row(client: TestClient, db_session: Session):
    """(a) 9.2 and 9.3 get no evidence row and count as clauses without evidence; the
    compliance summary counts them not_assessed. The audit-trail clause is the control."""
    _seed_change_log(db_session, count=3)
    standard = _make_standard(db_session)
    internal_audit = _make_clause(db_session, standard=standard, clause_number="9.2", title="Internal audit")
    mgmt_review = _make_clause(db_session, standard=standard, clause_number="9.3", title="Management review")
    audit_trail = _make_clause(db_session, standard=standard, clause_number="7.5.3", title="Audit trail")
    user = _make_user(db_session)

    body = _auto_link(client, standard.id, user)

    assert body["total_clauses"] == 3
    assert body["clauses_with_evidence"] == 1
    assert body["clauses_without_evidence"] == 2
    assert body["total_evidence_created"] == 1
    assert body["total_evidence_updated"] == 0
    assert body["total_evidence_retired"] == 0
    assert body["evidence_kept_reviewed"] == []
    # Before the fix: {"compliant": 3}, with a change-log row persisted on 9.2 and 9.3.
    assert body["compliance_summary"] == {"compliant": 1, "not_assessed": 2}

    db_session.expire_all()
    iar_rows = (
        db_session.query(QMSClauseEvidence)
        .filter(
            QMSClauseEvidence.clause_id.in_([internal_audit.id, mgmt_review.id, audit_trail.id]),
            (QMSClauseEvidence.auto_link_query == "internal_audit_review")
            | (QMSClauseEvidence.title == INTERNAL_AUDIT_REVIEW_TITLE),
        )
        .all()
    )
    assert iar_rows == []
    for clause in (internal_audit, mgmt_review):
        assert db_session.query(QMSClauseEvidence).filter(QMSClauseEvidence.clause_id == clause.id).count() == 0

    control = db_session.query(QMSClauseEvidence).filter(QMSClauseEvidence.clause_id == audit_trail.id).all()
    assert [(e.auto_link_query, e.is_auto_linked, e.is_deleted) for e in control] == [("audit", True, False)]


@pytest.mark.parametrize("clause_number,title", [("9.2", "Internal audit"), ("9.3", "Management review")])
def test_auto_link_retires_stale_unverified_change_log_link(
    client: TestClient, db_session: Session, clause_number: str, title: str
):
    """(b) The change-log link an earlier run made on 9.2 / 9.3 is soft-deleted, attributed to
    the caller, with one audit DELETE row and the run's summary row naming it."""
    _seed_change_log(db_session, count=2)
    standard = _make_standard(db_session)
    clause = _make_clause(db_session, standard=standard, clause_number=clause_number, title=title)
    stale = _make_evidence(db_session, clause=clause)
    stale_id, clause_id, standard_id = stale.id, clause.id, standard.id
    user = _make_user(db_session)

    body = _auto_link(client, standard_id, user)

    assert body["total_evidence_retired"] == 1
    assert body["evidence_kept_reviewed"] == []
    assert body["total_evidence_created"] == 0
    assert body["total_evidence_updated"] == 0
    assert body["clauses_without_evidence"] == 1
    assert body["compliance_summary"] == {"not_assessed": 1}

    row = _evidence(db_session, stale_id)
    assert row.is_deleted is True
    assert row.deleted_by == user.id
    assert row.deleted_at is not None

    ref = {"evidence_id": stale_id, "clause_id": clause_id, "clause_number": clause_number, "rule_id": "audit"}

    deletes = _committed_audit_rows(
        db_session, resource_type="qms_clause_evidence", resource_id=stale_id, action="DELETE"
    )
    assert len(deletes) == 1
    delete = deletes[0]
    assert delete.user_id == user.id
    assert delete.company_id == COMPANY_A
    assert delete.extra_data["retired_by"] == "auto_link"
    assert delete.extra_data["soft_delete"] is True
    assert {k: delete.extra_data[k] for k in ref} == ref
    # The pre-delete snapshot: the row as it stood before the retirement.
    assert delete.old_values["id"] == stale_id
    assert delete.old_values["is_deleted"] is False
    assert delete.old_values["auto_link_query"] == "audit"

    summary = _run_summary_row(db_session, standard_id)
    assert summary.user_id == user.id
    assert summary.company_id == COMPANY_A
    assert summary.extra_data["evidence_retired"] == [ref]
    assert "evidence_kept_reviewed" not in summary.extra_data

    # The retired row drops out of the standard's nested evidence links.
    listed = client.get(f"/api/v1/qms-standards/{standard_id}/clauses", headers=_headers_for(user))
    assert listed.status_code == status.HTTP_200_OK, listed.text
    links = [e["id"] for c in listed.json() if c["id"] == clause_id for e in c["evidence_links"]]
    assert stale_id not in links


def test_auto_link_keeps_verified_stale_link(client: TestClient, db_session: Session):
    """(c) A stale link a person verified is never retired; it is counted and listed."""
    standard = _make_standard(db_session)
    clause = _make_clause(db_session, standard=standard, clause_number="9.2", title="Internal audit")
    verified = _make_evidence(db_session, clause=clause, verified=True)
    verified_id, clause_id, standard_id = verified.id, clause.id, standard.id
    user = _make_user(db_session)

    body = _auto_link(client, standard_id, user)

    kept = {"evidence_id": verified_id, "clause_id": clause_id, "clause_number": "9.2", "rule_id": "audit"}
    assert body["evidence_kept_reviewed"] == [{**kept, "reason": "verified"}]
    assert body["total_evidence_retired"] == 0

    row = _assert_live(db_session, verified_id)
    assert row.is_verified is True
    assert row.is_auto_linked is True
    assert row.title == AUDIT_LOG_TITLE
    assert row.live_count == 42  # not refreshed: its rule no longer matches the clause

    assert (
        _committed_audit_rows(db_session, resource_type="qms_clause_evidence", resource_id=verified_id, action="DELETE")
        == []
    )
    summary = _run_summary_row(db_session, standard_id)
    assert summary.extra_data["evidence_kept_reviewed"] == [{**kept, "reason": "verified"}]
    assert "evidence_retired" not in summary.extra_data


def test_auto_link_keeps_annotated_unverified_stale_link(client: TestClient, db_session: Session):
    """(c2) Notes on an auto-linked row are only ever written by a person, so a stale row that
    carries them is kept like a verified one (reason "annotated"). Whitespace-only notes are
    not an annotation: that row is retired."""
    standard = _make_standard(db_session)
    clause = _make_clause(db_session, standard=standard, clause_number="9.2", title="Internal audit")
    annotated = _make_evidence(db_session, clause=clause, verification_notes="See the 2026 internal audit report")
    blank = _make_evidence(db_session, clause=clause, verification_notes="   ")
    annotated_id, blank_id, clause_id, standard_id = annotated.id, blank.id, clause.id, standard.id
    user = _make_user(db_session)

    body = _auto_link(client, standard_id, user)

    assert body["evidence_kept_reviewed"] == [
        {
            "evidence_id": annotated_id,
            "clause_id": clause_id,
            "clause_number": "9.2",
            "rule_id": "audit",
            "reason": "annotated",
        }
    ]
    assert body["total_evidence_retired"] == 1
    row = _assert_live(db_session, annotated_id)
    assert row.verification_notes == "See the 2026 internal audit report"
    assert _evidence(db_session, blank_id).is_deleted is True


def test_auto_link_rolls_back_when_a_retirement_audit_row_cannot_be_written(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """Each retirement's DELETE row is required evidence: when it cannot be written the whole
    run answers 503 and nothing is saved -- the stale row stays live, no new evidence row is
    created, and no audit row of the run is committed."""
    _seed_change_log(db_session, count=2)
    standard = _make_standard(db_session)
    clause = _make_clause(db_session, standard=standard, clause_number="9.2", title="Internal audit")
    trail = _make_clause(db_session, standard=standard, clause_number="7.5.3", title="Audit trail")
    stale = _make_evidence(db_session, clause=clause)
    stale_id, trail_id, standard_id = stale.id, trail.id, standard.id
    user = _make_user(db_session)

    real_log = AuditService.log

    def _log_unless_retirement(self, action, resource_type, *args, **kwargs):
        if action == "DELETE" and resource_type == "qms_clause_evidence":
            return None  # how ``log`` reports a write it swallowed
        return real_log(self, action, resource_type, *args, **kwargs)

    monkeypatch.setattr(AuditService, "log", _log_unless_retirement)

    resp = client.post(f"/api/v1/qms-standards/{standard_id}/auto-link", headers=_headers_for(user))

    assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE, resp.text
    assert "nothing was changed" in resp.json()["detail"]
    _assert_live(db_session, stale_id)
    assert db_session.query(QMSClauseEvidence).filter(QMSClauseEvidence.clause_id == trail_id).count() == 0
    assert (
        _committed_audit_rows(db_session, resource_type="qms_clause_evidence", resource_id=stale_id, action="DELETE")
        == []
    )
    assert (
        _committed_audit_rows(db_session, resource_type="qms_standard", resource_id=standard_id, action="CREATE") == []
    )


def test_auto_link_never_touches_manual_evidence(client: TestClient, db_session: Session):
    """(d) A manually linked row on the same clause stays as it is; only the stale auto row goes.
    The manual row has no rule id, so it would be "unmatched" if the auto-linked filter fell."""
    standard = _make_standard(db_session)
    clause = _make_clause(db_session, standard=standard, clause_number="9.2", title="Internal audit")
    manual = _make_evidence(db_session, clause=clause, auto_linked=False, title="Internal audit schedule (binder)")
    stale = _make_evidence(db_session, clause=clause)
    manual_id, stale_id, standard_id = manual.id, stale.id, standard.id
    user = _make_user(db_session)

    body = _auto_link(client, standard_id, user)

    assert body["total_evidence_retired"] == 1
    assert _evidence(db_session, stale_id).is_deleted is True

    row = _assert_live(db_session, manual_id)
    assert row.is_auto_linked is False
    assert row.auto_link_query is None
    assert row.title == "Internal audit schedule (binder)"
    assert row.description == "earlier run"
    assert (
        _committed_audit_rows(db_session, resource_type="qms_clause_evidence", resource_id=manual_id, action="DELETE")
        == []
    )


def test_auto_link_refreshes_change_log_link_on_audit_trail_clause(client: TestClient, db_session: Session):
    """(e) On a clause that names the audit trail the "audit" rule still matches: the stored
    row is refreshed in place, not retired and not duplicated."""
    _seed_change_log(db_session, count=3)
    standard = _make_standard(db_session)
    clause = _make_clause(db_session, standard=standard, clause_number="7.5.3", title="Audit trail")
    stored = _make_evidence(db_session, clause=clause, title="old title")
    stored_id, clause_id, standard_id = stored.id, clause.id, standard.id
    user = _make_user(db_session)

    body = _auto_link(client, standard_id, user)

    assert body["total_evidence_updated"] == 1
    assert body["total_evidence_created"] == 0
    assert body["total_evidence_retired"] == 0
    assert body["evidence_kept_reviewed"] == []
    assert body["compliance_summary"] == {"compliant": 1}

    row = _assert_live(db_session, stored_id)
    assert row.title == AUDIT_LOG_TITLE
    assert row.live_count >= 3
    assert row.last_refreshed > STALE_TIME
    assert db_session.query(QMSClauseEvidence).filter(QMSClauseEvidence.clause_id == clause_id).count() == 1
    assert (
        _committed_audit_rows(db_session, resource_type="qms_clause_evidence", resource_id=stored_id, action="DELETE")
        == []
    )


def test_auto_link_keeps_link_when_matched_rule_query_fails(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """(f) A rule still matches by its keywords when its query fails this run, so its stored
    row is not retired. Only "not deleted" is pinned: how a failed rule is reported in the
    run's counts is outside this change."""
    (audit_rule,) = [r for r in EVIDENCE_MAPPING_RULES if r["id"] == "audit"]

    def _boom(db, company_id):
        raise RuntimeError("simulated audit-log query failure")

    monkeypatch.setitem(audit_rule, "query_fn", _boom)

    standard = _make_standard(db_session)
    clause = _make_clause(db_session, standard=standard, clause_number="7.5.3", title="Audit trail")
    stored = _make_evidence(db_session, clause=clause)
    stored_id, standard_id = stored.id, standard.id
    user = _make_user(db_session)

    _auto_link(client, standard_id, user)

    _assert_live(db_session, stored_id)
    assert (
        _committed_audit_rows(db_session, resource_type="qms_clause_evidence", resource_id=stored_id, action="DELETE")
        == []
    )


def test_auto_link_second_run_retires_nothing_more(client: TestClient, db_session: Session):
    """(g) Retirement is one-shot: a re-run finds nothing left to retire, writes no second
    DELETE row and does not re-stamp the retired row."""
    standard = _make_standard(db_session)
    clause = _make_clause(db_session, standard=standard, clause_number="9.2", title="Internal audit")
    stale = _make_evidence(db_session, clause=clause)
    stale_id, standard_id = stale.id, standard.id
    user = _make_user(db_session)

    first = _auto_link(client, standard_id, user)
    assert first["total_evidence_retired"] == 1
    deleted_at = _evidence(db_session, stale_id).deleted_at

    second = _auto_link(client, standard_id, user)

    assert second["total_evidence_retired"] == 0
    assert second["evidence_kept_reviewed"] == []
    assert second["total_evidence_created"] == 0
    assert second["total_evidence_updated"] == 0
    row = _evidence(db_session, stale_id)
    assert row.is_deleted is True
    assert row.deleted_at == deleted_at
    assert (
        len(
            _committed_audit_rows(
                db_session, resource_type="qms_clause_evidence", resource_id=stale_id, action="DELETE"
            )
        )
        == 1
    )
    summaries = _committed_audit_rows(
        db_session, resource_type="qms_standard", resource_id=standard_id, action="CREATE"
    )
    assert len(summaries) == 2
    assert not (summaries[-1].extra_data or {}).get("evidence_retired")


def test_auto_link_does_not_touch_other_company_stale_links(client: TestClient, db_session: Session):
    """(h) Company A's run retires only A's stale row.

    * Company B's own 9.2 clause and stale link are untouched (and A cannot run B's standard).
    * A company-B row pinned to A's clause -- not a state the API can produce, seeded to prove
      the retirement query's own company predicate, not just the clause scoping -- is
      untouched too.
    """
    standard_a = _make_standard(db_session, company_id=COMPANY_A)
    clause_a = _make_clause(db_session, standard=standard_a, clause_number="9.2", title="Internal audit")
    stale_a = _make_evidence(db_session, clause=clause_a)

    standard_b = _make_standard(db_session, company_id=COMPANY_B)
    clause_b = _make_clause(db_session, standard=standard_b, clause_number="9.2", title="Internal audit")
    stale_b = _make_evidence(db_session, clause=clause_b)
    foreign_on_a = _make_evidence(db_session, clause=clause_a, company_id=COMPANY_B)

    stale_a_id, stale_b_id, foreign_id = stale_a.id, stale_b.id, foreign_on_a.id
    standard_a_id, standard_b_id = standard_a.id, standard_b.id
    user_a = _make_user(db_session, company_id=COMPANY_A)

    body = _auto_link(client, standard_a_id, user_a)

    assert body["total_evidence_retired"] == 1
    assert _evidence(db_session, stale_a_id).is_deleted is True
    for evidence_id in (stale_b_id, foreign_id):
        _assert_live(db_session, evidence_id)
        assert (
            _committed_audit_rows(
                db_session, resource_type="qms_clause_evidence", resource_id=evidence_id, action="DELETE"
            )
            == []
        )

    # A's token cannot reach B's standard at all.
    resp = client.post(f"/api/v1/qms-standards/{standard_b_id}/auto-link", headers=_headers_for(user_a))
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    _assert_live(db_session, stale_b_id)
