"""Committed-only audit-persistence coverage for the parts endpoints.

Locks in the audit-before-commit hardening on branch qa/full-pass-2026-06-04 for
``app.api.endpoints.parts`` -- specifically the four state-changing handlers:
``create_part`` (POST /), ``update_part`` (PUT /{id}), ``delete_part``
(DELETE /{id}, soft + hard), and ``restore_part`` (POST /{id}/restore).

Each of those handlers now calls its ``AuditService`` helper BEFORE ``db.commit()``
so the audit row lands in the same transaction as the entity write. The bug these
tests guard against is the inverse ordering -- emitting the audit row AFTER
``db.commit()`` -- which leaves the audit row flushed into a fresh, never-committed
transaction. It would silently vanish in production.

Why a naive test would NOT catch that bug
------------------------------------------
The ``client`` fixture (tests/conftest.py) overrides ``get_db`` to yield ONE
shared, never-closed ``db_session``; the endpoint and the test therefore share a
single open transaction. ``AuditService.log()`` only ``flush()``es. So a
flushed-but-uncommitted audit row is fully visible to a plain
``db.query(AuditLog)`` in the test -- the read happens inside the same open
transaction that holds the uncommitted insert. A naive assertion passes against
BOTH the fixed and the broken (audit-after-commit) code.

The guard
---------
``_committed_audit_rows`` calls ``db.rollback()`` BEFORE querying. A committed
audit row survives the rollback (the handler's ``commit()`` already ended its
transaction); a flushed-but-uncommitted one is discarded. The entity itself was
committed by the handler in both the buggy and fixed versions, so only the audit
row's durability is probed. This is the proven technique from
tests/api/test_qms_soft_delete_audit.py.

Per-handler, why each assertion would FAIL against the old audit-after-commit code:
- create_part: the CREATE row was flushed after commit -> rolled back -> the
  ``len(rows) == 1`` assertion sees 0 rows.
- update_part: same for the UPDATE row.
- delete_part (soft and hard): same for the DELETE row.
- restore_part: same for the RESTORE row.

We do NOT insert ``AuditLog`` rows directly (tamper-evident hash chain); they are
produced by the endpoints and only read back here. The default seeded company is
id=1 (tests/conftest.py).
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.part import Part
from app.models.user import User, UserRole

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1

# Module-level counter so every fixture row gets a globally unique natural key,
# even across tests sharing a worker DB under -n auto.
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _make_user(db: Session, *, company_id: int = COMPANY_A, role: UserRole = UserRole.ADMIN) -> User:
    n = _next()
    user = User(
        email=f"parts-audit-{n}@co{company_id}.test",
        employee_id=f"PARTSAUD-{n:05d}",
        first_name="Parts",
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


def _make_part(db: Session, *, company_id: int = COMPANY_A, is_deleted: bool = False) -> Part:
    """Seed a part directly so a single test can exercise update/delete/restore
    without depending on the create endpoint."""
    n = _next()
    part = Part(
        company_id=company_id,
        part_number=f"PART-AUD-{n:05d}",
        revision="A",
        name=f"Audit Part {n}",
        description="seed part for audit-persistence coverage",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=not is_deleted,
        status="obsolete" if is_deleted else "active",
    )
    if is_deleted:
        part.is_deleted = True
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def _part_create_payload() -> dict:
    n = _next()
    return {
        "part_number": f"NEWPART-{n:05d}",
        "name": f"Created Part {n}",
        "description": "created via POST /parts",
        "part_type": "manufactured",
        "unit_of_measure": "each",
    }


def _audit_rows(db: Session, *, resource_type: str, resource_id: int, action: str = None):
    """Fetch AuditLog rows for a resource, newest first, optionally by action.

    ``expire_all`` first so rows committed through the endpoint's session (the
    same ``db_session`` the client overrides ``get_db`` with) are reloaded
    instead of served stale from the identity map.
    """
    db.expire_all()
    q = db.query(AuditLog).filter(
        AuditLog.resource_type == resource_type,
        AuditLog.resource_id == resource_id,
    )
    if action is not None:
        q = q.filter(AuditLog.action == action)
    return q.order_by(AuditLog.sequence_number.desc()).all()


def _committed_audit_rows(db: Session, *, resource_type: str, resource_id: int, action: str = None):
    """Fetch AuditLog rows that were actually COMMITTED, not merely flushed.

    Rolling back BEFORE querying is the real guard against the audit-after-commit
    bug: a committed audit row survives the rollback, while a flushed-but-uncommitted
    one is discarded. See the module docstring for the full rationale.
    """
    db.rollback()
    return _audit_rows(db, resource_type=resource_type, resource_id=resource_id, action=action)


# ---------------------------------------------------------------------------
# create_part -> committed CREATE audit row
# ---------------------------------------------------------------------------


def test_create_part_emits_committed_create_audit(client: TestClient, db_session: Session):
    """POST /parts/ emits a CREATE AuditLog row for resource_type 'part', tagged
    with the caller's company. Would FAIL against audit-after-commit code: the
    CREATE row would be flushed into a never-committed transaction and discarded
    by the rollback in ``_committed_audit_rows`` (0 rows seen instead of 1)."""
    admin = _make_user(db_session, role=UserRole.ADMIN)

    resp = client.post("/api/v1/parts/", headers=_headers_for(admin), json=_part_create_payload())
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    new_id = resp.json()["id"]

    rows = _committed_audit_rows(db_session, resource_type="part", resource_id=new_id, action="CREATE")
    assert len(rows) == 1, "expected exactly one COMMITTED CREATE audit row for the new part"
    assert rows[0].action == "CREATE"
    assert rows[0].resource_id == new_id
    assert rows[0].company_id == COMPANY_A


def test_create_part_create_audit_committed_for_manager(client: TestClient, db_session: Session):
    """A MANAGER (another role permitted by require_role) also gets a committed
    CREATE audit row, tenant-tagged to their company."""
    manager = _make_user(db_session, role=UserRole.MANAGER)

    resp = client.post("/api/v1/parts/", headers=_headers_for(manager), json=_part_create_payload())
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    new_id = resp.json()["id"]

    rows = _committed_audit_rows(db_session, resource_type="part", resource_id=new_id, action="CREATE")
    assert len(rows) == 1
    assert rows[0].action == "CREATE"
    assert rows[0].company_id == COMPANY_A


# ---------------------------------------------------------------------------
# update_part -> committed UPDATE audit row
# ---------------------------------------------------------------------------


def test_update_part_emits_committed_update_audit(client: TestClient, db_session: Session):
    """PUT /parts/{id} with a real field change emits an UPDATE AuditLog row.
    Would FAIL against audit-after-commit code: the UPDATE row would not survive
    the rollback (0 rows instead of 1)."""
    admin = _make_user(db_session, role=UserRole.ADMIN)
    part = _make_part(db_session)

    resp = client.put(
        f"/api/v1/parts/{part.id}",
        headers=_headers_for(admin),
        json={"version": 0, "name": "Renamed Audit Part"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["name"] == "Renamed Audit Part"

    rows = _committed_audit_rows(db_session, resource_type="part", resource_id=part.id, action="UPDATE")
    assert len(rows) == 1, "expected exactly one COMMITTED UPDATE audit row"
    assert rows[0].action == "UPDATE"
    assert rows[0].resource_id == part.id
    assert rows[0].company_id == COMPANY_A


# ---------------------------------------------------------------------------
# delete_part -> committed DELETE audit row (soft + hard)
# ---------------------------------------------------------------------------


def test_soft_delete_part_emits_committed_delete_audit(client: TestClient, db_session: Session):
    """DELETE /parts/{id} (soft, the default) emits a DELETE AuditLog row.
    Would FAIL against audit-after-commit code: the DELETE row would be discarded
    by the rollback (0 rows instead of 1)."""
    admin = _make_user(db_session, role=UserRole.ADMIN)
    part = _make_part(db_session)

    resp = client.delete(f"/api/v1/parts/{part.id}", headers=_headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["can_restore"] is True

    rows = _committed_audit_rows(db_session, resource_type="part", resource_id=part.id, action="DELETE")
    assert len(rows) == 1, "expected exactly one COMMITTED DELETE audit row for the soft delete"
    assert rows[0].action == "DELETE"
    assert rows[0].resource_id == part.id
    assert rows[0].company_id == COMPANY_A
    # The handler records this as a soft delete in extra_data.
    assert (rows[0].extra_data or {}).get("soft_delete") is True

    # Sanity: the entity itself committed as a soft delete (row still present).
    db_session.expire_all()
    db_row = db_session.query(Part).filter(Part.id == part.id).first()
    assert db_row is not None and db_row.is_deleted is True


def test_hard_delete_part_emits_committed_delete_audit(client: TestClient, db_session: Session):
    """DELETE /parts/{id}?hard_delete=true emits a DELETE AuditLog row whose
    resource_id survives the physical row removal. Would FAIL against
    audit-after-commit code: the DELETE row would be discarded by the rollback
    (0 rows instead of 1)."""
    admin = _make_user(db_session, role=UserRole.ADMIN)
    part = _make_part(db_session)
    part_id = part.id

    resp = client.delete(f"/api/v1/parts/{part_id}?hard_delete=true", headers=_headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["message"] == "Part permanently deleted"

    rows = _committed_audit_rows(db_session, resource_type="part", resource_id=part_id, action="DELETE")
    assert len(rows) == 1, "expected exactly one COMMITTED DELETE audit row for the hard delete"
    assert rows[0].action == "DELETE"
    assert rows[0].resource_id == part_id
    assert rows[0].company_id == COMPANY_A

    # Sanity: the entity itself was physically removed.
    db_session.expire_all()
    assert db_session.query(Part).filter(Part.id == part_id).first() is None


# ---------------------------------------------------------------------------
# restore_part -> committed RESTORE audit row
# ---------------------------------------------------------------------------


def test_restore_part_emits_committed_restore_audit(client: TestClient, db_session: Session):
    """POST /parts/{id}/restore on a soft-deleted part emits a RESTORE AuditLog
    row (log_update is called with action='restore', so the audit action is the
    upper-cased 'RESTORE'). Would FAIL against audit-after-commit code: the
    RESTORE row would be discarded by the rollback (0 rows instead of 1)."""
    admin = _make_user(db_session, role=UserRole.ADMIN)
    part = _make_part(db_session, is_deleted=True)

    resp = client.post(f"/api/v1/parts/{part.id}/restore", headers=_headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["part_id"] == part.id

    rows = _committed_audit_rows(db_session, resource_type="part", resource_id=part.id, action="RESTORE")
    assert len(rows) == 1, "expected exactly one COMMITTED RESTORE audit row"
    assert rows[0].action == "RESTORE"
    assert rows[0].resource_id == part.id
    assert rows[0].company_id == COMPANY_A
    # log_update captures the old/new is_deleted + status transition.
    assert rows[0].old_values == {"is_deleted": True, "status": "obsolete"}
    assert rows[0].new_values == {"is_deleted": False, "status": "active"}

    # Sanity: the entity itself committed the restore.
    db_session.expire_all()
    db_row = db_session.query(Part).filter(Part.id == part.id).first()
    assert db_row is not None and db_row.is_deleted is False
