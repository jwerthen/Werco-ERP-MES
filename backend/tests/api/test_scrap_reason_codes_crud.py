"""Compliance locks for /quality/scrap-reason-codes (Lean Phase 1, issue #88).

The scrap-code vocabulary is quality-system configuration, so the invariants are
the AS9100D/CMMC set:
  * tenant isolation -- codes never read/write across companies; ``code`` is
    unique PER TENANT (two companies may share a code string; DowntimeReasonCode's
    global unique is a known defect deliberately not copied),
  * RBAC -- writes are ADMIN/MANAGER/QUALITY; operators can read (the pickers)
    but never write,
  * audit -- create/update land tamper-evident audit_log rows,
  * deactivate-not-delete -- retirement is ``is_active=false``; there is NO
    DELETE route (historical scrap rows reference these ids for traceability).
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.scrap_reason import ScrapReasonCode
from app.models.user import UserRole
from tests.lean_phase1_helpers import COMPANY_A, COMPANY_B, headers_for, make_scrap_code, make_user

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

URL = "/api/v1/quality/scrap-reason-codes"


def _payload(**overrides) -> dict:
    payload = {"code": "OT", "name": "Out of tolerance", "category": "operator", "display_order": 1}
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


def test_operator_can_read_but_not_write(client: TestClient, db_session: Session):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    make_scrap_code(db_session, code="RD", name="Readable")

    read = client.get(URL, headers=headers_for(operator))
    assert read.status_code == status.HTTP_200_OK
    assert [row["code"] for row in read.json()] == ["RD"]

    create = client.post(URL, json=_payload(), headers=headers_for(operator))
    assert create.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY])
def test_write_roles_can_create(client: TestClient, db_session: Session, role: UserRole):
    user = make_user(db_session, role=role)
    resp = client.post(URL, json=_payload(code=f"W-{role.value[:6].upper()}"), headers=headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text


def test_operator_cannot_update(client: TestClient, db_session: Session):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    code = make_scrap_code(db_session)
    resp = client.put(f"{URL}/{code.id}", json={"name": "hijacked"}, headers=headers_for(operator))
    assert resp.status_code == status.HTTP_403_FORBIDDEN


# ---------------------------------------------------------------------------
# Create + audit + per-tenant uniqueness
# ---------------------------------------------------------------------------


def test_create_persists_fields_and_writes_audit_row(client: TestClient, db_session: Session):
    quality = make_user(db_session, role=UserRole.QUALITY)
    resp = client.post(URL, json=_payload(description="Dimension out of print tolerance"), headers=headers_for(quality))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["code"] == "OT"
    assert body["name"] == "Out of tolerance"
    assert body["category"] == "operator"
    assert body["is_active"] is True
    assert body["display_order"] == 1

    audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == "scrap_reason_code", AuditLog.resource_id == body["id"])
        .all()
    )
    assert len(audit) == 1
    assert audit[0].action == "CREATE"
    assert audit[0].company_id == COMPANY_A
    assert audit[0].user_id == quality.id


def test_duplicate_code_same_tenant_is_400(client: TestClient, db_session: Session):
    manager = make_user(db_session, role=UserRole.MANAGER)
    make_scrap_code(db_session, code="DUP")
    resp = client.post(URL, json=_payload(code="DUP"), headers=headers_for(manager))
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "already exists" in resp.json()["detail"]


class _NoRows:
    """Stand-in query that finds nothing -- blinds a duplicate pre-check so the
    INSERT/UPDATE reaches the DB and trips the unique index at flush, exactly
    like a concurrent-create race the SELECT cannot see."""

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return None


def test_create_duplicate_that_escapes_the_precheck_is_400_not_500(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """The INSERT executes at the audit-path db.flush(), so a duplicate that the
    pre-check missed (lost race) raises IntegrityError THERE -- it must surface
    as the same 400 as the pre-check, never a 500 (pins the flush-level except)."""
    from app.api.endpoints import scrap_reasons as scrap_reasons_module

    manager = make_user(db_session, role=UserRole.MANAGER)
    make_scrap_code(db_session, code="RACE")

    monkeypatch.setattr(scrap_reasons_module, "tenant_query", lambda *a, **k: _NoRows())
    resp = client.post(URL, json=_payload(code="RACE"), headers=headers_for(manager))
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "already exists" in resp.json()["detail"]


def test_update_duplicate_that_escapes_the_precheck_is_400_not_500(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """Same race on the rename path: the UPDATE executes at the audit-path
    db.flush(); a duplicate the pre-check missed must 400, never 500."""
    from app.api.endpoints import scrap_reasons as scrap_reasons_module

    manager = make_user(db_session, role=UserRole.MANAGER)
    make_scrap_code(db_session, code="KEEP2")
    victim = make_scrap_code(db_session, code="MOVE2")

    real_tenant_query = scrap_reasons_module.tenant_query
    calls = {"n": 0}

    def fake_tenant_query(db, model, cid):
        calls["n"] += 1
        if calls["n"] == 1:  # the target-row fetch must still succeed
            return real_tenant_query(db, model, cid)
        return _NoRows()  # blind the duplicate pre-check

    monkeypatch.setattr(scrap_reasons_module, "tenant_query", fake_tenant_query)
    resp = client.put(f"{URL}/{victim.id}", json={"code": "KEEP2"}, headers=headers_for(manager))
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "already exists" in resp.json()["detail"]

    # The failed rename rolled back -- the victim keeps its code.
    db_session.expire_all()
    assert db_session.get(ScrapReasonCode, victim.id).code == "MOVE2"


def test_two_tenants_can_share_a_code_string(client: TestClient, db_session: Session):
    """Per-company uniqueness: 'OT' in company A must not block 'OT' in company B."""
    make_scrap_code(db_session, company_id=COMPANY_A, code="OT")
    manager_b = make_user(db_session, role=UserRole.MANAGER, company_id=COMPANY_B)

    resp = client.post(URL, json=_payload(code="OT"), headers=headers_for(manager_b))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    codes = db_session.query(ScrapReasonCode).filter(ScrapReasonCode.code == "OT").all()
    assert sorted(code.company_id for code in codes) == [COMPANY_A, COMPANY_B]


# ---------------------------------------------------------------------------
# Tenant isolation (read + write)
# ---------------------------------------------------------------------------


def test_list_is_tenant_scoped(client: TestClient, db_session: Session):
    make_scrap_code(db_session, company_id=COMPANY_A, code="MINE")
    make_scrap_code(db_session, company_id=COMPANY_B, code="THEIRS")
    user_a = make_user(db_session, company_id=COMPANY_A)

    resp = client.get(URL, params={"include_inactive": True}, headers=headers_for(user_a))
    assert resp.status_code == status.HTTP_200_OK
    assert [row["code"] for row in resp.json()] == ["MINE"]


def test_cross_tenant_update_is_404_and_mutates_nothing(client: TestClient, db_session: Session):
    foreign = make_scrap_code(db_session, company_id=COMPANY_B, code="FRGN", name="Foreign")
    manager_a = make_user(db_session, role=UserRole.MANAGER, company_id=COMPANY_A)

    resp = client.put(f"{URL}/{foreign.id}", json={"name": "stolen"}, headers=headers_for(manager_a))
    assert resp.status_code == status.HTTP_404_NOT_FOUND

    db_session.expire_all()
    assert db_session.get(ScrapReasonCode, foreign.id).name == "Foreign"


# ---------------------------------------------------------------------------
# Update + deactivate-not-delete
# ---------------------------------------------------------------------------


def test_update_writes_audit_row_with_old_and_new_values(client: TestClient, db_session: Session):
    manager = make_user(db_session, role=UserRole.MANAGER)
    code = make_scrap_code(db_session, code="UPD", name="Before")

    resp = client.put(f"{URL}/{code.id}", json={"name": "After"}, headers=headers_for(manager))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["name"] == "After"

    audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == "scrap_reason_code", AuditLog.resource_id == code.id)
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert audit is not None
    assert audit.action == "UPDATE"
    assert (audit.old_values or {}).get("name") == "Before"
    assert (audit.new_values or {}).get("name") == "After"


def test_update_to_duplicate_code_is_400(client: TestClient, db_session: Session):
    manager = make_user(db_session, role=UserRole.MANAGER)
    make_scrap_code(db_session, code="KEEP")
    victim = make_scrap_code(db_session, code="MOVE")
    resp = client.put(f"{URL}/{victim.id}", json={"code": "KEEP"}, headers=headers_for(manager))
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_deactivate_is_a_flag_not_a_delete_and_no_delete_route_exists(client: TestClient, db_session: Session):
    manager = make_user(db_session, role=UserRole.MANAGER)
    code = make_scrap_code(db_session, code="RET", name="Retired soon")

    resp = client.put(f"{URL}/{code.id}", json={"is_active": False}, headers=headers_for(manager))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["is_active"] is False

    # The row still exists (traceability) -- retirement is a flag.
    db_session.expire_all()
    row = db_session.get(ScrapReasonCode, code.id)
    assert row is not None and row.is_active is False

    # Default list hides it; include_inactive shows it (reactivation path).
    assert [r["code"] for r in client.get(URL, headers=headers_for(manager)).json()] == []
    listed = client.get(URL, params={"include_inactive": True}, headers=headers_for(manager)).json()
    assert [r["code"] for r in listed] == ["RET"]

    # There is deliberately NO delete endpoint.
    assert (
        client.delete(f"{URL}/{code.id}", headers=headers_for(manager)).status_code
        == status.HTTP_405_METHOD_NOT_ALLOWED
    )


def test_list_sorts_by_display_order_then_code_and_filters_category(client: TestClient, db_session: Session):
    user = make_user(db_session)
    make_scrap_code(db_session, code="ZZZ", display_order=0, category="material")
    make_scrap_code(db_session, code="AAA", display_order=2, category="operator")
    make_scrap_code(db_session, code="MMM", display_order=1, category="material")

    resp = client.get(URL, headers=headers_for(user))
    assert [row["code"] for row in resp.json()] == ["ZZZ", "MMM", "AAA"]

    filtered = client.get(URL, params={"category": "material"}, headers=headers_for(user))
    assert [row["code"] for row in filtered.json()] == ["ZZZ", "MMM"]
