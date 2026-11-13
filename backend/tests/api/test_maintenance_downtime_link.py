"""Maintenance work order -> DowntimeEvent (P2.3, owner decision 2026-11-13).

Only a WORK-CENTER-targeted maintenance work order with ``requires_shutdown`` records
downtime. Start opens an event on that machine -- preventive / predictive as PLANNED
maintenance (outside OEE availability loss), corrective / emergency as MECHANICAL, UNPLANNED
-- linked by ``downtime_events.maintenance_work_order_id``; or ADOPTS an open event an
operator already logged on that machine, so the minutes are never doubled. Complete and
cancel resolve it; complete fills ``downtime_minutes`` from it when the request left 0.
An equipment-asset work order never touches downtime.

The assertions read the STORED events, not only status codes.
"""

from datetime import datetime, timedelta

import pytest
from fastapi import status
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.downtime import DowntimeCategory, DowntimeEvent, DowntimePlannedType
from app.models.maintenance import MaintenanceStatus, MaintenanceWorkOrder
from app.models.operational_event import OperationalEvent
from app.models.user import UserRole
from app.services.oee_service import work_center_has_activity
from tests.api.test_maintenance_assets import make_asset, make_wc
from tests.api.test_receiving_compliance import headers_for, make_user

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

WORK_ORDERS = "/api/v1/maintenance/work-orders"


@pytest.fixture
def supervisor(db_session):
    return make_user(db_session, role=UserRole.SUPERVISOR, company_id=1)


@pytest.fixture
def headers(supervisor):
    return headers_for(supervisor)


def create_wo(client, headers, **body):
    payload = {"title": "Synthetic PM", "requires_shutdown": True, **body}
    resp = client.post(WORK_ORDERS, headers=headers, json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def events_for(db: Session, **filters):
    db.expire_all()
    query = db.query(DowntimeEvent)
    for name, value in filters.items():
        query = query.filter(getattr(DowntimeEvent, name) == value)
    return query.order_by(DowntimeEvent.id).all()


def backdate(db: Session, wo_id: int, minutes: float) -> None:
    """Move the WO's start and its linked event's start into the past so durations are real."""
    db.expire_all()
    wo = db.get(MaintenanceWorkOrder, wo_id)
    wo.started_at = wo.started_at - timedelta(minutes=minutes)
    for event in db.query(DowntimeEvent).filter(DowntimeEvent.maintenance_work_order_id == wo_id):
        event.start_time = event.start_time - timedelta(minutes=minutes)
    db.commit()


class TestStartOpensDowntime:
    def test_preventive_start_opens_a_planned_maintenance_event_and_complete_resolves_it(
        self, client, db_session, headers, supervisor
    ):
        wc = make_wc(db_session)
        wo = create_wo(client, headers, work_center_id=wc.id, maintenance_type="preventive")
        assert client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=headers).status_code == 200

        [event] = events_for(db_session, maintenance_work_order_id=wo["id"])
        assert event.work_center_id == wc.id and event.company_id == 1
        assert event.category == DowntimeCategory.PLANNED_MAINTENANCE
        assert event.planned_type == DowntimePlannedType.PLANNED
        assert event.end_time is None and event.reported_by == supervisor.id
        assert wo["wo_number"] in event.description
        active = client.get("/api/v1/downtime/active", headers=headers).json()
        assert [(row["id"], row["maintenance_work_order_id"]) for row in active] == [(event.id, wo["id"])]
        assert (
            db_session.query(OperationalEvent)
            .filter(OperationalEvent.event_type == "downtime_started", OperationalEvent.entity_id == event.id)
            .count()
            == 1
        )

        backdate(db_session, wo["id"], 90)
        done = client.post(f"{WORK_ORDERS}/{wo['id']}/complete", headers=headers, json={})
        assert done.status_code == 200, done.text
        [event] = events_for(db_session, maintenance_work_order_id=wo["id"])
        assert event.end_time is not None and event.resolved_by == supervisor.id
        assert 89 <= event.duration_minutes <= 91
        assert done.json()["downtime_minutes"] == event.duration_minutes  # filled because the request sent 0
        assert client.get("/api/v1/downtime/active", headers=headers).json() == []
        statuses = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "downtime_event", AuditLog.resource_id == event.id)
            .order_by(AuditLog.id)
            .all()
        )
        assert [row.action for row in statuses] == ["CREATE", "STATUS_CHANGE"]

    def test_explicit_downtime_minutes_are_kept(self, client, db_session, headers):
        wc = make_wc(db_session)
        wo = create_wo(client, headers, work_center_id=wc.id)
        client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=headers)
        done = client.post(f"{WORK_ORDERS}/{wo['id']}/complete", headers=headers, json={"downtime_minutes": 42})
        assert done.json()["downtime_minutes"] == 42
        [event] = events_for(db_session, maintenance_work_order_id=wo["id"])
        assert event.end_time is not None

    @pytest.mark.parametrize("kind", ["corrective", "emergency"])
    def test_breakdowns_are_mechanical_unplanned(self, client, db_session, headers, kind):
        wc = make_wc(db_session)
        wo = create_wo(client, headers, work_center_id=wc.id, maintenance_type=kind)
        client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=headers)
        [event] = events_for(db_session, maintenance_work_order_id=wo["id"])
        assert (event.category, event.planned_type) == (DowntimeCategory.MECHANICAL, DowntimePlannedType.UNPLANNED)

    def test_predictive_is_planned(self, client, db_session, headers):
        wc = make_wc(db_session)
        wo = create_wo(client, headers, work_center_id=wc.id, maintenance_type="predictive")
        client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=headers)
        [event] = events_for(db_session, maintenance_work_order_id=wo["id"])
        assert event.planned_type == DowntimePlannedType.PLANNED

    def test_no_shutdown_and_asset_work_orders_record_no_downtime(self, client, db_session, headers):
        wc = make_wc(db_session)
        asset = make_asset(db_session)
        no_shutdown = create_wo(client, headers, work_center_id=wc.id, requires_shutdown=False)
        on_asset = create_wo(client, headers, asset_id=asset.id, requires_shutdown=True, maintenance_type="emergency")
        for wo in (no_shutdown, on_asset):
            assert client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=headers).status_code == 200
            assert client.post(f"{WORK_ORDERS}/{wo['id']}/complete", headers=headers, json={}).status_code == 200
        assert events_for(db_session) == []

    def test_an_operator_can_start_and_complete(self, client, db_session, headers):
        wc = make_wc(db_session)
        wo = create_wo(client, headers, work_center_id=wc.id)
        operator_headers = headers_for(make_user(db_session, role=UserRole.OPERATOR, company_id=1))
        assert client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=operator_headers).status_code == 200
        assert client.post(f"{WORK_ORDERS}/{wo['id']}/complete", headers=operator_headers, json={}).status_code == 200
        [event] = events_for(db_session, maintenance_work_order_id=wo["id"])
        assert event.end_time is not None


class TestNoDoubleCounting:
    def test_an_operators_open_event_is_adopted_not_doubled(self, client, db_session, headers):
        wc = make_wc(db_session)
        logged = client.post(
            "/api/v1/downtime/",
            headers=headers,
            json={"work_center_id": wc.id, "category": "mechanical", "description": "Spindle noise"},
        )
        assert logged.status_code == 200, logged.text
        wo = create_wo(client, headers, work_center_id=wc.id, maintenance_type="corrective")
        client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=headers)

        [event] = events_for(db_session, work_center_id=wc.id)
        assert event.id == logged.json()["id"] and event.maintenance_work_order_id == wo["id"]
        assert event.description == "Spindle noise"  # the operator's record is kept as logged
        link = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.resource_type == "downtime_event",
                AuditLog.resource_id == event.id,
                AuditLog.action == "UPDATE",
            )
            .one()
        )
        assert link.new_values == {"maintenance_work_order_id": wo["id"]}

        client.post(f"{WORK_ORDERS}/{wo['id']}/complete", headers=headers, json={})
        [event] = events_for(db_session, work_center_id=wc.id)
        assert event.end_time is not None

    def test_an_event_another_work_order_owns_is_not_adopted(self, client, db_session, headers):
        wc = make_wc(db_session)
        first = create_wo(client, headers, work_center_id=wc.id)
        second = create_wo(client, headers, work_center_id=wc.id)
        client.post(f"{WORK_ORDERS}/{first['id']}/start", headers=headers)
        client.post(f"{WORK_ORDERS}/{second['id']}/start", headers=headers)
        owners = sorted(event.maintenance_work_order_id for event in events_for(db_session, work_center_id=wc.id))
        assert owners == sorted([first["id"], second["id"]])

    def test_resuming_from_hold_opens_no_second_event(self, client, db_session, headers):
        wc = make_wc(db_session)
        wo = create_wo(client, headers, work_center_id=wc.id)
        client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=headers)
        assert client.put(f"{WORK_ORDERS}/{wo['id']}", headers=headers, json={"status": "on_hold"}).status_code == 200
        assert client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=headers).status_code == 200
        assert len(events_for(db_session, maintenance_work_order_id=wo["id"])) == 1

    def test_a_mis_tenanted_legacy_event_is_never_adopted(self, client, db_session, headers):
        wc = make_wc(db_session)
        other_reporter = make_user(db_session, role=UserRole.OPERATOR, company_id=2)
        legacy = DowntimeEvent(
            company_id=2,
            work_center_id=wc.id,  # points at company 1's machine: a pre-guard legacy row
            start_time=datetime.utcnow(),
            category=DowntimeCategory.MECHANICAL,
            planned_type=DowntimePlannedType.UNPLANNED,
            reported_by=other_reporter.id,
        )
        db_session.add(legacy)
        db_session.commit()
        wo = create_wo(client, headers, work_center_id=wc.id, maintenance_type="corrective")
        client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=headers)
        db_session.expire_all()
        assert db_session.get(DowntimeEvent, legacy.id).maintenance_work_order_id is None
        [own] = events_for(db_session, maintenance_work_order_id=wo["id"])
        assert own.company_id == 1 and own.id != legacy.id


class TestEndingWithoutComplete:
    @pytest.mark.parametrize("final", ["cancelled", "completed"])
    def test_cancel_or_close_via_put_resolves_the_event(self, client, db_session, headers, final):
        wc = make_wc(db_session)
        wo = create_wo(client, headers, work_center_id=wc.id)
        client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=headers)
        resp = client.put(f"{WORK_ORDERS}/{wo['id']}", headers=headers, json={"status": final})
        assert resp.status_code == status.HTTP_200_OK, resp.text
        [event] = events_for(db_session, maintenance_work_order_id=wo["id"])
        assert event.end_time is not None and f"set to {final}" in event.resolution
        db_session.expire_all()
        assert db_session.get(MaintenanceWorkOrder, wo["id"]).status == MaintenanceStatus(final)

    def test_a_manually_resolved_event_is_not_resolved_twice(self, client, db_session, headers):
        wc = make_wc(db_session)
        wo = create_wo(client, headers, work_center_id=wc.id)
        client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=headers)
        backdate(db_session, wo["id"], 30)
        [event] = events_for(db_session, maintenance_work_order_id=wo["id"])
        assert client.post(f"/api/v1/downtime/{event.id}/resolve", headers=headers, json={}).status_code == 200
        done = client.post(f"{WORK_ORDERS}/{wo['id']}/complete", headers=headers, json={})
        assert done.status_code == 200
        assert 29 <= done.json()["downtime_minutes"] <= 31  # from the resolved linked event
        resolves = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "downtime_event", AuditLog.action == "STATUS_CHANGE")
            .count()
        )
        assert resolves == 1


class TestOeeTreatment:
    def test_planned_pm_downtime_is_not_oee_activity_but_a_breakdown_is(self, client, db_session, headers):
        pm_wc = make_wc(db_session)
        broken_wc = make_wc(db_session)
        pm = create_wo(client, headers, work_center_id=pm_wc.id, maintenance_type="preventive")
        breakdown = create_wo(client, headers, work_center_id=broken_wc.id, maintenance_type="corrective")
        for wo in (pm, breakdown):
            client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=headers)
        today = datetime.utcnow().date()
        assert work_center_has_activity(db_session, 1, pm_wc.id, today) is False
        assert work_center_has_activity(db_session, 1, broken_wc.id, today) is True
        summary_dates = {"date_from": (today - timedelta(days=1)).isoformat(), "date_to": today.isoformat()}
        for wo in (pm, breakdown):
            backdate(db_session, wo["id"], 60)
            client.post(f"{WORK_ORDERS}/{wo['id']}/complete", headers=headers, json={})
        summary = client.get("/api/v1/downtime/summary", headers=headers, params=summary_dates).json()
        assert summary["planned_hours"] == pytest.approx(1.0, abs=0.05)
        assert summary["unplanned_hours"] == pytest.approx(1.0, abs=0.05)
