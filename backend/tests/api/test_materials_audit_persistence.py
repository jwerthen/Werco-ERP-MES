"""Committed-only audit-persistence coverage for the materials endpoints.

Locks in the audit hardening on branch qa/full-pass-2026-06-04 for the three
state-changing handlers in ``app.api.endpoints.materials``:

- ``create_material``  (POST   /materials/)              -> action CREATE
- ``update_material``  (PUT    /materials/{id})          -> action UPDATE
- ``delete_material``  (DELETE /materials/{id})          -> action DELETE
                       (both the default soft delete and ``?hard_delete=true``)

This router has no compliance-status transition, so there is no STATUS_CHANGE
case to cover -- every audited material mutation maps to CREATE / UPDATE /
DELETE only.

Why these assertions require a COMMITTED audit row
--------------------------------------------------
The ``client`` fixture (tests/conftest.py) overrides ``get_db`` to yield ONE
shared, never-closed ``db_session``, so the endpoint and the test run inside a
single open transaction. ``AuditService.log()`` only ``flush()``es -- the
handler is responsible for the ``commit()``. If a handler logged AFTER
``db.commit()`` (the bug that was fixed: audit-after-commit), the audit row
would land in a fresh, never-committed transaction. A naive
``db.query(AuditLog)`` in the test would STILL SEE that flushed row, because the
read happens inside the same open transaction -- so a naive assertion passes
against broken code.

``_committed_audit_rows`` closes that loophole: it calls ``db.rollback()``
BEFORE querying. A committed audit row survives the rollback (commit already
ended its transaction); a flushed-but-uncommitted one is discarded. The entity
itself is committed by the handler in both the buggy and fixed versions, so only
the audit row's durability is probed.

Per handler, against the OLD audit-after-commit code these tests would FAIL:

- create_material: the entity is committed (db.commit on line 114), but if the
  log_create call ran after that commit, the CREATE audit row would be flushed
  into a new uncommitted transaction. ``_committed_audit_rows`` rolls it away ->
  ``len(rows) == 1`` fails (0 rows found).
- update_material: same shape -- a post-commit log_update flush is rolled back,
  so the UPDATE row count assertion fails.
- delete_material (soft and hard): a post-commit log_delete flush is rolled
  back, so the DELETE row count assertion fails.

We do NOT insert ``AuditLog`` rows directly (they carry a tamper-evident hash
chain); they are produced by the endpoints and only read back here. The default
seeded company is id=1 (tests/conftest.py).
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
RESOURCE_TYPE = "material"

# Module-level counter so every fixture row gets a globally unique natural key,
# even across tests sharing a worker DB under -n auto.
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _make_user(db: Session, *, company_id: int = COMPANY_A, role: UserRole = UserRole.ADMIN) -> User:
    n = _next()
    user = User(
        email=f"mat-audit-{n}@co{company_id}.test",
        employee_id=f"MATAUD-{n:05d}",
        first_name="Mat",
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


def _make_material(db: Session, *, company_id: int = COMPANY_A, part_type: str = "raw_material") -> Part:
    """Seed a material-type Part directly (bypassing the endpoint) for update/delete tests."""
    n = _next()
    part = Part(
        company_id=company_id,
        part_number=f"MAT-{n:06d}",
        revision="A",
        name=f"Material {n}",
        part_type=part_type,
        unit_of_measure="each",
        is_active=True,
        status="active",
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def _audit_rows(db: Session, *, resource_id: int, action: str = None):
    """Fetch AuditLog rows for a material, newest first, optionally filtered by action.

    ``expire_all`` first so rows committed through the endpoint's session (the
    same ``db_session`` the client overrides ``get_db`` with) are reloaded
    instead of served stale from the identity map.
    """
    db.expire_all()
    q = db.query(AuditLog).filter(
        AuditLog.resource_type == RESOURCE_TYPE,
        AuditLog.resource_id == resource_id,
    )
    if action is not None:
        q = q.filter(AuditLog.action == action)
    return q.order_by(AuditLog.sequence_number.desc()).all()


def _committed_audit_rows(db: Session, *, resource_id: int, action: str = None):
    """Fetch AuditLog rows that were actually COMMITTED, not merely flushed.

    Rolling back BEFORE querying is the real guard against the audit-after-commit
    bug: a committed audit row survives the rollback, while a flushed-only row is
    discarded. See the module docstring for the full rationale.
    """
    db.rollback()
    return _audit_rows(db, resource_id=resource_id, action=action)


def _material_payload() -> dict:
    n = _next()
    return {
        "part_number": f"NEWMAT-{n:06d}",
        "name": f"New Material {n}",
        "part_type": "raw_material",
        "unit_of_measure": "each",
    }


# ---------------------------------------------------------------------------
# CREATE
# ---------------------------------------------------------------------------


def test_create_material_emits_committed_create_audit(client: TestClient, db_session: Session):
    """POST /materials/ emits a COMMITTED CREATE AuditLog row for resource_type
    'material', carrying the new id and the caller's company.

    Against the old audit-after-commit code the CREATE log would be flushed into
    a post-commit transaction; ``_committed_audit_rows`` rolls it away and the
    count assertion fails.
    """
    admin = _make_user(db_session, role=UserRole.ADMIN)

    resp = client.post("/api/v1/materials/", headers=_headers_for(admin), json=_material_payload())
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    new_id = resp.json()["id"]

    rows = _committed_audit_rows(db_session, resource_id=new_id, action="CREATE")
    assert len(rows) == 1, "expected exactly one COMMITTED CREATE audit row for the new material"
    assert rows[0].action == "CREATE"
    assert rows[0].resource_type == RESOURCE_TYPE
    assert rows[0].resource_id == new_id
    assert rows[0].company_id == COMPANY_A


def test_create_material_create_audit_committed_for_manager(client: TestClient, db_session: Session):
    """A non-admin authorized role (MANAGER) creating a material also produces a
    COMMITTED CREATE audit row -- the audit emission is role-independent for the
    handler's allowed roles."""
    manager = _make_user(db_session, role=UserRole.MANAGER)

    resp = client.post("/api/v1/materials/", headers=_headers_for(manager), json=_material_payload())
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    new_id = resp.json()["id"]

    rows = _committed_audit_rows(db_session, resource_id=new_id, action="CREATE")
    assert len(rows) == 1
    assert rows[0].action == "CREATE"
    assert rows[0].company_id == COMPANY_A


# ---------------------------------------------------------------------------
# UPDATE
# ---------------------------------------------------------------------------


def test_update_material_emits_committed_update_audit(client: TestClient, db_session: Session):
    """PUT /materials/{id} that changes a real field emits a COMMITTED UPDATE
    AuditLog row, company-tagged.

    Against the old audit-after-commit code the UPDATE log would be flushed into
    a post-commit transaction; ``_committed_audit_rows`` rolls it away and the
    count assertion fails.
    """
    admin = _make_user(db_session, role=UserRole.ADMIN)
    material = _make_material(db_session)

    resp = client.put(
        f"/api/v1/materials/{material.id}",
        headers=_headers_for(admin),
        json={"version": 0, "name": "Renamed Material"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["name"] == "Renamed Material"

    rows = _committed_audit_rows(db_session, resource_id=material.id, action="UPDATE")
    assert len(rows) == 1, "expected exactly one COMMITTED UPDATE audit row for the material"
    assert rows[0].action == "UPDATE"
    assert rows[0].resource_type == RESOURCE_TYPE
    assert rows[0].resource_id == material.id
    assert rows[0].company_id == COMPANY_A


# ---------------------------------------------------------------------------
# DELETE (soft -- the default path)
# ---------------------------------------------------------------------------


def test_soft_delete_material_emits_committed_delete_audit(client: TestClient, db_session: Session):
    """DELETE /materials/{id} (default soft delete) emits a COMMITTED DELETE
    AuditLog row, company-tagged, and soft-flags the row.

    Against the old audit-after-commit code the DELETE log would be flushed into
    a post-commit transaction; ``_committed_audit_rows`` rolls it away and the
    count assertion fails.
    """
    admin = _make_user(db_session, role=UserRole.ADMIN)
    material = _make_material(db_session)

    resp = client.delete(f"/api/v1/materials/{material.id}", headers=_headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["can_restore"] is True

    rows = _committed_audit_rows(db_session, resource_id=material.id, action="DELETE")
    assert len(rows) == 1, "expected exactly one COMMITTED DELETE audit row for the soft-deleted material"
    assert rows[0].action == "DELETE"
    assert rows[0].resource_type == RESOURCE_TYPE
    assert rows[0].resource_id == material.id
    assert rows[0].company_id == COMPANY_A

    # Soft delete (not hard): the row persists, flagged and attributed.
    db_session.expire_all()
    row = db_session.query(Part).filter(Part.id == material.id).first()
    assert row is not None, "material was hard-deleted -- expected a soft delete"
    assert row.is_deleted is True
    assert row.deleted_by == admin.id


# ---------------------------------------------------------------------------
# DELETE (hard -- admin-only escape hatch)
# ---------------------------------------------------------------------------


def test_hard_delete_material_emits_committed_delete_audit(client: TestClient, db_session: Session):
    """DELETE /materials/{id}?hard_delete=true (no dependent rows) emits a
    COMMITTED DELETE AuditLog row and physically removes the material. The audit
    row must survive even though the entity itself is gone.

    Against the old audit-after-commit code the DELETE log would be flushed into
    a post-commit transaction; ``_committed_audit_rows`` rolls it away and the
    count assertion fails.
    """
    admin = _make_user(db_session, role=UserRole.ADMIN)
    material = _make_material(db_session)
    material_id = material.id

    resp = client.delete(
        f"/api/v1/materials/{material_id}",
        headers=_headers_for(admin),
        params={"hard_delete": "true"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["message"] == "Material permanently deleted"

    rows = _committed_audit_rows(db_session, resource_id=material_id, action="DELETE")
    assert len(rows) == 1, "expected exactly one COMMITTED DELETE audit row for the hard-deleted material"
    assert rows[0].action == "DELETE"
    assert rows[0].resource_type == RESOURCE_TYPE
    assert rows[0].resource_id == material_id
    assert rows[0].company_id == COMPANY_A

    # Hard delete physically removed the row.
    db_session.expire_all()
    assert db_session.query(Part).filter(Part.id == material_id).first() is None
