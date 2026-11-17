"""Completing a maintenance work order with part of its checklist undone needs a written reason.

``POST /maintenance/work-orders/{id}/complete``:

* any checklist item unticked and ``incomplete_checklist_reason`` blank -> **409**, judged before
  the first write: the work order stays open, no log entry, no audit row, the PM schedule does not
  move and the downtime the work order holds stays open;
* with a reason -> completes, and the completion's STATUS_CHANGE audit row carries
  ``checklist_item_count`` / ``checklist_unticked_items`` / ``incomplete_checklist_reason``. That
  row is then REQUIRED evidence (``log_required`` inside ``required_audit``): if it cannot be
  written the whole completion rolls back and the answer is **503**;
* the checklist judged is the one the completion sends (an explicit null included), else the
  stored one; an item the stored checklist carried that the sent one drops or clears counts as
  unticked, and a repeated item counts once per copy;
* every item ticked -> no reason needed, a sent one is ignored, and the audit row stays the
  best-effort ``log_status_change`` it always was;
* a standing API token may complete only with every item ticked: with any unticked it is **403**
  ``API_TOKEN_INCOMPLETE_CHECKLIST`` whatever reason it sends, before the reason check and before
  any write (owner decision 2026-11-17 -- why a PM step was skipped is a person's judgment).

The assertions read the STORED rows, not only status codes.
"""

import json

import pytest
from fastapi import status
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.downtime import DowntimeEvent
from app.models.maintenance import MaintenanceLog, MaintenanceSchedule, MaintenanceStatus, MaintenanceWorkOrder
from app.models.user import UserRole
from app.services.audit_service import AuditService, AuditWriteError
from app.services.company_time import company_today
from app.services.maintenance_checklist import API_TOKEN_INCOMPLETE_CHECKLIST
from tests.api.test_maintenance_assets import make_wc
from tests.api.test_maintenance_pm_roll_forward import make_schedule
from tests.api.test_receiving_compliance import headers_for, make_user

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

WORK_ORDERS = "/api/v1/maintenance/work-orders"
API_TOKENS = "/api/v1/api-tokens/"
REASON_FIELD = "incomplete_checklist_reason"


@pytest.fixture
def supervisor(db_session):
    return make_user(db_session, role=UserRole.SUPERVISOR, company_id=1)


@pytest.fixture
def headers(supervisor):
    return headers_for(supervisor)


@pytest.fixture
def token_headers(client, admin_headers, supervisor):
    """A standing (full-scope) API token bound to the same supervisor -- minted the way an Admin
    does it, so the request carries ``_api_token_id`` exactly as the Werco Assistant's would."""
    issued = client.post(API_TOKENS, json={"user_id": supervisor.id, "label": "PM probe"}, headers=admin_headers)
    assert issued.status_code == 201, issued.text
    return {"Authorization": f"Bearer {issued.json()['token']}", "X-Requested-With": "XMLHttpRequest"}


def checklist(*entries) -> str:
    return json.dumps([{"item": item, "done": done} for item, done in entries])


TWO_ITEMS_OPEN = checklist(("Lubricate ways", False), ("Check coolant", False))
ONE_TICKED = checklist(("Lubricate ways", True), ("Check coolant", False))
BOTH_TICKED = checklist(("Lubricate ways", True), ("Check coolant", True))


def start_pm(client, headers, db: Session, *, checklist_results=TWO_ITEMS_OPEN, start: bool = True):
    """A PM work order on a work-center schedule that shuts the machine down -- so it holds a
    downtime event once started -- opened with ``checklist_results``."""
    schedule = make_schedule(db, next_due=company_today(db, 1))
    created = client.post(
        WORK_ORDERS,
        headers=headers,
        json={
            "schedule_id": schedule.id,
            "title": "Synthetic PM",
            "requires_shutdown": True,
            "checklist_results": checklist_results,
        },
    )
    assert created.status_code == 200, created.text
    wo = created.json()
    if start:
        assert client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=headers).status_code == 200
    return schedule, wo


def complete(client, headers, wo_id: int, **body):
    return client.post(f"{WORK_ORDERS}/{wo_id}/complete", headers=headers, json=body)


def completion_audit(db: Session, wo_id: int):
    """The work order's STATUS_CHANGE rows INTO completed (``/start`` writes one too)."""
    db.expire_all()
    rows = (
        db.query(AuditLog)
        .filter(
            AuditLog.resource_type == "maintenance_work_order",
            AuditLog.resource_id == wo_id,
            AuditLog.action == "STATUS_CHANGE",
        )
        .order_by(AuditLog.id)
        .all()
    )
    return [row for row in rows if (row.new_values or {}).get("status") == "completed"]


def completed_logs(db: Session, wo_id: int):
    db.expire_all()
    return (
        db.query(MaintenanceLog)
        .filter(MaintenanceLog.maintenance_wo_id == wo_id, MaintenanceLog.event_type == "completed")
        .all()
    )


def stored_state(db: Session, wo_id: int, schedule_id: int) -> dict:
    """Everything a completion writes, as stored."""
    db.expire_all()
    wo = db.get(MaintenanceWorkOrder, wo_id)
    schedule = db.get(MaintenanceSchedule, schedule_id)
    events = db.query(DowntimeEvent).filter(DowntimeEvent.maintenance_work_order_id == wo_id).all()
    return {
        "status": wo.status,
        "completed_at": wo.completed_at,
        "completed_by": wo.completed_by,
        "checklist_results": wo.checklist_results,
        "notes": wo.notes,
        "total_cost": wo.total_cost,
        "last_completed_date": schedule.last_completed_date,
        "next_due_date": schedule.next_due_date,
        "open_downtime": [event.id for event in events if event.end_time is None],
        "resolved_downtime": [event.id for event in events if event.end_time is not None],
        "logs": db.query(MaintenanceLog).filter(MaintenanceLog.maintenance_wo_id == wo_id).count(),
        "audit_rows": db.query(AuditLog).count(),
    }


def fail_required_audit_for_work_orders(patch):
    """``log_required`` raises for the work order's completion row; anything else is untouched."""
    real = AuditService.log_required

    def failing(self, action, resource_type, **kwargs):
        if resource_type == "maintenance_work_order":
            raise AuditWriteError("Synthetic required audit failure")
        return real(self, action, resource_type, **kwargs)

    patch.setattr(AuditService, "log_required", failing)


class TestRefusedWithoutAReason:
    def test_an_unticked_item_without_a_reason_is_409_and_writes_nothing(self, client, db_session, headers):
        schedule, wo = start_pm(client, headers, db_session)
        before = stored_state(db_session, wo["id"], schedule.id)
        assert before["status"] == MaintenanceStatus.IN_PROGRESS and len(before["open_downtime"]) == 1

        refused = complete(
            client, headers, wo["id"], checklist_results=ONE_TICKED, notes="done", labor_cost=50.0, parts_cost=5.0
        )

        assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
        detail = refused.json()["detail"]
        assert isinstance(detail, str)
        assert "1 of 2 checklist items" in detail and wo["wo_number"] in detail
        # Nothing written: still in progress, the sent checklist/notes/costs not saved, no log
        # entry, no audit row, the schedule not advanced, the machine's downtime still open.
        assert stored_state(db_session, wo["id"], schedule.id) == before

    @pytest.mark.parametrize("reason", ["", "   ", "\n\t "])
    def test_a_blank_reason_is_no_reason(self, client, db_session, headers, reason):
        schedule, wo = start_pm(client, headers, db_session)
        before = stored_state(db_session, wo["id"], schedule.id)
        refused = complete(client, headers, wo["id"], checklist_results=ONE_TICKED, **{REASON_FIELD: reason})
        assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
        assert stored_state(db_session, wo["id"], schedule.id) == before

    def test_an_omitted_checklist_is_judged_as_stored(self, client, db_session, headers):
        """Not sending ``checklist_results`` completes with the stored checklist -- whose items
        are all unticked here."""
        schedule, wo = start_pm(client, headers, db_session)
        refused = complete(client, headers, wo["id"], notes="done")
        assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
        assert "2 of 2 checklist items" in refused.json()["detail"]

    def test_a_plain_text_checklist_is_all_unticked(self, client, db_session, headers):
        schedule, wo = start_pm(client, headers, db_session, checklist_results="Drain tank\nCheck belt\nGrease")
        refused = complete(client, headers, wo["id"])
        assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
        assert "3 of 3 checklist items" in refused.json()["detail"]

    def test_dropping_an_item_is_not_a_way_around_it(self, client, db_session, headers):
        schedule, wo = start_pm(client, headers, db_session)
        before = stored_state(db_session, wo["id"], schedule.id)
        dropped = complete(client, headers, wo["id"], checklist_results=checklist(("Lubricate ways", True)))
        assert dropped.status_code == status.HTTP_409_CONFLICT, dropped.text
        assert "1 of 2 checklist items" in dropped.json()["detail"]
        assert stored_state(db_session, wo["id"], schedule.id) == before

    @pytest.mark.parametrize("cleared", [None, "", "[]"])
    def test_clearing_the_checklist_is_not_a_way_around_it(self, client, db_session, headers, cleared):
        schedule, wo = start_pm(client, headers, db_session)
        before = stored_state(db_session, wo["id"], schedule.id)
        refused = complete(client, headers, wo["id"], checklist_results=cleared)
        assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
        assert "2 of 2 checklist items" in refused.json()["detail"]
        assert stored_state(db_session, wo["id"], schedule.id) == before

    def test_one_tick_does_not_cover_a_repeated_item(self, client, db_session, headers):
        """The step "Grease bearing" appears twice (two bearings): ticking one copy leaves the
        other undone, so the completion still needs a reason."""
        repeated = checklist(("Grease bearing", False), ("Grease bearing", False), ("Check belt", False))
        schedule, wo = start_pm(client, headers, db_session, checklist_results=repeated)
        before = stored_state(db_session, wo["id"], schedule.id)
        sent = checklist(("Grease bearing", True), ("Grease bearing", False), ("Check belt", True))
        refused = complete(client, headers, wo["id"], checklist_results=sent)
        assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
        assert "1 of 3 checklist items" in refused.json()["detail"]
        assert stored_state(db_session, wo["id"], schedule.id) == before

    def test_the_status_refusal_comes_first(self, client, db_session, headers):
        """An on-hold work order cannot be completed at all (400), checklist or not."""
        schedule, wo = start_pm(client, headers, db_session)
        assert client.put(f"{WORK_ORDERS}/{wo['id']}", headers=headers, json={"status": "on_hold"}).status_code == 200
        refused = complete(client, headers, wo["id"])
        assert refused.status_code == status.HTTP_400_BAD_REQUEST, refused.text

    def test_a_reason_over_2000_characters_is_422(self, client, db_session, headers):
        schedule, wo = start_pm(client, headers, db_session)
        before = stored_state(db_session, wo["id"], schedule.id)
        refused = complete(client, headers, wo["id"], **{REASON_FIELD: "x" * 2001})
        assert refused.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, refused.text
        assert stored_state(db_session, wo["id"], schedule.id) == before


class TestCompletedWithAReason:
    def test_the_reason_and_the_unticked_items_are_recorded(self, client, db_session, headers):
        schedule, wo = start_pm(client, headers, db_session)
        done = complete(
            client,
            headers,
            wo["id"],
            checklist_results=ONE_TICKED,
            **{REASON_FIELD: "  Coolant concentrate on back order  "},
        )
        assert done.status_code == 200, done.text
        assert done.json()["status"] == "completed"
        assert done.json()["checklist_results"] == ONE_TICKED

        today = company_today(db_session, 1)
        state = stored_state(db_session, wo["id"], schedule.id)
        assert state["status"] == MaintenanceStatus.COMPLETED
        assert state["last_completed_date"] == today  # the schedule still rolls forward
        assert state["open_downtime"] == [] and len(state["resolved_downtime"]) == 1

        [row] = completion_audit(db_session, wo["id"])
        assert row.old_values == {"status": "in_progress"} and row.new_values == {"status": "completed"}
        assert row.extra_data["checklist_item_count"] == 2
        assert row.extra_data["checklist_unticked_items"] == ["Check coolant"]
        assert row.extra_data["incomplete_checklist_reason"] == "Coolant concentrate on back order"  # trimmed
        # The fields every completion row carries are still there.
        assert row.extra_data["schedule_id"] == schedule.id
        assert row.extra_data["resolved_downtime_event_ids"] == state["resolved_downtime"]
        assert "1 of 2 checklist items not ticked" in row.description

        [log] = completed_logs(db_session, wo["id"])
        assert "1 of 2 items not ticked" in log.description
        assert "Check coolant" in log.description
        assert "Reason: Coolant concentrate on back order" in log.description

    def test_a_repeated_item_is_recorded_once_per_unticked_copy(self, client, db_session, headers):
        repeated = checklist(("Grease bearing", False), ("Grease bearing", False), ("Check belt", False))
        schedule, wo = start_pm(client, headers, db_session, checklist_results=repeated)
        sent = checklist(("Grease bearing", True), ("Grease bearing", False), ("Check belt", True))
        done = complete(client, headers, wo["id"], checklist_results=sent, **{REASON_FIELD: "Second bearing seized"})
        assert done.status_code == 200, done.text

        [row] = completion_audit(db_session, wo["id"])
        assert row.extra_data["checklist_unticked_items"] == ["Grease bearing"]
        assert row.extra_data["checklist_item_count"] == 3
        assert row.extra_data["incomplete_checklist_reason"] == "Second bearing seized"

    def test_a_cleared_checklist_records_every_stored_item(self, client, db_session, headers):
        schedule, wo = start_pm(client, headers, db_session, checklist_results=ONE_TICKED)
        done = complete(client, headers, wo["id"], checklist_results=None, **{REASON_FIELD: "Checklist superseded"})
        assert done.status_code == 200, done.text
        assert done.json()["checklist_results"] is None

        [row] = completion_audit(db_session, wo["id"])
        # A stored tick does not survive the clear: both items are undone.
        assert row.extra_data["checklist_unticked_items"] == ["Lubricate ways", "Check coolant"]
        assert row.extra_data["checklist_item_count"] == 2

    def test_a_reason_of_exactly_2000_characters_is_accepted(self, client, db_session, headers):
        schedule, wo = start_pm(client, headers, db_session)
        done = complete(client, headers, wo["id"], **{REASON_FIELD: "y" * 2000})
        assert done.status_code == 200, done.text
        [row] = completion_audit(db_session, wo["id"])
        assert row.extra_data["incomplete_checklist_reason"] == "y" * 2000

    def test_completing_straight_from_scheduled_is_judged_the_same(self, client, db_session, headers):
        schedule, wo = start_pm(client, headers, db_session, start=False)
        assert complete(client, headers, wo["id"]).status_code == status.HTTP_409_CONFLICT
        done = complete(client, headers, wo["id"], **{REASON_FIELD: "Done by the OEM tech; sheet lost"})
        assert done.status_code == 200, done.text
        [row] = completion_audit(db_session, wo["id"])
        assert row.old_values == {"status": "scheduled"}
        assert row.extra_data["checklist_unticked_items"] == ["Lubricate ways", "Check coolant"]


class TestTheReasonIsRequiredEvidence:
    def test_a_failed_audit_write_rolls_the_whole_completion_back_with_503(
        self, client, db_session, headers, monkeypatch
    ):
        schedule, wo = start_pm(client, headers, db_session)
        before = stored_state(db_session, wo["id"], schedule.id)
        assert len(before["open_downtime"]) == 1

        with monkeypatch.context() as patch:
            fail_required_audit_for_work_orders(patch)
            failed = complete(
                client,
                headers,
                wo["id"],
                checklist_results=ONE_TICKED,
                notes="done",
                labor_cost=50.0,
                **{REASON_FIELD: "Coolant concentrate on back order"},
            )

        assert failed.status_code == status.HTTP_503_SERVICE_UNAVAILABLE, failed.text
        assert "could not be saved" in failed.json()["detail"] and wo["wo_number"] in failed.json()["detail"]
        # Rolled back: still in progress, no completed log entry, no audit row (not even the
        # schedule's roll-forward row), the schedule not advanced, the downtime event still open.
        after = stored_state(db_session, wo["id"], schedule.id)
        assert after == before
        assert after["status"] == MaintenanceStatus.IN_PROGRESS and after["completed_at"] is None
        assert completed_logs(db_session, wo["id"]) == []
        assert completion_audit(db_session, wo["id"]) == []

        # With the audit trail back, the same request completes.
        done = complete(
            client,
            headers,
            wo["id"],
            checklist_results=ONE_TICKED,
            **{REASON_FIELD: "Coolant concentrate on back order"},
        )
        assert done.status_code == 200, done.text


class TestEveryItemTicked:
    def test_no_reason_is_needed_and_a_sent_one_is_ignored(self, client, db_session, headers):
        schedule, wo = start_pm(client, headers, db_session)
        done = complete(
            client, headers, wo["id"], checklist_results=BOTH_TICKED, **{REASON_FIELD: "Nothing was skipped"}
        )
        assert done.status_code == 200, done.text

        [row] = completion_audit(db_session, wo["id"])
        for key in ("checklist_item_count", "checklist_unticked_items", "incomplete_checklist_reason"):
            assert key not in row.extra_data
        [log] = completed_logs(db_session, wo["id"])
        assert "Reason" not in log.description and "not ticked" not in log.description

    def test_a_clean_completion_stays_best_effort_audit(self, client, db_session, headers, monkeypatch):
        """Only an incomplete checklist makes the row required evidence: with every item ticked
        the completion never calls ``log_required``, so an outage of it does not block."""
        schedule, wo = start_pm(client, headers, db_session)
        fail_required_audit_for_work_orders(monkeypatch)
        done = complete(client, headers, wo["id"], checklist_results=BOTH_TICKED)
        assert done.status_code == 200, done.text
        assert len(completion_audit(db_session, wo["id"])) == 1

    def test_a_work_order_with_no_checklist_completes_as_before(self, client, db_session, headers):
        wc = make_wc(db_session)
        created = client.post(WORK_ORDERS, headers=headers, json={"work_center_id": wc.id, "title": "Corrective"})
        assert created.status_code == 200, created.text
        done = complete(client, headers, created.json()["id"])
        assert done.status_code == 200, done.text
        [row] = completion_audit(db_session, created.json()["id"])
        assert "checklist_unticked_items" not in row.extra_data

    def test_ticks_on_the_stored_checklist_count_when_none_is_sent(self, client, db_session, headers):
        schedule, wo = start_pm(client, headers, db_session, checklist_results=BOTH_TICKED)
        done = complete(client, headers, wo["id"])
        assert done.status_code == 200, done.text
        assert done.json()["checklist_results"] == BOTH_TICKED


class TestAnApiTokenCannotSkipChecklistItems:
    def test_a_token_with_a_reason_is_403_and_writes_nothing(self, client, db_session, headers, token_headers):
        schedule, wo = start_pm(client, headers, db_session)
        before = stored_state(db_session, wo["id"], schedule.id)

        refused = complete(
            client,
            token_headers,
            wo["id"],
            checklist_results=ONE_TICKED,
            notes="done",
            **{REASON_FIELD: "Coolant concentrate on back order"},
        )

        assert refused.status_code == status.HTTP_403_FORBIDDEN, refused.text
        assert refused.json()["detail"] == API_TOKEN_INCOMPLETE_CHECKLIST
        assert stored_state(db_session, wo["id"], schedule.id) == before

    def test_a_token_without_a_reason_gets_the_403_not_the_409(self, client, db_session, headers, token_headers):
        """The token refusal comes first: telling a token to "give a reason" would invite a retry
        that can never succeed."""
        schedule, wo = start_pm(client, headers, db_session)
        before = stored_state(db_session, wo["id"], schedule.id)
        for body in ({}, {"checklist_results": None}, {"checklist_results": checklist(("Lubricate ways", True))}):
            refused = complete(client, token_headers, wo["id"], **body)
            assert refused.status_code == status.HTTP_403_FORBIDDEN, (body, refused.text)
            assert refused.json()["detail"] == API_TOKEN_INCOMPLETE_CHECKLIST
        assert stored_state(db_session, wo["id"], schedule.id) == before

    def test_a_token_ticking_every_item_still_completes(self, client, db_session, headers, token_headers):
        schedule, wo = start_pm(client, headers, db_session)
        done = complete(client, token_headers, wo["id"], checklist_results=BOTH_TICKED)
        assert done.status_code == 200, done.text
        assert done.json()["status"] == "completed"
        [row] = completion_audit(db_session, wo["id"])
        assert "credential" in row.extra_data  # attributed to the token, not the person
        assert "checklist_unticked_items" not in row.extra_data

    def test_a_token_completing_a_work_order_with_no_checklist_still_completes(
        self, client, db_session, headers, token_headers
    ):
        wc = make_wc(db_session)
        created = client.post(WORK_ORDERS, headers=headers, json={"work_center_id": wc.id, "title": "Corrective"})
        assert created.status_code == 200, created.text
        done = complete(client, token_headers, created.json()["id"])
        assert done.status_code == 200, done.text

    def test_the_same_person_signed_in_completes_with_a_reason(self, client, db_session, headers, token_headers):
        """The refusal is about the credential, not the user: the token's own holder, signed in
        to the app, records the reason and completes."""
        schedule, wo = start_pm(client, headers, db_session)
        assert complete(client, token_headers, wo["id"], **{REASON_FIELD: "x"}).status_code == 403
        done = complete(client, headers, wo["id"], **{REASON_FIELD: "Coolant concentrate on back order"})
        assert done.status_code == 200, done.text
        [row] = completion_audit(db_session, wo["id"])
        assert "credential" not in row.extra_data
        assert row.extra_data["incomplete_checklist_reason"] == "Coolant concentrate on back order"
