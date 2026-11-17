"""The QMS "Audit Trail" evidence row must not claim protection the system lacks.

``_query_audit_log`` (``app.services.auto_evidence_service``) feeds an auditor-facing view.
It used to say "integrity hash chain active" and suggest ``compliant`` unconditionally, but
the chain is runtime-pausable (``settings.AUDIT_HASH_CHAIN_ENABLED``, CLAUDE.md invariant 2):
paused rows carry the ``LEGACY_CHAIN_PAUSED`` placeholder and can never be verified. The
owner's rule for this view is that a false "compliant" or a false claim is worse than
"not assessed", so:

* chain on, no paused entry anywhere in the company's history, triggers not reported
  missing -> ``healthy`` / ``compliant``;
* chain paused now, any paused entry (however old), or the UPDATE/DELETE triggers missing ->
  ``warning`` / ``partial``, and ``description`` (what auto-link persists) and
  ``health_detail`` both say why;
* no entries at all -> ``no_data`` / ``not_assessed``, still honest about a pause.

Rows are written through ``AuditService`` with the setting flipped, so the paused rows carry
the real placeholder the production writer stamps -- the test does not hand-build them. The
suite runs on SQLite, where trigger state cannot be read (``None`` -> "could not be checked");
the PostgreSQL answers are injected by patching ``_audit_triggers_in_force``.
"""

from datetime import datetime, timedelta
from pathlib import Path

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
    _AUDIT_LOG_TRIGGER_CHECK_SQL,
    AUDIT_LOG_TRIGGER_NAMES,
    AUDIT_TRIGGERS_IN_FORCE_SENTENCE,
    AUDIT_TRIGGERS_MISSING_SENTENCE,
    AUDIT_TRIGGERS_UNCHECKED_SENTENCE,
    EVIDENCE_MAPPING_RULES,
    _audit_triggers_in_force,
    _query_audit_log,
    compute_overall_compliance,
    discover_evidence_for_clause,
)

pytestmark = [pytest.mark.unit, pytest.mark.requires_db]

# Distinct from the ids other auto-evidence suites use, so counts here are this file's own
# even on a worker DB those suites share.
COMPANY_A = 931
COMPANY_B = 932

HEALTHY_CHAIN_DETAIL = (
    "Integrity hash chain enabled and no entry was written while it was paused "
    "(chain integrity is not re-verified by this check)."
)

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _set_chain(monkeypatch, enabled: bool) -> None:
    """Flip the one ``settings`` object both the writer and the evidence row read per call."""
    monkeypatch.setattr(settings, "AUDIT_HASH_CHAIN_ENABLED", enabled)


def _set_triggers(monkeypatch, state) -> None:
    """Inject what the PostgreSQL catalog read would answer (True / False / None)."""
    monkeypatch.setattr(auto_evidence_service, "_audit_triggers_in_force", lambda db: state)


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


def _cutoff_after_every_row(monkeypatch) -> None:
    """Move the 12-month cutoff past every row, so every entry reads as older than the window."""
    monkeypatch.setattr(auto_evidence_service, "_recent_cutoff", lambda: datetime.utcnow() + timedelta(days=1))


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
    assert "hash chain enabled and" not in result["health_detail"]
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
    # SQLite: no catalog to read, so the triggers are neither claimed nor denied.
    assert result["health_detail"] == (
        f"Audit trail with 3 entries. {HEALTHY_CHAIN_DETAIL} {AUDIT_TRIGGERS_UNCHECKED_SENTENCE}"
    )
    assert "Immutable" not in result["health_detail"]


def test_chain_on_but_paused_entries_in_history_is_warning_and_partial(db_session: Session, monkeypatch):
    """Resumed chain: new rows are hashed again, the paused span is not."""
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
        "6 audit log entries (6 in last 12 months); SHA-256 integrity chain enabled, "
        "but 3 entries were written while it was paused (3 in last 12 months)"
    )
    assert result["health_detail"] == (
        "Entries written while the integrity hash chain was paused: 3 of 6 (3 in the last 12 months). "
        "They carry no integrity hash and can never be cryptographically verified. "
        f"The chain setting is on again. {AUDIT_TRIGGERS_UNCHECKED_SENTENCE}"
    )
    _assert_no_chain_claim(result)


def test_paused_entries_older_than_the_window_still_cap_the_row(db_session: Session, monkeypatch):
    """A paused span never becomes verifiable by ageing, so it is counted over the whole history.

    With a 12-month count only, a company whose every entry was written while paused would read
    ``healthy`` / ``compliant`` "SHA-256 integrity chain" once those entries left the window.
    """
    svc = _writer(db_session, COMPANY_A)
    _set_chain(monkeypatch, False)
    _write(db_session, svc, 2)
    _set_chain(monkeypatch, True)
    _cutoff_after_every_row(monkeypatch)

    result = _query_audit_log(db_session, COMPANY_A)

    assert result["total_count"] == 2
    assert result["recent_count"] == 0
    assert result["health_status"] == "warning"
    assert result["suggested_compliance"] == "partial"
    assert result["description"] == (
        "2 audit log entries (0 in last 12 months); SHA-256 integrity chain enabled, "
        "but 2 entries were written while it was paused (0 in last 12 months)"
    )
    assert "paused: 2 of 2 (0 in the last 12 months)." in result["health_detail"]


def test_a_single_paused_entry_reads_in_the_singular(db_session: Session, monkeypatch):
    svc = _writer(db_session, COMPANY_A)
    _set_chain(monkeypatch, False)
    _write(db_session, svc, 1)
    _set_chain(monkeypatch, True)

    result = _query_audit_log(db_session, COMPANY_A)

    assert result["description"] == (
        "1 audit log entry (1 in last 12 months); SHA-256 integrity chain enabled, "
        "but 1 entry was written while it was paused (1 in last 12 months)"
    )


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
        "(2 entries written without it, 2 in last 12 months)"
    )
    assert result["health_detail"] == (
        "The integrity hash chain is paused (AUDIT_HASH_CHAIN_ENABLED is off): entries written "
        "while it is paused carry no integrity hash and can never be cryptographically verified. "
        f"Entries written while paused: 2 of 3 (2 in the last 12 months). {AUDIT_TRIGGERS_UNCHECKED_SENTENCE}"
    )
    _assert_no_chain_claim(result)


def test_chain_paused_now_with_no_paused_entries_yet_is_still_partial(db_session: Session, monkeypatch):
    """Just flipped off: every stored row is hashed, but the next one will not be."""
    _set_chain(monkeypatch, True)
    _write(db_session, _writer(db_session, COMPANY_A), 2)
    _set_chain(monkeypatch, False)

    result = _query_audit_log(db_session, COMPANY_A)

    assert result["health_status"] == "warning"
    assert result["suggested_compliance"] == "partial"
    assert result["description"] == (
        "2 audit log entries (2 in last 12 months); SHA-256 integrity chain paused "
        "(no entries written without it yet)"
    )
    _assert_no_chain_claim(result)


def test_no_entries_with_chain_paused_is_not_assessed_and_discloses_pause(db_session: Session, monkeypatch):
    _ensure_company(db_session, COMPANY_A)
    _set_chain(monkeypatch, False)

    result = _query_audit_log(db_session, COMPANY_A)

    assert result["health_status"] == "no_data"
    assert result["suggested_compliance"] == "not_assessed"
    assert result["total_count"] == 0
    assert result["description"] == "0 audit log entries (0 in last 12 months); SHA-256 integrity chain paused"
    assert result["health_detail"] == (
        "No audit log entries found. The integrity hash chain is paused, so entries written now "
        f"will not be hash-chained. {AUDIT_TRIGGERS_UNCHECKED_SENTENCE}"
    )


def test_no_entries_with_chain_on_is_unchanged(db_session: Session, monkeypatch):
    _ensure_company(db_session, COMPANY_A)
    _set_chain(monkeypatch, True)

    result = _query_audit_log(db_session, COMPANY_A)

    assert result["health_status"] == "no_data"
    assert result["suggested_compliance"] == "not_assessed"
    assert result["description"] == "0 audit log entries (0 in last 12 months), SHA-256 integrity chain"
    assert result["health_detail"] == "No audit log entries found"


# ---------------------------------------------------------------------------
# The UPDATE/DELETE triggers: stated only when the catalog says so
# ---------------------------------------------------------------------------


def test_triggers_in_force_are_stated_and_keep_the_row_healthy(db_session: Session, monkeypatch):
    _set_chain(monkeypatch, True)
    _set_triggers(monkeypatch, True)
    _write(db_session, _writer(db_session, COMPANY_A), 2)

    result = _query_audit_log(db_session, COMPANY_A)

    assert result["health_status"] == "healthy"
    assert result["suggested_compliance"] == "compliant"
    assert result["health_detail"] == (
        f"Audit trail with 2 entries. {HEALTHY_CHAIN_DETAIL} {AUDIT_TRIGGERS_IN_FORCE_SENTENCE}"
    )


def test_triggers_in_force_are_stated_while_the_chain_is_paused(db_session: Session, monkeypatch):
    """With the chain off the triggers are the remaining control; say so, without overclaiming."""
    _set_chain(monkeypatch, False)
    _set_triggers(monkeypatch, True)
    _write(db_session, _writer(db_session, COMPANY_A), 1)

    result = _query_audit_log(db_session, COMPANY_A)

    assert result["suggested_compliance"] == "partial"
    assert result["health_detail"].endswith(AUDIT_TRIGGERS_IN_FORCE_SENTENCE)
    _assert_no_chain_claim(result)


def test_missing_triggers_cap_an_otherwise_healthy_row(db_session: Session, monkeypatch):
    """Prod has run without the 008 triggers before; the row must not call that trail protected."""
    _set_chain(monkeypatch, True)
    _set_triggers(monkeypatch, False)
    _write(db_session, _writer(db_session, COMPANY_A), 2)

    result = _query_audit_log(db_session, COMPANY_A)

    assert result["health_status"] == "warning"
    assert result["suggested_compliance"] == "partial"
    assert result["description"] == (
        "2 audit log entries (2 in last 12 months), SHA-256 integrity chain; "
        "database UPDATE/DELETE triggers missing or disabled"
    )
    assert result["health_detail"].endswith(AUDIT_TRIGGERS_MISSING_SENTENCE)
    assert AUDIT_TRIGGERS_IN_FORCE_SENTENCE not in result["health_detail"]


def test_missing_triggers_and_a_paused_chain_are_both_disclosed(db_session: Session, monkeypatch):
    _set_chain(monkeypatch, False)
    _set_triggers(monkeypatch, False)
    _write(db_session, _writer(db_session, COMPANY_A), 1)

    result = _query_audit_log(db_session, COMPANY_A)

    assert result["suggested_compliance"] == "partial"
    assert result["description"] == (
        "1 audit log entry (1 in last 12 months); SHA-256 integrity chain paused "
        "(1 entry written without it, 1 in last 12 months); database UPDATE/DELETE triggers missing or disabled"
    )
    assert result["health_detail"].endswith(AUDIT_TRIGGERS_MISSING_SENTENCE)


def test_trigger_state_is_not_checked_off_postgresql(db_session: Session):
    assert _audit_triggers_in_force(db_session) is None


def test_failed_trigger_read_on_postgresql_caps_the_row_and_leaves_the_transaction_usable(
    db_session: Session, monkeypatch
):
    """Pretend the bind is PostgreSQL: the ``pg_trigger`` read then really fails on SQLite.

    The failure must answer ``None`` ("could not be checked", never a claim either way), be
    contained by the check's own savepoint so the evidence query after it still runs, and -- on
    PostgreSQL, where the read should have worked -- cap the row: an unknown about the last
    tamper control is not evidence of compliance.
    """
    _set_chain(monkeypatch, True)
    _write(db_session, _writer(db_session, COMPANY_A), 2)
    monkeypatch.setattr(auto_evidence_service, "_dialect_name", lambda db: "postgresql")

    assert _audit_triggers_in_force(db_session) is None
    result = _query_audit_log(db_session, COMPANY_A)

    assert result["total_count"] == 2
    assert result["health_status"] == "warning"
    assert result["suggested_compliance"] == "partial"
    assert result["description"] == (
        "2 audit log entries (2 in last 12 months), SHA-256 integrity chain; "
        "database UPDATE/DELETE trigger status could not be checked"
    )
    assert result["health_detail"].endswith(AUDIT_TRIGGERS_UNCHECKED_SENTENCE)


def test_trigger_check_sql_names_the_migration_triggers():
    """The SQL literal and the name tuple are written twice on purpose (bandit B608).

    Hold them together, and to the triggers migrations 008/060 create on public.audit_logs.
    """
    versions = Path(__file__).resolve().parents[2] / "alembic" / "versions"
    migration_060 = (versions / "060_audit_log_immutability.py").read_text()
    for name in AUDIT_LOG_TRIGGER_NAMES:
        assert f"'{name}'" in _AUDIT_LOG_TRIGGER_CHECK_SQL
        assert name in migration_060
    assert _AUDIT_LOG_TRIGGER_CHECK_SQL.count("tr_audit_log_") == len(AUDIT_LOG_TRIGGER_NAMES)
    assert "to_regclass('public.audit_logs')" in _AUDIT_LOG_TRIGGER_CHECK_SQL
    # 'D' (disabled) and 'R' (replica-only) do not fire in a normal session.
    assert "tgenabled IN ('O', 'A')" in _AUDIT_LOG_TRIGGER_CHECK_SQL


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
    assert "4 entries were written while it was paused" in result_b["description"]


@pytest.mark.parametrize("chain_enabled", [True, False])
def test_title_and_module_reference_do_not_vary_with_chain_state(db_session: Session, monkeypatch, chain_enabled):
    """Title / module_reference / evidence_type are fixed per rule.

    Auto-link matches stored evidence by rule id and overwrites its ``title`` on every run, so a
    title that followed the chain state would rewrite the record of evidence back and forth.
    """
    _set_chain(monkeypatch, True)
    _write(db_session, _writer(db_session, COMPANY_A), 1)
    _set_chain(monkeypatch, chain_enabled)

    result = _query_audit_log(db_session, COMPANY_A)

    assert result["evidence_type"] == "module"
    assert result["title"] == "Audit Trail (Audit Log & Hash-Chain Status)"
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
