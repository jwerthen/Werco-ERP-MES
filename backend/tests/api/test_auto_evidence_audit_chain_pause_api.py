"""HTTP-level coverage for the QMS "Audit Trail" evidence row under a paused hash chain.

Companion to tests/services/test_auto_evidence_audit_chain_pause.py, which pins every branch
of ``_query_audit_log``. This file proves the two doors that put that row in front of an
auditor carry the pause through:

* ``GET /api/v1/qms-standards/clauses/{clause_id}/auto-evidence`` -- the live view. The
  ``AutoEvidenceResult`` response model must keep ``health_status`` / ``health_detail`` /
  ``suggested_compliance``, the clause roll-up must drop to ``partial``, and the setting must
  be read per REQUEST (no restart, no cached answer).
* ``POST /api/v1/qms-standards/{standard_id}/auto-link`` -- the persisted view. The stored
  ``QMSClauseEvidence`` row has no health fields, so its ``description`` is the only place a
  pause shows on the record of evidence; a re-run must overwrite a stale unqualified "chain"
  claim rather than keep it -- and a run in which the audit rule FAILS must leave the stored
  link exactly as the last run that computed it wrote it, pre-rename title included.

The paused rows are written by the app's own request-scoped writer (``get_audit_service``)
through real API calls with the setting flipped, so the reader is held to the placeholder the
production writer actually stamps.
"""

from datetime import datetime

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.qms_standard import QMSClause, QMSClauseEvidence, QMSStandard
from app.models.user import User, UserRole
from app.services import auto_evidence_service
from app.services.audit_service import PAUSED_CHAIN_PLACEHOLDER

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

# Not the default seeded company 1, so nothing a fixture or app startup writes there can
# move the counts this file asserts exactly.
COMPANY = 941

AUDIT_TITLE = "Audit Trail (Audit Log & Hash-Chain Status)"
# What every link auto-linked before the rename still carries.
PRE_RENAME_AUDIT_TITLE = "Audit Trail (Immutable Hash-Chain Log)"

STALE_LAST_REFRESHED = datetime(2026, 1, 2, 3, 4, 5)
STALE_UPDATED_AT = datetime(2026, 1, 2, 3, 4, 6)

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


@pytest.fixture(autouse=True)
def _reset_trigger_read_failure_flag():
    """Start and end every test with no failed ``pg_trigger`` read on record.

    ``auto_evidence_service._trigger_read_failure_reported`` is module state that outlives a test
    (see the same fixture in tests/services/test_auto_evidence_audit_chain_pause.py). Nothing here
    sets it today: these tests run on SQLite, where the trigger check returns before it touches
    the flag. The reset is for a test added here that fakes PostgreSQL, so it neither inherits a
    value an earlier test on this worker left nor hands one on. Plain assignment: ``monkeypatch``
    would restore a leaked value.
    """
    auto_evidence_service._trigger_read_failure_reported = False
    yield
    auto_evidence_service._trigger_read_failure_reported = False


def _set_chain(monkeypatch, enabled: bool) -> None:
    """Flip the one ``settings`` object both the request-scoped writer and the evidence row read."""
    monkeypatch.setattr(settings, "AUDIT_HASH_CHAIN_ENABLED", enabled)


def _ensure_company(db: Session, company_id: int) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"chain-api-{company_id}", is_active=True))
        db.commit()


def _make_user(db: Session, *, company_id: int = COMPANY, role: UserRole = UserRole.QUALITY) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"chain-api-{n}@co{company_id}.test",
        employee_id=f"CHA-{n:05d}",
        first_name="Chain",
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


def _create_standard_via_api(client: TestClient, headers: dict) -> int:
    """One audited write through the real route -- ``create_standard`` logs exactly one CREATE."""
    resp = client.post(
        "/api/v1/qms-standards/",
        json={"name": f"CHAIN-API-STD-{_next()}", "version": "2016", "standard_body": "SAE"},
        headers=headers,
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    return resp.json()["id"]


def _add_audit_clause(db: Session, standard_id: int) -> QMSClause:
    """A clause whose text routes to the audit rule alone (no audit row: seeded, not an API write)."""
    clause = QMSClause(
        standard_id=standard_id,
        company_id=COMPANY,
        clause_number=f"7.5.{_next()}",
        title="Audit trail",
        description="audit log of record changes",
    )
    db.add(clause)
    db.commit()
    db.refresh(clause)
    return clause


def _seed_standard_with_audit_clause(db: Session) -> tuple:
    """A standard + audit clause written straight to the DB, so neither adds an audit row."""
    _ensure_company(db, COMPANY)
    standard = QMSStandard(name=f"CHAIN-API-SEED-{_next()}", version="2016", is_active=True, company_id=COMPANY)
    db.add(standard)
    db.commit()
    db.refresh(standard)
    return standard, _add_audit_clause(db, standard.id)


def _company_hashes(db: Session) -> list:
    db.expire_all()
    return [
        h for (h,) in db.query(AuditLog.integrity_hash).filter(AuditLog.company_id == COMPANY).order_by(AuditLog.id)
    ]


def _audit_block(body: dict) -> dict:
    blocks = [e for e in body["discovered_evidence"] if e["title"] == AUDIT_TITLE]
    assert len(blocks) == 1, body["discovered_evidence"]
    return blocks[0]


def _get_auto_evidence(client: TestClient, headers: dict, clause_id: int) -> dict:
    resp = client.get(f"/api/v1/qms-standards/clauses/{clause_id}/auto-evidence", headers=headers)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()


def _auto_link(client: TestClient, headers: dict, standard_id: int) -> dict:
    resp = client.post(f"/api/v1/qms-standards/{standard_id}/auto-link", headers=headers)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()


def _stored_audit_evidence(client: TestClient, headers: dict, standard_id: int, clause_id: int) -> dict:
    """What an auditor reads back: the clause's evidence links, through ``GET /{standard_id}/clauses``."""
    resp = client.get(f"/api/v1/qms-standards/{standard_id}/clauses", headers=headers)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    clause = next(c for c in resp.json() if c["id"] == clause_id)
    links = [e for e in clause["evidence_links"] if e["is_auto_linked"] and e["auto_link_query"] == "audit"]
    assert len(links) == 1, clause["evidence_links"]
    return links[0]


# ---------------------------------------------------------------------------
# GET /clauses/{clause_id}/auto-evidence
# ---------------------------------------------------------------------------


def test_auto_evidence_endpoint_counts_rows_the_api_wrote_while_paused(
    client: TestClient, db_session: Session, monkeypatch
):
    """A write through the request-scoped writer while paused is what the live view reports."""
    headers = _headers_for(_make_user(db_session))
    _set_chain(monkeypatch, True)
    _create_standard_via_api(client, headers)
    _set_chain(monkeypatch, False)
    standard_id = _create_standard_via_api(client, headers)
    hashes = _company_hashes(db_session)
    assert len(hashes) == 2
    assert hashes[0] != PAUSED_CHAIN_PLACEHOLDER
    assert hashes[1] == PAUSED_CHAIN_PLACEHOLDER
    clause = _add_audit_clause(db_session, standard_id)

    body = _get_auto_evidence(client, headers, clause.id)

    assert body["clause_id"] == clause.id
    assert body["overall_suggested_compliance"] == "partial"
    block = _audit_block(body)
    assert block["health_status"] == "warning"
    assert block["suggested_compliance"] == "partial"
    assert block["total_count"] == 2
    assert block["recent_count"] == 2
    assert block["description"] == (
        "2 audit log entries (2 in last 12 months); SHA-256 integrity chain paused "
        "(1 entry written without it, 1 in last 12 months)"
    )
    assert "AUDIT_HASH_CHAIN_ENABLED is off" in block["health_detail"]
    assert "Entries written while paused: 1 of 2 (1 in the last 12 months)." in block["health_detail"]
    assert "hash chain enabled" not in block["health_detail"]


def test_auto_evidence_endpoint_reads_the_chain_setting_per_request(
    client: TestClient, db_session: Session, monkeypatch
):
    """Same clause, same rows: flipping the setting changes the very next response, both ways.

    Also pins that the GET is a pure read -- it writes no audit row of its own, so the paused
    count stays at zero and resuming the chain restores ``compliant``.
    """
    headers = _headers_for(_make_user(db_session))
    _set_chain(monkeypatch, True)
    standard_id = _create_standard_via_api(client, headers)
    clause = _add_audit_clause(db_session, standard_id)

    on = _get_auto_evidence(client, headers, clause.id)
    _set_chain(monkeypatch, False)
    paused = _get_auto_evidence(client, headers, clause.id)
    _set_chain(monkeypatch, True)
    resumed = _get_auto_evidence(client, headers, clause.id)

    assert on["overall_suggested_compliance"] == "compliant"
    assert _audit_block(on)["health_status"] == "healthy"
    assert _audit_block(on)["description"] == "1 audit log entry (1 in last 12 months), SHA-256 integrity chain"

    assert paused["overall_suggested_compliance"] == "partial"
    assert _audit_block(paused)["health_status"] == "warning"
    assert _audit_block(paused)["description"] == (
        "1 audit log entry (1 in last 12 months); SHA-256 integrity chain paused " "(no entries written without it yet)"
    )

    assert resumed == on
    hashes = _company_hashes(db_session)
    assert len(hashes) == 1
    assert PAUSED_CHAIN_PLACEHOLDER not in hashes


# ---------------------------------------------------------------------------
# POST /{standard_id}/auto-link
# ---------------------------------------------------------------------------


def test_auto_link_persists_the_paused_chain_disclosure(client: TestClient, db_session: Session, monkeypatch):
    """The stored evidence row carries no health fields, so its description must say it."""
    headers = _headers_for(_make_user(db_session))
    _set_chain(monkeypatch, False)
    _create_standard_via_api(client, headers)
    _create_standard_via_api(client, headers)
    standard, clause = _seed_standard_with_audit_clause(db_session)

    summary = _auto_link(client, headers, standard.id)

    assert summary["total_clauses"] == 1
    assert summary["clauses_with_evidence"] == 1
    assert summary["total_evidence_created"] == 1
    assert summary["total_evidence_updated"] == 0
    assert summary["compliance_summary"] == {"partial": 1}
    stored = _stored_audit_evidence(client, headers, standard.id, clause.id)
    assert stored["title"] == AUDIT_TITLE
    assert stored["module_reference"] == "/audit"
    assert stored["live_count"] == 2
    assert stored["description"] == (
        "2 audit log entries (2 in last 12 months); SHA-256 integrity chain paused "
        "(2 entries written without it, 2 in last 12 months)"
    )


def test_auto_link_rerun_replaces_a_stale_chain_claim_on_the_same_row(
    client: TestClient, db_session: Session, monkeypatch
):
    """Linked while healthy, re-linked after a pause, re-linked after resuming: one row, current text.

    The auto-link's own audit row counts too: the run made while paused writes its
    ``log_create`` with the placeholder, so the run after resuming reports that one entry as
    written without the chain -- the stored claim never reverts to an unqualified "chain".
    """
    headers = _headers_for(_make_user(db_session))
    _set_chain(monkeypatch, True)
    _create_standard_via_api(client, headers)
    standard, clause = _seed_standard_with_audit_clause(db_session)

    first = _auto_link(client, headers, standard.id)
    linked_on = _stored_audit_evidence(client, headers, standard.id, clause.id)
    _set_chain(monkeypatch, False)
    second = _auto_link(client, headers, standard.id)
    linked_paused = _stored_audit_evidence(client, headers, standard.id, clause.id)
    _set_chain(monkeypatch, True)
    third = _auto_link(client, headers, standard.id)
    linked_resumed = _stored_audit_evidence(client, headers, standard.id, clause.id)

    assert first["compliance_summary"] == {"compliant": 1}
    assert first["total_evidence_created"] == 1
    # Before the first run: the API-created standard's CREATE.
    assert linked_on["description"] == "1 audit log entry (1 in last 12 months), SHA-256 integrity chain"

    assert second["compliance_summary"] == {"partial": 1}
    assert (second["total_evidence_created"], second["total_evidence_updated"]) == (0, 1)
    assert linked_paused["id"] == linked_on["id"]
    # Before the second run: + the first auto-link's own (chained) CREATE.
    assert linked_paused["live_count"] == 2
    assert linked_paused["description"] == (
        "2 audit log entries (2 in last 12 months); SHA-256 integrity chain paused "
        "(no entries written without it yet)"
    )

    assert third["compliance_summary"] == {"partial": 1}
    assert (third["total_evidence_created"], third["total_evidence_updated"]) == (0, 1)
    assert linked_resumed["id"] == linked_on["id"]
    # Before the third run: + the second auto-link's CREATE, written while paused.
    assert linked_resumed["live_count"] == 3
    assert linked_resumed["description"] == (
        "3 audit log entries (3 in last 12 months); SHA-256 integrity chain enabled, "
        "but 1 entry was written while it was paused (1 in last 12 months)"
    )

    assert _company_hashes(db_session).count(PAUSED_CHAIN_PLACEHOLDER) == 1
    evidence_rows = (
        db_session.query(QMSClauseEvidence)
        .filter(QMSClauseEvidence.clause_id == clause.id, QMSClauseEvidence.auto_link_query == "audit")
        .count()
    )
    assert evidence_rows == 1


class _TriggerReadBoom(RuntimeError):
    pass


def _stale_pre_rename_audit_link(db: Session, clause: QMSClause) -> QMSClauseEvidence:
    """An audit link as a run before the rename stored it: old title, old wording, stale stamps."""
    evidence = QMSClauseEvidence(
        company_id=COMPANY,
        clause_id=clause.id,
        evidence_type="module",
        title=PRE_RENAME_AUDIT_TITLE,
        description="5 audit log entries (5 in last 12 months), SHA-256 integrity chain",
        module_reference="/audit",
        record_type="module",
        is_auto_linked=True,
        auto_link_query="audit",
        live_count=5,
        last_refreshed=STALE_LAST_REFRESHED,
        updated_at=STALE_UPDATED_AT,
    )
    db.add(evidence)
    db.commit()
    db.refresh(evidence)
    return evidence


def _evidence_snapshot(db: Session, evidence_id: int) -> dict:
    db.expire_all()
    row = db.query(QMSClauseEvidence).filter(QMSClauseEvidence.id == evidence_id).one()
    return {
        "title": row.title,
        "description": row.description,
        "module_reference": row.module_reference,
        "live_count": row.live_count,
        "last_refreshed": row.last_refreshed,
        "updated_at": row.updated_at,
        "is_deleted": row.is_deleted,
    }


def _auto_link_audit_rows(db: Session, standard_id: int) -> list:
    db.expire_all()
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY,
            AuditLog.resource_type == "qms_standard",
            AuditLog.resource_id == standard_id,
            AuditLog.description.like("Auto-linked evidence:%"),
        )
        .order_by(AuditLog.id)
        .all()
    )


def test_auto_link_leaves_the_stored_audit_link_alone_when_the_audit_rule_fails(
    client: TestClient, db_session: Session, monkeypatch
):
    """The docs promise stored links keep the pre-rename title "until the next auto-link run in
    which the audit rule computes". A run where it fails rewrites nothing -- not the title, the
    description, the count or the stamps -- and names the skipped (clause, rule) on its audit row.

    The failure is raised from inside the real ``_query_audit_log``, after its aggregate query has
    run, by making the trigger helper it calls raise. A control run on the same row then proves
    the lookup really matched it: once the rule computes, title and wording are rewritten.
    """
    headers = _headers_for(_make_user(db_session))
    _set_chain(monkeypatch, True)
    standard, clause = _seed_standard_with_audit_clause(db_session)
    stale = _stale_pre_rename_audit_link(db_session, clause)
    before = _evidence_snapshot(db_session, stale.id)

    def _boom(db):
        raise _TriggerReadBoom("audit rule exploded mid-query")

    with monkeypatch.context() as failing:
        failing.setattr(auto_evidence_service, "_audit_triggers_in_force", _boom)
        failed = _auto_link(client, headers, standard.id)

    assert failed["total_evidence_failed"] == 1
    assert (failed["total_evidence_created"], failed["total_evidence_updated"]) == (0, 0)
    assert failed["clauses_with_evidence"] == 0
    assert failed["compliance_summary"] == {"not_assessed": 1}
    assert _evidence_snapshot(db_session, stale.id) == before
    stored = _stored_audit_evidence(client, headers, standard.id, clause.id)
    assert stored["id"] == stale.id
    assert stored["title"] == PRE_RENAME_AUDIT_TITLE
    assert stored["description"] == "5 audit log entries (5 in last 12 months), SHA-256 integrity chain"
    audit_rows = _auto_link_audit_rows(db_session, standard.id)
    assert len(audit_rows) == 1
    assert audit_rows[0].description == "Auto-linked evidence: 0 created, 0 updated, 1 not computed (left unchanged)"
    assert audit_rows[0].extra_data == {
        "evidence_not_computed": [
            {"clause_id": clause.id, "clause_number": clause.clause_number, "rule_id": "audit"},
        ]
    }

    # Control: the rule computes, and the same row picks up the new title and wording. The one
    # entry it counts is the failed run's own audit row.
    computed = _auto_link(client, headers, standard.id)

    assert computed["total_evidence_failed"] == 0
    assert (computed["total_evidence_created"], computed["total_evidence_updated"]) == (0, 1)
    assert computed["compliance_summary"] == {"compliant": 1}
    refreshed = _stored_audit_evidence(client, headers, standard.id, clause.id)
    assert refreshed["id"] == stale.id
    assert refreshed["title"] == AUDIT_TITLE
    assert refreshed["description"] == "1 audit log entry (1 in last 12 months), SHA-256 integrity chain"
    assert refreshed["live_count"] == 1
    assert _evidence_snapshot(db_session, stale.id)["last_refreshed"] > STALE_LAST_REFRESHED
    audit_rows = _auto_link_audit_rows(db_session, standard.id)
    assert len(audit_rows) == 2
    assert audit_rows[1].extra_data is None
