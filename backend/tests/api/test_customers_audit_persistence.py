"""Committed-only audit-persistence coverage for the customers endpoints.

Locks in the hardening on branch qa/full-pass-2026-06-04 that makes the
delete/restore handlers in ``app.api.endpoints.customers`` log through
``AuditService`` BEFORE the terminal ``db.commit()`` so the audit row commits
atomically with the entity change.

Why "committed-only" matters (the loophole these tests close)
-------------------------------------------------------------
The ``client`` fixture overrides ``get_db`` to yield ONE shared, never-closed
``db_session`` (see tests/conftest.py). The endpoint and the test therefore
share a single open transaction. ``AuditService.log()`` only ``flush()``es; the
handler owns the ``commit()``. If a handler logged AFTER ``db.commit()`` (the
class of bug just fixed -- "audit-after-commit"), the audit row would land in a
fresh, never-committed transaction -- yet a plain ``db.query(AuditLog)`` in the
test would STILL see it, because the read happens inside that same open
transaction. A naive assertion passes against the bug.

The fix, mirrored from tests/api/test_qms_soft_delete_audit.py:
``_committed_audit_rows`` calls ``db.rollback()`` BEFORE querying ``AuditLog``.
A committed audit row survives the rollback (the commit already ended its
transaction); a flushed-but-uncommitted one is discarded. The entity itself is
committed by the handler in both the buggy and fixed versions, so only the
audit row's durability is being probed.

Why each assertion FAILS against the old audit-after-commit code
----------------------------------------------------------------
Each test asserts ``len(rows) == 1`` AFTER a rollback. Against a handler that
logged after committing, the audit row would be flushed into the post-commit
transaction and discarded by ``_committed_audit_rows``' rollback -- so the
query would return ``[]`` and ``len(rows) == 1`` would fail. Only logging
BEFORE the commit (the current, fixed code) makes the row durable and the
assertion pass.

Handlers covered (the ones that emit audit rows):
- ``DELETE /customers/{id}``            -> soft delete -> action "DELETE"
- ``DELETE /customers/{id}?hard_delete``-> hard delete -> action "DELETE"
- ``POST   /customers/{id}/restore``    -> restore     -> action "RESTORE"

``create_customer`` / ``update_customer`` do not invoke ``AuditService`` at all
in this router, so there is no commit-ordering invariant to probe there; a
guard test documents that absence rather than asserting a row that is never
written.

We do NOT insert ``AuditLog`` rows directly (tamper-evident hash chain); they
are produced by the endpoints and only read back here. The default seeded
company is id=1 (tests/conftest.py).
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.user import User, UserRole

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
RESOURCE = "customer"

# Module-level counter so every fixture row gets a globally unique natural key,
# even across tests sharing a worker DB under -n auto.
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _make_user(db: Session, *, company_id: int = COMPANY_A, role: UserRole = UserRole.ADMIN) -> User:
    n = _next()
    user = User(
        email=f"cust-aud-{n}@co{company_id}.test",
        employee_id=f"CUSTAUD-{n:05d}",
        first_name="Cust",
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


def _make_customer(db: Session, *, company_id: int = COMPANY_A, is_deleted: bool = False) -> Customer:
    n = _next()
    customer = Customer(
        company_id=company_id,
        name=f"Customer {n}",
        code=f"CUS{n:05d}",
        is_active=not is_deleted,
        is_deleted=is_deleted,
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def _audit_rows(db: Session, *, resource_id: int, action: str = None):
    """Fetch AuditLog rows for the customer resource, newest first.

    ``expire_all`` first so rows committed through the endpoint's session (the
    same ``db_session`` the client overrides ``get_db`` with) are reloaded
    instead of served stale from the identity map.
    """
    db.expire_all()
    q = db.query(AuditLog).filter(
        AuditLog.resource_type == RESOURCE,
        AuditLog.resource_id == resource_id,
    )
    if action is not None:
        q = q.filter(AuditLog.action == action)
    return q.order_by(AuditLog.sequence_number.desc()).all()


def _committed_audit_rows(db: Session, *, resource_id: int, action: str = None):
    """Fetch AuditLog rows that were actually COMMITTED, not merely flushed.

    Rolling back BEFORE querying is the real guard against the audit-after-commit
    bug: a committed audit row survives the rollback, while a flushed-but-uncommitted
    one is discarded. See the module docstring.
    """
    db.rollback()
    return _audit_rows(db, resource_id=resource_id, action=action)


# ---------------------------------------------------------------------------
# DELETE -> soft delete -> committed DELETE audit row
# ---------------------------------------------------------------------------


def test_soft_delete_customer_persists_delete_audit(client: TestClient, db_session: Session):
    """DELETE /customers/{id} (soft) emits a COMMITTED DELETE AuditLog row for
    resource_type 'customer', tagged with the caller's company.

    FAILS against audit-after-commit code: the row would be flushed into the
    post-commit transaction and discarded by the rollback in
    ``_committed_audit_rows``, leaving ``rows == []`` and ``len(rows) == 1`` failing.
    """
    admin = _make_user(db_session, role=UserRole.ADMIN)
    customer = _make_customer(db_session)

    resp = client.delete(f"/api/v1/customers/{customer.id}", headers=_headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json().get("can_restore") is True

    # Entity was soft-deleted (committed by the handler in both buggy and fixed code).
    db_session.expire_all()
    row = db_session.query(Customer).filter(Customer.id == customer.id).first()
    assert row is not None, "customer was hard-deleted -- expected a soft delete"
    assert row.is_deleted is True
    assert row.deleted_by == admin.id

    # Require the audit row to be COMMITTED (rollback-then-query), not merely flushed.
    rows = _committed_audit_rows(db_session, resource_id=customer.id, action="DELETE")
    assert len(rows) == 1, "expected exactly one COMMITTED DELETE audit row for the soft-deleted customer"
    assert rows[0].action == "DELETE"
    assert rows[0].resource_type == RESOURCE
    assert rows[0].resource_id == customer.id
    assert rows[0].company_id == COMPANY_A


# ---------------------------------------------------------------------------
# DELETE?hard_delete=true -> hard delete -> committed DELETE audit row
# ---------------------------------------------------------------------------


def test_hard_delete_customer_persists_delete_audit(client: TestClient, db_session: Session):
    """DELETE /customers/{id}?hard_delete=true emits a COMMITTED DELETE AuditLog
    row, and the row is logged BEFORE the row is physically removed so it commits
    atomically with the delete.

    FAILS against audit-after-commit code: logging after ``db.commit()`` would put
    the audit row in a never-committed transaction, discarded by the rollback, so
    ``len(rows) == 1`` fails.
    """
    admin = _make_user(db_session, role=UserRole.ADMIN)
    customer = _make_customer(db_session)
    customer_id = customer.id

    resp = client.delete(
        f"/api/v1/customers/{customer_id}",
        params={"hard_delete": True},
        headers=_headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json() == {"message": "Customer permanently deleted"}

    # Entity is physically gone (committed by the handler).
    db_session.expire_all()
    assert db_session.query(Customer).filter(Customer.id == customer_id).first() is None

    # Require the audit row to be COMMITTED (rollback-then-query), not merely flushed.
    rows = _committed_audit_rows(db_session, resource_id=customer_id, action="DELETE")
    assert len(rows) == 1, "expected exactly one COMMITTED DELETE audit row for the hard-deleted customer"
    assert rows[0].action == "DELETE"
    assert rows[0].resource_type == RESOURCE
    assert rows[0].resource_id == customer_id
    assert rows[0].company_id == COMPANY_A


# ---------------------------------------------------------------------------
# POST /restore -> restore -> committed RESTORE audit row
# ---------------------------------------------------------------------------


def test_restore_customer_persists_restore_audit(client: TestClient, db_session: Session):
    """POST /customers/{id}/restore on a soft-deleted customer emits a COMMITTED
    RESTORE AuditLog row carrying the is_deleted True->False transition,
    company-tagged.

    ``restore_customer`` calls ``audit.log_update(..., action="restore")``, which
    ``log_update`` uppercases to action "RESTORE".

    FAILS against audit-after-commit code: the RESTORE row would be flushed into
    the post-commit transaction and discarded by the rollback, so ``len(rows) == 1``
    fails.
    """
    admin = _make_user(db_session, role=UserRole.ADMIN)
    customer = _make_customer(db_session, is_deleted=True)

    resp = client.post(f"/api/v1/customers/{customer.id}/restore", headers=_headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    # Entity was restored (committed by the handler).
    db_session.expire_all()
    row = db_session.query(Customer).filter(Customer.id == customer.id).first()
    assert row is not None
    assert row.is_deleted is False
    assert row.is_active is True

    # Require the audit row to be COMMITTED (rollback-then-query), not merely flushed.
    rows = _committed_audit_rows(db_session, resource_id=customer.id, action="RESTORE")
    assert len(rows) == 1, "expected exactly one COMMITTED RESTORE audit row for the restored customer"
    restore_row = rows[0]
    assert restore_row.action == "RESTORE"
    assert restore_row.resource_type == RESOURCE
    assert restore_row.resource_id == customer.id
    assert restore_row.company_id == COMPANY_A
    # log_update captures the is_deleted transition in old/new values.
    assert restore_row.old_values == {"is_deleted": True}
    assert restore_row.new_values == {"is_deleted": False}


# ---------------------------------------------------------------------------
# Guard: create/update in this router emit no audit row (no invariant to probe)
# ---------------------------------------------------------------------------


def test_create_and_update_customer_emit_no_audit_rows(client: TestClient, db_session: Session):
    """Documents that ``create_customer`` and ``update_customer`` do NOT log to
    ``AuditService`` in this router, so there is no commit-ordering invariant to
    assert for them. If audit logging is later added to these handlers, this guard
    will fail and prompt committed-row coverage like the delete/restore tests above.
    """
    admin = _make_user(db_session, role=UserRole.ADMIN)

    create_resp = client.post(
        "/api/v1/customers/",
        headers=_headers_for(admin),
        json={"name": f"Created Co {_next()}"},
    )
    assert create_resp.status_code == status.HTTP_200_OK, create_resp.text
    new_id = create_resp.json()["id"]

    update_resp = client.put(
        f"/api/v1/customers/{new_id}",
        headers=_headers_for(admin),
        json={"phone": "555-0100"},
    )
    assert update_resp.status_code == status.HTTP_200_OK, update_resp.text

    # No audit rows of any action for this resource (read after a rollback so we
    # only count committed rows -- and there should be none at all).
    rows = _committed_audit_rows(db_session, resource_id=new_id)
    assert rows == [], "create/update do not log audit in this router -- add committed-row coverage if that changes"
