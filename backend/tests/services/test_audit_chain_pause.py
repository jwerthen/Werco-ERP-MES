"""Behavior locks for the PAUSABLE audit-log hash chain (``AUDIT_HASH_CHAIN_ENABLED``).

Every audited write used to take ONE GLOBAL transaction-scoped Postgres advisory lock
(``_AUDIT_CHAIN_LOCK_KEY`` -- a single fixed key for the whole ``audit_logs`` table
across ALL tenants), held until the caller's transaction committed, plus a full tail
read and a SHA-256 over the entire row. ``settings.AUDIT_HASH_CHAIN_ENABLED`` makes that
cost removable. This module pins BOTH modes and the transitions between them, because
this is a compliance-sensitive write path where "it still writes a row" is not enough.

What is asserted here, and why each one matters:

1. **The default is True** -- read off ``Settings.model_fields``, not the live ``settings``
   singleton, so an env var in the runner cannot mask a code change. This is the test that
   guarantees merging the feature is a runtime no-op.
2. **Enabled mode is unchanged**: a real 64-hex SHA-256 that RECOMPUTES to the stored
   value, ``previous_hash`` linking to the prior row's ``integrity_hash``, contiguous
   sequences, and a clean ``verify_full_chain``.
3. **Paused mode keeps the audit VALUE**: the row is still written with its full content
   (action / resource / description / old+new values / company_id / user), only the
   cryptographic proof is dropped -- ``integrity_hash`` is the ``LEGACY_CHAIN_PAUSED``
   placeholder and ``previous_hash`` is NULL.
4. **Paused mode does not take the advisory lock.** This is the entire point of the
   change, so it is asserted directly (spy on ``_acquire_chain_lock``) with the enabled
   mode as the control, plus a SQL-level assertion that the lock statement really is
   ``pg_advisory_xact_lock`` on a postgresql bind.
5. **Transitions converge**: enable -> pause -> enable -> pause -> enable leaves a chain
   that verifies clean, with the paused rows counted as legacy (both in the Python
   verifier and in ``get_chain_status``'s SQL ``LIKE 'LEGACY_%'`` filter).
6. **Gaps**: while paused, ``nextval`` values are consumed by rolled-back transactions, so
   gaps are NORMAL and must be counted in ``legacy_sequence_gaps`` rather than raised --
   otherwise every verify run reports tampering forever. The CONTROL matters just as much:
   with the chain ENABLED an injected gap IS still a ``sequence_gap`` issue that flips
   ``chain_valid`` to False. Both directions are covered.
7. **``verify_chain_link`` with a LEGACY_ CURRENT record** returns valid -- the regression
   that made ``GET /audit/integrity/record/{seq}`` report the first paused row as a
   ``chain_break``. Covered at the service level AND through the endpoint.
8. **``_next_sequence_paused`` dialect branch**: SQLite falls back to MAX+1; PostgreSQL
   emits ``nextval`` and never reads the tail (asserted against a fake bind).

HONEST COST, stated here because tests are where it gets rediscovered: rows written while
paused can NEVER be made verifiable retroactively, and gap-based deletion detection is
permanently gone across the paused window. The migration-008/060 DATABASE triggers that
block UPDATE/DELETE on ``audit_logs`` remain the real protection against deletion.
"""

from types import SimpleNamespace

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.user import User, UserRole
from app.services import audit_service as audit_service_module
from app.services.audit_integrity_service import AuditIntegrityService
from app.services.audit_service import (
    _AUDIT_CHAIN_LOCK_KEY,
    _AUDIT_SEQUENCE_NAME,
    PAUSED_CHAIN_PLACEHOLDER,
    AuditService,
    compute_audit_hash,
)

pytestmark = [pytest.mark.unit, pytest.mark.requires_db]

HEX = set("0123456789abcdef")

# Module-level counter so each user row gets a unique natural key across the module,
# even when several tests run under -n auto in the same worker DB.
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True)
        db.add(company)
        db.commit()
    return company


def _make_user(db: Session, *, company_id: int = 1, role: UserRole = UserRole.ADMIN) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"chainpause-{n}@co{company_id}.test",
        employee_id=f"CHP-{n:05d}",
        first_name="Chain",
        last_name="Pause",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=role,
        is_active=True,
        is_superuser=False,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _set_chain(monkeypatch, enabled: bool) -> None:
    """Flip the pause flag on the singleton ``log()`` actually reads.

    ``audit_service`` does ``from app.core.config import settings`` and reads
    ``settings.AUDIT_HASH_CHAIN_ENABLED`` per call, so patching the attribute on that
    one object is what the service sees. monkeypatch restores it at teardown, which
    matters: the flag is process-global and would otherwise leak across tests.
    """
    monkeypatch.setattr(audit_service_module.settings, "AUDIT_HASH_CHAIN_ENABLED", enabled)


def _recompute(record: AuditLog) -> str:
    """The hash the verifier would expect for ``record``."""
    return compute_audit_hash(
        sequence_number=record.sequence_number,
        timestamp=record.timestamp,
        user_id=record.user_id,
        user_email=record.user_email,
        action=record.action,
        resource_type=record.resource_type,
        resource_id=record.resource_id,
        resource_identifier=record.resource_identifier,
        description=record.description,
        old_values=record.old_values,
        new_values=record.new_values,
        ip_address=record.ip_address,
        session_id=record.session_id,
        success=record.success,
        previous_hash=record.previous_hash,
    )


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value


class _FakeSession:
    """Stands in for the ORM ``Session`` at the ``get_bind`` / ``execute`` seam only.

    Deliberately has NO ``query`` attribute: if the code under test ever fell through to
    the MAX+1 tail read on a postgresql bind, this raises ``AttributeError`` instead of
    quietly passing.
    """

    def __init__(self, dialect_name: str, scalar=None):
        self._bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect_name))
        self.statements = []
        self._scalar = scalar
        # The paused allocator wraps its nextval in a SAVEPOINT so that a missing
        # sequence cannot abort (and thereby poison) the caller's transaction.
        # Record the savepoint calls so tests can assert the containment exists.
        self.savepoints = []

    def get_bind(self):
        return self._bind

    def begin_nested(self):
        marker = SimpleNamespace(committed=False, rolled_back=False)

        def _commit():
            marker.committed = True

        def _rollback():
            marker.rolled_back = True

        marker.commit = _commit
        marker.rollback = _rollback
        self.savepoints.append(marker)
        return marker

    def execute(self, statement, params=None):
        self.statements.append((str(statement), params))
        return _FakeResult(self._scalar)


# ---------------------------------------------------------------------------
# 1. The default is ENABLED -- merging this change must be a runtime no-op
# ---------------------------------------------------------------------------


def test_hash_chain_is_enabled_by_default_on_the_settings_model():
    """Read the default off the MODEL, not the live ``settings`` object.

    ``settings`` is materialized from the environment, so an ``AUDIT_HASH_CHAIN_ENABLED``
    env var in the test runner (or a developer's .env) would mask a change to the code
    default. The declared default is what ships, and it must stay True: pausing is opt-in
    precisely because it is not fully reversible.
    """
    field = Settings.model_fields["AUDIT_HASH_CHAIN_ENABLED"]
    assert field.default is True
    assert field.annotation is bool
    assert field.is_required() is False


# ---------------------------------------------------------------------------
# 2. ENABLED mode is byte-identical to the pre-change behavior
# ---------------------------------------------------------------------------


def test_enabled_mode_writes_a_real_recomputable_sha256_chain(db_session: Session, monkeypatch):
    """Real SHA-256, real links, contiguous sequence, clean verification."""
    _set_chain(monkeypatch, True)
    user = _make_user(db_session)
    svc = AuditService(db_session, user)

    rows = [
        svc.log(action="CREATE", resource_type="part", resource_id=i, resource_identifier=f"ENA-{i}") for i in range(3)
    ]
    db_session.commit()
    assert all(r is not None for r in rows)

    for row in rows:
        assert row.integrity_hash != PAUSED_CHAIN_PLACEHOLDER
        assert not row.integrity_hash.startswith("LEGACY_")
        assert len(row.integrity_hash) == 64 and set(row.integrity_hash) <= HEX
        # Not merely "a hash was stored" -- it recomputes to exactly this value.
        assert row.integrity_hash == _recompute(row)

    # The table is empty at test start (conftest drops/creates per test), so the first
    # audited write really is sequence 1 with a NULL previous_hash.
    assert rows[0].sequence_number == 1
    assert rows[0].previous_hash is None
    for prev, cur in zip(rows, rows[1:]):
        assert cur.sequence_number == prev.sequence_number + 1
        assert cur.previous_hash == prev.integrity_hash

    report = AuditIntegrityService(db_session).verify_full_chain()
    assert report.chain_valid is True
    assert report.issues == []
    assert report.legacy_records == 0
    assert report.legacy_sequence_gaps == 0
    assert report.records_checked == 3


# ---------------------------------------------------------------------------
# 3. PAUSED mode: the audit VALUE survives; only the proof is dropped
# ---------------------------------------------------------------------------


def test_paused_mode_still_writes_the_full_audit_row(db_session: Session, monkeypatch):
    """A paused row keeps every field an auditor reads; it just carries no hash.

    This is the compliance line the pause is allowed to cross and the one it is not:
    losing the SHA-256 is the accepted cost, losing the RECORD is not.
    """
    _set_chain(monkeypatch, False)
    user = _make_user(db_session, company_id=1)
    svc = AuditService(db_session, user)

    row = svc.log(
        action="STATUS_CHANGE",
        resource_type="work_order",
        resource_id=1077,
        resource_identifier="WO-1077",
        description="Changed work_order status: WO-1077 from 'RELEASED' to 'IN_PROGRESS'",
        old_values={"status": "RELEASED"},
        new_values={"status": "IN_PROGRESS"},
        extra_data={"reason": "operator start"},
    )
    db_session.commit()
    assert row is not None

    persisted = db_session.query(AuditLog).filter(AuditLog.resource_identifier == "WO-1077").one()
    # The pause-specific shape.
    assert persisted.integrity_hash == PAUSED_CHAIN_PLACEHOLDER == "LEGACY_CHAIN_PAUSED"
    assert persisted.previous_hash is None
    # It reuses the 'LEGACY_' prefix every consumer already tests and skips, and it is
    # distinguishable from a genuine hash (and fits integrity_hash's String(64)).
    assert persisted.integrity_hash.startswith("LEGACY_")
    assert len(persisted.integrity_hash) <= 64
    assert set(persisted.integrity_hash) - HEX, "the placeholder must not look like a hex digest"
    # ...and everything an audit reader actually needs is intact.
    assert persisted.action == "STATUS_CHANGE"
    assert persisted.resource_type == "work_order"
    assert persisted.resource_id == 1077
    assert persisted.description.startswith("Changed work_order status")
    assert persisted.old_values == {"status": "RELEASED"}
    assert persisted.new_values == {"status": "IN_PROGRESS"}
    assert persisted.extra_data == {"reason": "operator start"}
    assert persisted.company_id == 1
    assert persisted.user_id == user.id
    assert persisted.user_email == user.email
    assert persisted.success == "true"
    assert persisted.sequence_number >= 1


def test_paused_helpers_still_write_rows(db_session: Session, monkeypatch):
    """The ``log_create``/``log_update``/``log_delete``/``log_status_change`` seam is unchanged.

    Every call site uses a helper, not ``log()`` directly, so a paused-mode regression that
    only showed up through the helpers would still be a total audit outage.
    """
    _set_chain(monkeypatch, False)
    user = _make_user(db_session)
    svc = AuditService(db_session, user)

    created = svc.log_create("part", 5, "P-5", new_values={"part_number": "P-5"})
    updated = svc.log_update("part", 5, "P-5", old_values={"name": "a"}, new_values={"name": "b"})
    deleted = svc.log_delete("part", 5, "P-5", soft_delete=True)
    status_changed = svc.log_status_change("work_order", 6, "WO-6", "DRAFT", "RELEASED")
    db_session.commit()

    rows = [created, updated, deleted, status_changed]
    assert all(r is not None for r in rows)
    assert [r.action for r in rows] == ["CREATE", "UPDATE", "DELETE", "STATUS_CHANGE"]
    assert all(r.integrity_hash == PAUSED_CHAIN_PLACEHOLDER for r in rows)
    assert all(r.previous_hash is None for r in rows)
    # Sequences are still unique and allocated (SQLite: MAX+1).
    assert len({r.sequence_number for r in rows}) == 4


def test_paused_mode_keeps_the_savepoint_retry_so_no_audit_row_is_dropped(db_session: Session, monkeypatch):
    """The savepoint/retry wrapper is kept in BOTH modes -- it is the caller's
    session-poisoning guard, not merely a collision guard.

    Dropping it from the paused path would mean a residual ``sequence_number`` collision
    poisons the caller's outer transaction (or loses the audit row), and a state change
    with no audit row is worse than one with no hash.
    """
    _set_chain(monkeypatch, False)
    user = _make_user(db_session)
    svc = AuditService(db_session, user)

    first = svc.log(action="CREATE", resource_type="part", resource_id=1, resource_identifier="SP-1")
    db_session.commit()
    taken = first.sequence_number

    real_alloc = svc._next_sequence_paused
    calls = {"n": 0}

    def colliding_alloc():
        calls["n"] += 1
        if calls["n"] == 1:
            return taken  # hand back an ALREADY-USED value -> unique violation
        return real_alloc()

    monkeypatch.setattr(svc, "_next_sequence_paused", colliding_alloc)
    second = svc.log(action="CREATE", resource_type="part", resource_id=2, resource_identifier="SP-2")

    assert second is not None, "the retry must recover the audit row, not drop it"
    assert calls["n"] >= 2, "expected a retry after the simulated collision"
    assert second.sequence_number != taken
    assert second.integrity_hash == PAUSED_CHAIN_PLACEHOLDER

    # The caller's OUTER transaction survived the collision and still commits.
    db_session.commit()
    assert db_session.query(AuditLog).count() == 2


# ---------------------------------------------------------------------------
# 4. PAUSED mode does NOT take the global advisory lock (the whole point)
# ---------------------------------------------------------------------------


def test_paused_mode_skips_the_advisory_lock_and_enabled_mode_takes_it(db_session: Session, monkeypatch):
    """The lock is the system-wide funnel being removed -- pinned, not assumed.

    Both directions in one test so the paused assertion cannot silently pass because the
    spy never fires at all.
    """
    user = _make_user(db_session)
    svc = AuditService(db_session, user)

    calls = []
    real_lock = svc._acquire_chain_lock
    real_paused_alloc = svc._next_sequence_paused

    def spy_lock():
        calls.append("lock")
        return real_lock()

    def spy_paused_alloc():
        calls.append("paused_alloc")
        return real_paused_alloc()

    monkeypatch.setattr(svc, "_acquire_chain_lock", spy_lock)
    monkeypatch.setattr(svc, "_next_sequence_paused", spy_paused_alloc)

    # Control: the chain ENABLED still serializes on the lock and never uses the
    # sequence allocator.
    _set_chain(monkeypatch, True)
    assert svc.log(action="CREATE", resource_type="part", resource_id=1, resource_identifier="LK-1") is not None
    assert calls == ["lock"]

    # Paused: no lock at all; the lock-free allocator is used instead.
    calls.clear()
    _set_chain(monkeypatch, False)
    assert svc.log(action="CREATE", resource_type="part", resource_id=2, resource_identifier="LK-2") is not None
    db_session.commit()
    assert "lock" not in calls, "the paused path must NOT take the global advisory lock"
    assert calls == ["paused_alloc"]


def test_acquire_chain_lock_emits_pg_advisory_xact_lock_only_on_postgres():
    """SQL-level companion to the spy above: what the lock actually is.

    Without this, ``_acquire_chain_lock`` could be gutted to a no-op and the spy test
    would still pass.
    """
    pg = _FakeSession("postgresql")
    AuditService(pg)._acquire_chain_lock()
    assert len(pg.statements) == 1
    sql, params = pg.statements[0]
    assert "pg_advisory_xact_lock" in sql
    assert params == {"k": _AUDIT_CHAIN_LOCK_KEY}
    # One fixed key for the whole table, inside the signed-64-bit range pg requires.
    assert -(2**63) < _AUDIT_CHAIN_LOCK_KEY < 2**63

    sqlite = _FakeSession("sqlite")
    AuditService(sqlite)._acquire_chain_lock()
    assert sqlite.statements == [], "pg_advisory_xact_lock does not exist on SQLite"


# ---------------------------------------------------------------------------
# 5. pause -> resume -> pause -> resume converges
# ---------------------------------------------------------------------------


def test_repeated_pause_and_resume_leaves_a_verifiable_chain(db_session: Session, monkeypatch):
    """enabled -> paused -> enabled -> paused -> enabled: clean verify, paused rows legacy."""
    user = _make_user(db_session)
    svc = AuditService(db_session, user)

    def write(tag, n):
        return [
            svc.log(action="CREATE", resource_type="part", resource_id=i, resource_identifier=f"{tag}-{i}")
            for i in range(n)
        ]

    _set_chain(monkeypatch, True)
    first = write("E1", 2)
    _set_chain(monkeypatch, False)
    paused_a = write("P1", 2)
    _set_chain(monkeypatch, True)
    resumed_a = write("E2", 2)
    _set_chain(monkeypatch, False)
    paused_b = write("P2", 1)
    _set_chain(monkeypatch, True)
    resumed_b = write("E3", 2)
    db_session.commit()

    everything = first + paused_a + resumed_a + paused_b + resumed_b
    assert all(r is not None for r in everything)
    # Sequences stay contiguous on SQLite (MAX+1 in both modes) and strictly increasing.
    seqs = [r.sequence_number for r in everything]
    assert seqs == list(range(1, len(everything) + 1))

    assert all(r.integrity_hash == PAUSED_CHAIN_PLACEHOLDER for r in paused_a + paused_b)
    assert all(r.previous_hash is None for r in paused_a + paused_b)

    # Resuming relinks off whatever tail exists -- the last PAUSED row's placeholder --
    # and then re-forms a real chain from there.
    assert resumed_a[0].previous_hash == PAUSED_CHAIN_PLACEHOLDER
    assert resumed_a[1].previous_hash == resumed_a[0].integrity_hash
    assert resumed_b[0].previous_hash == PAUSED_CHAIN_PLACEHOLDER
    assert resumed_b[1].previous_hash == resumed_b[0].integrity_hash

    report = AuditIntegrityService(db_session).verify_full_chain()
    assert report.chain_valid is True
    assert report.is_valid is True
    assert report.issues == []
    assert report.records_checked == len(everything)
    assert report.legacy_records == 3, "the three paused rows are counted as legacy"
    assert report.legacy_sequence_gaps == 0

    payload = report.to_dict()
    assert payload["legacy_records"] == 3
    assert payload["legacy_sequence_gaps"] == 0
    assert payload["chain_valid"] is True
    assert payload["issue_count"] == 0

    # The SQL-side legacy filter agrees with the Python one (LIKE 'LEGACY_%').
    stat = AuditIntegrityService(db_session).get_chain_status()
    assert stat["legacy_records"] == 3
    assert stat["protected_records"] == len(everything) - 3
    assert stat["has_gaps"] is False


# ---------------------------------------------------------------------------
# 6. Gap handling -- both directions
# ---------------------------------------------------------------------------


def test_gaps_inside_a_paused_window_are_counted_not_reported(db_session: Session, monkeypatch):
    """``nextval`` burns values on rolled-back transactions, so paused gaps are NORMAL.

    Simulated by making the paused allocator hand back non-contiguous values, which is
    exactly what a real Postgres sequence does after a caller's transaction rolls back.
    Without the legacy-aware branch, every verify run reports ``sequence_gap`` forever and
    pins ``chain_valid`` False.
    """
    user = _make_user(db_session)
    svc = AuditService(db_session, user)

    _set_chain(monkeypatch, True)
    assert svc.log(action="CREATE", resource_type="part", resource_id=1, resource_identifier="G-1") is not None

    _set_chain(monkeypatch, False)
    holes = iter([5, 9])  # values 2-4 and 6-8 were consumed by rolled-back transactions
    monkeypatch.setattr(svc, "_next_sequence_paused", lambda: next(holes))
    paused = [
        svc.log(action="CREATE", resource_type="part", resource_id=i, resource_identifier=f"G-P{i}") for i in (2, 3)
    ]
    _set_chain(monkeypatch, True)
    resumed = svc.log(action="CREATE", resource_type="part", resource_id=4, resource_identifier="G-4")
    db_session.commit()

    assert [r.sequence_number for r in paused] == [5, 9]
    assert resumed.sequence_number == 10, "resuming continues from the live tail"

    report = AuditIntegrityService(db_session).verify_full_chain()
    assert report.issues == [], report.to_dict()
    assert report.chain_valid is True
    assert report.legacy_sequence_gaps == 2, "1->5 and 5->9 are both paused-window gaps"
    assert report.legacy_records == 2
    assert report.to_dict()["legacy_sequence_gaps"] == 2

    # KNOWN, DOCUMENTED LIMITATION (pinned so it can't drift silently): the cheap
    # ``get_chain_status`` summary computes has_gaps as a naive
    # ``total != last - first + 1`` and is NOT legacy-aware, so it reads True across a
    # paused window. /integrity/verify above is the authoritative, legacy-aware answer.
    assert AuditIntegrityService(db_session).get_chain_status()["has_gaps"] is True


def test_gap_on_the_resume_boundary_is_counted_not_reported(db_session: Session, monkeypatch):
    """The other legacy-gap shape: the gap's PREVIOUS row is paused, the current one is not.

    ``record_is_legacy`` is False here, so only the ``previous_was_legacy`` half of the
    check can absorb this one.
    """
    user = _make_user(db_session)
    svc = AuditService(db_session, user)

    _set_chain(monkeypatch, True)
    svc.log(action="CREATE", resource_type="part", resource_id=1, resource_identifier="B-1")
    _set_chain(monkeypatch, False)
    paused = svc.log(action="CREATE", resource_type="part", resource_id=2, resource_identifier="B-2")
    assert paused.sequence_number == 2

    # Resume, but the tail read lands past a hole the paused sequence had already burned.
    _set_chain(monkeypatch, True)
    monkeypatch.setattr(svc, "_get_next_sequence_and_previous_hash", lambda: (7, paused.integrity_hash))
    resumed = svc.log(action="CREATE", resource_type="part", resource_id=3, resource_identifier="B-3")
    db_session.commit()

    assert resumed.sequence_number == 7
    assert resumed.integrity_hash != PAUSED_CHAIN_PLACEHOLDER

    report = AuditIntegrityService(db_session).verify_full_chain()
    assert report.issues == [], report.to_dict()
    assert report.chain_valid is True
    assert report.legacy_sequence_gaps == 1
    assert report.legacy_records == 1


def test_control_gap_with_the_chain_enabled_is_still_reported(db_session: Session, monkeypatch):
    """CONTROL: nothing about the legacy-aware branch weakens gap detection on a LIVE chain.

    Two real chained rows with a hole punched between them (and a correct ``previous_hash``,
    so ONLY the gap is anomalous) must still raise ``sequence_gap`` and flip ``chain_valid``.
    If this ever passes silently, the pause change has disabled tamper detection outright.
    """
    _set_chain(monkeypatch, True)
    user = _make_user(db_session)
    svc = AuditService(db_session, user)

    svc.log(action="CREATE", resource_type="part", resource_id=1, resource_identifier="C-1")
    second = svc.log(action="CREATE", resource_type="part", resource_id=2, resource_identifier="C-2")
    assert second.sequence_number == 2

    monkeypatch.setattr(svc, "_get_next_sequence_and_previous_hash", lambda: (5, second.integrity_hash))
    third = svc.log(action="CREATE", resource_type="part", resource_id=3, resource_identifier="C-3")
    db_session.commit()
    assert third.sequence_number == 5

    report = AuditIntegrityService(db_session).verify_full_chain()
    assert report.chain_valid is False
    assert report.is_valid is False
    assert [i.issue_type for i in report.issues] == ["sequence_gap"]
    issue = report.issues[0]
    assert issue.sequence_number == 3
    assert issue.expected_value == "3"
    assert issue.actual_value == "5"
    assert report.legacy_sequence_gaps == 0, "no legacy row is involved, so nothing may be absorbed"
    assert report.legacy_records == 0


def test_empty_table_reports_zero_legacy_gaps(db_session: Session):
    """The early-return construction omits ``legacy_sequence_gaps`` -- its default must hold."""
    report = AuditIntegrityService(db_session).verify_full_chain()
    assert report.total_records == 0
    assert report.legacy_sequence_gaps == 0
    assert report.to_dict()["legacy_sequence_gaps"] == 0


# ---------------------------------------------------------------------------
# 7. verify_chain_link with a LEGACY_ CURRENT record (the /record/{seq} regression)
# ---------------------------------------------------------------------------


def test_verify_chain_link_accepts_a_paused_current_record(db_session: Session, monkeypatch):
    """A paused row at sequence > 1 has ``previous_hash = NULL`` behind a REAL hashed row.

    Before the fix, ``verify_chain_link`` only skipped when the PREVIOUS record was legacy,
    so this exact pair -- the first row written after pausing -- was reported as a
    ``chain_break``.
    """
    user = _make_user(db_session)
    svc = AuditService(db_session, user)

    _set_chain(monkeypatch, True)
    first = svc.log(action="CREATE", resource_type="part", resource_id=1, resource_identifier="L-1")
    second = svc.log(action="CREATE", resource_type="part", resource_id=2, resource_identifier="L-2")
    _set_chain(monkeypatch, False)
    paused = svc.log(action="CREATE", resource_type="part", resource_id=3, resource_identifier="L-3")
    db_session.commit()

    assert paused.sequence_number == second.sequence_number + 1
    assert paused.previous_hash is None and second.integrity_hash is not None

    integrity = AuditIntegrityService(db_session)
    valid, issue = integrity.verify_chain_link(paused, second)
    assert valid is True and issue is None

    # Same row with NO previous record found (the gap case the endpoint hits when the
    # preceding sequence value was burned by a rolled-back transaction).
    valid, issue = integrity.verify_chain_link(paused, None)
    assert valid is True and issue is None

    # Positive control: a genuinely broken link between two LIVE (non-legacy) rows is
    # still reported -- the early return keys on the LEGACY_ prefix, nothing else.
    valid, issue = integrity.verify_single_record(second)
    assert valid is True and issue is None
    tampered = SimpleNamespace(
        id=second.id,
        sequence_number=second.sequence_number,
        integrity_hash=second.integrity_hash,
        previous_hash="deadbeef",
    )
    valid, issue = integrity.verify_chain_link(tampered, first)
    assert valid is False
    assert issue.issue_type == "chain_break"


@pytest.mark.api
def test_integrity_record_endpoint_reports_a_paused_row_as_valid(client: TestClient, db_session: Session, monkeypatch):
    """End-to-end shape of the regression: GET /audit/integrity/record/{seq} on a paused row."""
    admin = _make_user(db_session, company_id=1, role=UserRole.ADMIN)
    svc = AuditService(db_session, admin)

    _set_chain(monkeypatch, True)
    svc.log(action="CREATE", resource_type="part", resource_id=1, resource_identifier="EP-1")
    _set_chain(monkeypatch, False)
    paused = svc.log(action="CREATE", resource_type="part", resource_id=2, resource_identifier="EP-2")
    db_session.commit()
    paused_seq = paused.sequence_number

    token = create_access_token(subject=admin.id, company_id=1)
    resp = client.get(
        f"/api/v1/audit/integrity/record/{paused_seq}",
        headers={"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["sequence_number"] == paused_seq
    assert body["integrity_hash"] == PAUSED_CHAIN_PLACEHOLDER
    assert body["previous_hash"] is None
    assert body["is_legacy"] is True
    assert body["hash_valid"] is True
    assert body["chain_valid"] is True, "a paused row must not be reported as a chain_break"
    assert body["issues"] == [None, None]
    # The audit content is still served to the auditor.
    assert body["resource_identifier"] == "EP-2"
    assert body["action"] == "CREATE"


# ---------------------------------------------------------------------------
# 8. _next_sequence_paused: the dialect branch
# ---------------------------------------------------------------------------


def test_next_sequence_paused_falls_back_to_max_plus_one_on_sqlite(db_session: Session, monkeypatch):
    """SQLite (the test backend) has no sequences, so the allocator is MAX+1."""
    user = _make_user(db_session)
    svc = AuditService(db_session, user)

    # Empty table -> 1.
    assert svc._next_sequence_paused() == 1

    _set_chain(monkeypatch, True)
    row = svc.log(action="CREATE", resource_type="part", resource_id=1, resource_identifier="SQ-1")
    db_session.commit()
    assert svc._next_sequence_paused() == row.sequence_number + 1

    # Move the tail somewhere non-trivial and re-read: it tracks MAX, not a counter.
    row.sequence_number = 4100
    db_session.flush()
    assert svc._next_sequence_paused() == 4101
    db_session.rollback()


def test_next_sequence_paused_uses_nextval_on_postgres():
    """PostgreSQL allocates via ``nextval`` -- lock-free, and no tail read at all.

    The fake session has no ``query`` attribute, so a fall-through to MAX+1 would raise
    rather than pass quietly.
    """
    pg = _FakeSession("postgresql", scalar=98765)
    value = AuditService(pg)._next_sequence_paused()

    assert value == 98765
    assert isinstance(value, int)
    assert len(pg.statements) == 1
    sql = pg.statements[0][0]
    assert "nextval" in sql
    assert _AUDIT_SEQUENCE_NAME in sql
    assert "MAX(" not in sql.upper(), "the paused Postgres path must not read the chain tail"
    assert "pg_advisory_xact_lock" not in sql

    # The nextval MUST run inside a savepoint, and the savepoint must be released on
    # the happy path. See the failure mode pinned by the next test.
    assert len(pg.savepoints) == 1
    assert pg.savepoints[0].committed is True
    assert pg.savepoints[0].rolled_back is False


def test_missing_sequence_degrades_to_max_plus_one_instead_of_killing_the_caller(db_session, monkeypatch):
    """A missing sequence must NOT propagate an audit failure to the business write.

    If migration 077 has not been applied (or was downgraded while paused, or the DB was
    bootstrapped by ``create_all`` and never migrated), ``nextval`` raises UndefinedTable
    and PostgreSQL aborts the ENTIRE transaction (SQLSTATE 25P02). ``log()``'s broad
    ``except`` would swallow that, but every subsequent statement in the CALLER's
    transaction then fails with InFailedSqlTransaction -- so the business write dies
    too, breaking this service's core contract that audit failures never reach the
    caller. A migration-ordering slip would become an outage of every audited endpoint.

    Containing it in a savepoint and degrading to the MAX+1 allocator keeps the write
    correct; only the lock-free performance win is lost.
    """

    class _ExplodingPgSession(_FakeSession):
        def execute(self, statement, params=None):
            self.statements.append((str(statement), params))
            raise RuntimeError('relation "audit_logs_sequence_number_seq" does not exist')

    pg = _ExplodingPgSession("postgresql")
    # Borrow the real session's ORM query support so the MAX+1 fallback can run.
    pg.query = db_session.query

    value = AuditService(pg)._next_sequence_paused()

    assert value == 1, "must fall back to MAX+1 (empty table -> 1), not raise"
    assert len(pg.savepoints) == 1
    assert pg.savepoints[0].rolled_back is True, "the failed nextval must be contained by a savepoint"
    assert pg.savepoints[0].committed is False
