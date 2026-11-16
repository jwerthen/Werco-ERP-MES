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
from app.services.production_calendar import current_production_day
from app.services.production_calendar_service import load_production_calendar
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
        assert link.new_values == {"maintenance_work_order_id": wo["id"], "maintenance_adopted": True}
        assert event.maintenance_adopted is True

        client.post(f"{WORK_ORDERS}/{wo['id']}/complete", headers=headers, json={})
        [event] = events_for(db_session, work_center_id=wc.id)
        assert event.end_time is not None

    def test_a_second_shutdown_work_order_shares_the_open_event_and_inherits_it(self, client, db_session, headers):
        """Two shutdown WOs on one machine (a monthly and an annual PM started together, or a
        corrective WO started during a PM) are ONE stoppage. The second used to open a second
        open event, and every downtime total sums ``duration_minutes`` without merging overlaps,
        so the minutes counted twice. Now it shares the first WO's event; when the first ends the
        event is handed over, and it resolves once, when the LAST work order ends."""
        wc = make_wc(db_session)
        first = create_wo(client, headers, work_center_id=wc.id, maintenance_type="corrective")
        second = create_wo(client, headers, work_center_id=wc.id, maintenance_type="corrective")
        assert client.post(f"{WORK_ORDERS}/{first['id']}/start", headers=headers).status_code == 200
        assert client.post(f"{WORK_ORDERS}/{second['id']}/start", headers=headers).status_code == 200

        [event] = events_for(db_session, work_center_id=wc.id)
        assert event.maintenance_work_order_id == first["id"] and event.end_time is None
        start_row = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.resource_type == "maintenance_work_order",
                AuditLog.resource_id == second["id"],
                AuditLog.action == "STATUS_CHANGE",
            )
            .one()
        )
        assert start_row.extra_data == {
            "downtime_event_id": event.id,
            "downtime_link": "shared",
            "downtime_shared_with": first["wo_number"],
        }

        backdate(db_session, first["id"], 60)
        first_done = client.post(f"{WORK_ORDERS}/{first['id']}/complete", headers=headers, json={})
        assert first_done.status_code == 200, first_done.text
        assert first_done.json()["downtime_minutes"] == 0  # the stoppage runs on under the second WO
        [event] = events_for(db_session, work_center_id=wc.id)
        assert event.end_time is None and event.maintenance_work_order_id == second["id"]
        assert [row["id"] for row in client.get("/api/v1/downtime/active", headers=headers).json()] == [event.id]

        second_done = client.post(f"{WORK_ORDERS}/{second['id']}/complete", headers=headers, json={})
        assert second_done.status_code == 200, second_done.text
        [event] = events_for(db_session, work_center_id=wc.id)
        assert event.end_time is not None and 59 <= event.duration_minutes <= 61
        assert second_done.json()["downtime_minutes"] == event.duration_minutes
        today = datetime.utcnow().date()
        summary = client.get(
            "/api/v1/downtime/summary",
            headers=headers,
            params={"date_from": (today - timedelta(days=1)).isoformat(), "date_to": today.isoformat()},
        ).json()
        assert summary["unplanned_hours"] == pytest.approx(1.0, abs=0.05)  # once, not twice

    def test_cancelling_the_first_work_order_hands_the_event_to_the_one_still_working(
        self, client, db_session, headers
    ):
        wc = make_wc(db_session)
        first = create_wo(client, headers, work_center_id=wc.id)
        second = create_wo(client, headers, work_center_id=wc.id)
        client.post(f"{WORK_ORDERS}/{first['id']}/start", headers=headers)
        client.post(f"{WORK_ORDERS}/{second['id']}/start", headers=headers)
        assert (
            client.put(f"{WORK_ORDERS}/{first['id']}", headers=headers, json={"status": "cancelled"}).status_code == 200
        )
        [event] = events_for(db_session, work_center_id=wc.id)
        assert event.end_time is None and event.maintenance_work_order_id == second["id"]
        assert (
            client.put(f"{WORK_ORDERS}/{second['id']}", headers=headers, json={"status": "cancelled"}).status_code
            == 200
        )
        [event] = events_for(db_session, work_center_id=wc.id)
        assert event.end_time is not None

    def test_cancelling_the_adopter_hands_the_breakdown_to_the_work_order_still_working(
        self, client, db_session, headers
    ):
        """An operator logged the breakdown; corrective WO A adopted it and WO B shared it. A is
        cancelled as the duplicate. B still has the machine shut down, so the event goes to B --
        not back to the machine, where nobody would own it and B's completion of the repair would
        leave it open for good. It keeps its adopted mark, so B's cancel still hands it back."""
        wc = make_wc(db_session)
        logged = client.post(
            "/api/v1/downtime/",
            headers=headers,
            json={"work_center_id": wc.id, "category": "mechanical", "description": "Spindle seized"},
        ).json()
        adopter = create_wo(client, headers, work_center_id=wc.id, maintenance_type="corrective")
        sharer = create_wo(client, headers, work_center_id=wc.id, maintenance_type="corrective")
        client.post(f"{WORK_ORDERS}/{adopter['id']}/start", headers=headers)
        client.post(f"{WORK_ORDERS}/{sharer['id']}/start", headers=headers)
        [event] = events_for(db_session, work_center_id=wc.id)
        assert event.id == logged["id"] and event.maintenance_work_order_id == adopter["id"]

        cancelled = client.put(f"{WORK_ORDERS}/{adopter['id']}", headers=headers, json={"status": "cancelled"})
        assert cancelled.status_code == 200, cancelled.text
        [event] = events_for(db_session, work_center_id=wc.id)
        assert event.end_time is None and event.maintenance_work_order_id == sharer["id"]
        assert event.maintenance_adopted is True

        backdate(db_session, sharer["id"], 45)
        done = client.post(f"{WORK_ORDERS}/{sharer['id']}/complete", headers=headers, json={})
        assert done.status_code == 200, done.text
        [event] = events_for(db_session, work_center_id=wc.id)
        assert event.end_time is not None and event.resolved_by is not None
        assert done.json()["downtime_minutes"] == event.duration_minutes > 0
        assert client.get("/api/v1/downtime/active", headers=headers).json() == []

    def test_cancelling_both_hands_the_shared_breakdown_back_to_the_machine(self, client, db_session, headers):
        wc = make_wc(db_session)
        logged = client.post(
            "/api/v1/downtime/", headers=headers, json={"work_center_id": wc.id, "category": "mechanical"}
        ).json()
        adopter = create_wo(client, headers, work_center_id=wc.id, maintenance_type="corrective")
        sharer = create_wo(client, headers, work_center_id=wc.id, maintenance_type="corrective")
        client.post(f"{WORK_ORDERS}/{adopter['id']}/start", headers=headers)
        client.post(f"{WORK_ORDERS}/{sharer['id']}/start", headers=headers)
        for wo in (adopter, sharer):
            assert (
                client.put(f"{WORK_ORDERS}/{wo['id']}", headers=headers, json={"status": "cancelled"}).status_code
                == 200
            )
        [event] = events_for(db_session, work_center_id=wc.id)
        # Nobody repaired it: still open, owned by no work order, ready for the next one to adopt.
        assert event.id == logged["id"] and event.end_time is None
        assert event.maintenance_work_order_id is None and event.maintenance_adopted is False

    def test_an_operator_report_on_a_machine_maintenance_holds_down_is_refused(self, client, db_session, headers):
        """The other direction of "never two open events for one stoppage": a preventive WO
        started (its planned-maintenance event is open), then an operator logs the same PM stop.
        Two overlapping open events would count it twice in every downtime total."""
        wc = make_wc(db_session)
        operator = make_user(db_session, role=UserRole.OPERATOR, company_id=1)
        wo = create_wo(client, headers, work_center_id=wc.id, maintenance_type="preventive")
        client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=headers)
        [held] = events_for(db_session, work_center_id=wc.id)
        report = {"work_center_id": wc.id, "category": "planned_maintenance", "planned_type": "planned"}

        refused = client.post("/api/v1/downtime/", headers=headers_for(operator), json=report)
        assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
        assert wo["wo_number"] in refused.json()["detail"] and f"DT-{held.id}" in refused.json()["detail"]
        assert len(events_for(db_session, work_center_id=wc.id)) == 1

        # On hold still holds the machine down.
        client.put(f"{WORK_ORDERS}/{wo['id']}", headers=headers, json={"status": "on_hold"})
        assert client.post("/api/v1/downtime/", headers=headers_for(operator), json=report).status_code == 409

        # Positive controls: another machine takes a report, and so does this one once the WO ends.
        other = client.post(
            "/api/v1/downtime/", headers=headers_for(operator), json={"work_center_id": make_wc(db_session).id}
        )
        assert other.status_code == 200, other.text
        client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=headers)
        client.post(f"{WORK_ORDERS}/{wo['id']}/complete", headers=headers, json={})
        after = client.post("/api/v1/downtime/", headers=headers_for(operator), json=report)
        assert after.status_code == 200, after.text

    def test_a_held_sharer_still_inherits_and_a_later_start_after_the_end_opens_fresh(
        self, client, db_session, headers
    ):
        wc = make_wc(db_session)
        first = create_wo(client, headers, work_center_id=wc.id)
        second = create_wo(client, headers, work_center_id=wc.id)
        client.post(f"{WORK_ORDERS}/{first['id']}/start", headers=headers)
        client.post(f"{WORK_ORDERS}/{second['id']}/start", headers=headers)
        assert (
            client.put(f"{WORK_ORDERS}/{second['id']}", headers=headers, json={"status": "on_hold"}).status_code == 200
        )
        client.post(f"{WORK_ORDERS}/{first['id']}/complete", headers=headers, json={})
        [event] = events_for(db_session, work_center_id=wc.id)
        assert event.end_time is None and event.maintenance_work_order_id == second["id"]
        resumed = client.post(f"{WORK_ORDERS}/{second['id']}/start", headers=headers)
        assert resumed.status_code == 200 and len(events_for(db_session, work_center_id=wc.id)) == 1
        assert client.post(f"{WORK_ORDERS}/{second['id']}/complete", headers=headers, json={}).status_code == 200
        third = create_wo(client, headers, work_center_id=wc.id)
        client.post(f"{WORK_ORDERS}/{third['id']}/start", headers=headers)
        events = events_for(db_session, work_center_id=wc.id)
        assert len(events) == 2 and [e.end_time is None for e in events] == [False, True]
        assert events[1].maintenance_work_order_id == third["id"]

    def test_resuming_from_hold_opens_no_second_event(self, client, db_session, headers):
        wc = make_wc(db_session)
        wo = create_wo(client, headers, work_center_id=wc.id)
        client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=headers)
        assert client.put(f"{WORK_ORDERS}/{wo['id']}", headers=headers, json={"status": "on_hold"}).status_code == 200
        assert client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=headers).status_code == 200
        assert len(events_for(db_session, maintenance_work_order_id=wo["id"])) == 1

    def test_a_mis_tenanted_legacy_work_order_opens_no_event_on_the_foreign_machine(self, client, db_session):
        """A legacy company-2 maintenance WO pointing at company 1's machine (written before the
        maintenance writes checked whose machine it was) must not open a company-2 event there:
        the downtime reads would render company 1's machine name to company 2."""
        foreign_wc = make_wc(db_session, company_id=1)
        tech = make_user(db_session, role=UserRole.SUPERVISOR, company_id=2)
        legacy = MaintenanceWorkOrder(
            company_id=2,
            work_center_id=foreign_wc.id,
            wo_number="MWO-LEGACY-X",
            title="Legacy mis-tenanted",
            status=MaintenanceStatus.SCHEDULED,
            requires_shutdown=True,
        )
        db_session.add(legacy)
        db_session.commit()
        resp = client.post(f"{WORK_ORDERS}/{legacy.id}/start", headers=headers_for(tech))
        assert resp.status_code == 200, resp.text
        assert events_for(db_session) == []

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
    def test_cancel_via_put_resolves_the_event_it_opened(self, client, db_session, headers):
        wc = make_wc(db_session)
        wo = create_wo(client, headers, work_center_id=wc.id)
        client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=headers)
        resp = client.put(f"{WORK_ORDERS}/{wo['id']}", headers=headers, json={"status": "cancelled"})
        assert resp.status_code == status.HTTP_200_OK, resp.text
        [event] = events_for(db_session, maintenance_work_order_id=wo["id"])
        assert event.end_time is not None and "set to cancelled" in event.resolution
        db_session.expire_all()
        assert db_session.get(MaintenanceWorkOrder, wo["id"]).status == MaintenanceStatus.CANCELLED

    def test_cancelling_hands_an_adopted_breakdown_back_to_the_machine_still_open(self, client, db_session, headers):
        """An operator logged the breakdown; a corrective WO adopted it; the WO is cancelled (to
        be redone differently). The machine is still broken, so the event must stay OPEN --
        unlinked, for the next work order to adopt -- not be resolved as if it were fixed."""
        wc = make_wc(db_session)
        logged = client.post(
            "/api/v1/downtime/",
            headers=headers,
            json={"work_center_id": wc.id, "category": "mechanical", "description": "Spindle seized"},
        ).json()
        wo = create_wo(client, headers, work_center_id=wc.id, maintenance_type="corrective")
        client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=headers)
        assert client.put(f"{WORK_ORDERS}/{wo['id']}", headers=headers, json={"status": "cancelled"}).status_code == 200

        [event] = events_for(db_session, work_center_id=wc.id)
        assert event.id == logged["id"] and event.end_time is None and event.resolved_by is None
        assert event.maintenance_work_order_id is None and event.maintenance_adopted is False
        handback = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.resource_type == "downtime_event",
                AuditLog.resource_id == event.id,
                AuditLog.action == "UPDATE",
            )
            .order_by(AuditLog.id)
            .all()[-1]
        )
        assert handback.new_values == {"maintenance_work_order_id": None, "maintenance_adopted": False}

        redo = create_wo(client, headers, work_center_id=wc.id, maintenance_type="corrective")
        client.post(f"{WORK_ORDERS}/{redo['id']}/start", headers=headers)
        [event] = events_for(db_session, work_center_id=wc.id)
        assert event.maintenance_work_order_id == redo["id"] and event.maintenance_adopted is True
        client.post(f"{WORK_ORDERS}/{redo['id']}/complete", headers=headers, json={})
        [event] = events_for(db_session, work_center_id=wc.id)
        assert event.end_time is not None  # completing the repair does resolve it

    def test_completing_resolves_an_adopted_breakdown(self, client, db_session, headers):
        wc = make_wc(db_session)
        client.post("/api/v1/downtime/", headers=headers, json={"work_center_id": wc.id, "category": "mechanical"})
        wo = create_wo(client, headers, work_center_id=wc.id, maintenance_type="corrective")
        client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=headers)
        client.post(f"{WORK_ORDERS}/{wo['id']}/complete", headers=headers, json={})
        [event] = events_for(db_session, work_center_id=wc.id)
        assert event.end_time is not None

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
        # The OEE activity window is the company's PRODUCTION day (P1.2), never the UTC date: a
        # stoppage opened at 23:50 Central sits on the Central day, which UTC has already left.
        today = current_production_day(load_production_calendar(db_session, 1))
        assert work_center_has_activity(db_session, 1, pm_wc.id, today) is False
        assert work_center_has_activity(db_session, 1, broken_wc.id, today) is True
        summary_dates = {"date_from": (today - timedelta(days=1)).isoformat(), "date_to": today.isoformat()}
        for wo in (pm, breakdown):
            backdate(db_session, wo["id"], 60)
            client.post(f"{WORK_ORDERS}/{wo['id']}/complete", headers=headers, json={})
        summary = client.get("/api/v1/downtime/summary", headers=headers, params=summary_dates).json()
        assert summary["planned_hours"] == pytest.approx(1.0, abs=0.05)
        assert summary["unplanned_hours"] == pytest.approx(1.0, abs=0.05)


class TestPutCannotBypassTheVerbs:
    """``PUT /maintenance/work-orders/{id}`` is also an MCP tool. Its blind setattr loop used to
    start a work order with no ``started_at`` and no downtime event, complete one without the
    PM schedule advancing, return a started one to scheduled with its event still open, and
    flip ``requires_shutdown`` under a running stoppage. Each is now a 409 that writes nothing."""

    def _refused(self, client, db_session, headers, wo_id, body):
        before = client.get(f"{WORK_ORDERS}/{wo_id}", headers=headers).json()
        resp = client.put(f"{WORK_ORDERS}/{wo_id}", headers=headers, json=body)
        assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
        after = client.get(f"{WORK_ORDERS}/{wo_id}", headers=headers).json()
        assert after["status"] == before["status"] and after["requires_shutdown"] == before["requires_shutdown"]
        return resp.json()["detail"]

    def test_in_progress_is_the_start_verbs(self, client, db_session, headers):
        wc = make_wc(db_session)
        wo = create_wo(client, headers, work_center_id=wc.id)
        detail = self._refused(client, db_session, headers, wo["id"], {"status": "in_progress"})
        assert "/start" in detail
        assert events_for(db_session) == []

    def test_completed_is_the_complete_verbs(self, client, db_session, headers):
        wc = make_wc(db_session)
        wo = create_wo(client, headers, work_center_id=wc.id)
        client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=headers)
        detail = self._refused(client, db_session, headers, wo["id"], {"status": "completed"})
        assert "/complete" in detail
        [event] = events_for(db_session, maintenance_work_order_id=wo["id"])
        assert event.end_time is None

    @pytest.mark.parametrize("back_to", ["scheduled", "overdue"])
    def test_a_started_work_order_cannot_be_unstarted(self, client, db_session, headers, back_to):
        wc = make_wc(db_session)
        wo = create_wo(client, headers, work_center_id=wc.id)
        client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=headers)
        self._refused(client, db_session, headers, wo["id"], {"status": back_to})
        client.put(f"{WORK_ORDERS}/{wo['id']}", headers=headers, json={"status": "on_hold"})
        self._refused(client, db_session, headers, wo["id"], {"status": back_to})

    def test_requires_shutdown_is_fixed_once_started(self, client, db_session, headers):
        wc = make_wc(db_session)
        no_shutdown = create_wo(client, headers, work_center_id=wc.id, requires_shutdown=False)
        client.post(f"{WORK_ORDERS}/{no_shutdown['id']}/start", headers=headers)
        self._refused(client, db_session, headers, no_shutdown["id"], {"requires_shutdown": True})
        shutdown = create_wo(client, headers, work_center_id=make_wc(db_session).id)
        client.post(f"{WORK_ORDERS}/{shutdown['id']}/start", headers=headers)
        self._refused(client, db_session, headers, shutdown["id"], {"requires_shutdown": False})
        assert len(events_for(db_session, end_time=None)) == 1

    @pytest.mark.parametrize(
        "end, to",
        [
            ("complete", "on_hold"),
            ("complete", "scheduled"),
            ("complete", "overdue"),
            ("complete", "cancelled"),
            ("cancel", "on_hold"),
            ("cancel", "scheduled"),
            ("cancel", "overdue"),
        ],
    )
    def test_a_completed_or_cancelled_work_order_is_final(self, client, db_session, headers, end, to):
        """Reopening one used to pass: a completed WO put on hold kept ``started_at``, looked like
        a WO still holding the machine down, and took over the next WO's downtime event, which
        then never closed; reopened to scheduled, a second ``/complete`` overwrote the findings,
        costs and completer and advanced the PM schedule again."""
        wc = make_wc(db_session)
        wo = create_wo(client, headers, work_center_id=wc.id)
        client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=headers)
        if end == "complete":
            done = client.post(f"{WORK_ORDERS}/{wo['id']}/complete", headers=headers, json={"findings": "orig"})
        else:
            done = client.put(f"{WORK_ORDERS}/{wo['id']}", headers=headers, json={"status": "cancelled"})
        assert done.status_code == 200, done.text
        before = client.get(f"{WORK_ORDERS}/{wo['id']}", headers=headers).json()
        detail = self._refused(client, db_session, headers, wo["id"], {"status": to})
        assert "final record" in detail and wo["wo_number"] in detail
        after = client.get(f"{WORK_ORDERS}/{wo['id']}", headers=headers).json()
        assert after == before
        # Re-sending the terminal status itself is an echo, not a change.
        echo = client.put(f"{WORK_ORDERS}/{wo['id']}", headers=headers, json={"status": before["status"]})
        assert echo.status_code == 200, echo.text

    def test_a_reopened_legacy_work_order_never_takes_over_a_machines_downtime(self, client, db_session, headers):
        """Backstop for rows reopened before the guard: a WO that was ever completed is never
        "still holding the machine down", whatever its status now says."""
        wc = make_wc(db_session)
        legacy = create_wo(client, headers, work_center_id=wc.id)
        client.post(f"{WORK_ORDERS}/{legacy['id']}/start", headers=headers)
        client.post(f"{WORK_ORDERS}/{legacy['id']}/complete", headers=headers, json={})
        db_session.expire_all()
        row = db_session.get(MaintenanceWorkOrder, legacy["id"])
        row.status = MaintenanceStatus.ON_HOLD  # what a pre-guard PUT left behind
        db_session.commit()

        current = create_wo(client, headers, work_center_id=wc.id)
        client.post(f"{WORK_ORDERS}/{current['id']}/start", headers=headers)
        [opened] = events_for(db_session, maintenance_work_order_id=current["id"])
        assert client.post(f"{WORK_ORDERS}/{current['id']}/complete", headers=headers, json={}).status_code == 200
        db_session.expire_all()
        event = db_session.get(DowntimeEvent, opened.id)
        assert event.end_time is not None and event.maintenance_work_order_id == current["id"]
        assert client.get("/api/v1/downtime/active", headers=headers).json() == []

    def test_echoes_holds_and_unstarted_edits_still_work(self, client, db_session, headers):
        wc = make_wc(db_session)
        wo = create_wo(client, headers, work_center_id=wc.id)
        # Not started: the shutdown flag and the planning statuses stay editable.
        for body in ({"requires_shutdown": False}, {"requires_shutdown": True}, {"status": "on_hold"}):
            assert client.put(f"{WORK_ORDERS}/{wo['id']}", headers=headers, json=body).status_code == 200
        assert client.put(f"{WORK_ORDERS}/{wo['id']}", headers=headers, json={"status": "scheduled"}).status_code == 200
        client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=headers)
        echo = {"status": "in_progress", "requires_shutdown": True, "title": "Renamed"}
        assert client.put(f"{WORK_ORDERS}/{wo['id']}", headers=headers, json=echo).status_code == 200
        assert client.put(f"{WORK_ORDERS}/{wo['id']}", headers=headers, json={"status": "on_hold"}).status_code == 200
        [event] = events_for(db_session, maintenance_work_order_id=wo["id"])
        assert event.end_time is None  # a hold keeps the machine down


class TestScheduleTarget:
    def test_an_explicit_target_must_be_the_schedules(self, client, db_session, headers):
        """Completing a work order advances ITS schedule's due date, so a work order on another
        machine must not carry the schedule."""
        wc, other = make_wc(db_session), make_wc(db_session)
        schedule = client.post(
            "/api/v1/maintenance/schedules",
            headers=headers,
            json={"name": "Synthetic monthly PM", "work_center_id": wc.id, "frequency": "monthly"},
        )
        assert schedule.status_code == 200, schedule.text
        schedule_id = schedule.json()["id"]
        refused = client.post(
            WORK_ORDERS, headers=headers, json={"title": "PM", "schedule_id": schedule_id, "work_center_id": other.id}
        )
        assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
        asset = make_asset(db_session)
        refused = client.post(
            WORK_ORDERS, headers=headers, json={"title": "PM", "schedule_id": schedule_id, "asset_id": asset.id}
        )
        assert refused.status_code == status.HTTP_409_CONFLICT
        assert db_session.query(MaintenanceWorkOrder).count() == 0
        same = client.post(
            WORK_ORDERS, headers=headers, json={"title": "PM", "schedule_id": schedule_id, "work_center_id": wc.id}
        )
        inherited = client.post(WORK_ORDERS, headers=headers, json={"title": "PM", "schedule_id": schedule_id})
        assert same.status_code == inherited.status_code == 200
        assert same.json()["work_center_id"] == inherited.json()["work_center_id"] == wc.id
