"""The QMS "Audit Trail" evidence row must not claim a hash chain that is paused.

``_query_audit_log`` (``app.services.auto_evidence_service``) feeds an auditor-facing view.
It used to say "integrity hash chain active" and suggest ``compliant`` unconditionally, but
the chain is runtime-pausable (``settings.AUDIT_HASH_CHAIN_ENABLED``, CLAUDE.md invariant 2):
paused rows carry the ``LEGACY_CHAIN_PAUSED`` placeholder and can never be verified. The
owner's rule for this view is that a false "compliant" or a false claim is worse than
"not assessed", so:

* chain on, no paused entries in the 12-month window -> ``healthy`` / ``compliant``;
* chain paused now, or paused entries in the window -> ``warning`` / ``partial``, and both
  ``description`` (what auto-link persists) and ``health_detail`` say so;
* no entries at all -> ``no_data`` / ``not_assessed``, still honest about a pause.

Rows are written through ``AuditService`` with the setting flipped, so the paused rows carry
the real placeholder the production writer stamps -- the test does not hand-build them.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.qms_standard import QMSClause, QMSStandard
from app.models.user import User, UserRole
from app.services import auto_evidence_service
from app.services.audit_service import PAUSED_CHAIN_PLACEHOLDER, AuditService
from app.services.auto_evidence_service import (
    AUDIT_TRIGGERS_SENTENCE,
    EVIDENCE_MAPPING_RULES,
    _query_audit_log,
    compute_overall_compliance,
    discover_evidence_for_clause,
)

pytestmark = [pytest.mark.unit, pytest.mark.requires_db]

# Distinct from the ids other auto-evidence suites use, so counts here are this file's own
# even on a worker DB those suites share.
COMPANY_A = 931
COMPANY_B = 932

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _set_chain(monkeypatch, enabled: bool) -> None:
    """Flip the one ``settings`` object both the writer and the evidence row read per call."""
    monkeypatch.setattr(settings, "AUDIT_HASH_CHAIN_ENABLED", enabled)


def _ensure_company(db: Session, company_id: int) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"chain-co-{company_id}", is_active=True))
        db.commit()


def _writer(db: Session, company_id: int) -> AuditService:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"chain-evidence-{n}@co{company_id}.test",
        employee_id=f"CHN-{n:05d}",
        first_name="Chain",
        last_name=f"C{company_id}",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=UserRole.ADMIN,
        is_active=True,
        is_superuser=False,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    svc = AuditService(db, user)
    assert svc.company_id == company_id
    return svc


def _write(db: Session, svc: AuditService, count: int) -> None:
    for _ in range(count):
        row = svc.log(action="CREATE", resource_type="part", resource_id=_next(), resource_identifier="P")
        assert row is not None
    db.commit()


def _company_hashes(db: Session, company_id: int) -> list:
    return [h for (h,) in db.query(AuditLog.integrity_hash).filter(AuditLog.company_id == company_id).all()]


def _audit_rule() -> dict:
    return next(rule for rule in EVIDENCE_MAPPING_RULES if rule["id"] == "audit")


def _make_clause(db: Session, company_id: int) -> QMSClause:
    n = _next()
    standard = QMSStandard(name=f"CHAIN-STD-{n}", version="2016", is_active=True, company_id=company_id)
    db.add(standard)
    db.flush()
    clause = QMSClause(
        standard_id=standard.id,
        company_id=company_id,
        clause_number=f"7.5.{n}",
        title="Audit trail",
        description="audit log of record changes",
    )
    db.add(clause)
    db.commit()
    db.refresh(clause)
    return clause


def _assert_no_chain_claim(result: dict) -> None:
    """Paused-state text must never present the chain as protecting the trail."""
    assert "chain active;" not in result["health_detail"]
    assert "hash chain active" not in result["health_detail"]
    assert not result["description"].endswith(", SHA-256 integrity chain")


# ---------------------------------------------------------------------------
# Chain on
# ---------------------------------------------------------------------------


def test_chain_on_with_only_chained_entries_is_healthy_and_compliant(db_session: Session, monkeypatch):
    _set_chain(monkeypatch, True)
    _write(db_session, _writer(db_session, COMPANY_A), 3)
    assert PAUSED_CHAIN_PLACEHOLDER not in _company_hashes(db_session, COMPANY_A)

    result = _query_audit_log(db_session, COMPANY_A)

    assert result["health_status"] == "healthy"
    assert result["suggested_compliance"] == "compliant"
    assert result["total_count"] == 3
    assert result["recent_count"] == 3
    assert result["description"] == "3 audit log entries (3 in last 12 months), SHA-256 integrity chain"
    # Names the window it actually checked, rather than vouching for the whole history.
    assert result["health_detail"] == (
        "Immutable audit trail with 3 entries, integrity hash chain active; "
        "no entries in the last 12 months were written while it was paused"
    )


def test_chain_on_but_paused_entries_in_window_is_warning_and_partial(db_session: Session, monkeypatch):
    """Resumed chain: new rows are hashed again, the paused span in the window is not."""
    svc = _writer(db_session, COMPANY_A)
    _set_chain(monkeypatch, True)
    _write(db_session, svc, 2)
    _set_chain(monkeypatch, False)
    _write(db_session, svc, 3)
    _set_chain(monkeypatch, True)
    _write(db_session, svc, 1)
    assert _company_hashes(db_session, COMPANY_A).count(PAUSED_CHAIN_PLACEHOLDER) == 3

    result = _query_audit_log(db_session, COMPANY_A)

    assert result["health_status"] == "warning"
    assert result["suggested_compliance"] == "partial"
    assert result["total_count"] == 6
    assert result["recent_count"] == 6
    assert result["description"] == (
        "6 audit log entries (6 in last 12 months); SHA-256 integrity chain active, "
        "but 3 entries in last 12 months were written while it was paused"
    )
    assert "Entries in the last 12 months written while the integrity hash chain was paused: 3 of 6." in (
        result["health_detail"]
    )
    assert "can never be cryptographically verified" in result["health_detail"]
    assert result["health_detail"].endswith(AUDIT_TRIGGERS_SENTENCE)


def test_paused_entries_older_than_the_window_are_not_counted(db_session: Session, monkeypatch):
    """The paused count is the 12-month window's, which is what the healthy text says it checked.

    Moving the cutoff past every row puts the paused rows outside the window; the row then
    reports the chain as it is now (on) and makes no claim about the entries it did not check.
    """
    svc = _writer(db_session, COMPANY_A)
    _set_chain(monkeypatch, False)
    _write(db_session, svc, 2)
    _set_chain(monkeypatch, True)
    monkeypatch.setattr(auto_evidence_service, "_recent_cutoff", lambda: datetime.utcnow() + timedelta(days=1))

    result = _query_audit_log(db_session, COMPANY_A)

    assert result["total_count"] == 2
    assert result["recent_count"] == 0
    assert result["health_status"] == "healthy"
    assert "no entries in the last 12 months were written while it was paused" in result["health_detail"]


# ---------------------------------------------------------------------------
# Chain paused now
# ---------------------------------------------------------------------------


def test_chain_paused_now_is_warning_and_partial_and_says_so(db_session: Session, monkeypatch):
    svc = _writer(db_session, COMPANY_A)
    _set_chain(monkeypatch, True)
    _write(db_session, svc, 1)
    _set_chain(monkeypatch, False)
    _write(db_session, svc, 2)

    result = _query_audit_log(db_session, COMPANY_A)

    assert result["health_status"] == "warning"
    assert result["suggested_compliance"] == "partial"
    assert result["description"] == (
        "3 audit log entries (3 in last 12 months); SHA-256 integrity chain paused "
        "(2 entries in last 12 months written without it)"
    )
    assert result["health_detail"].startswith(
        "The integrity hash chain is paused (AUDIT_HASH_CHAIN_ENABLED is off): entries written "
        "while it is paused carry no integrity hash and can never be cryptographically verified."
    )
    assert "Entries in the last 12 months written while paused: 2 of 3." in result["health_detail"]
    # The append-only triggers do not depend on the setting, so the row may state them.
    assert result["health_detail"].endswith(AUDIT_TRIGGERS_SENTENCE)
    _assert_no_chain_claim(result)


def test_chain_paused_now_with_no_paused_entries_yet_is_still_partial(db_session: Session, monkeypatch):
    """Just flipped off: every stored row is hashed, but the next one will not be."""
    _set_chain(monkeypatch, True)
    _write(db_session, _writer(db_session, COMPANY_A), 2)
    _set_chain(monkeypatch, False)

    result = _query_audit_log(db_session, COMPANY_A)

    assert result["health_status"] == "warning"
    assert result["suggested_compliance"] == "partial"
    assert "SHA-256 integrity chain paused (0 entries in last 12 months written without it)" in (result["description"])
    _assert_no_chain_claim(result)


def test_no_entries_with_chain_paused_is_not_assessed_and_discloses_pause(db_session: Session, monkeypatch):
    _ensure_company(db_session, COMPANY_A)
    _set_chain(monkeypatch, False)

    result = _query_audit_log(db_session, COMPANY_A)

    assert result["health_status"] == "no_data"
    assert result["suggested_compliance"] == "not_assessed"
    assert result["total_count"] == 0
    assert result["description"] == "0 audit log entries (0 in last 12 months); SHA-256 integrity chain paused"
    assert "integrity hash chain is paused" in result["health_detail"]
    _assert_no_chain_claim(result)


def test_no_entries_with_chain_on_is_unchanged(db_session: Session, monkeypatch):
    _ensure_company(db_session, COMPANY_A)
    _set_chain(monkeypatch, True)

    result = _query_audit_log(db_session, COMPANY_A)

    assert result["health_status"] == "no_data"
    assert result["suggested_compliance"] == "not_assessed"
    assert result["description"] == "0 audit log entries (0 in last 12 months), SHA-256 integrity chain"
    assert result["health_detail"] == "No audit log entries found"


# ---------------------------------------------------------------------------
# Scope, shape and roll-up
# ---------------------------------------------------------------------------


def test_another_companys_paused_entries_do_not_downgrade_this_company(db_session: Session, monkeypatch):
    """The paused count is tenant-scoped like every other figure on the row."""
    svc_a = _writer(db_session, COMPANY_A)
    svc_b = _writer(db_session, COMPANY_B)
    _set_chain(monkeypatch, True)
    _write(db_session, svc_a, 2)
    _set_chain(monkeypatch, False)
    _write(db_session, svc_b, 4)
    _set_chain(monkeypatch, True)

    result_a = _query_audit_log(db_session, COMPANY_A)
    result_b = _query_audit_log(db_session, COMPANY_B)

    assert result_a["health_status"] == "healthy"
    assert result_a["suggested_compliance"] == "compliant"
    assert result_a["total_count"] == 2
    assert result_b["health_status"] == "warning"
    assert result_b["suggested_compliance"] == "partial"
    assert "4 entries in last 12 months were written while it was paused" in result_b["description"]


@pytest.mark.parametrize("chain_enabled", [True, False])
def test_title_and_module_reference_stay_pinned_to_the_rule(db_session: Session, monkeypatch, chain_enabled):
    """Title / module_reference / evidence_type do not vary with the chain state.

    Auto-link matches stored evidence by rule id and overwrites ``title``, and the rule's own
    copy of these fields stands in when the query fails; a state-dependent title would drift.
    """
    _set_chain(monkeypatch, True)
    _write(db_session, _writer(db_session, COMPANY_A), 1)
    _set_chain(monkeypatch, chain_enabled)

    result = _query_audit_log(db_session, COMPANY_A)

    assert result["evidence_type"] == "module"
    assert result["title"] == "Audit Trail (Immutable Hash-Chain Log)"
    assert result["module_reference"] == "/audit"
    assert set(result) == {
        "evidence_type",
        "title",
        "description",
        "module_reference",
        "total_count",
        "recent_count",
        "health_status",
        "health_detail",
        "examples",
        "suggested_compliance",
    }
    assert _audit_rule()["query_fn"] is _query_audit_log


def test_clause_backed_only_by_a_paused_audit_trail_never_rolls_up_compliant(db_session: Session, monkeypatch):
    svc = _writer(db_session, COMPANY_A)
    _set_chain(monkeypatch, False)
    _write(db_session, svc, 2)
    clause = _make_clause(db_session, COMPANY_A)

    results = discover_evidence_for_clause(db_session, clause, COMPANY_A)

    assert [r["_rule_id"] for r in results] == ["audit"]
    assert results[0]["suggested_compliance"] == "partial"
    assert compute_overall_compliance(results) == "partial"
