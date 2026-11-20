"""PM calendar-month intervals and checklist normalization, through the API (PR-2).

(e) A completed PM rolls its schedule forward by CALENDAR months from the company's completion day,
clamped to a shorter month's end (``app.services.maintenance_interval``): a monthly PM completed
Jan 31 is next due Feb 28, and the clamp sticks (Feb 28 -> Mar 28). The response, the stored
schedule and the schedule's audit row all carry the same date.

(h) Checklist item text is normalized on every read and matched case-insensitively
(``app.services.maintenance_checklist``): a work order's "- Grease  bearing" and the schedule's
"grease bearing" are ONE step, ticked once; ``schedule_checklist_items`` and the snapshot come back
normalized; legacy stored text matches with no rewrite; the PM import normalizes and de-dups its
tasks, so a re-run against a schedule that already holds them is a no-op; and a token edit that only
re-spells a step removes nothing.

The per-string contract is the shared fixture (``test_maintenance_checklist_normalization_parity``).
Every fixture here is synthetic.
"""

import json
from datetime import date

import pytest
from fastapi import status
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.maintenance import MaintenanceFrequency, MaintenanceSchedule, MaintenanceStatus, MaintenanceWorkOrder
from app.models.user import UserRole
from app.services.company_time import company_today
from tests.api.test_maintenance_assets import make_wc
from tests.api.test_maintenance_checklist_completion import complete, completed_logs, completion_audit
from tests.api.test_maintenance_pm_integrity import (
    commit,
    import_audit,
    items,
    legacy_pm,
    monthly_rows,
    preview,
    put_schedule,
    stored_checklist,
    stored_wo,
)
from tests.api.test_maintenance_pm_roll_forward import freeze_company_day, make_schedule, schedule_audit_rows
from tests.api.test_maintenance_schedule_import import xlsx
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
    """A standing (full-scope) API token bound to the same supervisor, minted by an Admin."""
    issued = client.post(API_TOKENS, json={"user_id": supervisor.id, "label": "PM probe"}, headers=admin_headers)
    assert issued.status_code == 201, issued.text
    return {"Authorization": f"Bearer {issued.json()['token']}", "X-Requested-With": "XMLHttpRequest"}


def entries(*pairs) -> str:
    return json.dumps([{"item": item, "done": done} for item, done in pairs])


def raw_schedule_checklist(db: Session, schedule_id: int):
    db.expire_all()
    return db.get(MaintenanceSchedule, schedule_id).checklist


# --------------------------------------------------------------------------- (h) normalization


class TestOneStepHoweverItIsSpelled:
    def test_a_respelled_work_order_step_is_ticked_once_against_the_schedule_step(self, client, db_session, headers):
        schedule = make_schedule(
            db_session, next_due=company_today(db_session, 1), checklist=json.dumps(["grease bearing"])
        )
        opened = client.post(
            WORK_ORDERS,
            headers=headers,
            json={
                "schedule_id": schedule.id,
                "title": "PM",
                "checklist_results": entries(("- Grease  bearing", False)),
            },
        )
        assert opened.status_code == 200, opened.text
        wo = opened.json()

        # Unticked, the two spellings are ONE step: "1 of 1", not "2 of 2".
        refused = complete(client, headers, wo["id"])
        assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
        assert refused.json()["detail"].startswith("1 of 1 checklist items")

        done = complete(client, headers, wo["id"], checklist_results=entries(("- Grease  bearing", True)))
        assert done.status_code == 200, done.text
        [row] = completion_audit(db_session, wo["id"])
        assert "checklist_unticked_items" not in row.extra_data
        [log] = completed_logs(db_session, wo["id"])
        assert "not ticked" not in log.description

    def test_case_alone_ticks_the_step(self, client, db_session, headers):
        schedule = make_schedule(
            db_session, next_due=company_today(db_session, 1), checklist=json.dumps(["Check Coolant"])
        )
        created = client.post(WORK_ORDERS, headers=headers, json={"schedule_id": schedule.id, "title": "PM"})
        done = complete(client, headers, created.json()["id"], checklist_results=entries(("CHECK COOLANT", True)))
        assert done.status_code == 200, done.text

    def test_the_snapshot_and_the_served_steps_are_normalized_and_the_schedule_text_is_not_rewritten(
        self, client, db_session, headers
    ):
        raw = "- Grease  bearing\r\n•Check oil\n\n1. Drain tank\n"
        schedule = make_schedule(db_session, next_due=company_today(db_session, 1), checklist=raw)
        created = client.post(WORK_ORDERS, headers=headers, json={"schedule_id": schedule.id, "title": "PM"})
        assert created.status_code == 200, created.text
        expected = ["Grease bearing", "Check oil", "1. Drain tank"]
        assert created.json()["schedule_checklist_items"] == expected
        assert stored_wo(db_session, created.json()["id"]).schedule_checklist_snapshot == json.dumps(expected)
        assert items(client, headers, created.json()["id"]) == expected
        assert raw_schedule_checklist(db_session, schedule.id) == raw

    def test_a_legacy_work_order_and_schedule_match_as_stored_with_no_write(self, client, db_session, headers):
        """Text stored before normalization (stray spacing, bullets, other case) is read through the
        normalizer: the reads serve it normalized and completion matches it, but nothing rewrites the
        stored rows (an UPDATE outside AuditService would break invariant 2)."""
        raw_schedule = "  - Check   oil \n•Grease bearing\n"
        raw_results = json.dumps([{"item": "  check OIL ", "done": False}, {"item": "* grease bearing", "done": False}])
        schedule = make_schedule(db_session, next_due=company_today(db_session, 1), checklist=raw_schedule)
        wo = legacy_pm(db_session, schedule, status_=MaintenanceStatus.IN_PROGRESS)
        stored = stored_wo(db_session, wo.id)
        stored.checklist_results = raw_results
        db_session.commit()

        assert items(client, headers, wo.id) == ["Check oil", "Grease bearing"]
        one = client.get(f"{WORK_ORDERS}/{wo.id}", headers=headers).json()
        assert one["checklist_results"] == raw_results  # served as stored
        assert raw_schedule_checklist(db_session, schedule.id) == raw_schedule
        assert stored_wo(db_session, wo.id).checklist_results == raw_results
        assert stored_wo(db_session, wo.id).schedule_checklist_snapshot is None

        refused = complete(client, headers, wo.id)
        assert refused.json()["detail"].startswith("2 of 2 checklist items")
        ticked = json.dumps([{"item": "check oil", "done": True}, {"item": "GREASE BEARING", "done": True}])
        done = complete(client, headers, wo.id, checklist_results=ticked)
        assert done.status_code == 200, done.text
        assert raw_schedule_checklist(db_session, schedule.id) == raw_schedule
        # Completion freezes the normalized steps it judged.
        assert stored_wo(db_session, wo.id).schedule_checklist_snapshot == json.dumps(["Check oil", "Grease bearing"])


class TestATokenRespellingAStepRemovesNothing:
    def test_a_case_and_spacing_edit_under_a_legacy_open_work_order_is_allowed(
        self, client, db_session, headers, token_headers
    ):
        schedule = make_schedule(
            db_session, next_due=company_today(db_session, 1), checklist=json.dumps(["Lubricate ways", "Check coolant"])
        )
        wo = legacy_pm(db_session, schedule)
        respelled = ["- LUBRICATE  ways", "• check coolant"]
        edited = put_schedule(client, token_headers, schedule.id, respelled)
        assert edited.status_code == status.HTTP_200_OK, edited.text
        assert raw_schedule_checklist(db_session, schedule.id) == json.dumps(respelled)
        assert items(client, headers, wo.id) == ["LUBRICATE ways", "check coolant"]
        # A real removal is still refused.
        refused = put_schedule(client, token_headers, schedule.id, ["lubricate ways"])
        assert refused.status_code == status.HTTP_403_FORBIDDEN, refused.text


@pytest.fixture
def lathe_schedule(db_session):
    """An ACTIVE schedule the sheet's ``CNC-02 Monthly PM`` group upserts."""
    lathe = make_wc(db_session, code="CNC-02")
    schedule = MaintenanceSchedule(
        company_id=1,
        work_center_id=lathe.id,
        name="CNC-02 Monthly PM",
        frequency=MaintenanceFrequency.MONTHLY,
        checklist=json.dumps(["Check oil"]),
        next_due_date=date(2026, 12, 1),
        is_active=True,
    )
    db_session.add(schedule)
    db_session.commit()
    db_session.refresh(schedule)
    return schedule


class TestThePmImportNormalizesItsTasks:
    @pytest.mark.parametrize("stored", [json.dumps(["Check oil"]), "- Check  oil\n"], ids=["normalized", "legacy"])
    def test_a_bulleted_task_already_on_the_schedule_is_unchanged(
        self, client, db_session, headers, lathe_schedule, stored
    ):
        lathe_schedule.checklist = stored
        db_session.commit()
        content = xlsx(monthly_rows(["- Check oil"]))
        before = import_audit(db_session)

        planned = preview(client, headers, content)
        [group] = planned["schedules"]
        assert group["outcome"] == "unchanged" and group["tasks"] == ["Check oil"]
        assert commit(client, headers, content, planned).status_code == 200
        again = preview(client, headers, content)
        assert again["schedules"][0]["outcome"] == "unchanged"
        assert commit(client, headers, content, again).status_code == 200

        assert import_audit(db_session) == before  # nothing written, either time
        assert raw_schedule_checklist(db_session, lathe_schedule.id) == stored

    def test_two_spellings_of_one_task_are_listed_once_with_a_warning(self, client, db_session, headers):
        make_wc(db_session, code="CNC-07")
        content = xlsx(monthly_rows(["Check oil", "check  OIL", "• Grease bearing"], code="CNC-07"))
        planned = preview(client, headers, content)
        [group] = planned["schedules"]
        assert group["tasks"] == ["Check oil", "Grease bearing"]  # the first spelling kept
        rows = {row["row"]: row for row in planned["rows"]}
        assert rows[3]["warnings"] == ["Duplicate of row 2's task in this schedule; listed once."]
        assert not rows[2]["warnings"] and not rows[4]["warnings"]

        done = commit(client, headers, content, planned)
        assert done.status_code == 200, done.text
        [created] = done.json()["schedules"]
        assert stored_checklist(db_session, created["schedule_id"]) == ["Check oil", "Grease bearing"]


# --------------------------------------------------------------------------- (e) calendar months


class TestACompletionRollsForwardByCalendarMonths:
    def test_jan_31_is_next_due_feb_28_and_the_clamp_sticks(self, client, db_session, headers, monkeypatch):
        freeze_company_day(monkeypatch, db_session, date(2027, 1, 31))
        schedule = make_schedule(db_session, next_due=date(2027, 1, 31))  # monthly

        first = client.post(WORK_ORDERS, headers=headers, json={"schedule_id": schedule.id, "title": "PM"})
        done = complete(client, headers, first.json()["id"])
        assert done.status_code == 200, done.text
        assert done.json()["schedule_next_due_date"] == "2027-02-28"  # 30 days would be Mar 2
        db_session.expire_all()
        assert db_session.get(MaintenanceSchedule, schedule.id).next_due_date == date(2027, 2, 28)
        [row] = schedule_audit_rows(db_session, schedule.id)
        assert row.old_values == {"last_completed_date": None, "next_due_date": "2027-01-31"}
        assert row.new_values == {"last_completed_date": "2027-01-31", "next_due_date": "2027-02-28"}
        [completion] = completion_audit(db_session, first.json()["id"])
        assert completion.extra_data["schedule_next_due_date"] == "2027-02-28"

        # Completed on time again on Feb 28: Mar 28, not back to Mar 31.
        freeze_company_day(monkeypatch, db_session, date(2027, 2, 28))
        second = client.post(WORK_ORDERS, headers=headers, json={"schedule_id": schedule.id, "title": "PM"})
        done = complete(client, headers, second.json()["id"])
        assert done.status_code == 200, done.text
        assert done.json()["schedule_next_due_date"] == "2027-03-28"
        assert schedule_audit_rows(db_session, schedule.id)[-1].new_values == {
            "last_completed_date": "2027-02-28",
            "next_due_date": "2027-03-28",
        }

    def test_a_late_pm_floats_from_the_completion_day(self, client, db_session, headers, monkeypatch):
        """The base is the COMPLETION day, not the old due date: a PM due Jan 15 and done Jan 31 is next
        due Feb 28."""
        freeze_company_day(monkeypatch, db_session, date(2027, 1, 31))
        schedule = make_schedule(db_session, next_due=date(2027, 1, 15))
        created = client.post(WORK_ORDERS, headers=headers, json={"schedule_id": schedule.id, "title": "PM"})
        done = complete(client, headers, created.json()["id"])
        assert done.json()["schedule_next_due_date"] == "2027-02-28"

    def test_a_leap_year_january_31_is_next_due_feb_29(self, client, db_session, headers, monkeypatch):
        freeze_company_day(monkeypatch, db_session, date(2028, 1, 31))
        schedule = make_schedule(db_session, next_due=date(2028, 1, 31))
        created = client.post(WORK_ORDERS, headers=headers, json={"schedule_id": schedule.id, "title": "PM"})
        done = complete(client, headers, created.json()["id"])
        assert done.json()["schedule_next_due_date"] == "2028-02-29"
        db_session.expire_all()
        assert db_session.query(MaintenanceWorkOrder).filter_by(id=created.json()["id"]).one().status == (
            MaintenanceStatus.COMPLETED
        )
        assert db_session.query(AuditLog).filter(AuditLog.resource_type == "maintenance_schedule").count() == 1
