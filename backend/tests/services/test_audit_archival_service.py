"""Unit/integration coverage for the audit-log retention archival service.

Locks in the behaviour added on branch ``qa/full-pass-2026-06-04`` for
``app.services.audit_archival_service.AuditArchivalService`` — the compliant,
*non-destructive* alternative to deleting aged audit rows (CMMC AU-3.3.8).

The headline guarantee is that archival NEVER mutates or removes ``audit_logs``
rows: it exports aged rows to cold storage (NDJSON + sha256), records the export
in the ``ExportEvent`` governance ledger, and writes an ``EXPORT`` audit row —
while leaving the live, hash-chained rows in place and verifiable.

These tests drive the service directly against the DB session fixture (mirroring
tests/services/test_audit_service_tenant.py and tests/services/test_mrp_service.py)
and seed audit rows through the *real* writer ``AuditService.log`` so every row
carries a valid sequence number and integrity hash. Age is controlled by passing
``as_of`` far in the future so freshly written rows fall past the retention
window.

A temporary archive directory is provided via the ``archive_env`` fixture, which
monkeypatches ``settings.AUDIT_ARCHIVE_DIR`` to a ``tmp_path`` and forces
``AUDIT_ARCHIVE_ENABLED=True``.
"""

import hashlib
import json
from contextlib import contextmanager, nullcontext as _nullcontext
from datetime import datetime, timedelta
from unittest import mock

import pytest
from sqlalchemy.orm import Session

import app.services.audit_service as audit_service_module
from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.governance import ExportEvent, RetentionPolicy
from app.services.audit_archival_service import (
    ARCHIVE_EXPORT_TYPE,
    ARCHIVE_RECORD_TYPE,
    SECURITY_AUDIT_POLICY_KEY,
    AuditArchivalService,
)
from app.services.audit_integrity_service import AuditIntegrityService
from app.services.audit_service import AuditService

pytestmark = [pytest.mark.unit, pytest.mark.requires_db]


@contextmanager
def _frozen_utcnow(when: datetime):
    """Make ``AuditService.log`` see ``when`` as the current UTC time.

    ``audit_service`` does ``from datetime import datetime`` and calls
    ``datetime.utcnow()``; we swap that module reference for a datetime subclass
    whose ``utcnow`` returns ``when``. The returned value is a real datetime, so
    downstream ``.isoformat()`` in the hash computation still works.
    """

    class _FrozenDateTime(datetime):
        @classmethod
        def utcnow(cls):  # type: ignore[override]
            return when

    with mock.patch.object(audit_service_module, "datetime", _FrozenDateTime):
        yield


# Far enough in the future that any row written "now" is well past even the
# longest retention window we test (1095 days) once used as ``as_of``.
FUTURE = datetime.utcnow() + timedelta(days=4000)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def archive_env(tmp_path, monkeypatch):
    """Point the service at a throwaway archive dir and enable archival."""
    monkeypatch.setattr(settings, "AUDIT_ARCHIVE_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "AUDIT_ARCHIVE_ENABLED", True, raising=False)
    return tmp_path


def _ensure_company(db: Session, company_id: int, *, is_active: bool = True) -> Company:
    """Create the company if missing, or set its active flag if it exists.

    Note: callers that merely need *a* company (e.g. row seeding) should NOT use
    this to avoid clobbering a deliberately-inactive company; use
    ``_ensure_company_exists`` instead.
    """
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(
            id=company_id,
            name=f"Company {company_id}",
            slug=f"company-{company_id}",
            is_active=is_active,
        )
        db.add(company)
        db.commit()
    elif company.is_active != is_active:
        company.is_active = is_active
        db.commit()
    return company


def _ensure_company_exists(db: Session, company_id: int) -> Company:
    """Create the company only if missing; never change an existing active flag."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(
            id=company_id,
            name=f"Company {company_id}",
            slug=f"company-{company_id}",
            is_active=True,
        )
        db.add(company)
        db.commit()
    return company


def _seed_audit_rows(db: Session, company_id: int, n: int, *, age_days: int | None = None) -> list[AuditLog]:
    """Write ``n`` real audit rows for ``company_id`` via AuditService.log.

    Using the real writer guarantees valid sequence numbers + integrity hashes
    (and keeps the single global hash chain intact across companies).

    When ``age_days`` is given the rows are written *as if* the clock were that
    many days in the past: we patch the ``utcnow`` the writer reads so the older
    timestamp is the one that is folded into the integrity hash. (Backdating the
    timestamp column *after* the row is written would invalidate the hash, since
    ``compute_audit_hash`` hashes the timestamp — so the clock must move at write
    time.) This lets idempotency tests age the seeded rows without also aging the
    fresh EXPORT audit row the archival writes.
    """
    _ensure_company_exists(db, company_id)
    svc = AuditService(db, user=None, company_id=company_id)
    rows: list[AuditLog] = []

    aged_now = (datetime.utcnow() - timedelta(days=age_days)) if age_days is not None else None
    cm = _frozen_utcnow(aged_now) if aged_now is not None else _nullcontext()
    with cm:
        for i in range(n):
            row = svc.log(
                action="CREATE",
                resource_type="part",
                resource_id=i,
                resource_identifier=f"C{company_id}-P{i}",
                description=f"seed row {i} for company {company_id}",
            )
            assert row is not None
            rows.append(row)
    db.commit()
    return rows


def _seed_retention_policy(db: Session, company_id: int, days: int) -> RetentionPolicy:
    policy = RetentionPolicy(
        company_id=company_id,
        policy_key=SECURITY_AUDIT_POLICY_KEY,
        name="Security Audit Record",
        default_retention_days=days,
        retention_basis="Test policy basis.",
        retention_trigger="event_timestamp",
        applies_to_record_types=["audit_logs"],
        active=True,
    )
    db.add(policy)
    db.commit()
    return policy


def _count_audit_rows(db: Session, company_id: int) -> int:
    return db.query(AuditLog).filter(AuditLog.company_id == company_id).count()


def _export_events(db: Session, company_id: int) -> list[ExportEvent]:
    return (
        db.query(ExportEvent)
        .filter(
            ExportEvent.company_id == company_id,
            ExportEvent.record_type == ARCHIVE_RECORD_TYPE,
            ExportEvent.export_type == ARCHIVE_EXPORT_TYPE,
        )
        .all()
    )


def _export_audit_rows(db: Session, company_id: int) -> list[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.company_id == company_id,
            AuditLog.action == "EXPORT",
            AuditLog.resource_type == ARCHIVE_RECORD_TYPE,
        )
        .all()
    )


# ---------------------------------------------------------------------------
# Non-destructive guarantee (the headline)
# ---------------------------------------------------------------------------


def test_archive_company_never_deletes_audit_rows(db_session: Session, archive_env):
    """Seed N rows, archive them all, prove all N still exist and the chain is
    still valid. This is the core compliance invariant: the *code* never deletes
    audit rows (SQLite has no immutability triggers, so this isolates the code)."""
    _seed_audit_rows(db_session, company_id=1, n=5)
    before = _count_audit_rows(db_session, 1)
    assert before == 5

    result = AuditArchivalService(db_session).archive_company(1, as_of=FUTURE)

    assert result["status"] == "archived"
    assert result["archived_count"] == 5

    # The EXPORT audit row the archival itself wrote is the only addition; the 5
    # archived rows are untouched and still present.
    assert _count_audit_rows(db_session, 1) == before + 1  # +1 EXPORT row
    # None of the original 5 were removed.
    original_seqs = (
        db_session.query(AuditLog.sequence_number).filter(AuditLog.company_id == 1, AuditLog.action == "CREATE").count()
    )
    assert original_seqs == 5

    report = AuditIntegrityService(db_session).verify_full_chain()
    assert report.is_valid, report.to_dict()
    assert report.chain_valid is True


# ---------------------------------------------------------------------------
# Retention window resolution
# ---------------------------------------------------------------------------


def test_retention_uses_policy_when_present(db_session: Session, archive_env):
    """With an active security_audit_record policy, the cutoff uses its days."""
    _seed_retention_policy(db_session, company_id=1, days=1095)
    _seed_audit_rows(db_session, company_id=1, n=3)

    svc = AuditArchivalService(db_session)
    assert svc._resolve_retention_days(1) == 1095

    # 1094 days in the future: rows are NOT yet past the 1095-day window.
    near = AuditArchivalService(db_session).archive_company(1, as_of=datetime.utcnow() + timedelta(days=1094))
    assert near["status"] == "nothing_to_archive"
    assert near["archived_count"] == 0

    # Past the window -> archived.
    far = AuditArchivalService(db_session).archive_company(1, as_of=datetime.utcnow() + timedelta(days=1096))
    assert far["status"] == "archived"
    assert far["archived_count"] == 3
    assert far["retention_days"] == 1095


def test_retention_falls_back_to_settings_default(db_session: Session, archive_env, monkeypatch):
    """No policy row -> settings.AUDIT_RETENTION_DAYS_DEFAULT is used."""
    monkeypatch.setattr(settings, "AUDIT_RETENTION_DAYS_DEFAULT", 1095, raising=False)
    _seed_audit_rows(db_session, company_id=1, n=2)

    svc = AuditArchivalService(db_session)
    # No security_audit_record policy seeded for company 1.
    assert (
        db_session.query(RetentionPolicy)
        .filter(
            RetentionPolicy.company_id == 1,
            RetentionPolicy.policy_key == SECURITY_AUDIT_POLICY_KEY,
        )
        .count()
        == 0
    )
    assert svc._resolve_retention_days(1) == 1095

    result = AuditArchivalService(db_session).archive_company(1, as_of=FUTURE)
    assert result["status"] == "archived"
    assert result["retention_days"] == 1095


def test_rows_newer_than_window_are_not_archived(db_session: Session, archive_env):
    """A current-time run leaves recent rows alone (cutoff is ~3 years back)."""
    _seed_audit_rows(db_session, company_id=1, n=4)
    # as_of defaults to utcnow(): nothing is 1095+ days old.
    result = AuditArchivalService(db_session).archive_company(1)
    assert result["status"] == "nothing_to_archive"
    assert result["archived_count"] == 0
    assert _export_events(db_session, 1) == []


# ---------------------------------------------------------------------------
# Export artifacts: file + ledger
# ---------------------------------------------------------------------------


def test_export_artifacts_written_and_consistent(db_session: Session, archive_env):
    """A real run writes a sha256-matching NDJSON file and an ExportEvent whose
    refs and content_sha256 line up with the returned summary."""
    rows = _seed_audit_rows(db_session, company_id=1, n=6)
    first_seq = rows[0].sequence_number
    last_seq = rows[-1].sequence_number

    result = AuditArchivalService(db_session).archive_company(1, as_of=FUTURE)
    assert result["status"] == "archived"
    assert result["archived_count"] == 6
    assert result["first_sequence"] == first_seq
    assert result["last_sequence"] == last_seq

    # ExportEvent ledger row.
    events = _export_events(db_session, 1)
    assert len(events) == 1
    event = events[0]
    assert event.company_id == 1
    assert event.export_format == "ndjson"
    assert event.content_sha256 == result["content_sha256"]
    assert event.included_record_refs["first_sequence"] == first_seq
    assert event.included_record_refs["last_sequence"] == last_seq
    assert event.included_record_refs["count"] == 6
    assert event.destination_reference == result["archive_path"]

    # The NDJSON file exists at the returned path.
    from pathlib import Path

    archive_path = Path(result["archive_path"])
    assert archive_path.exists()
    # File lives under tmp archive dir / company_1/.
    assert archive_path.parent == archive_env / "company_1"

    payload = archive_path.read_bytes()
    # File sha256 == returned/ledger content_sha256.
    assert hashlib.sha256(payload).hexdigest() == result["content_sha256"]

    # One JSON object per archived row; sequences match the seeded segment.
    lines = payload.decode("utf-8").splitlines()
    assert len(lines) == 6
    parsed = [json.loads(line) for line in lines]
    assert [p["sequence_number"] for p in parsed] == [r.sequence_number for r in rows]
    assert all(p["company_id"] == 1 for p in parsed)


def test_export_audit_entry_is_written(db_session: Session, archive_env):
    """Archival records itself: an EXPORT audit row stamped with the archived
    company, resource_type=audit_logs."""
    _seed_audit_rows(db_session, company_id=1, n=3)

    AuditArchivalService(db_session).archive_company(1, as_of=FUTURE)

    export_rows = _export_audit_rows(db_session, 1)
    assert len(export_rows) == 1
    entry = export_rows[0]
    assert entry.action == "EXPORT"
    assert entry.resource_type == ARCHIVE_RECORD_TYPE
    assert entry.company_id == 1
    # The EXPORT row sequences after the archived segment (it is not part of it).
    assert entry.extra_data["export_type"] == ARCHIVE_EXPORT_TYPE


# ---------------------------------------------------------------------------
# Idempotency / high-water mark
# ---------------------------------------------------------------------------


def test_idempotent_second_run_archives_nothing(db_session: Session, archive_env):
    """Immediately re-running archives nothing (high-water advanced).

    Rows are backdated past the window and the run uses a present-time ``as_of``,
    so the EXPORT audit row the first run writes (timestamped now) is itself NOT
    yet aged — the second run therefore finds nothing, isolating idempotency."""
    _seed_audit_rows(db_session, company_id=1, n=4, age_days=1200)

    first = AuditArchivalService(db_session).archive_company(1)
    assert first["status"] == "archived"
    assert first["archived_count"] == 4
    first_last_seq = first["last_sequence"]

    second = AuditArchivalService(db_session).archive_company(1)
    assert second["status"] == "nothing_to_archive"
    assert second["archived_count"] == 0
    assert second["high_water_sequence"] == first_last_seq

    # Still exactly one archival ExportEvent.
    assert len(_export_events(db_session, 1)) == 1


def test_third_run_archives_only_new_delta(db_session: Session, archive_env):
    """After a run + more aged rows, a later run archives only the new segment
    (sequences strictly greater than the prior last_sequence)."""
    _seed_audit_rows(db_session, company_id=1, n=3, age_days=1200)
    first = AuditArchivalService(db_session).archive_company(1)
    assert first["status"] == "archived"
    assert first["archived_count"] == 3
    boundary = first["last_sequence"]

    # Seed more aged rows; they sequence after the boundary.
    new_rows = _seed_audit_rows(db_session, company_id=1, n=2, age_days=1200)
    assert min(r.sequence_number for r in new_rows) > boundary

    third = AuditArchivalService(db_session).archive_company(1)
    assert third["status"] == "archived"
    assert third["archived_count"] == 2
    assert third["first_sequence"] > boundary
    assert third["first_sequence"] == new_rows[0].sequence_number
    assert third["last_sequence"] == new_rows[-1].sequence_number

    # Two archival exports now (delta runs), each covering a disjoint segment.
    events = _export_events(db_session, 1)
    assert len(events) == 2


# ---------------------------------------------------------------------------
# Integrity abort
# ---------------------------------------------------------------------------


def test_integrity_failure_aborts_without_side_effects(db_session: Session, archive_env):
    """A tampered row in the segment aborts the run: no file, no ExportEvent,
    no EXPORT audit row, status=integrity_failed."""
    rows = _seed_audit_rows(db_session, company_id=1, n=4)

    # Corrupt one row's integrity hash directly via the session (simulating
    # tampering the verifier must catch).
    target = rows[2]
    target.integrity_hash = "0" * 64
    db_session.add(target)
    db_session.commit()

    result = AuditArchivalService(db_session).archive_company(1, as_of=FUTURE)

    assert result["status"] == "integrity_failed"
    assert result["archived_count"] == 0
    assert target.sequence_number in result["failed_sequences"]

    # No side effects at all.
    assert _export_events(db_session, 1) == []
    assert _export_audit_rows(db_session, 1) == []
    # No archive directory/file for the company was created.
    company_dir = archive_env / "company_1"
    assert not company_dir.exists() or list(company_dir.glob("*.ndjson")) == []


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_dry_run_reports_but_writes_nothing(db_session: Session, archive_env):
    """dry_run returns a positive count but performs no writes."""
    _seed_audit_rows(db_session, company_id=1, n=5)

    result = AuditArchivalService(db_session).archive_company(1, as_of=FUTURE, dry_run=True)

    assert result["status"] == "dry_run"
    assert result["archived_count"] == 5
    # No file, no ledger row, no EXPORT audit row.
    assert "archive_path" not in result
    assert _export_events(db_session, 1) == []
    assert _export_audit_rows(db_session, 1) == []
    company_dir = archive_env / "company_1"
    assert not company_dir.exists() or list(company_dir.glob("*.ndjson")) == []

    # A real run afterwards still sees all 5 (dry run didn't advance high-water).
    real = AuditArchivalService(db_session).archive_company(1, as_of=FUTURE)
    assert real["status"] == "archived"
    assert real["archived_count"] == 5


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


def test_archive_company_is_tenant_isolated(db_session: Session, archive_env):
    """archive_company(A) touches only company A's rows; the NDJSON, ExportEvent
    and EXPORT audit row are all company A's."""
    _ensure_company(db_session, 1)
    _ensure_company(db_session, 2)
    rows_a = _seed_audit_rows(db_session, company_id=1, n=3)
    _seed_audit_rows(db_session, company_id=2, n=4)

    result = AuditArchivalService(db_session).archive_company(1, as_of=FUTURE)
    assert result["status"] == "archived"
    assert result["archived_count"] == 3  # only A's rows

    # NDJSON contains ONLY company A rows.
    from pathlib import Path

    parsed = [json.loads(line) for line in Path(result["archive_path"]).read_text().splitlines()]
    assert {p["company_id"] for p in parsed} == {1}
    assert [p["sequence_number"] for p in parsed] == [r.sequence_number for r in rows_a]

    # Ledger + EXPORT audit row are company A; company B has neither.
    assert len(_export_events(db_session, 1)) == 1
    assert _export_events(db_session, 2) == []
    assert len(_export_audit_rows(db_session, 1)) == 1
    assert _export_audit_rows(db_session, 2) == []

    # Company B's audit rows are entirely untouched (still 4 CREATE rows).
    assert db_session.query(AuditLog).filter(AuditLog.company_id == 2, AuditLog.action == "CREATE").count() == 4


# ---------------------------------------------------------------------------
# archive_all
# ---------------------------------------------------------------------------


def test_archive_all_disabled_is_noop(db_session: Session, archive_env, monkeypatch):
    """With AUDIT_ARCHIVE_ENABLED False, archive_all is a no-op."""
    monkeypatch.setattr(settings, "AUDIT_ARCHIVE_ENABLED", False, raising=False)
    _seed_audit_rows(db_session, company_id=1, n=3)

    result = AuditArchivalService(db_session).archive_all(as_of=FUTURE)
    assert result["status"] == "disabled"
    assert result["total_archived"] == 0
    # Nothing exported.
    assert _export_events(db_session, 1) == []


def test_archive_all_processes_active_companies(db_session: Session, archive_env):
    """Enabled: archive_all aggregates archived counts across active companies."""
    _ensure_company(db_session, 1, is_active=True)
    _ensure_company(db_session, 2, is_active=True)
    _seed_audit_rows(db_session, company_id=1, n=2)
    _seed_audit_rows(db_session, company_id=2, n=3)

    result = AuditArchivalService(db_session).archive_all(as_of=FUTURE)

    assert result["status"] == "completed"
    assert result["total_archived"] == 5
    assert result["errors"] == []
    # One ExportEvent per company.
    assert len(_export_events(db_session, 1)) == 1
    assert len(_export_events(db_session, 2)) == 1


def test_archive_all_skips_inactive_companies(db_session: Session, archive_env):
    """An inactive company is not processed by archive_all."""
    _ensure_company(db_session, 1, is_active=True)
    _ensure_company(db_session, 3, is_active=False)
    _seed_audit_rows(db_session, company_id=1, n=2)
    # Rows exist for the inactive company too, but it must be skipped.
    _seed_audit_rows(db_session, company_id=3, n=2)

    result = AuditArchivalService(db_session).archive_all(as_of=FUTURE)

    assert result["status"] == "completed"
    assert result["total_archived"] == 2  # only company 1
    assert _export_events(db_session, 3) == []
