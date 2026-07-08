"""Committed-only audit-persistence coverage for the routing copy endpoint.

Locks in the audit-before-commit fix for ``copy_routing``
(POST /routing/{routing_id}/copy in ``app.api.endpoints.routing``). The handler
previously created the new ``Routing`` + copied ``RoutingOperation`` rows with
NO audit call at all; it now calls ``audit.log_create(...)`` BEFORE its terminal
``db.commit()`` so the CREATE audit row lands in the same transaction as the
copied routing. The bug class these tests guard against is an audit call placed
AFTER ``db.commit()``: ``AuditService.log()`` only ``flush()``es, so the row is
flushed into a fresh, never-committed transaction that request teardown rolls
back -- the state change silently loses its audit trail (an AS9100D / CMMC
violation).

Why a naive test would NOT catch that bug
------------------------------------------
The ``client`` fixture (tests/conftest.py) overrides ``get_db`` to yield ONE
shared, never-closed ``db_session``; the endpoint and the test share a single
open transaction. A flushed-but-uncommitted audit row is therefore fully
visible to a plain ``db.query(AuditLog)`` in the test -- a naive assertion
passes against BOTH the fixed and the broken (audit-after-commit) code.

The guard
---------
``_committed_audit_rows`` calls ``db.rollback()`` BEFORE querying. A committed
audit row survives the rollback (the handler's ``commit()`` already ended its
transaction); a flushed-but-uncommitted one is discarded. The copied routing
itself was committed by the handler either way, so only the audit row's
durability is probed. This is the proven technique from
tests/api/test_qms_soft_delete_audit.py and the rest of the
test_*_audit_persistence.py family.

We do NOT insert ``AuditLog`` rows directly (tamper-evident hash chain); they
are produced by the endpoint and only read back here. The default seeded
company is id=1 (tests/conftest.py).
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.part import Part
from app.models.routing import Routing, RoutingOperation
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter

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
        email=f"routing-audit-{n}@co{company_id}.test",
        employee_id=f"RTGAUD-{n:05d}",
        first_name="Routing",
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


def _make_part(db: Session, *, company_id: int = COMPANY_A) -> Part:
    n = _next()
    part = Part(
        company_id=company_id,
        part_number=f"RTG-AUD-P-{n:05d}",
        revision="A",
        name=f"Routing Audit Part {n}",
        description="seed part for routing audit-persistence coverage",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def _make_routing_with_operation(db: Session, *, part_id: int, company_id: int = COMPANY_A) -> Routing:
    """Seed a source routing with one operation so the copy path exercises the
    operation-copy loop, not just the bare Routing insert."""
    n = _next()
    work_center = WorkCenter(
        code=f"RTG-AUD-WC-{n:05d}",
        name=f"Routing Audit WC {n}",
        work_center_type="machining",
        is_active=True,
        company_id=company_id,
    )
    db.add(work_center)
    db.flush()

    routing = Routing(
        part_id=part_id,
        revision="A",
        description="source routing for copy audit coverage",
        status="released",
        is_active=True,
        company_id=company_id,
    )
    db.add(routing)
    db.flush()
    db.add(
        RoutingOperation(
            routing_id=routing.id,
            company_id=company_id,
            sequence=10,
            operation_number="Op 10",
            name="Machine Part",
            work_center_id=work_center.id,
            setup_hours=0.5,
            run_hours_per_unit=0.1,
            is_active=True,
        )
    )
    db.commit()
    db.refresh(routing)
    return routing


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
# copy_routing -> committed CREATE audit row
# ---------------------------------------------------------------------------


def test_copy_routing_emits_committed_create_audit(client: TestClient, db_session: Session):
    """POST /routing/{id}/copy emits a CREATE AuditLog row for resource_type
    'routing' keyed to the NEW routing id, with extra_data recording the source
    routing id, tenant-tagged to the caller's company. Would FAIL against
    audit-after-commit (or no-audit) code: zero committed rows would survive the
    rollback in ``_committed_audit_rows``."""
    admin = _make_user(db_session, role=UserRole.ADMIN)
    source_part = _make_part(db_session)
    target_part = _make_part(db_session)
    source_routing = _make_routing_with_operation(db_session, part_id=source_part.id)
    source_routing_id = source_routing.id

    resp = client.post(
        f"/api/v1/routing/{source_routing_id}/copy",
        headers=_headers_for(admin),
        params={"target_part_id": target_part.id, "new_revision": "B"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["message"] == "Routing copied"
    new_routing_id = body["new_routing_id"]
    assert isinstance(new_routing_id, int) and new_routing_id != source_routing_id

    rows = _committed_audit_rows(db_session, resource_type="routing", resource_id=new_routing_id, action="CREATE")
    assert len(rows) == 1, "expected exactly one COMMITTED CREATE audit row for the copied routing"
    assert rows[0].action == "CREATE"
    assert rows[0].resource_type == "routing"
    assert rows[0].resource_id == new_routing_id
    assert rows[0].company_id == COMPANY_A
    # The handler identifies the copy by the TARGET part's part number and
    # records the source routing in extra_data for traceability.
    assert rows[0].resource_identifier == target_part.part_number
    assert (rows[0].extra_data or {}).get("copied_from") == source_routing_id

    # Sanity: the copied routing itself committed, on the target part, with the
    # requested revision and the copied operation.
    db_session.expire_all()
    copied = db_session.query(Routing).filter(Routing.id == new_routing_id).first()
    assert copied is not None
    assert copied.part_id == target_part.id
    assert copied.revision == "B"
    ops = db_session.query(RoutingOperation).filter(RoutingOperation.routing_id == new_routing_id).all()
    assert len(ops) == 1 and ops[0].name == "Machine Part"


def test_copy_routing_create_audit_committed_for_manager(client: TestClient, db_session: Session):
    """A MANAGER (the other role permitted by require_role) also gets a
    committed CREATE audit row, tenant-tagged to their company."""
    manager = _make_user(db_session, role=UserRole.MANAGER)
    source_part = _make_part(db_session)
    target_part = _make_part(db_session)
    source_routing = _make_routing_with_operation(db_session, part_id=source_part.id)
    source_routing_id = source_routing.id

    resp = client.post(
        f"/api/v1/routing/{source_routing_id}/copy",
        headers=_headers_for(manager),
        params={"target_part_id": target_part.id, "new_revision": "B"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    new_routing_id = resp.json()["new_routing_id"]

    rows = _committed_audit_rows(db_session, resource_type="routing", resource_id=new_routing_id, action="CREATE")
    assert len(rows) == 1
    assert rows[0].action == "CREATE"
    assert rows[0].company_id == COMPANY_A
    assert (rows[0].extra_data or {}).get("copied_from") == source_routing_id
