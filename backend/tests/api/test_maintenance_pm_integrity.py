"""Preventive-maintenance record integrity (migration 132).

(d) **The checklist a PM work order is judged on is snapshotted when it is OPENED.**
``POST /maintenance/work-orders`` with a ``schedule_id`` writes
``schedule_checklist_snapshot = json.dumps(parse_checklist(schedule.checklist))`` (``"[]"`` for a
schedule with no steps, NULL for a work order without a schedule). Completion judges the snapshot,
and every response serves it as ``schedule_checklist_items``, so a later schedule edit or PM import
binds the NEXT work order -- never an open one, and never a finished record. A work order opened
BEFORE migration 132 has no snapshot: while it stays open it reads and is judged on the schedule's
LIVE steps (the pre-132 rule, never backfilled); completing it freezes the steps that completion
judged.

(g) **The API-token rule follows the snapshot.** ``PUT /maintenance/schedules/{id}`` refuses a token
403 ``API_TOKEN_SCHEDULE_CHECKLIST_REMOVAL`` only while a LEGACY (NULL-snapshot) work order is open
on the schedule. The PM import loads the company's open work orders: a snapshotted one is a row
warning naming it; a legacy one is a warning for a person and -- when the import takes steps away
and the caller is a token -- a row error on every row of that schedule, so the commit is 409 and
writes nothing.

(i) **One open work order per schedule, in the database** (``uq_maintenance_work_orders_open_schedule``,
partial on the open statuses, per company); ``create_work_order`` maps a flush ``IntegrityError`` to a
409 that writes nothing.

(a) **Completion notes**: omitted keeps the stored notes, a string replaces them, an explicit null
clears them; a change is carried (old and new) in the completion's audit row on both audit paths.

(j) **The MWO number** uses the company's calendar year, takes the install-wide
``maintenance_work_order_number`` generator lock, and follows the highest NUMERIC suffix.

(k) **Row locks**: the schedule is ``FOR UPDATE`` on Start PM, on completion of a PM work order (after
the work order's own lock) and on the schedule ``PUT`` -- asserted on the query each verb built,
compiled for PostgreSQL (SQLite renders no ``FOR UPDATE``).

Every fixture is synthetic. The assertions read STORED rows, not only status codes.
"""

import json
from datetime import date
from datetime import datetime as real_datetime
from datetime import timezone

import pytest
from fastapi import status
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Query, Session

import app.api.endpoints.maintenance as maintenance_endpoints
import app.core.time_utils as time_utils
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.maintenance import (
    OPEN_MAINTENANCE_STATUSES,
    MaintenanceFrequency,
    MaintenanceSchedule,
    MaintenanceStatus,
    MaintenanceWorkOrder,
)
from app.models.user import UserRole
from app.services.company_time import company_today
from app.services.maintenance_checklist import API_TOKEN_INCOMPLETE_CHECKLIST, API_TOKEN_SCHEDULE_CHECKLIST_REMOVAL
from tests.api.test_maintenance_assets import make_wc
from tests.api.test_maintenance_checklist_completion import (
    checklist,
    complete,
    completion_audit,
    fail_required_audit_for_work_orders,
    stored_state,
)
from tests.api.test_maintenance_company_day import FrozenDate, FrozenDatetime, _FrozenClock
from tests.api.test_maintenance_pm_roll_forward import make_raw_wo, make_schedule
from tests.api.test_maintenance_schedule_import import COMMIT, PREVIEW, upload, xlsx
from tests.api.test_receiving_compliance import _next, headers_for, make_user

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

WORK_ORDERS = "/api/v1/maintenance/work-orders"
SCHEDULES = "/api/v1/maintenance/schedules"
API_TOKENS = "/api/v1/api-tokens/"
REASON_FIELD = "incomplete_checklist_reason"

STEPS = ["Lubricate ways", "Check coolant"]
BOTH_TICKED = checklist(("Lubricate ways", True), ("Check coolant", True))
FIRST_TICKED = checklist(("Lubricate ways", True))


# --------------------------------------------------------------------------- fixtures / helpers


@pytest.fixture
def supervisor(db_session):
    return make_user(db_session, role=UserRole.SUPERVISOR, company_id=1)


@pytest.fixture
def headers(supervisor):
    return headers_for(supervisor)


@pytest.fixture
def token_headers(client, admin_headers, supervisor):
    """A standing (full-scope) API token bound to the same supervisor, minted the way an Admin does
    it -- so the request carries ``_api_token_id`` exactly as an agent's would."""
    issued = client.post(API_TOKENS, json={"user_id": supervisor.id, "label": "PM probe"}, headers=admin_headers)
    assert issued.status_code == 201, issued.text
    return {"Authorization": f"Bearer {issued.json()['token']}", "X-Requested-With": "XMLHttpRequest"}


def open_pm(client, headers, db: Session, *, steps=STEPS, schedule_checklist=None, checklist_results=...):
    """Start PM through the API on a fresh schedule whose checklist is ``steps`` (or the raw
    ``schedule_checklist`` text). Returns ``(schedule, work order json)``."""
    raw = schedule_checklist if schedule_checklist is not None else (json.dumps(steps) if steps is not None else None)
    schedule = make_schedule(db, next_due=company_today(db, 1), checklist=raw)
    body = {"schedule_id": schedule.id, "title": "Synthetic PM"}
    if checklist_results is not ...:
        body["checklist_results"] = checklist_results
    created = client.post(WORK_ORDERS, headers=headers, json=body)
    assert created.status_code == 200, created.text
    return schedule, created.json()


def legacy_pm(db: Session, schedule: MaintenanceSchedule, *, status_=MaintenanceStatus.IN_PROGRESS, company_id=None):
    """A work order opened BEFORE migration 132, written straight to the table: on the schedule's
    own target, ``schedule_checklist_snapshot`` left NULL (the column is never backfilled)."""
    wo = MaintenanceWorkOrder(
        company_id=company_id if company_id is not None else schedule.company_id,
        schedule_id=schedule.id,
        work_center_id=schedule.work_center_id,
        asset_id=schedule.asset_id,
        wo_number=f"MWO-LEGACY-{_next():05d}",
        title="Synthetic legacy PM",
        status=status_,
        requires_shutdown=False,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    assert wo.schedule_checklist_snapshot is None
    return wo


def stored_wo(db: Session, wo_id: int) -> MaintenanceWorkOrder:
    db.expire_all()
    return db.get(MaintenanceWorkOrder, wo_id)


def put_schedule(client, headers, schedule_id: int, steps):
    text = json.dumps(steps) if isinstance(steps, list) else steps
    return client.put(f"{SCHEDULES}/{schedule_id}", headers=headers, json={"checklist": text})


def items(client, headers, wo_id: int):
    """``schedule_checklist_items`` -- the same on the detail read and in the list."""
    one = client.get(f"{WORK_ORDERS}/{wo_id}", headers=headers)
    assert one.status_code == 200, one.text
    [listed] = [row for row in client.get(WORK_ORDERS, headers=headers).json() if row["id"] == wo_id]
    assert listed["schedule_checklist_items"] == one.json()["schedule_checklist_items"]
    return one.json()["schedule_checklist_items"]


def audit_count(db: Session) -> int:
    db.expire_all()
    return db.query(AuditLog).count()


def work_order_count(db: Session) -> int:
    db.expire_all()
    return db.query(MaintenanceWorkOrder).count()


# --------------------------------------------------------------------------- (d) the snapshot


class TestTheChecklistIsSnapshottedWhenTheWorkOrderOpens:
    @pytest.mark.parametrize(
        "raw",
        [
            json.dumps(STEPS),
            "- Lubricate ways\n\n* Check coolant\n",
            json.dumps([{"item": "Lubricate ways"}, "Check coolant"]),
        ],
        ids=["json", "plain-text", "mixed"],
    )
    def test_start_pm_stores_the_schedules_parsed_checklist(self, client, db_session, headers, raw):
        schedule, wo = open_pm(client, headers, db_session, schedule_checklist=raw)
        assert stored_wo(db_session, wo["id"]).schedule_checklist_snapshot == json.dumps(STEPS)
        assert wo["schedule_checklist_items"] == STEPS
        assert items(client, headers, wo["id"]) == STEPS

    def test_the_create_audit_row_records_the_snapshot(self, client, db_session, headers):
        schedule, wo = open_pm(client, headers, db_session)
        db_session.expire_all()
        [row] = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.resource_type == "maintenance_work_order",
                AuditLog.resource_id == wo["id"],
                AuditLog.action == "CREATE",
            )
            .all()
        )
        assert row.new_values["schedule_checklist_snapshot"] == json.dumps(STEPS)

    @pytest.mark.parametrize("raw", [None, "", "[]", "  \n "], ids=["null", "empty", "empty-list", "blank"])
    def test_a_schedule_with_no_steps_snapshots_an_empty_list_not_null(self, client, db_session, headers, raw):
        schedule = make_schedule(db_session, next_due=company_today(db_session, 1), checklist=raw)
        created = client.post(WORK_ORDERS, headers=headers, json={"schedule_id": schedule.id, "title": "PM"})
        assert created.status_code == 200, created.text
        wo = created.json()
        assert stored_wo(db_session, wo["id"]).schedule_checklist_snapshot == "[]"
        assert wo["schedule_checklist_items"] == []
        # "[]" is a schedule that had no steps, not a legacy row: steps added later bind the NEXT
        # work order, so this one still completes with nothing to tick.
        assert put_schedule(client, headers, schedule.id, ["Wipe down"]).status_code == 200
        assert items(client, headers, wo["id"]) == []
        done = complete(client, headers, wo["id"])
        assert done.status_code == 200, done.text
        assert done.json()["schedule_checklist_items"] == []

    def test_a_work_order_without_a_schedule_has_no_snapshot(self, client, db_session, headers):
        wc = make_wc(db_session)
        created = client.post(WORK_ORDERS, headers=headers, json={"work_center_id": wc.id, "title": "Corrective"})
        assert created.status_code == 200, created.text
        assert stored_wo(db_session, created.json()["id"]).schedule_checklist_snapshot is None
        assert created.json()["schedule_checklist_items"] == []

    def test_a_client_cannot_supply_or_rewrite_the_snapshot(self, client, db_session, headers):
        """The field is on no request schema: a sent value is ignored on create and on PUT."""
        schedule = make_schedule(db_session, next_due=company_today(db_session, 1), checklist=json.dumps(STEPS))
        created = client.post(
            WORK_ORDERS,
            headers=headers,
            json={"schedule_id": schedule.id, "title": "PM", "schedule_checklist_snapshot": json.dumps(["Nothing"])},
        )
        assert created.status_code == 200, created.text
        wo_id = created.json()["id"]
        assert stored_wo(db_session, wo_id).schedule_checklist_snapshot == json.dumps(STEPS)
        edited = client.put(
            f"{WORK_ORDERS}/{wo_id}", headers=headers, json={"title": "PM", "schedule_checklist_snapshot": "[]"}
        )
        assert edited.status_code == 200, edited.text
        assert stored_wo(db_session, wo_id).schedule_checklist_snapshot == json.dumps(STEPS)
        assert items(client, headers, wo_id) == STEPS

    def test_a_schedule_edit_never_changes_what_the_open_work_order_answers_for(self, client, db_session, headers):
        schedule, wo = open_pm(client, headers, db_session, checklist_results=FIRST_TICKED)
        # Mid-work-order: one step removed from the schedule, one added.
        edited = put_schedule(client, headers, schedule.id, ["Lubricate ways", "Wipe down"])
        assert edited.status_code == 200, edited.text
        assert items(client, headers, wo["id"]) == STEPS
        assert stored_wo(db_session, wo["id"]).schedule_checklist_snapshot == json.dumps(STEPS)

        # The step REMOVED from the schedule mid-work-order is still one of this PM's steps...
        refused = complete(client, headers, wo["id"])
        assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
        assert refused.json()["detail"].startswith("1 of 2 checklist items")
        assert "(Check coolant)" in refused.json()["detail"] and "Wipe down" not in refused.json()["detail"]
        assert stored_wo(db_session, wo["id"]).status == MaintenanceStatus.SCHEDULED

        # ...and the step ADDED mid-work-order is not: ticking the snapshot's steps is a clean close.
        done = complete(client, headers, wo["id"], checklist_results=BOTH_TICKED)
        assert done.status_code == 200, done.text
        [row] = completion_audit(db_session, wo["id"])
        assert "checklist_unticked_items" not in row.extra_data

    def test_a_token_is_judged_on_the_snapshot_too(self, client, db_session, headers, token_headers):
        """A step added to the schedule mid-work-order is no "unticked step" to refuse a token for."""
        schedule, wo = open_pm(client, headers, db_session)
        assert put_schedule(client, headers, schedule.id, STEPS + ["Wipe down"]).status_code == 200
        assert complete(client, token_headers, wo["id"], checklist_results=FIRST_TICKED).json()["detail"] == (
            API_TOKEN_INCOMPLETE_CHECKLIST
        )
        done = complete(client, token_headers, wo["id"], checklist_results=BOTH_TICKED)
        assert done.status_code == 200, done.text

    def test_a_completed_work_order_keeps_the_steps_it_was_judged_on(self, client, db_session, headers):
        schedule, wo = open_pm(client, headers, db_session)
        done = complete(client, headers, wo["id"], checklist_results=BOTH_TICKED)
        assert done.status_code == 200, done.text
        assert done.json()["schedule_checklist_items"] == STEPS
        # Snapshotted at open: the completion writes no snapshot, so its audit row carries none.
        [row] = completion_audit(db_session, wo["id"])
        assert "schedule_checklist_snapshot" not in row.extra_data

        assert put_schedule(client, headers, schedule.id, ["Replace filter"]).status_code == 200
        assert put_schedule(client, headers, schedule.id, None).status_code == 200
        assert items(client, headers, wo["id"]) == STEPS
        assert stored_wo(db_session, wo["id"]).schedule_checklist_snapshot == json.dumps(STEPS)

    def test_a_cancelled_work_order_keeps_its_snapshot(self, client, db_session, headers):
        schedule, wo = open_pm(client, headers, db_session)
        cancelled = client.put(f"{WORK_ORDERS}/{wo['id']}", headers=headers, json={"status": "cancelled"})
        assert cancelled.status_code == 200, cancelled.text
        assert put_schedule(client, headers, schedule.id, ["Replace filter"]).status_code == 200
        assert items(client, headers, wo["id"]) == STEPS
        assert stored_wo(db_session, wo["id"]).schedule_checklist_snapshot == json.dumps(STEPS)

    def test_the_next_work_order_takes_the_edited_checklist(self, client, db_session, headers):
        schedule, first = open_pm(client, headers, db_session)
        assert put_schedule(client, headers, schedule.id, ["Lubricate ways", "Wipe down"]).status_code == 200
        assert complete(client, headers, first["id"], checklist_results=BOTH_TICKED).status_code == 200

        second = client.post(WORK_ORDERS, headers=headers, json={"schedule_id": schedule.id, "title": "PM"})
        assert second.status_code == 200, second.text
        assert second.json()["schedule_checklist_items"] == ["Lubricate ways", "Wipe down"]
        assert items(client, headers, first["id"]) == STEPS


class TestALegacyWorkOrderStillFollowsTheLiveChecklist:
    """A work order opened before migration 132 (NULL snapshot) reads and is judged on the schedule's
    CURRENT steps while it is open; completing it freezes the steps that completion judged."""

    def test_an_open_legacy_work_order_reads_and_is_judged_live(self, client, db_session, headers):
        schedule = make_schedule(db_session, next_due=company_today(db_session, 1), checklist=json.dumps(STEPS))
        wo = legacy_pm(db_session, schedule)
        assert items(client, headers, wo.id) == STEPS

        assert put_schedule(client, headers, schedule.id, STEPS + ["Wipe down"]).status_code == 200
        assert items(client, headers, wo.id) == STEPS + ["Wipe down"]
        refused = complete(client, headers, wo.id, checklist_results=BOTH_TICKED)
        assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
        assert refused.json()["detail"].startswith("1 of 3 checklist items") and "(Wipe down)" in refused.text

        assert put_schedule(client, headers, schedule.id, ["Lubricate ways"]).status_code == 200
        assert items(client, headers, wo.id) == ["Lubricate ways"]
        # Still open, still legacy: the reads wrote nothing.
        assert stored_wo(db_session, wo.id).schedule_checklist_snapshot is None
        assert stored_wo(db_session, wo.id).status == MaintenanceStatus.IN_PROGRESS

    @pytest.mark.parametrize("path", ["all-ticked", "with-a-reason"])
    def test_completing_a_legacy_work_order_freezes_the_steps_it_was_judged_on(self, client, db_session, headers, path):
        schedule = make_schedule(db_session, next_due=company_today(db_session, 1), checklist=json.dumps(STEPS))
        wo = legacy_pm(db_session, schedule)
        if path == "all-ticked":
            done = complete(client, headers, wo.id, checklist_results=BOTH_TICKED)
        else:
            done = complete(client, headers, wo.id, checklist_results=FIRST_TICKED, **{REASON_FIELD: "Coolant out"})
        assert done.status_code == 200, done.text
        assert done.json()["schedule_checklist_items"] == STEPS
        assert stored_wo(db_session, wo.id).schedule_checklist_snapshot == json.dumps(STEPS)
        [row] = completion_audit(db_session, wo.id)
        # The frozen list is written by the completion, so its audit row records it.
        assert row.extra_data["schedule_checklist_snapshot"] == STEPS
        if path == "with-a-reason":
            assert row.extra_data["checklist_item_count"] == 2
            assert row.extra_data["checklist_unticked_items"] == ["Check coolant"]

        # The finished record no longer follows the schedule.
        assert (
            put_schedule(client, headers, schedule.id, ["Replace filter", "Wipe down", "Drain tank"]).status_code == 200
        )
        assert put_schedule(client, headers, schedule.id, None).status_code == 200
        assert items(client, headers, wo.id) == STEPS
        assert stored_wo(db_session, wo.id).schedule_checklist_snapshot == json.dumps(STEPS)

    def test_a_legacy_work_order_completed_on_an_empty_schedule_freezes_an_empty_list(
        self, client, db_session, headers
    ):
        schedule = make_schedule(db_session, next_due=company_today(db_session, 1), checklist=None)
        wo = legacy_pm(db_session, schedule)
        assert complete(client, headers, wo.id).status_code == 200
        assert stored_wo(db_session, wo.id).schedule_checklist_snapshot == "[]"
        [row] = completion_audit(db_session, wo.id)
        assert row.extra_data["schedule_checklist_snapshot"] == []
        assert put_schedule(client, headers, schedule.id, STEPS).status_code == 200
        assert items(client, headers, wo.id) == []

    def test_a_legacy_work_order_on_another_companys_schedule_copies_nothing(self, client, db_session, headers):
        """A pre-fix row may name a FOREIGN schedule id. Completion reads the schedule tenant-scoped, so
        company 2's steps are never judged, served or frozen into company 1's record."""
        foreign = make_schedule(
            db_session,
            company_id=2,
            next_due=company_today(db_session, 2),
            checklist=json.dumps(["ACME-SECRET step"]),
        )
        wo = make_raw_wo(db_session, company_id=1, schedule_id=foreign.id, status=MaintenanceStatus.IN_PROGRESS)
        assert items(client, headers, wo.id) == []
        done = complete(client, headers, wo.id)
        assert done.status_code == 200, done.text
        assert "ACME-SECRET" not in done.text
        assert stored_wo(db_session, wo.id).schedule_checklist_snapshot is None
        [row] = completion_audit(db_session, wo.id)
        assert "schedule_checklist_snapshot" not in row.extra_data and "ACME-SECRET" not in str(row.extra_data)
        db_session.expire_all()
        assert db_session.get(MaintenanceSchedule, foreign.id).last_completed_date is None


# --------------------------------------------------------------------------- (g) the token rule


class TestTheScheduleEditTokenRuleFollowsTheSnapshot:
    def test_a_token_may_take_steps_off_a_schedule_whose_open_work_order_is_snapshotted(
        self, client, db_session, headers, token_headers
    ):
        schedule, wo = open_pm(client, headers, db_session)
        trimmed = put_schedule(client, token_headers, schedule.id, ["Lubricate ways"])
        assert trimmed.status_code == 200, trimmed.text
        assert items(client, headers, wo["id"]) == STEPS
        assert complete(client, headers, wo["id"], checklist_results=FIRST_TICKED).status_code == 409

    def test_a_token_is_refused_while_a_legacy_work_order_is_open(self, client, db_session, headers, token_headers):
        schedule = make_schedule(db_session, next_due=company_today(db_session, 1), checklist=json.dumps(STEPS))
        wo = legacy_pm(db_session, schedule, status_=MaintenanceStatus.ON_HOLD)
        before = audit_count(db_session)
        refused = put_schedule(client, token_headers, schedule.id, ["Lubricate ways"])
        assert refused.status_code == status.HTTP_403_FORBIDDEN, refused.text
        assert refused.json()["detail"] == API_TOKEN_SCHEDULE_CHECKLIST_REMOVAL
        db_session.expire_all()
        assert db_session.get(MaintenanceSchedule, schedule.id).checklist == json.dumps(STEPS)
        assert audit_count(db_session) == before
        # Adding a step is still a token's to make.
        added = put_schedule(client, token_headers, schedule.id, STEPS + ["Wipe down"])
        assert added.status_code == 200, added.text
        assert items(client, headers, wo.id) == STEPS + ["Wipe down"]

    @pytest.mark.parametrize("ended", [MaintenanceStatus.COMPLETED, MaintenanceStatus.CANCELLED])
    def test_a_finished_legacy_work_order_does_not_block_a_token(
        self, client, db_session, headers, token_headers, ended
    ):
        schedule = make_schedule(db_session, next_due=company_today(db_session, 1), checklist=json.dumps(STEPS))
        legacy_pm(db_session, schedule, status_=ended)
        assert put_schedule(client, token_headers, schedule.id, None).status_code == 200

    def test_another_companys_legacy_row_on_this_schedule_never_blocks_a_token(
        self, client, db_session, headers, token_headers
    ):
        """``company_id`` is part of no FK, so a pre-fix company-2 row may name company 1's schedule.
        The probe is tenant-scoped: it neither refuses company 1's token nor names that row."""
        schedule = make_schedule(db_session, next_due=company_today(db_session, 1), checklist=json.dumps(STEPS))
        foreign = make_raw_wo(db_session, company_id=2, schedule_id=schedule.id, status=MaintenanceStatus.IN_PROGRESS)
        trimmed = put_schedule(client, token_headers, schedule.id, None)
        assert trimmed.status_code == 200, trimmed.text
        assert foreign.wo_number not in trimmed.text


# --------------------------------------------------------------------------- (g) the PM import


def monthly_rows(tasks, code="CNC-02"):
    return [{"work_center_code": code, "task": task, "frequency": "monthly"} for task in tasks]


@pytest.fixture
def lathe(db_session):
    return make_wc(db_session, code="CNC-02")


@pytest.fixture
def lathe_schedule(db_session, lathe):
    """An ACTIVE schedule the sheet's ``CNC-02 Monthly PM`` group upserts, carrying ``STEPS``."""
    schedule = MaintenanceSchedule(
        company_id=1,
        work_center_id=lathe.id,
        name="CNC-02 Monthly PM",
        frequency=MaintenanceFrequency.MONTHLY,
        checklist=json.dumps(STEPS),
        next_due_date=date(2026, 12, 1),
        is_active=True,
    )
    db_session.add(schedule)
    db_session.commit()
    db_session.refresh(schedule)
    return schedule


def import_audit(db: Session):
    db.expire_all()
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.resource_type.in_(["maintenance_schedule", "maintenance_asset", "maintenance_schedule_import"])
        )
        .count()
    )


def preview(client, headers, content):
    resp = upload(client, PREVIEW, headers, content)
    assert resp.status_code == 200, resp.text
    return resp.json()


def commit(client, headers, content, body):
    return upload(client, COMMIT, headers, content, expected_source_hash=body["source_hash"])


def warnings_of(body):
    return [warning for row in body["rows"] for warning in row["warnings"]]


def stored_checklist(db: Session, schedule_id: int):
    db.expire_all()
    return json.loads(db.get(MaintenanceSchedule, schedule_id).checklist)


class TestThePmImportBindsTheNextWorkOrder:
    def test_a_snapshotted_open_work_order_is_named_in_a_warning_and_keeps_its_steps(
        self, client, db_session, headers, lathe_schedule
    ):
        created = client.post(WORK_ORDERS, headers=headers, json={"schedule_id": lathe_schedule.id, "title": "PM"})
        wo = created.json()
        content = xlsx(monthly_rows(["Lubricate ways", "Drain tank"]))  # removes a step, adds one

        planned = preview(client, headers, content)
        [warning] = [w for w in warnings_of(planned) if wo["wo_number"] in w]
        assert "keeps the checklist it was opened with" in warning
        assert planned["summary"]["error_count"] == 0

        done = commit(client, headers, content, planned)
        assert done.status_code == 200, done.text
        assert any(wo["wo_number"] in w for w in warnings_of(done.json()))  # the commit's own probe agrees
        assert stored_checklist(db_session, lathe_schedule.id) == ["Lubricate ways", "Drain tank"]
        assert items(client, headers, wo["id"]) == STEPS
        refused = complete(client, headers, wo["id"])
        assert refused.json()["detail"].startswith("2 of 2 checklist items")

        # A re-run of the same sheet is still a no-op: nothing changes, nothing to warn about.
        before = import_audit(db_session)
        again = preview(client, headers, content)
        assert {row["outcome"] for row in again["schedules"]} == {"unchanged"}
        assert not any(wo["wo_number"] in w for w in warnings_of(again))
        assert commit(client, headers, content, again).status_code == 200
        assert import_audit(db_session) == before

    def test_a_token_may_take_steps_away_under_a_snapshotted_work_order(
        self, client, db_session, headers, token_headers, lathe_schedule
    ):
        created = client.post(WORK_ORDERS, headers=headers, json={"schedule_id": lathe_schedule.id, "title": "PM"})
        content = xlsx(monthly_rows(["Lubricate ways"]))
        planned = preview(client, token_headers, content)
        assert planned["summary"]["error_count"] == 0
        done = commit(client, token_headers, content, planned)
        assert done.status_code == 200, done.text
        assert stored_checklist(db_session, lathe_schedule.id) == ["Lubricate ways"]
        assert items(client, headers, created.json()["id"]) == STEPS

    def test_a_legacy_open_work_order_warns_a_person_and_follows_the_import(
        self, client, db_session, headers, lathe_schedule
    ):
        wo = legacy_pm(db_session, lathe_schedule)
        content = xlsx(monthly_rows(["Lubricate ways"]))
        planned = preview(client, headers, content)
        [warning] = [w for w in warnings_of(planned) if wo.wo_number in w]
        assert "opened before checklist snapshots" in warning
        assert planned["summary"]["error_count"] == 0

        done = commit(client, headers, content, planned)
        assert done.status_code == 200, done.text
        assert stored_checklist(db_session, lathe_schedule.id) == ["Lubricate ways"]
        assert items(client, headers, wo.id) == ["Lubricate ways"]  # still judged live

    def test_a_token_taking_steps_away_under_a_legacy_work_order_is_refused_and_writes_nothing(
        self, client, db_session, headers, token_headers, lathe_schedule
    ):
        wo = legacy_pm(db_session, lathe_schedule)
        rows = monthly_rows(["Lubricate ways", "Drain tank"]) + [
            {"work_center_code": "CNC-02", "task": "Replace batteries", "frequency": "annual"}
        ]
        content = xlsx(rows)
        schedules_before = db_session.query(MaintenanceSchedule).count()
        audit_before = audit_count(db_session)

        planned = preview(client, token_headers, content)
        by_row = {row["row"]: row for row in planned["rows"]}
        for row_number in (2, 3):  # every row of the Monthly group
            assert by_row[row_number]["outcome"] == "error", by_row[row_number]
            assert by_row[row_number]["error"].startswith(API_TOKEN_SCHEDULE_CHECKLIST_REMOVAL)
            assert wo.wo_number in by_row[row_number]["error"]
        assert by_row[4]["error"] is None  # the Annual group is untouched by the rule

        refused = commit(client, token_headers, content, planned)
        assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
        errors = refused.json()["detail"]["errors"]
        assert sorted(error["row"] for error in errors) == [2, 3]
        assert all(error["error"].startswith(API_TOKEN_SCHEDULE_CHECKLIST_REMOVAL) for error in errors)
        # All-or-nothing: the schedule unchanged, the Annual schedule not created, no audit row.
        assert stored_checklist(db_session, lathe_schedule.id) == STEPS
        assert db_session.query(MaintenanceSchedule).count() == schedules_before
        assert audit_count(db_session) == audit_before
        assert items(client, headers, wo.id) == STEPS

        # The refusal is about the credential: the same person, signed in, imports the sheet.
        person = preview(client, headers, content)
        assert person["summary"]["error_count"] == 0
        assert commit(client, headers, content, person).status_code == 200
        assert stored_checklist(db_session, lathe_schedule.id) == ["Lubricate ways", "Drain tank"]

    def test_a_token_only_adding_steps_under_a_legacy_work_order_is_allowed(
        self, client, db_session, headers, token_headers, lathe_schedule
    ):
        wo = legacy_pm(db_session, lathe_schedule)
        content = xlsx(monthly_rows(STEPS + ["Drain tank"]))
        planned = preview(client, token_headers, content)
        assert planned["summary"]["error_count"] == 0
        assert any(wo.wo_number in w and "opened before checklist snapshots" in w for w in warnings_of(planned))
        done = commit(client, token_headers, content, planned)
        assert done.status_code == 200, done.text
        assert items(client, headers, wo.id) == STEPS + ["Drain tank"]

        before = import_audit(db_session)
        again = preview(client, token_headers, content)
        assert {row["outcome"] for row in again["schedules"]} == {"unchanged"}
        assert warnings_of(again) == []
        assert commit(client, token_headers, content, again).status_code == 200
        assert import_audit(db_session) == before

    @pytest.mark.parametrize("ended", [MaintenanceStatus.COMPLETED, MaintenanceStatus.CANCELLED])
    def test_a_finished_legacy_work_order_neither_warns_nor_blocks(
        self, client, db_session, token_headers, lathe_schedule, ended
    ):
        wo = legacy_pm(db_session, lathe_schedule, status_=ended)
        content = xlsx(monthly_rows(["Lubricate ways"]))
        planned = preview(client, token_headers, content)
        assert planned["summary"]["error_count"] == 0
        assert not any(wo.wo_number in w for w in warnings_of(planned))
        assert commit(client, token_headers, content, planned).status_code == 200

    def test_another_companys_legacy_row_on_this_schedule_never_blocks_or_leaks(
        self, client, db_session, token_headers, lathe_schedule
    ):
        foreign = make_raw_wo(
            db_session, company_id=2, schedule_id=lathe_schedule.id, status=MaintenanceStatus.IN_PROGRESS
        )
        content = xlsx(monthly_rows(["Lubricate ways"]))
        planned = preview(client, token_headers, content)
        assert planned["summary"]["error_count"] == 0
        assert foreign.wo_number not in json.dumps(planned)
        done = commit(client, token_headers, content, planned)
        assert done.status_code == 200, done.text
        assert foreign.wo_number not in done.text


# --------------------------------------------------------------------------- (i) one open per schedule


def raw_wo(db: Session, schedule: MaintenanceSchedule, status_: MaintenanceStatus, *, company_id=None, **kwargs):
    """A work order inserted straight through the ORM -- the "future writer" the index backstops."""
    wo = MaintenanceWorkOrder(
        company_id=company_id if company_id is not None else schedule.company_id,
        schedule_id=schedule.id,
        work_center_id=schedule.work_center_id,
        wo_number=f"MWO-IDX-{_next():05d}",
        title="Synthetic PM",
        status=status_,
        requires_shutdown=False,
        **kwargs,
    )
    db.add(wo)
    db.commit()
    return wo


class TestOneOpenWorkOrderPerScheduleInTheDatabase:
    @pytest.mark.parametrize("first", OPEN_MAINTENANCE_STATUSES, ids=lambda s: s.name)
    @pytest.mark.parametrize("second", OPEN_MAINTENANCE_STATUSES, ids=lambda s: s.name)
    def test_a_second_open_work_order_on_one_schedule_is_refused(self, db_session, first, second):
        schedule = make_schedule(db_session, next_due=company_today(db_session, 1))
        raw_wo(db_session, schedule, first)
        with pytest.raises(
            IntegrityError, match="maintenance_work_orders.company_id, maintenance_work_orders.schedule_id"
        ):
            raw_wo(db_session, schedule, second)
        db_session.rollback()
        assert work_order_count(db_session) == 1

    @pytest.mark.parametrize("ended", [MaintenanceStatus.COMPLETED, MaintenanceStatus.CANCELLED])
    def test_finished_siblings_are_allowed(self, db_session, ended):
        schedule = make_schedule(db_session, next_due=company_today(db_session, 1))
        raw_wo(db_session, schedule, ended)
        raw_wo(db_session, schedule, ended)
        raw_wo(db_session, schedule, MaintenanceStatus.IN_PROGRESS)
        assert work_order_count(db_session) == 3

    def test_moving_a_finished_row_back_open_beside_an_open_one_is_refused(self, db_session):
        """The index covers UPDATE as well as INSERT."""
        schedule = make_schedule(db_session, next_due=company_today(db_session, 1))
        finished = raw_wo(db_session, schedule, MaintenanceStatus.COMPLETED)
        raw_wo(db_session, schedule, MaintenanceStatus.SCHEDULED)
        finished.status = MaintenanceStatus.ON_HOLD
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

    def test_other_schedules_and_unscheduled_work_orders_are_unaffected(self, db_session):
        wc = make_wc(db_session)
        one = make_schedule(db_session, next_due=company_today(db_session, 1), work_center_id=wc.id)
        two = make_schedule(db_session, next_due=company_today(db_session, 1), work_center_id=wc.id)
        raw_wo(db_session, one, MaintenanceStatus.IN_PROGRESS)
        raw_wo(db_session, two, MaintenanceStatus.IN_PROGRESS)
        for _ in range(3):  # corrective work orders carry no schedule
            db_session.add(
                MaintenanceWorkOrder(
                    company_id=1,
                    work_center_id=wc.id,
                    wo_number=f"MWO-IDX-{_next():05d}",
                    title="Corrective",
                    status=MaintenanceStatus.SCHEDULED,
                )
            )
        db_session.commit()
        assert work_order_count(db_session) == 5

    def test_the_index_is_per_company(self, db_session):
        """A pre-fix company-2 row naming company 1's schedule id (``company_id`` is part of no FK)
        neither blocks company 1's own PM nor is blocked by it."""
        schedule = make_schedule(db_session, next_due=company_today(db_session, 1))
        make_wc(db_session, company_id=2)  # company 2 exists
        raw_wo(db_session, schedule, MaintenanceStatus.IN_PROGRESS)
        raw_wo(db_session, schedule, MaintenanceStatus.IN_PROGRESS, company_id=2)
        assert work_order_count(db_session) == 2


def skip_the_open_work_order_pre_check(monkeypatch, *, then_real: bool):
    """``create_work_order``'s pre-check finds no open work order (the writer that skipped it), so the
    flush reaches the index; with ``then_real`` the handler's re-probe after the rollback is real."""
    real = maintenance_endpoints._open_work_order_for_schedule
    calls = {"n": 0}

    def blind_first(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1 or not then_real:
            return None
        return real(*args, **kwargs)

    monkeypatch.setattr(maintenance_endpoints, "_open_work_order_for_schedule", blind_first)
    return calls


class TestTheApiAnswersTheIndexWith409:
    def _skip_the_pre_check(self, monkeypatch, *, then_real: bool):
        return skip_the_open_work_order_pre_check(monkeypatch, then_real=then_real)

    def test_the_flush_conflict_is_a_409_naming_the_open_work_order(self, client, db_session, headers, monkeypatch):
        schedule, first = open_pm(client, headers, db_session)
        count, audits = work_order_count(db_session), audit_count(db_session)
        calls = self._skip_the_pre_check(monkeypatch, then_real=True)

        refused = client.post(WORK_ORDERS, headers=headers, json={"schedule_id": schedule.id, "title": "PM"})

        assert calls["n"] == 2  # skipped the pre-check, then named the survivor after the rollback
        assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
        detail = refused.json()["detail"]
        assert isinstance(detail, str)
        assert schedule.name in detail and f"{first['wo_number']} (scheduled)" in detail
        assert work_order_count(db_session) == count and audit_count(db_session) == audits

    def test_without_a_survivor_to_name_the_409_says_try_again(self, client, db_session, headers, monkeypatch):
        schedule, first = open_pm(client, headers, db_session)
        count, audits = work_order_count(db_session), audit_count(db_session)
        self._skip_the_pre_check(monkeypatch, then_real=False)

        refused = client.post(WORK_ORDERS, headers=headers, json={"schedule_id": schedule.id, "title": "PM"})

        assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
        assert "Nothing was changed" in refused.json()["detail"]
        assert work_order_count(db_session) == count and audit_count(db_session) == audits
        # The session is usable afterwards: the open work order is still there and completes.
        assert complete(client, headers, first["id"], checklist_results=BOTH_TICKED).status_code == 200


# --------------------------------------------------------------------------- (a) completion notes


def completion_row(db: Session, wo_id: int):
    [row] = completion_audit(db, wo_id)
    return row


class TestCompletionNotes:
    def open_with_notes(self, client, headers, db_session, notes="Planner: use the long ladder"):
        wc = make_wc(db_session)
        created = client.post(WORK_ORDERS, headers=headers, json={"work_center_id": wc.id, "title": "Corrective"})
        assert created.status_code == 200, created.text
        wo_id = created.json()["id"]
        if notes is not None:
            edited = client.put(f"{WORK_ORDERS}/{wo_id}", headers=headers, json={"notes": notes})
            assert edited.status_code == 200, edited.text
        return wo_id

    def test_omitted_notes_keep_the_planners_and_are_not_in_the_audit_row(self, client, db_session, headers):
        wo_id = self.open_with_notes(client, headers, db_session)
        done = complete(client, headers, wo_id, findings="Ways dry")
        assert done.status_code == 200, done.text
        assert done.json()["notes"] == "Planner: use the long ladder"
        assert stored_wo(db_session, wo_id).notes == "Planner: use the long ladder"
        row = completion_row(db_session, wo_id)
        assert row.old_values == {"status": "scheduled"} and row.new_values == {"status": "completed"}

    def test_sent_notes_replace_and_the_audit_row_keeps_both(self, client, db_session, headers):
        wo_id = self.open_with_notes(client, headers, db_session)
        done = complete(client, headers, wo_id, notes="Tech: replaced the wiper")
        assert done.status_code == 200, done.text
        assert stored_wo(db_session, wo_id).notes == "Tech: replaced the wiper"
        row = completion_row(db_session, wo_id)
        assert row.old_values == {"status": "scheduled", "notes": "Planner: use the long ladder"}
        assert row.new_values == {"status": "completed", "notes": "Tech: replaced the wiper"}

    def test_an_explicit_null_clears_and_is_recorded(self, client, db_session, headers):
        wo_id = self.open_with_notes(client, headers, db_session)
        done = complete(client, headers, wo_id, notes=None)
        assert done.status_code == 200, done.text
        assert stored_wo(db_session, wo_id).notes is None
        row = completion_row(db_session, wo_id)
        assert row.old_values == {"status": "scheduled", "notes": "Planner: use the long ladder"}
        assert row.new_values == {"status": "completed", "notes": None}

    def test_resending_the_same_notes_is_no_change(self, client, db_session, headers):
        wo_id = self.open_with_notes(client, headers, db_session)
        assert complete(client, headers, wo_id, notes="Planner: use the long ladder").status_code == 200
        row = completion_row(db_session, wo_id)
        assert "notes" not in row.old_values and "notes" not in row.new_values

    def test_notes_on_a_work_order_that_had_none_are_recorded(self, client, db_session, headers):
        wo_id = self.open_with_notes(client, headers, db_session, notes=None)
        assert complete(client, headers, wo_id, notes="Tech: all good").status_code == 200
        row = completion_row(db_session, wo_id)
        assert row.old_values == {"status": "scheduled", "notes": None}
        assert row.new_values == {"status": "completed", "notes": "Tech: all good"}

    def test_a_refused_completion_leaves_the_notes_alone(self, client, db_session, headers):
        schedule, wo = open_pm(client, headers, db_session)
        assert (
            client.put(f"{WORK_ORDERS}/{wo['id']}", headers=headers, json={"notes": "Planner note"}).status_code == 200
        )
        before = audit_count(db_session)
        refused = complete(client, headers, wo["id"], notes="Tech note", checklist_results=FIRST_TICKED)
        assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
        stored = stored_wo(db_session, wo["id"])
        assert stored.notes == "Planner note" and stored.status == MaintenanceStatus.SCHEDULED
        assert audit_count(db_session) == before

    def test_the_required_audit_path_records_the_notes_too(self, client, db_session, headers):
        schedule, wo = open_pm(client, headers, db_session)
        assert (
            client.put(f"{WORK_ORDERS}/{wo['id']}", headers=headers, json={"notes": "Planner note"}).status_code == 200
        )
        done = complete(
            client,
            headers,
            wo["id"],
            notes="Tech note",
            checklist_results=FIRST_TICKED,
            **{REASON_FIELD: "Coolant concentrate on back order"},
        )
        assert done.status_code == 200, done.text
        row = completion_row(db_session, wo["id"])
        assert row.extra_data["incomplete_checklist_reason"] == "Coolant concentrate on back order"
        assert row.old_values == {"status": "scheduled", "notes": "Planner note"}
        assert row.new_values == {"status": "completed", "notes": "Tech note"}

    def test_a_failed_required_audit_keeps_the_old_notes(self, client, db_session, headers, monkeypatch):
        """The replaced notes survive only in the audit row, so the row failing must roll the
        replacement back with the completion (503)."""
        schedule, wo = open_pm(client, headers, db_session)
        assert (
            client.put(f"{WORK_ORDERS}/{wo['id']}", headers=headers, json={"notes": "Planner note"}).status_code == 200
        )
        fail_required_audit_for_work_orders(monkeypatch)
        failed = complete(
            client, headers, wo["id"], notes="Tech note", checklist_results=FIRST_TICKED, **{REASON_FIELD: "x"}
        )
        assert failed.status_code == status.HTTP_503_SERVICE_UNAVAILABLE, failed.text
        stored = stored_wo(db_session, wo["id"])
        assert stored.notes == "Planner note" and stored.status == MaintenanceStatus.SCHEDULED


# --------------------------------------------------------------------------- (j) the MWO number

# 9 PM on Dec 31 in Chicago (CST, UTC-6) is already 3 AM on Jan 1 in UTC.
NEW_YEARS_EVE_UTC = real_datetime(2027, 1, 1, 3, 0, tzinfo=timezone.utc)


@pytest.fixture
def new_years_eve(monkeypatch, db_session):
    """Pin every clock the endpoint can read to :data:`NEW_YEARS_EVE_UTC`; company 1 is in Chicago."""
    company = db_session.get(Company, 1)
    company.timezone = "America/Chicago"
    db_session.commit()
    monkeypatch.setattr(_FrozenClock, "instant", NEW_YEARS_EVE_UTC)
    monkeypatch.setattr(maintenance_endpoints, "datetime", FrozenDatetime)
    monkeypatch.setattr(maintenance_endpoints, "date", FrozenDate)
    monkeypatch.setattr(time_utils, "datetime", FrozenDatetime)


def seed_number(db: Session, wo_number: str, *, company_id: int = 1):
    wc = make_wc(db, company_id=company_id)
    db.add(
        MaintenanceWorkOrder(
            company_id=company_id,
            work_center_id=wc.id,
            wo_number=wo_number,
            title="Synthetic history",
            status=MaintenanceStatus.COMPLETED,
        )
    )
    db.commit()


def create_corrective(client, headers, db: Session):
    wc = make_wc(db)
    created = client.post(WORK_ORDERS, headers=headers, json={"work_center_id": wc.id, "title": "Corrective"})
    assert created.status_code == 200, created.text
    return created.json()


class TestTheWorkOrderNumber:
    def test_the_year_is_the_companys_not_the_servers(self, client, db_session, headers, new_years_eve):
        assert company_today(db_session, 1) == date(2026, 12, 31)
        wo = create_corrective(client, headers, db_session)
        assert wo["wo_number"] == "MWO-2026-0001"
        schedule = make_schedule(db_session, next_due=date(2026, 12, 31))
        pm = client.post(WORK_ORDERS, headers=headers, json={"schedule_id": schedule.id, "title": "PM"})
        assert pm.json()["wo_number"] == "MWO-2026-0002"

    @pytest.mark.parametrize(
        "history",
        [
            ["MWO-2026-0010", "MWO-2026-0009", "MWO-2026-LEGACY"],
            ["MWO-2026-LEGACY", "MWO-2026-0009", "MWO-2026-0010"],
            ["MWO-2026-0009", "MWO-2026-LEGACY", "MWO-2026-0010", "MWO-2026-0010A"],
        ],
        ids=["legacy-last", "legacy-first", "mixed"],
    )
    def test_the_next_number_follows_the_highest_numeric_suffix(
        self, client, db_session, headers, new_years_eve, history
    ):
        """Insert (id) order and a non-numeric legacy suffix change nothing: the old scan took the
        row with the highest id, so ``LEGACY`` last restarted the year at 0001 and ``0009`` last
        produced ``0010`` -- both a duplicate of an existing number."""
        for number in history:
            seed_number(db_session, number)
        assert create_corrective(client, headers, db_session)["wo_number"] == "MWO-2026-0011"

    def test_other_years_and_other_companies_numbers(self, client, db_session, headers, new_years_eve):
        """Another year's numbers are not this year's sequence; another company's ARE (``wo_number``
        is unique install-wide, so the scan is deliberately not tenant-scoped)."""
        seed_number(db_session, "MWO-2027-0050")
        seed_number(db_session, "MWO-2025-0099")
        seed_number(db_session, "MWO-2026-0004", company_id=2)
        wo = create_corrective(client, headers, db_session)
        assert wo["wo_number"] == "MWO-2026-0005"
        assert "MWO-2026-0004" not in json.dumps(client.get(WORK_ORDERS, headers=headers).json())

    def test_allocation_takes_the_install_wide_generator_lock_after_the_schedule_lock(
        self, client, db_session, headers, monkeypatch
    ):
        events = []
        real_lock = maintenance_endpoints.acquire_generator_lock
        real_for_update = Query.with_for_update

        def lock_spy(*args, **kwargs):
            events.append(("generator", args[1:], kwargs))
            return real_lock(*args, **kwargs)

        def for_update_spy(self, *args, **kwargs):
            locked = real_for_update(self, *args, **kwargs)
            events.append(("row", str(locked.statement.compile(dialect=postgresql.dialect())), None))
            return locked

        schedule = make_schedule(db_session, next_due=company_today(db_session, 1))
        monkeypatch.setattr(maintenance_endpoints, "acquire_generator_lock", lock_spy)
        monkeypatch.setattr(Query, "with_for_update", for_update_spy)

        created = client.post(WORK_ORDERS, headers=headers, json={"schedule_id": schedule.id, "title": "PM"})

        assert created.status_code == 200, created.text
        generator = [event for event in events if event[0] == "generator"]
        # Namespace only: no company id, because the number is unique across companies.
        assert generator == [("generator", ("maintenance_work_order_number",), {})]
        schedule_lock = next(
            i for i, event in enumerate(events) if event[0] == "row" and "FROM maintenance_schedules" in event[1]
        )
        assert schedule_lock < events.index(generator[0])


# --------------------------------------------------------------------------- (k) row locks


def spy_row_locks(monkeypatch):
    """Every ``Query.with_for_update`` the request builds, compiled for PostgreSQL, in order."""
    built = []
    original = Query.with_for_update

    def spy(self, *args, **kwargs):
        locked = original(self, *args, **kwargs)
        built.append(locked)
        return locked

    monkeypatch.setattr(Query, "with_for_update", spy)
    return lambda: [str(query.statement.compile(dialect=postgresql.dialect())) for query in built]


def locks_table(sql: str, table: str) -> bool:
    """``sql`` row-locks ``table``'s rows: a single-table ``SELECT ... FROM table`` ending in ``FOR UPDATE``
    (which on PostgreSQL locks exactly that table's rows), or a ``FOR UPDATE OF table`` when the
    statement joins (a bare ``FOR UPDATE`` over a join would lock every joined table too)."""
    if f"FOR UPDATE OF {table}" in sql:
        return True
    return f"FROM {table} " in f"{sql} " and "JOIN" not in sql and sql.rstrip().endswith("FOR UPDATE")


def lock_positions(compiled, table):
    return [i for i, sql in enumerate(compiled) if locks_table(sql, table)]


class TestTheScheduleIsRowLocked:
    def test_start_pm_locks_the_schedule(self, client, db_session, headers, monkeypatch):
        schedule = make_schedule(db_session, next_due=company_today(db_session, 1))
        compiled = spy_row_locks(monkeypatch)
        created = client.post(WORK_ORDERS, headers=headers, json={"schedule_id": schedule.id, "title": "PM"})
        assert created.status_code == 200, created.text
        assert lock_positions(compiled(), "maintenance_schedules"), compiled()

    def test_completion_locks_the_work_order_then_the_schedule(self, client, db_session, headers, monkeypatch):
        schedule, wo = open_pm(client, headers, db_session)
        compiled = spy_row_locks(monkeypatch)
        done = complete(client, headers, wo["id"], checklist_results=BOTH_TICKED)
        assert done.status_code == 200, done.text
        sql = compiled()
        work_order_locks = lock_positions(sql, "maintenance_work_orders")
        schedule_locks = lock_positions(sql, "maintenance_schedules")
        assert work_order_locks and schedule_locks, sql
        assert work_order_locks[0] < schedule_locks[0], sql

    def test_the_schedule_edit_locks_the_schedule(self, client, db_session, headers, monkeypatch):
        schedule = make_schedule(db_session, next_due=company_today(db_session, 1), checklist=json.dumps(STEPS))
        compiled = spy_row_locks(monkeypatch)
        edited = put_schedule(client, headers, schedule.id, ["Lubricate ways"])
        assert edited.status_code == 200, edited.text
        assert lock_positions(compiled(), "maintenance_schedules"), compiled()

    def test_the_import_commit_locks_the_schedules_before_reading_open_work_orders(
        self, client, db_session, headers, lathe_schedule, monkeypatch
    ):
        legacy_pm(db_session, lathe_schedule)
        content = xlsx(monthly_rows(STEPS + ["Drain tank"]))
        planned = preview(client, headers, content)
        compiled = spy_row_locks(monkeypatch)
        executed = []
        real_all = Query.all

        def all_spy(self):
            executed.append(str(self.statement.compile(dialect=postgresql.dialect())))
            return real_all(self)

        monkeypatch.setattr(Query, "all", all_spy)
        done = commit(client, headers, content, planned)
        assert done.status_code == 200, done.text
        assert lock_positions(compiled(), "maintenance_schedules"), compiled()
        locked_read = next(i for i, sql in enumerate(executed) if locks_table(sql, "maintenance_schedules"))
        open_probe = next(
            i
            for i, sql in enumerate(executed)
            if "FROM maintenance_work_orders" in sql and "schedule_id IS NOT NULL" in sql
        )
        assert locked_read < open_probe, executed


# --------------------------------------------------------------------------- (a) replaced notes are evidence


def pm_with_notes(client, headers, db: Session, notes="Planner note"):
    """A PM work order (two schedule steps) carrying ``notes`` written by a planner's PUT."""
    schedule, wo = open_pm(client, headers, db)
    if notes is not None:
        edited = client.put(f"{WORK_ORDERS}/{wo['id']}", headers=headers, json={"notes": notes})
        assert edited.status_code == 200, edited.text
    return schedule, wo


REPLACING = [("Tech note", "Tech note"), (None, None), ("   ", None), ("", None)]
REPLACING_IDS = ["replace", "null", "blank", "empty"]


class TestReplacedNotesAreRequiredEvidence:
    """The notes a completion replaces or clears survive only in its audit row, so that row is REQUIRED
    (``log_required`` inside ``required_audit``: 503, nothing saved) even with every item ticked. Adding
    notes where there were none, or keeping them, stays the best-effort row. Blank text is "no notes"
    (stored NULL)."""

    @pytest.mark.parametrize("sent,stored", REPLACING, ids=REPLACING_IDS)
    def test_a_failed_audit_write_keeps_the_old_notes_and_the_open_work_order(
        self, client, db_session, headers, monkeypatch, sent, stored
    ):
        schedule, wo = pm_with_notes(client, headers, db_session)
        before = stored_state(db_session, wo["id"], schedule.id)
        assert before["notes"] == "Planner note"
        fail_required_audit_for_work_orders(monkeypatch)

        failed = complete(client, headers, wo["id"], checklist_results=BOTH_TICKED, notes=sent)

        assert failed.status_code == status.HTTP_503_SERVICE_UNAVAILABLE, failed.text
        assert "could not be saved" in failed.json()["detail"] and wo["wo_number"] in failed.json()["detail"]
        # Nothing saved: still open, the planner's notes intact, the schedule not advanced, no log
        # entry and no audit row (not even the schedule's roll-forward row).
        assert stored_state(db_session, wo["id"], schedule.id) == before
        assert completion_audit(db_session, wo["id"]) == []

    @pytest.mark.parametrize("sent,stored", REPLACING, ids=REPLACING_IDS)
    def test_with_the_audit_trail_up_the_row_records_old_and_new(self, client, db_session, headers, sent, stored):
        schedule, wo = pm_with_notes(client, headers, db_session)
        done = complete(client, headers, wo["id"], checklist_results=BOTH_TICKED, notes=sent)
        assert done.status_code == 200, done.text
        assert stored_wo(db_session, wo["id"]).notes == stored
        row = completion_row(db_session, wo["id"])
        assert row.old_values == {"status": "scheduled", "notes": "Planner note"}
        assert row.new_values == {"status": "completed", "notes": stored}
        assert "checklist_unticked_items" not in row.extra_data

    @pytest.mark.parametrize("body", [{}, {"notes": "Planner note"}], ids=["omitted", "resent"])
    def test_keeping_the_notes_stays_best_effort(self, client, db_session, headers, monkeypatch, body):
        schedule, wo = pm_with_notes(client, headers, db_session)
        fail_required_audit_for_work_orders(monkeypatch)
        done = complete(client, headers, wo["id"], checklist_results=BOTH_TICKED, **body)
        assert done.status_code == 200, done.text
        assert stored_wo(db_session, wo["id"]).notes == "Planner note"
        row = completion_row(db_session, wo["id"])
        assert row.old_values == {"status": "scheduled"} and row.new_values == {"status": "completed"}

    def test_adding_notes_where_there_were_none_stays_best_effort(self, client, db_session, headers, monkeypatch):
        schedule, wo = pm_with_notes(client, headers, db_session, notes=None)
        fail_required_audit_for_work_orders(monkeypatch)
        done = complete(client, headers, wo["id"], checklist_results=BOTH_TICKED, notes="Tech note")
        assert done.status_code == 200, done.text
        row = completion_row(db_session, wo["id"])
        assert row.old_values == {"status": "scheduled", "notes": None}
        assert row.new_values == {"status": "completed", "notes": "Tech note"}

    @pytest.mark.parametrize("blank", ["   ", "", "\n\t "], ids=["spaces", "empty", "whitespace"])
    def test_blank_notes_on_a_work_order_without_notes_are_no_change(
        self, client, db_session, headers, monkeypatch, blank
    ):
        schedule, wo = pm_with_notes(client, headers, db_session, notes=None)
        fail_required_audit_for_work_orders(monkeypatch)  # no change is no required row either
        done = complete(client, headers, wo["id"], checklist_results=BOTH_TICKED, notes=blank)
        assert done.status_code == 200, done.text
        assert stored_wo(db_session, wo["id"]).notes is None
        row = completion_row(db_session, wo["id"])
        assert row.old_values == {"status": "scheduled"} and row.new_values == {"status": "completed"}


# --------------------------------------------------------------------------- (d) a legacy cancel freezes too


def cancel(client, headers, wo_id: int):
    return client.put(f"{WORK_ORDERS}/{wo_id}", headers=headers, json={"status": "cancelled"})


def update_audit(db: Session, wo_id: int):
    db.expire_all()
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.resource_type == "maintenance_work_order",
            AuditLog.resource_id == wo_id,
            AuditLog.action == "UPDATE",
        )
        .order_by(AuditLog.id)
        .all()
    )


class TestCancellingALegacyWorkOrderFreezesItsSteps:
    """``PUT {"status": "cancelled"}`` on a work order opened before migration 132 writes the snapshot
    from the schedule's CURRENT steps (the PUT's ``log_update`` row records NULL -> the steps), so the
    final record stops following the schedule -- as completing one does."""

    @pytest.mark.parametrize("open_status", OPEN_MAINTENANCE_STATUSES, ids=lambda s: s.name)
    def test_the_cancel_writes_and_audits_the_snapshot(self, client, db_session, headers, open_status):
        schedule = make_schedule(db_session, next_due=company_today(db_session, 1), checklist=json.dumps(STEPS))
        wo = legacy_pm(db_session, schedule, status_=open_status)

        cancelled = cancel(client, headers, wo.id)

        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["schedule_checklist_items"] == STEPS
        assert stored_wo(db_session, wo.id).schedule_checklist_snapshot == json.dumps(STEPS)
        [row] = update_audit(db_session, wo.id)
        assert row.old_values["schedule_checklist_snapshot"] is None
        assert row.new_values["schedule_checklist_snapshot"] == json.dumps(STEPS)
        assert row.new_values["status"] == "cancelled"

        assert put_schedule(client, headers, schedule.id, ["Replace filter"]).status_code == 200
        assert put_schedule(client, headers, schedule.id, None).status_code == 200
        assert items(client, headers, wo.id) == STEPS

    def test_a_legacy_work_order_on_an_empty_schedule_freezes_an_empty_list(self, client, db_session, headers):
        schedule = make_schedule(db_session, next_due=company_today(db_session, 1), checklist=None)
        wo = legacy_pm(db_session, schedule, status_=MaintenanceStatus.SCHEDULED)
        assert cancel(client, headers, wo.id).status_code == 200
        assert stored_wo(db_session, wo.id).schedule_checklist_snapshot == "[]"
        assert put_schedule(client, headers, schedule.id, STEPS).status_code == 200
        assert items(client, headers, wo.id) == []

    def test_cancelling_a_snapshotted_work_order_leaves_its_snapshot(self, client, db_session, headers):
        schedule, wo = open_pm(client, headers, db_session)
        assert put_schedule(client, headers, schedule.id, ["Replace filter"]).status_code == 200
        assert cancel(client, headers, wo["id"]).status_code == 200
        assert stored_wo(db_session, wo["id"]).schedule_checklist_snapshot == json.dumps(STEPS)
        [row] = update_audit(db_session, wo["id"])
        # ``log_update`` stores the whole row: the snapshot is in both, unchanged.
        assert row.old_values["schedule_checklist_snapshot"] == json.dumps(STEPS)
        assert row.new_values["schedule_checklist_snapshot"] == json.dumps(STEPS)
        assert items(client, headers, wo["id"]) == STEPS

    def test_a_legacy_work_order_on_another_companys_schedule_copies_nothing(self, client, db_session, headers):
        foreign = make_schedule(
            db_session,
            company_id=2,
            next_due=company_today(db_session, 2),
            checklist=json.dumps(["ACME-SECRET step"]),
        )
        wo = make_raw_wo(db_session, company_id=1, schedule_id=foreign.id, status=MaintenanceStatus.SCHEDULED)
        cancelled = cancel(client, headers, wo.id)
        assert cancelled.status_code == 200, cancelled.text
        assert "ACME-SECRET" not in cancelled.text
        assert stored_wo(db_session, wo.id).schedule_checklist_snapshot is None
        [row] = update_audit(db_session, wo.id)
        assert row.old_values["schedule_checklist_snapshot"] is None
        assert row.new_values["schedule_checklist_snapshot"] is None
        assert "ACME-SECRET" not in str(row.old_values) + str(row.new_values)

    def test_putting_a_legacy_work_order_on_hold_writes_no_snapshot(self, client, db_session, headers):
        schedule = make_schedule(db_session, next_due=company_today(db_session, 1), checklist=json.dumps(STEPS))
        wo = legacy_pm(db_session, schedule, status_=MaintenanceStatus.IN_PROGRESS)
        held = client.put(f"{WORK_ORDERS}/{wo.id}", headers=headers, json={"status": "on_hold"})
        assert held.status_code == 200, held.text
        assert stored_wo(db_session, wo.id).schedule_checklist_snapshot is None
        # Still open, still legacy: it keeps following the schedule.
        assert put_schedule(client, headers, schedule.id, ["Lubricate ways"]).status_code == 200
        assert items(client, headers, wo.id) == ["Lubricate ways"]

    def test_the_cancel_locks_the_work_order_then_the_schedule(self, client, db_session, headers, monkeypatch):
        schedule = make_schedule(db_session, next_due=company_today(db_session, 1), checklist=json.dumps(STEPS))
        wo = legacy_pm(db_session, schedule, status_=MaintenanceStatus.SCHEDULED)
        compiled = spy_row_locks(monkeypatch)
        assert cancel(client, headers, wo.id).status_code == 200
        sql = compiled()
        work_order_locks = lock_positions(sql, "maintenance_work_orders")
        schedule_locks = lock_positions(sql, "maintenance_schedules")
        assert work_order_locks and schedule_locks, sql
        assert work_order_locks[0] < schedule_locks[0], sql


# --------------------------------------------------------------------------- (i) the create handler's scope


def fail_the_work_order_flush(monkeypatch, db: Session, message: str):
    """The request's flush of a NEW maintenance work order raises an ``IntegrityError`` whose driver
    message is ``message`` (PostgreSQL's wording, which SQLite cannot produce); every other flush is
    real."""
    real_flush = db.flush

    def flush(*args, **kwargs):
        if any(isinstance(obj, MaintenanceWorkOrder) for obj in db.new):
            raise IntegrityError("INSERT INTO maintenance_work_orders ...", {}, Exception(message))
        return real_flush(*args, **kwargs)

    monkeypatch.setattr(db, "flush", flush)


OTHER_VIOLATIONS = [
    'insert or update on table "maintenance_work_orders" violates foreign key constraint '
    '"maintenance_work_orders_assigned_to_fkey"',
    'insert or update on table "maintenance_work_orders" violates foreign key constraint '
    '"maintenance_work_orders_schedule_id_fkey"',
    'new row for relation "maintenance_work_orders" violates check constraint "ck_maintenance_work_orders_one_target"',
    "FOREIGN KEY constraint failed",
]


class TestTheCreateHandlerAnswersOnlyItsOwnConflicts:
    @pytest.mark.parametrize("message", OTHER_VIOLATIONS, ids=["assigned-to-fk", "schedule-fk", "check", "sqlite-fk"])
    def test_any_other_violation_is_re_raised_not_a_409(self, client, db_session, headers, monkeypatch, message):
        schedule = make_schedule(db_session, next_due=company_today(db_session, 1))
        count, audits = work_order_count(db_session), audit_count(db_session)
        fail_the_work_order_flush(monkeypatch, db_session, message)

        with pytest.raises(IntegrityError) as raised:
            client.post(WORK_ORDERS, headers=headers, json={"schedule_id": schedule.id, "title": "PM"})

        assert str(raised.value.orig) == message
        db_session.rollback()
        assert work_order_count(db_session) == count and audit_count(db_session) == audits

    def test_a_real_wo_number_collision_is_the_generic_retryable_409(self, client, db_session, headers, monkeypatch):
        existing = create_corrective(client, headers, db_session)
        count, audits = work_order_count(db_session), audit_count(db_session)
        monkeypatch.setattr(maintenance_endpoints, "_generate_wo_number", lambda db, year: existing["wo_number"])

        refused = create_corrective_response(client, headers, db_session)

        assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
        assert "another one was saved at the same time" in refused.json()["detail"]
        assert work_order_count(db_session) == count and audit_count(db_session) == audits

    def test_postgres_wo_number_wording_is_the_generic_409(self, client, db_session, headers, monkeypatch):
        count, audits = work_order_count(db_session), audit_count(db_session)
        fail_the_work_order_flush(
            monkeypatch,
            db_session,
            'duplicate key value violates unique constraint "ix_maintenance_work_orders_wo_number"',
        )
        refused = create_corrective_response(client, headers, db_session)
        assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
        assert "Nothing was changed; try again." in refused.json()["detail"]
        assert work_order_count(db_session) == count and audit_count(db_session) == audits

    def test_postgres_open_schedule_wording_names_the_open_work_order(self, client, db_session, headers, monkeypatch):
        schedule, first = open_pm(client, headers, db_session)
        count, audits = work_order_count(db_session), audit_count(db_session)
        skip_the_open_work_order_pre_check(monkeypatch, then_real=True)
        fail_the_work_order_flush(
            monkeypatch,
            db_session,
            'duplicate key value violates unique constraint "uq_maintenance_work_orders_open_schedule"',
        )
        refused = client.post(WORK_ORDERS, headers=headers, json={"schedule_id": schedule.id, "title": "PM"})
        assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
        assert f"{first['wo_number']} (scheduled)" in refused.json()["detail"]
        assert work_order_count(db_session) == count and audit_count(db_session) == audits


def create_corrective_response(client, headers, db: Session):
    wc = make_wc(db)
    return client.post(WORK_ORDERS, headers=headers, json={"work_center_id": wc.id, "title": "Corrective"})


# --------------------------------------------------------------------------- (j) non-ASCII digit suffixes


class TestNonAsciiDigitSuffixes:
    @pytest.mark.parametrize(
        "history,expected",
        [
            (["MWO-2026-²", "MWO-2026-١٢"], "MWO-2026-0001"),
            (["MWO-2026-0003", "MWO-2026-²", "MWO-2026-١٢"], "MWO-2026-0004"),
        ],
        ids=["only-non-ascii", "beside-ascii"],
    )
    def test_a_non_ascii_digit_suffix_is_skipped_without_raising(
        self, client, db_session, headers, new_years_eve, history, expected
    ):
        """``str.isdigit`` accepts a superscript two (which ``int`` refuses -- a 500) and Arabic-Indic
        digits (which ``int`` reads as 12 -- a number nobody issued). Neither is a sequence number."""
        for number in history:
            seed_number(db_session, number)
        assert create_corrective(client, headers, db_session)["wo_number"] == expected


# --------------------------------------------------------------------------- (k) the locked read is fresh


class TestTheLockedScheduleReadIsTheRowAsItIsNow:
    def test_the_schedule_edit_reads_the_row_not_a_stale_identity_map_copy(self, client, db_session, headers):
        """``_schedule_or_404(for_update=True)`` uses ``populate_existing``: a copy of the schedule the
        session already holds must not stand in for the locked row, or the audit row's ``old_values``
        (and the token rule's "what is being removed") would describe a checklist that is gone."""
        schedule = make_schedule(db_session, next_due=company_today(db_session, 1), checklist=json.dumps(STEPS))
        changed = json.dumps(["Changed by another writer"])
        db_session.execute(
            text("UPDATE maintenance_schedules SET checklist = :checklist WHERE id = :id"),
            {"checklist": changed, "id": schedule.id},
        )
        assert schedule.checklist == json.dumps(STEPS)  # the session's copy is stale

        edited = put_schedule(client, headers, schedule.id, ["Lubricate ways"])

        assert edited.status_code == 200, edited.text
        db_session.expire_all()
        [row] = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.resource_type == "maintenance_schedule",
                AuditLog.resource_id == schedule.id,
                AuditLog.action == "UPDATE",
            )
            .all()
        )
        assert row.old_values["checklist"] == changed
