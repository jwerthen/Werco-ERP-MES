"""Downtime events stay inside the caller's tenant, are role-gated and audited.

``api/endpoints/downtime.py`` scoped its own rows by ``company_id`` but trusted every id it
was handed: ``work_center_id`` was stored unvalidated (so a caller could log downtime against
another tenant's machine, and the response rendered that machine's name straight back), and
``PUT`` was a blind setattr that could re-point an event at a foreign machine or job. Every
handler was a bare ``get_current_user`` and none wrote an audit row, although downtime is the
OEE availability input.

Write cases assert the STORED rows and the audit-row count, not just the status code.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.downtime import DowntimeCategory, DowntimeEvent, DowntimeReasonCode
from app.models.user import UserRole
from tests.api.kiosk_test_helpers import (
    COMPANY_A,
    COMPANY_B,
    ensure_company,
    make_user,
    make_wo_with_operation,
    make_work_center,
    user_headers,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

DOWNTIME_URL = "/api/v1/downtime"
FOREIGN_WC_NAME = "ACME-SECRET Mazak Integrex Cell 3"


def headers_for(db: Session, *, company_id: int = COMPANY_A, role: UserRole = UserRole.OPERATOR) -> dict:
    return user_headers(make_user(db, company_id=company_id, role=role))


def audit_count(db: Session) -> int:
    return db.query(AuditLog).count()


def event_count(db: Session) -> int:
    return db.query(DowntimeEvent).count()


def make_event(db: Session, *, company_id: int = COMPANY_A, work_center_id: int, resolved: bool = False):
    reporter = make_user(db, company_id=company_id)
    start = datetime.utcnow() - timedelta(hours=2)
    event = DowntimeEvent(
        work_center_id=work_center_id,
        start_time=start,
        end_time=start + timedelta(minutes=45) if resolved else None,
        duration_minutes=45.0 if resolved else None,
        category=DowntimeCategory.MECHANICAL,
        description="Spindle alarm",
        reported_by=reporter.id,
        company_id=company_id,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


# ---------------------------------------------------------------------------
# 1. Writes are audited and stamped with the caller's company
# ---------------------------------------------------------------------------


class TestDowntimeWrites:
    def test_create_update_resolve_each_write_an_audit_row(self, client: TestClient, db_session: Session):
        work_center = make_work_center(db_session)
        other_center = make_work_center(db_session)
        operator = headers_for(db_session)
        supervisor = headers_for(db_session, role=UserRole.SUPERVISOR)

        created = client.post(
            f"{DOWNTIME_URL}/", json={"work_center_id": work_center.id, "category": "mechanical"}, headers=operator
        )
        assert created.status_code == 200, created.text
        event_id = created.json()["id"]
        assert created.json()["work_center"]["id"] == work_center.id

        updated = client.put(
            f"{DOWNTIME_URL}/{event_id}",
            json={"work_center_id": other_center.id, "category": "electrical"},
            headers=supervisor,
        )
        assert updated.status_code == 200, updated.text
        resolved = client.post(
            f"{DOWNTIME_URL}/{event_id}/resolve", json={"resolution": "Reset drive"}, headers=operator
        )
        assert resolved.status_code == 200, resolved.text

        event = db_session.query(DowntimeEvent).filter(DowntimeEvent.id == event_id).one()
        assert event.company_id == COMPANY_A
        assert event.work_center_id == other_center.id
        rows = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "downtime_event", AuditLog.resource_id == event_id)
            .order_by(AuditLog.id)
            .all()
        )
        assert [row.action for row in rows] == ["CREATE", "UPDATE", "STATUS_CHANGE"]
        assert all(row.company_id == COMPANY_A for row in rows)
        assert rows[1].extra_data["changes"]["work_center_id"] == {"old": work_center.id, "new": other_center.id}
        assert rows[2].new_values == {"status": "resolved"}

    def test_reads_serialize_events_that_have_a_reporter(self, client: TestClient, db_session: Session):
        # ``UserInfo.username`` was required but ``User`` has no such column, so every
        # event (``reported_by`` is NOT NULL) failed response validation with a 500.
        work_center = make_work_center(db_session)
        event = make_event(db_session, work_center_id=work_center.id)
        headers = headers_for(db_session, role=UserRole.VIEWER)

        listed = client.get(f"{DOWNTIME_URL}/", headers=headers)
        single = client.get(f"{DOWNTIME_URL}/{event.id}", headers=headers)

        assert listed.status_code == 200, listed.text
        assert single.status_code == 200, single.text
        assert [row["id"] for row in listed.json()] == [event.id]
        assert single.json()["reporter"]["id"] == event.reported_by
        assert single.json()["reporter"]["username"] is None

    @pytest.mark.parametrize("field", ["work_center_id", "category", "planned_type"])
    def test_explicit_null_on_a_required_field_is_422_and_writes_nothing(
        self, client: TestClient, db_session: Session, field: str
    ):
        work_center = make_work_center(db_session)
        event = make_event(db_session, work_center_id=work_center.id)
        headers = headers_for(db_session, role=UserRole.MANAGER)
        audit_before = audit_count(db_session)

        response = client.put(f"{DOWNTIME_URL}/{event.id}", json={field: None}, headers=headers)

        assert response.status_code == 422
        assert audit_count(db_session) == audit_before
        db_session.refresh(event)
        assert (event.work_center_id, event.category) == (work_center.id, DowntimeCategory.MECHANICAL)

    def test_reason_code_writes_are_audited(self, client: TestClient, db_session: Session):
        headers = headers_for(db_session, role=UserRole.MANAGER)
        created = client.post(
            f"{DOWNTIME_URL}/reason-codes",
            json={"code": "MECH-01", "name": "Spindle", "category": "mechanical"},
            headers=headers,
        )
        assert created.status_code == 200, created.text
        code_id = created.json()["id"]
        updated = client.put(f"{DOWNTIME_URL}/reason-codes/{code_id}", json={"name": "Spindle fault"}, headers=headers)
        assert updated.status_code == 200, updated.text
        actions = [
            row.action
            for row in db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "downtime_reason_code", AuditLog.resource_id == code_id)
            .order_by(AuditLog.id)
        ]
        assert actions == ["CREATE", "UPDATE"]


# ---------------------------------------------------------------------------
# 2. Foreign ids are refused; another tenant's event is a flat 404
# ---------------------------------------------------------------------------


class TestDowntimeTenantIsolation:
    def test_create_against_another_tenants_work_center_is_404_and_writes_nothing(
        self, client: TestClient, db_session: Session
    ):
        foreign_wc = make_work_center(db_session, company_id=COMPANY_A, name=FOREIGN_WC_NAME)
        headers = headers_for(db_session, company_id=COMPANY_B)
        events_before = event_count(db_session)
        audit_before = audit_count(db_session)

        response = client.post(f"{DOWNTIME_URL}/", json={"work_center_id": foreign_wc.id}, headers=headers)

        assert response.status_code == 404
        assert FOREIGN_WC_NAME not in response.text
        assert event_count(db_session) == events_before
        assert audit_count(db_session) == audit_before

    def test_create_against_another_tenants_work_order_is_404(self, client: TestClient, db_session: Session):
        foreign_wc = make_work_center(db_session, company_id=COMPANY_A)
        foreign_wo, _ = make_wo_with_operation(db_session, company_id=COMPANY_A, work_center=foreign_wc)
        own_wc = make_work_center(db_session, company_id=COMPANY_B)
        headers = headers_for(db_session, company_id=COMPANY_B)
        events_before = event_count(db_session)

        response = client.post(
            f"{DOWNTIME_URL}/", json={"work_center_id": own_wc.id, "work_order_id": foreign_wo.id}, headers=headers
        )

        assert response.status_code == 404
        assert event_count(db_session) == events_before

    def test_another_tenants_event_is_404_on_every_verb_with_no_side_effects(
        self, client: TestClient, db_session: Session
    ):
        victim_wc = make_work_center(db_session, company_id=COMPANY_A, name=FOREIGN_WC_NAME)
        event = make_event(db_session, company_id=COMPANY_A, work_center_id=victim_wc.id)
        headers = headers_for(db_session, company_id=COMPANY_B, role=UserRole.ADMIN)
        audit_before = audit_count(db_session)

        read = client.get(f"{DOWNTIME_URL}/{event.id}", headers=headers)
        put = client.put(f"{DOWNTIME_URL}/{event.id}", json={"description": "pwned"}, headers=headers)
        resolve = client.post(f"{DOWNTIME_URL}/{event.id}/resolve", json={}, headers=headers)

        assert (read.status_code, put.status_code, resolve.status_code) == (404, 404, 404)
        assert FOREIGN_WC_NAME not in read.text
        assert audit_count(db_session) == audit_before
        db_session.refresh(event)
        assert event.description == "Spindle alarm"
        assert event.end_time is None

    def test_put_cannot_repoint_an_event_at_another_tenants_machine(self, client: TestClient, db_session: Session):
        own_wc = make_work_center(db_session, company_id=COMPANY_B)
        foreign_wc = make_work_center(db_session, company_id=COMPANY_A)
        event = make_event(db_session, company_id=COMPANY_B, work_center_id=own_wc.id)
        headers = headers_for(db_session, company_id=COMPANY_B, role=UserRole.ADMIN)
        audit_before = audit_count(db_session)

        response = client.put(f"{DOWNTIME_URL}/{event.id}", json={"work_center_id": foreign_wc.id}, headers=headers)

        assert response.status_code == 404
        assert audit_count(db_session) == audit_before
        db_session.refresh(event)
        assert event.work_center_id == own_wc.id

    def test_reads_never_show_another_tenants_events_or_machine_names(self, client: TestClient, db_session: Session):
        victim_wc = make_work_center(db_session, company_id=COMPANY_A, name=FOREIGN_WC_NAME)
        make_event(db_session, company_id=COMPANY_A, work_center_id=victim_wc.id)
        make_event(db_session, company_id=COMPANY_A, work_center_id=victim_wc.id, resolved=True)
        headers = headers_for(db_session, company_id=COMPANY_B, role=UserRole.ADMIN)

        for path in ("/", "/active", "/by-work-center"):
            response = client.get(f"{DOWNTIME_URL}{path}", headers=headers)
            assert response.status_code == 200, path
            assert response.json() == [], path
        summary = client.get(f"{DOWNTIME_URL}/summary", headers=headers).json()
        assert summary["event_count"] == 0

    def test_a_legacy_mis_tenanted_event_does_not_render_the_foreign_machine(
        self, client: TestClient, db_session: Session
    ):
        # A row written before work_center_id was validated: owned by B, pointing at A's machine.
        foreign_wc = make_work_center(db_session, company_id=COMPANY_A, name=FOREIGN_WC_NAME)
        event = make_event(db_session, company_id=COMPANY_B, work_center_id=foreign_wc.id, resolved=True)
        headers = headers_for(db_session, company_id=COMPANY_B, role=UserRole.ADMIN)

        single = client.get(f"{DOWNTIME_URL}/{event.id}", headers=headers)
        listed = client.get(f"{DOWNTIME_URL}/", headers=headers)
        by_wc = client.get(f"{DOWNTIME_URL}/by-work-center", headers=headers)

        assert single.status_code == 200, single.text
        assert single.json()["work_center"] is None
        assert FOREIGN_WC_NAME not in single.text
        assert FOREIGN_WC_NAME not in listed.text
        assert FOREIGN_WC_NAME not in by_wc.text


# ---------------------------------------------------------------------------
# 3. RBAC: the floor keeps logging downtime; Viewer cannot; corrections are supervisory
# ---------------------------------------------------------------------------


class TestDowntimeRbac:
    def test_viewer_is_refused_on_every_write_with_no_side_effects(self, client: TestClient, db_session: Session):
        work_center = make_work_center(db_session)
        event = make_event(db_session, work_center_id=work_center.id)
        headers = headers_for(db_session, role=UserRole.VIEWER)
        events_before = event_count(db_session)
        codes_before = db_session.query(DowntimeReasonCode).count()
        audit_before = audit_count(db_session)

        statuses = [
            client.post(f"{DOWNTIME_URL}/", json={"work_center_id": work_center.id}, headers=headers).status_code,
            client.put(f"{DOWNTIME_URL}/{event.id}", json={"description": "x"}, headers=headers).status_code,
            client.post(f"{DOWNTIME_URL}/{event.id}/resolve", json={}, headers=headers).status_code,
            client.post(
                f"{DOWNTIME_URL}/reason-codes",
                json={"code": "V-1", "name": "x", "category": "other"},
                headers=headers,
            ).status_code,
        ]

        assert statuses == [403] * 4
        assert event_count(db_session) == events_before
        assert db_session.query(DowntimeReasonCode).count() == codes_before
        assert audit_count(db_session) == audit_before
        db_session.refresh(event)
        assert (event.description, event.end_time) == ("Spindle alarm", None)

    def test_operator_keeps_reporting_and_resolving_but_cannot_correct(self, client: TestClient, db_session: Session):
        work_center = make_work_center(db_session)
        headers = headers_for(db_session, role=UserRole.OPERATOR)

        created = client.post(f"{DOWNTIME_URL}/", json={"work_center_id": work_center.id}, headers=headers)
        assert created.status_code == 200, created.text
        event_id = created.json()["id"]
        assert client.put(f"{DOWNTIME_URL}/{event_id}", json={"description": "x"}, headers=headers).status_code == 403
        assert client.post(f"{DOWNTIME_URL}/{event_id}/resolve", json={}, headers=headers).status_code == 200
        assert (
            client.post(
                f"{DOWNTIME_URL}/reason-codes", json={"code": "OP-1", "name": "x", "category": "other"}, headers=headers
            ).status_code
            == 403
        )

    def test_reads_stay_open_to_a_viewer(self, client: TestClient, db_session: Session):
        headers = headers_for(db_session, role=UserRole.VIEWER)
        assert client.get(f"{DOWNTIME_URL}/", headers=headers).status_code == 200
        assert client.get(f"{DOWNTIME_URL}/summary", headers=headers).status_code == 200


# ---------------------------------------------------------------------------
# 4. Gaps the first pass left open (test-engineer review)
# ---------------------------------------------------------------------------


def make_reason_code(
    db: Session, *, company_id: int = COMPANY_A, code: str, name: str = "Spindle"
) -> DowntimeReasonCode:
    ensure_company(db, company_id)
    reason = DowntimeReasonCode(code=code, name=name, category=DowntimeCategory.MECHANICAL, company_id=company_id)
    db.add(reason)
    db.commit()
    db.refresh(reason)
    return reason


def reason_code_audit_count(db: Session) -> int:
    return db.query(AuditLog).filter(AuditLog.resource_type == "downtime_reason_code").count()


class TestDowntimeWorkOrderReferences:
    """The work-order half of ``_assert_event_refs_in_company``.

    Create already validated a foreign work order before this change; PUT did not, and
    neither refused a soft-deleted job. Only the work-center half had a PUT test, so
    the work-order branch and the ``is_deleted`` filter could be deleted with the suite green.
    """

    def test_put_cannot_repoint_an_event_at_another_tenants_work_order(self, client: TestClient, db_session: Session):
        own_wc = make_work_center(db_session, company_id=COMPANY_B)
        foreign_wo, _ = make_wo_with_operation(
            db_session, company_id=COMPANY_A, work_center=make_work_center(db_session, company_id=COMPANY_A)
        )
        event = make_event(db_session, company_id=COMPANY_B, work_center_id=own_wc.id)
        headers = headers_for(db_session, company_id=COMPANY_B, role=UserRole.ADMIN)
        audit_before = audit_count(db_session)

        response = client.put(f"{DOWNTIME_URL}/{event.id}", json={"work_order_id": foreign_wo.id}, headers=headers)

        assert response.status_code == 404, response.text
        assert response.json()["detail"] == "Work order not found"
        assert audit_count(db_session) == audit_before
        db_session.refresh(event)
        assert event.work_order_id is None

    def _soft_deleted_own_work_order(self, db: Session, work_center):
        work_order, _ = make_wo_with_operation(db, company_id=COMPANY_A, work_center=work_center)
        work_order.soft_delete(make_user(db, role=UserRole.ADMIN).id)
        db.commit()
        return work_order

    def test_create_against_an_own_soft_deleted_work_order_is_refused(self, client: TestClient, db_session: Session):
        work_center = make_work_center(db_session)
        deleted_wo = self._soft_deleted_own_work_order(db_session, work_center)
        headers = headers_for(db_session)
        events_before, audit_before = event_count(db_session), audit_count(db_session)

        response = client.post(
            f"{DOWNTIME_URL}/",
            json={"work_center_id": work_center.id, "work_order_id": deleted_wo.id},
            headers=headers,
        )

        assert response.status_code == 404, response.text
        assert event_count(db_session) == events_before
        assert audit_count(db_session) == audit_before

    def test_put_cannot_link_an_own_soft_deleted_work_order(self, client: TestClient, db_session: Session):
        work_center = make_work_center(db_session)
        deleted_wo = self._soft_deleted_own_work_order(db_session, work_center)
        event = make_event(db_session, work_center_id=work_center.id)
        headers = headers_for(db_session, role=UserRole.SUPERVISOR)
        audit_before = audit_count(db_session)

        response = client.put(f"{DOWNTIME_URL}/{event.id}", json={"work_order_id": deleted_wo.id}, headers=headers)

        assert response.status_code == 404, response.text
        assert audit_count(db_session) == audit_before
        db_session.refresh(event)
        assert event.work_order_id is None

    def test_an_own_live_work_order_is_accepted_and_rendered(self, client: TestClient, db_session: Session):
        # Positive control for the refusals above.
        work_center = make_work_center(db_session)
        work_order, _ = make_wo_with_operation(db_session, work_center=work_center)
        response = client.post(
            f"{DOWNTIME_URL}/",
            json={"work_center_id": work_center.id, "work_order_id": work_order.id},
            headers=headers_for(db_session),
        )
        assert response.status_code == 200, response.text
        assert response.json()["work_order"]["id"] == work_order.id


class TestReasonCodeIntegrity:
    def test_another_tenants_reason_code_is_404_on_update_with_no_side_effects(
        self, client: TestClient, db_session: Session
    ):
        foreign = make_reason_code(db_session, company_id=COMPANY_A, code="A-SECRET-01")
        headers = headers_for(db_session, company_id=COMPANY_B, role=UserRole.ADMIN)
        audit_before = reason_code_audit_count(db_session)

        response = client.put(f"{DOWNTIME_URL}/reason-codes/{foreign.id}", json={"name": "pwned"}, headers=headers)

        assert response.status_code == 404
        assert reason_code_audit_count(db_session) == audit_before
        db_session.refresh(foreign)
        assert foreign.name == "Spindle"

    def test_reason_code_list_is_scoped(self, client: TestClient, db_session: Session):
        make_reason_code(db_session, company_id=COMPANY_A, code="A-SECRET-02")
        headers = headers_for(db_session, company_id=COMPANY_B, role=UserRole.ADMIN)
        listed = client.get(f"{DOWNTIME_URL}/reason-codes", headers=headers)
        assert listed.status_code == 200, listed.text
        assert listed.json() == []

    def test_renaming_onto_an_existing_code_is_400_and_changes_nothing(self, client: TestClient, db_session: Session):
        make_reason_code(db_session, code="ELEC-01")
        target = make_reason_code(db_session, code="MECH-02")
        headers = headers_for(db_session, role=UserRole.MANAGER)
        audit_before = reason_code_audit_count(db_session)

        response = client.put(
            f"{DOWNTIME_URL}/reason-codes/{target.id}", json={"code": "ELEC-01", "name": "Renamed"}, headers=headers
        )

        assert response.status_code == 400, response.text
        assert response.json()["detail"] == "Reason code already exists"
        assert reason_code_audit_count(db_session) == audit_before
        db_session.refresh(target)
        assert (target.code, target.name) == ("MECH-02", "Spindle")

    def test_a_code_another_tenant_already_uses_is_400_not_500(self, client: TestClient, db_session: Session):
        # ``downtime_reason_codes.code`` is still GLOBALLY unique, so the tenant-scoped probe
        # misses it and the flush is what refuses.
        make_reason_code(db_session, company_id=COMPANY_A, code="SHARED-01")
        headers = headers_for(db_session, company_id=COMPANY_B, role=UserRole.MANAGER)
        audit_before = reason_code_audit_count(db_session)

        response = client.post(
            f"{DOWNTIME_URL}/reason-codes",
            json={"code": "SHARED-01", "name": "Mine", "category": "mechanical"},
            headers=headers,
        )

        assert response.status_code == 400, response.text
        assert db_session.query(DowntimeReasonCode).filter(DowntimeReasonCode.company_id == COMPANY_B).count() == 0
        assert reason_code_audit_count(db_session) == audit_before

    @pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.OPERATOR, UserRole.QUALITY, UserRole.SHIPPING])
    def test_non_manage_roles_cannot_edit_a_reason_code(self, client: TestClient, db_session: Session, role: UserRole):
        reason = make_reason_code(db_session, code=f"RBAC-{role.value}")
        headers = headers_for(db_session, role=role)
        audit_before = reason_code_audit_count(db_session)

        response = client.put(
            f"{DOWNTIME_URL}/reason-codes/{reason.id}", json={"name": "x", "is_active": False}, headers=headers
        )

        assert response.status_code == 403
        assert reason_code_audit_count(db_session) == audit_before
        db_session.refresh(reason)
        assert (reason.name, reason.is_active) == ("Spindle", True)

    @pytest.mark.parametrize("role", [UserRole.QUALITY, UserRole.SHIPPING])
    def test_non_manage_roles_cannot_correct_an_event(self, client: TestClient, db_session: Session, role: UserRole):
        work_center = make_work_center(db_session)
        event = make_event(db_session, work_center_id=work_center.id)
        audit_before = audit_count(db_session)

        response = client.put(
            f"{DOWNTIME_URL}/{event.id}", json={"description": "x"}, headers=headers_for(db_session, role=role)
        )

        assert response.status_code == 403
        assert audit_count(db_session) == audit_before
        db_session.refresh(event)
        assert event.description == "Spindle alarm"
