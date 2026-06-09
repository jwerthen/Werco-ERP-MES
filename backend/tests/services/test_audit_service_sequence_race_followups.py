"""Behavior locks for the A1 ``sequence_number`` race fix in ``AuditService.log()``
(fix/wo-followups-round2, FIX 5).

``log()`` now serializes the global hash-chain allocate+insert: a Postgres advisory lock
(PG-only) plus a SAVEPOINT (``begin_nested``) around the INSERT with bounded retry on
``IntegrityError``. A ``sequence_number`` collision rolls back ONLY the savepoint -- leaving
the caller's OUTER transaction usable -- then re-reads the chain tail and retries with the
NEXT free sequence. Chain semantics (contiguous sequence, ``previous_hash`` link,
``integrity_hash``) are unchanged.

Covered:
  (a) normal audited writes still produce a correct, contiguous, hash-linked chain that
      verifies via AuditIntegrityService;
  (b) a simulated sequence collision (the tail-read returns a STALE max on the first attempt)
      is resolved by the retry -- the row lands at the NEXT free sequence, the chain stays
      valid, and the caller's outer transaction is NOT poisoned (a later commit succeeds);
  (c) an audited write nested under a caller that ITSELF opened a savepoint still works (the
      coc_service / completion_inventory_service pattern the A1 author flagged).
"""

import pytest
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.part import Part
from app.models.user import User, UserRole
from app.services.audit_integrity_service import AuditIntegrityService
from app.services.audit_service import AuditService

pytestmark = [pytest.mark.unit, pytest.mark.requires_db]

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


def _make_user(db: Session, *, company_id: int = 1) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"audit-race-{n}@co{company_id}.test",
        employee_id=f"AUDR-{n:05d}",
        first_name="Audit",
        last_name="Race",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=UserRole.ADMIN,
        is_active=True,
        is_superuser=False,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# ---------------------------------------------------------------------------
# (a) normal chain stays contiguous + hash-linked + verifiable
# ---------------------------------------------------------------------------


def test_normal_audited_writes_form_a_valid_contiguous_chain(db_session: Session):
    user = _make_user(db_session)
    svc = AuditService(db_session, user)

    rows = []
    for i in range(5):
        row = svc.log(action="CREATE", resource_type="part", resource_id=i, resource_identifier=f"P-{i}")
        assert row is not None
        rows.append(row)
    db_session.commit()

    seqs = [r.sequence_number for r in rows]
    # Strictly increasing & contiguous.
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))
    # Hash chain links each row to the prior.
    for prev, cur in zip(rows, rows[1:]):
        assert cur.previous_hash == prev.integrity_hash
        assert cur.integrity_hash is not None

    report = AuditIntegrityService(db_session).verify_full_chain()
    assert report.is_valid, report.to_dict()
    assert report.chain_valid is True


# ---------------------------------------------------------------------------
# (b) a simulated collision is resolved by the retry; outer txn not poisoned
# ---------------------------------------------------------------------------


def test_sequence_collision_retries_to_next_free_sequence(db_session: Session, monkeypatch):
    """Force the tail-read to return a STALE (already-used) sequence on the FIRST attempt so
    the INSERT trips the unique-``sequence_number`` constraint; the savepoint rollback + retry
    must re-read the real tail and land the row at the NEXT free sequence, with the chain valid
    and the caller's outer transaction still usable."""
    user = _make_user(db_session)
    svc = AuditService(db_session, user)

    # Seed one row so a real tail exists.
    first = svc.log(action="CREATE", resource_type="part", resource_id=1, resource_identifier="P-1")
    db_session.commit()
    assert first is not None
    seeded_seq = first.sequence_number
    seeded_hash = first.integrity_hash

    real_tail_read = svc._get_next_sequence_and_previous_hash
    calls = {"n": 0}

    def flaky_tail_read():
        calls["n"] += 1
        if calls["n"] == 1:
            # STALE read: hand back the ALREADY-USED sequence -> the INSERT collides.
            return seeded_seq, seeded_hash
        # Subsequent attempts read the true tail (the next free sequence).
        return real_tail_read()

    monkeypatch.setattr(svc, "_get_next_sequence_and_previous_hash", flaky_tail_read)

    second = svc.log(action="CREATE", resource_type="part", resource_id=2, resource_identifier="P-2")
    # The retry resolved the collision: the row was written.
    assert second is not None
    assert calls["n"] >= 2, "expected at least one retry after the simulated collision"
    # It landed at the NEXT free sequence (not the stale/colliding one), linked to the real tail.
    assert second.sequence_number == seeded_seq + 1
    assert second.previous_hash == seeded_hash

    # The caller's OUTER transaction is NOT poisoned: a normal commit succeeds.
    db_session.commit()

    # Both rows persisted with distinct, contiguous sequences and the chain verifies.
    rows = db_session.query(AuditLog).order_by(AuditLog.sequence_number).all()
    seqs = [r.sequence_number for r in rows]
    assert seqs == sorted(set(seqs)), "sequence numbers must be unique"
    report = AuditIntegrityService(db_session).verify_full_chain()
    assert report.is_valid, report.to_dict()


def test_outer_transaction_survives_collision_and_can_commit_business_row(db_session: Session, monkeypatch):
    """The headline A1 guarantee: a collision inside log() must not poison the caller's unit of
    work. After the audited write (which internally retried), the caller can still flush+commit
    its OWN business row in the same transaction."""
    user = _make_user(db_session)
    svc = AuditService(db_session, user)

    first = svc.log(action="CREATE", resource_type="part", resource_id=10, resource_identifier="P-10")
    db_session.commit()
    seeded_seq = first.sequence_number
    seeded_hash = first.integrity_hash

    calls = {"n": 0}
    real_tail_read = svc._get_next_sequence_and_previous_hash

    def flaky_tail_read():
        calls["n"] += 1
        if calls["n"] == 1:
            return seeded_seq, seeded_hash
        return real_tail_read()

    monkeypatch.setattr(svc, "_get_next_sequence_and_previous_hash", flaky_tail_read)

    # Audited write collides+retries, then the caller adds a business row in the SAME txn.
    svc.log(action="CREATE", resource_type="part", resource_id=11, resource_identifier="P-11")
    biz = Part(
        part_number=f"AUDR-BIZ-{_next()}",
        name="post-collision business row",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=1,
    )
    db_session.add(biz)
    db_session.commit()  # outer txn not poisoned -> this commits cleanly
    db_session.refresh(biz)
    assert biz.id is not None


# ---------------------------------------------------------------------------
# (c) audited write nested under a caller that ALSO opened a savepoint
# ---------------------------------------------------------------------------


def test_audited_write_under_caller_savepoint_is_safe(db_session: Session):
    """coc_service / completion_inventory_service call audit.log() while the caller itself is
    inside a ``begin_nested`` savepoint. log() opens its OWN nested savepoint; the two must
    compose -- the audit row writes and both savepoints commit cleanly."""
    user = _make_user(db_session)
    svc = AuditService(db_session, user)

    outer = db_session.begin_nested()  # caller's savepoint (mirrors coc_service)
    row = svc.log(action="CREATE", resource_type="shipment", resource_id=99, resource_identifier="SHP-99")
    assert row is not None
    outer.commit()
    db_session.commit()

    persisted = db_session.query(AuditLog).filter(AuditLog.resource_identifier == "SHP-99").all()
    assert len(persisted) == 1
    report = AuditIntegrityService(db_session).verify_full_chain()
    assert report.is_valid, report.to_dict()
