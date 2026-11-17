"""A completed or cancelled maintenance work order is a final record.

``PUT /maintenance/work-orders/{id}`` used to refuse only a STATUS change on a completed or
cancelled work order; every other ``WorkOrderUpdate`` field could still be rewritten after the
fact -- a planning role (or a standing API token) could re-tick a completed PM's checklist, the
very steps ``/complete`` demanded a written reason to leave unticked. Now:

* any change to ``priority``, ``title``, ``description``, ``checklist_results``,
  ``scheduled_date``, ``due_date``, ``requires_shutdown``, ``assigned_to`` or ``notes`` on a
  completed or cancelled work order is **409** (plain string naming the changed fields), raised
  before the first write -- no UPDATE, no audit row;
* a value identical to the record's is an echo and is accepted (200): enums by value, dates as
  parsed (any encoding the schema accepts), ``requires_shutdown`` by truth (a legacy NULL equals
  ``false``), ``title`` against the trimmed stored value (the request's is trimmed by the schema),
  everything else exact equality;
* such an accepted request on a final record -- an empty body included -- writes NOTHING: not the
  legacy NULL ``requires_shutdown`` an echoed ``false`` would replace, nor a legacy padded title;
* it is a state rule, not a role rule: a superuser and a Platform Admin get the same 409;
* the terminal refusal comes before the API-token checklist rule, so a token gets the same 409;
* a later note is a maintenance log entry on the work order (``POST /maintenance/log``), which
  still works;
* open work orders (scheduled / overdue / on hold / in progress) are edited exactly as before.

The assertions read the STORED rows and audit rows, not only status codes.
"""

import json
from contextlib import contextmanager
from typing import Iterator, List

import pytest
from fastapi import status
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.maintenance import MaintenanceLog, MaintenanceSchedule, MaintenanceStatus, MaintenanceWorkOrder
from app.models.user import UserRole
from tests.api.test_maintenance_checklist_completion import (
    BOTH_TICKED,
    ONE_TICKED,
    TWO_ITEMS_OPEN,
    completed_logs,
    completion_audit,
    start_pm,
)
from tests.api.test_maintenance_downtime_link import create_wo, events_for, make_wc
from tests.api.test_receiving_compliance import headers_for, make_user

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

WORK_ORDERS = "/api/v1/maintenance/work-orders"
API_TOKENS = "/api/v1/api-tokens/"
REASON_FIELD = "incomplete_checklist_reason"

# What every work order here is opened with (``assigned_to`` is the technician; ``notes`` is
# written by the completion, or set before the cancel).
ORIGINAL = {
    "priority": "high",
    "title": "Synthetic PM",
    "description": "Grease the ways",
    "checklist_results": BOTH_TICKED,
    "scheduled_date": "2026-11-02",
    "due_date": "2026-11-09",
    "requires_shutdown": True,
}
ORIGINAL_NOTES = "Ways greased, no wear found"

# A different value for every WorkOrderUpdate field but ``status`` (which keeps its own message).
# ``assigned_to`` is resolved at runtime to another real user in the company.
OTHER_ASSIGNEE = object()
CHANGED = {
    "priority": "critical",
    "title": "Rewritten after the fact",
    "description": "Rewritten description",
    "checklist_results": TWO_ITEMS_OPEN,
    "scheduled_date": "2026-12-01",
    "due_date": "2026-12-08",
    "requires_shutdown": False,
    "assigned_to": OTHER_ASSIGNEE,
    "notes": "Rewritten notes",
}
FIELDS = list(CHANGED)
ENDS = ["completed", "cancelled"]


@pytest.fixture
def supervisor(db_session):
    return make_user(db_session, role=UserRole.SUPERVISOR, company_id=1)


@pytest.fixture
def headers(supervisor):
    return headers_for(supervisor)


@pytest.fixture
def technician(db_session):
    return make_user(db_session, role=UserRole.OPERATOR, company_id=1)


@pytest.fixture
def other_technician(db_session):
    return make_user(db_session, role=UserRole.OPERATOR, company_id=1)


@pytest.fixture
def token_headers(client, admin_headers, supervisor):
    """A standing (full-scope) API token bound to the same supervisor, minted the way an Admin
    does it -- the same pattern as ``test_maintenance_checklist_completion.token_headers``."""
    issued = client.post(API_TOKENS, json={"user_id": supervisor.id, "label": "Final probe"}, headers=admin_headers)
    assert issued.status_code == 201, issued.text
    return {"Authorization": f"Bearer {issued.json()['token']}", "X-Requested-With": "XMLHttpRequest"}


# ── helpers ──────────────────────────────────────────────────────────────


def final_record_detail(wo_number: str, end: str, fields: List[str]) -> str:
    return (
        f"Maintenance work order {wo_number} is {end}, a final record; its {', '.join(fields)} can no longer "
        "change. Add a later note as a maintenance log entry on the work order, or open a new maintenance work "
        "order for further work."
    )


def get_wo(client, headers, wo_id: int) -> dict:
    resp = client.get(f"{WORK_ORDERS}/{wo_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def put_wo(client, headers, wo_id: int, body: dict):
    return client.put(f"{WORK_ORDERS}/{wo_id}", headers=headers, json=body)


def stored_row(db: Session, wo_id: int) -> dict:
    """Every column of the work order as stored (``updated_at`` included)."""
    db.expire_all()
    wo = db.get(MaintenanceWorkOrder, wo_id)
    return {column.key: getattr(wo, column.key) for column in wo.__table__.columns}


def wo_audit_rows(db: Session, wo_id: int) -> list:
    """The work order's audit rows, with everything a later write could add or change."""
    db.expire_all()
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.resource_type == "maintenance_work_order", AuditLog.resource_id == wo_id)
        .order_by(AuditLog.id)
        .all()
    )
    return [(row.id, row.action, row.old_values, row.new_values, row.extra_data) for row in rows]


@contextmanager
def writes_to_record(db: Session) -> Iterator[List[str]]:
    """Capture every INSERT / UPDATE / DELETE that touches a maintenance work order or the audit log
    while the block runs (the client shares this test's engine)."""
    statements: List[str] = []
    engine = db.get_bind()

    def watch(_conn, _cursor, statement, _params, _context, _many):
        verb = statement.lstrip().split(None, 1)[0].upper()
        lowered = statement.lower()
        if verb in ("INSERT", "UPDATE", "DELETE") and ("maintenance_work_orders" in lowered or "audit_logs" in lowered):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", watch)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", watch)


def final_wo(client, headers, db: Session, assignee_id: int, end: str) -> dict:
    """A work-center shutdown work order opened with ``ORIGINAL``, then ended: ``completed`` through
    ``POST .../start`` + ``.../complete`` (which writes the notes), ``cancelled`` through
    ``PUT {"status": "cancelled"}`` (notes set first). Returns the GET of the final record."""
    wc = make_wc(db)
    wo = create_wo(client, headers, work_center_id=wc.id, assigned_to=assignee_id, **ORIGINAL)
    if end == "completed":
        assert client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=headers).status_code == 200
        done = client.post(
            f"{WORK_ORDERS}/{wo['id']}/complete",
            headers=headers,
            json={"notes": ORIGINAL_NOTES, "findings": "No wear on the ways"},
        )
    else:
        noted = put_wo(client, headers, wo["id"], {"notes": ORIGINAL_NOTES})
        assert noted.status_code == 200, noted.text
        done = put_wo(client, headers, wo["id"], {"status": "cancelled"})
    assert done.status_code == 200, done.text
    record = get_wo(client, headers, wo["id"])
    assert record["status"] == end
    for field, value in ORIGINAL.items():
        assert record[field] == value, field
    assert record["assigned_to"] == assignee_id and record["notes"] == ORIGINAL_NOTES
    return record


def assert_refused_and_untouched(client, headers, db: Session, record: dict, body: dict) -> str:
    """PUT ``body`` and require a 409 that changed nothing: same GET, same stored row, same audit
    rows, and no write to the work order or the audit log on the way. Returns the detail."""
    wo_id = record["id"]
    row_before = stored_row(db, wo_id)
    audit_before = wo_audit_rows(db, wo_id)
    with writes_to_record(db) as writes:
        resp = put_wo(client, headers, wo_id, body)
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, str)  # a plain string, not a structured object
    assert writes == []
    assert get_wo(client, headers, wo_id) == record
    assert stored_row(db, wo_id) == row_before
    assert wo_audit_rows(db, wo_id) == audit_before
    return detail


def assert_accepted_and_untouched(client, headers, db: Session, record: dict, body: dict) -> None:
    """PUT ``body`` and require a 200 that changed nothing: the response IS the record, and the
    stored row (``updated_at`` included) and audit rows are as they were, with no write on the way."""
    wo_id = record["id"]
    row_before = stored_row(db, wo_id)
    audit_before = wo_audit_rows(db, wo_id)
    with writes_to_record(db) as writes:
        resp = put_wo(client, headers, wo_id, body)
    assert resp.status_code == 200, resp.text
    assert resp.json() == record
    assert writes == []
    assert get_wo(client, headers, wo_id) == record
    assert stored_row(db, wo_id) == row_before
    assert wo_audit_rows(db, wo_id) == audit_before


# ── 1. every field, on both final states ─────────────────────────────────


class TestEveryFieldOfAFinalRecordIsFixed:
    @pytest.mark.parametrize("end", ENDS)
    @pytest.mark.parametrize("field", FIELDS)
    def test_changing_the_field_is_409_naming_it_and_writes_nothing(
        self, client, db_session, headers, technician, other_technician, end, field
    ):
        record = final_wo(client, headers, db_session, technician.id, end)
        value = other_technician.id if CHANGED[field] is OTHER_ASSIGNEE else CHANGED[field]
        assert record[field] != value  # a real change, not an echo

        detail = assert_refused_and_untouched(client, headers, db_session, record, {field: value})
        assert record["wo_number"] in detail and "final record" in detail and field in detail
        assert detail == final_record_detail(record["wo_number"], end, [field])

    @pytest.mark.parametrize("end", ENDS)
    def test_a_status_change_keeps_its_own_message(self, client, db_session, headers, technician, end):
        record = final_wo(client, headers, db_session, technician.id, end)
        detail = assert_refused_and_untouched(client, headers, db_session, record, {"status": "on_hold"})
        assert detail == (
            f"Maintenance work order {record['wo_number']} is {end}, a final record; it cannot be moved to "
            "on_hold. Open a new maintenance work order instead."
        )


# ── 2. the case that motivated it ────────────────────────────────────────


class TestACompletedChecklistCannotBeReTicked:
    def test_ticking_the_steps_completion_recorded_as_skipped_is_409(self, client, db_session, headers):
        """Completed with "Check coolant" unticked and a written reason; a later PUT ticking it would
        have made the record say the step was done while the audit row says why it was not."""
        schedule, wo = start_pm(client, headers, db_session)
        done = client.post(
            f"{WORK_ORDERS}/{wo['id']}/complete",
            headers=headers,
            json={"checklist_results": ONE_TICKED, REASON_FIELD: "Coolant concentrate on back order"},
        )
        assert done.status_code == 200, done.text
        [completion] = completion_audit(db_session, wo["id"])
        assert completion.extra_data["checklist_unticked_items"] == ["Check coolant"]
        completion_before = (completion.id, completion.old_values, completion.new_values, completion.extra_data)
        logs_before = [(log.id, log.description, log.event_date) for log in completed_logs(db_session, wo["id"])]
        db_session.expire_all()
        schedule_before = db_session.get(MaintenanceSchedule, schedule.id)
        schedule_dates = (schedule_before.last_completed_date, schedule_before.next_due_date)

        record = get_wo(client, headers, wo["id"])
        detail = assert_refused_and_untouched(client, headers, db_session, record, {"checklist_results": BOTH_TICKED})
        assert detail == final_record_detail(wo["wo_number"], "completed", ["checklist_results"])

        assert stored_row(db_session, wo["id"])["checklist_results"] == ONE_TICKED  # still unticked
        [completion] = completion_audit(db_session, wo["id"])
        assert (completion.id, completion.old_values, completion.new_values, completion.extra_data) == (
            completion_before
        )
        assert [(log.id, log.description, log.event_date) for log in completed_logs(db_session, wo["id"])] == (
            logs_before
        )
        db_session.expire_all()
        schedule_after = db_session.get(MaintenanceSchedule, schedule.id)
        assert (schedule_after.last_completed_date, schedule_after.next_due_date) == schedule_dates


# ── 3. an identical re-send is an echo ───────────────────────────────────


class TestAnEchoIsAccepted:
    @pytest.mark.parametrize("end", ENDS)
    def test_resending_every_field_is_200_and_writes_nothing(self, client, db_session, headers, technician, end):
        record = final_wo(client, headers, db_session, technician.id, end)
        echo = {field: record[field] for field in [*FIELDS, "status"]}
        row_before = stored_row(db_session, record["id"])
        audit_before = wo_audit_rows(db_session, record["id"])

        with writes_to_record(db_session) as writes:
            resp = put_wo(client, headers, record["id"], echo)
        assert resp.status_code == 200, resp.text
        assert resp.json() == record
        assert writes == []  # no UPDATE, no audit INSERT
        assert get_wo(client, headers, record["id"]) == record
        assert stored_row(db_session, record["id"]) == row_before  # updated_at included
        assert wo_audit_rows(db_session, record["id"]) == audit_before

    @pytest.mark.parametrize("end", ENDS)
    def test_a_null_field_echoed_as_null_is_accepted(self, client, db_session, headers, end):
        """``description`` / ``assigned_to`` / ``checklist_results`` never set: an explicit null
        repeats the record."""
        wc = make_wc(db_session)
        wo = create_wo(client, headers, work_center_id=wc.id, requires_shutdown=False)
        if end == "completed":
            client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=headers)
            done = client.post(f"{WORK_ORDERS}/{wo['id']}/complete", headers=headers, json={})
        else:
            done = put_wo(client, headers, wo["id"], {"status": "cancelled"})
        assert done.status_code == 200, done.text
        record = get_wo(client, headers, wo["id"])
        body = {"description": None, "assigned_to": None, "checklist_results": None, "notes": None}
        assert all(record[field] is None for field in body)
        audit_before = wo_audit_rows(db_session, wo["id"])
        with writes_to_record(db_session) as writes:
            resp = put_wo(client, headers, wo["id"], body)
        assert resp.status_code == 200, resp.text
        assert writes == [] and resp.json() == record
        assert wo_audit_rows(db_session, wo["id"]) == audit_before

    @pytest.mark.parametrize("end", ENDS)
    def test_an_empty_body_is_200_and_writes_nothing(self, client, db_session, headers, technician, end):
        record = final_wo(client, headers, db_session, technician.id, end)
        assert_accepted_and_untouched(client, headers, db_session, record, {})

    @pytest.mark.parametrize("end", ENDS)
    @pytest.mark.parametrize(
        "body",
        [
            {"scheduled_date": "2026-11-02T00:00:00"},
            {"scheduled_date": "2026-11-02T00:00:00Z", "due_date": "2026-11-09T00:00:00.000"},
            {"scheduled_date": 1793577600},  # 2026-11-02T00:00:00Z as a Unix timestamp
        ],
        ids=["midnight-datetime", "midnight-utc-and-millis", "unix-timestamp"],
    )
    def test_a_date_in_another_accepted_encoding_is_an_echo(self, client, db_session, headers, technician, end, body):
        """The schema parses each of these to the stored date, so the comparison sees the record's own
        value: 200, nothing written."""
        record = final_wo(client, headers, db_session, technician.id, end)
        assert (record["scheduled_date"], record["due_date"]) == ("2026-11-02", "2026-11-09")
        assert_accepted_and_untouched(client, headers, db_session, record, body)


# ── 3b. a legacy title stored with stray spaces ──────────────────────────


class TestALegacyPaddedTitle:
    """The request's title is trimmed by ``WorkOrderUpdate``, so it is compared against the trimmed
    STORED title: a legacy row whose title carries stray spaces can still be echoed, and the echo
    leaves the stored value exactly as it was."""

    @pytest.mark.parametrize("end", ENDS)
    def test_the_trimmed_title_echoes_and_the_padding_stays_another_title_is_refused(
        self, client, db_session, headers, technician, end
    ):
        record = final_wo(client, headers, db_session, technician.id, end)
        db_session.expire_all()
        row = db_session.get(MaintenanceWorkOrder, record["id"])
        row.title = "  Synthetic PM  "  # what a pre-trim row carries
        db_session.commit()
        record = get_wo(client, headers, record["id"])
        assert record["title"] == "  Synthetic PM  "

        assert_accepted_and_untouched(client, headers, db_session, record, {"title": "Synthetic PM"})
        # The record's own padded value, sent back as read, is trimmed by the schema to the same echo.
        assert_accepted_and_untouched(client, headers, db_session, record, {"title": record["title"]})
        assert stored_row(db_session, record["id"])["title"] == "  Synthetic PM  "  # not rewritten trimmed

        detail = assert_refused_and_untouched(client, headers, db_session, record, {"title": "Other"})
        assert detail == final_record_detail(record["wo_number"], end, ["title"])
        assert stored_row(db_session, record["id"])["title"] == "  Synthetic PM  "

    def test_only_the_title_is_trimmed(self, client, db_session, headers, technician):
        """``description`` is free text the schema does not trim, so a padded stored description is not
        echoed by its trimmed form."""
        record = final_wo(client, headers, db_session, technician.id, "completed")
        db_session.expire_all()
        row = db_session.get(MaintenanceWorkOrder, record["id"])
        row.description = "  Grease the ways  "
        db_session.commit()
        record = get_wo(client, headers, record["id"])
        detail = assert_refused_and_untouched(client, headers, db_session, record, {"description": "Grease the ways"})
        assert detail == final_record_detail(record["wo_number"], "completed", ["description"])


# ── 3c. a state rule, not a role rule ────────────────────────────────────


def privileged_caller(db: Session, kind: str):
    """A caller ``require_role`` waves through every gate: a superuser (here on a role the planning
    gate would otherwise refuse) or a Platform Admin, both in company 1."""
    if kind == "superuser":
        user = make_user(db, role=UserRole.VIEWER, company_id=1)
        user.is_superuser = True
        db.commit()
        db.refresh(user)
        return user
    return make_user(db, role=UserRole.PLATFORM_ADMIN, company_id=1)


class TestEveryCallerGetsTheSameRefusal:
    @pytest.mark.parametrize("end", ENDS)
    @pytest.mark.parametrize("kind", ["superuser", "platform_admin"])
    def test_a_superuser_or_platform_admin_is_refused_the_same_409(
        self, client, db_session, headers, technician, kind, end
    ):
        caller = headers_for(privileged_caller(db_session, kind))
        record = final_wo(client, headers, db_session, technician.id, end)

        detail = assert_refused_and_untouched(
            client, caller, db_session, record, {"checklist_results": TWO_ITEMS_OPEN, "notes": "Overruled"}
        )
        assert detail == final_record_detail(record["wo_number"], end, ["checklist_results", "notes"])
        status_detail = assert_refused_and_untouched(client, caller, db_session, record, {"status": "scheduled"})
        assert "a final record; it cannot be moved to scheduled" in status_detail
        # Their echo is accepted like anyone's...
        assert_accepted_and_untouched(client, caller, db_session, record, {"title": record["title"]})

        # ...and they DO pass the role gate: the same caller edits an open work order.
        open_one = create_wo(client, headers, work_center_id=make_wc(db_session).id)
        edited = put_wo(client, caller, open_one["id"], {"notes": "Planned by a privileged caller"})
        assert edited.status_code == 200, edited.text
        assert stored_row(db_session, open_one["id"])["notes"] == "Planned by a privileged caller"


# ── 4. only what changed is named ────────────────────────────────────────


class TestOnlyTheChangedFieldsAreNamed:
    @pytest.mark.parametrize("end", ENDS)
    def test_an_identical_field_beside_a_changed_one(self, client, db_session, headers, technician, end):
        record = final_wo(client, headers, db_session, technician.id, end)
        body = {"title": record["title"], "notes": "A note added after the fact"}
        detail = assert_refused_and_untouched(client, headers, db_session, record, body)
        assert detail == final_record_detail(record["wo_number"], end, ["notes"])
        assert "title" not in detail

    def test_a_status_echo_beside_a_change_names_only_the_change(self, client, db_session, headers, technician):
        record = final_wo(client, headers, db_session, technician.id, "completed")
        body = {"status": "completed", "priority": "high", "due_date": "2026-12-31"}
        detail = assert_refused_and_untouched(client, headers, db_session, record, body)
        assert detail == final_record_detail(record["wo_number"], "completed", ["due_date"])

    def test_several_changes_are_all_named(self, client, db_session, headers, technician):
        record = final_wo(client, headers, db_session, technician.id, "cancelled")
        body = {
            "notes": "Rewritten",
            "title": "Rewritten",
            "description": record["description"],
            "requires_shutdown": True,  # echo
            "priority": "low",
        }
        detail = assert_refused_and_untouched(client, headers, db_session, record, body)
        # Named in WorkOrderUpdate's field order, the identical ones left out.
        assert detail == final_record_detail(record["wo_number"], "cancelled", ["priority", "title", "notes"])

    def test_clearing_a_field_with_null_is_a_change(self, client, db_session, headers, technician):
        record = final_wo(client, headers, db_session, technician.id, "completed")
        body = {"description": None, "assigned_to": None}
        detail = assert_refused_and_untouched(client, headers, db_session, record, body)
        assert detail == final_record_detail(record["wo_number"], "completed", ["description", "assigned_to"])

    @pytest.mark.parametrize(
        "field, near_echo",
        [
            ("description", "Grease the ways "),  # trailing space
            ("notes", "ways greased, no wear found"),  # case
            # The same items, re-serialized with different whitespace: equal as JSON, not as stored.
            ("checklist_results", json.dumps(json.loads(BOTH_TICKED), indent=2)),
        ],
        ids=["trailing-space", "case", "reserialized-json"],
    )
    def test_text_is_compared_exactly(self, client, db_session, headers, technician, field, near_echo):
        record = final_wo(client, headers, db_session, technician.id, "completed")
        assert record[field] != near_echo
        detail = assert_refused_and_untouched(client, headers, db_session, record, {field: near_echo})
        assert detail == final_record_detail(record["wo_number"], "completed", [field])


# ── 5. a legacy NULL requires_shutdown ───────────────────────────────────


class TestALegacyNullShutdownFlag:
    @pytest.mark.parametrize("end", ENDS)
    def test_false_echoes_a_null_and_leaves_it_null_true_is_refused(self, client, db_session, headers, technician, end):
        record = final_wo(client, headers, db_session, technician.id, end)
        db_session.expire_all()
        row = db_session.get(MaintenanceWorkOrder, record["id"])
        row.requires_shutdown = None  # what a pre-default row carries
        db_session.commit()
        record = get_wo(client, headers, record["id"])
        assert record["requires_shutdown"] is None
        row_before = stored_row(db_session, record["id"])
        audit_before = wo_audit_rows(db_session, record["id"])

        with writes_to_record(db_session) as writes:
            echoed = put_wo(client, headers, record["id"], {"requires_shutdown": False})
        assert echoed.status_code == 200, echoed.text
        assert echoed.json()["requires_shutdown"] is None
        assert writes == []
        assert stored_row(db_session, record["id"]) == row_before
        assert stored_row(db_session, record["id"])["requires_shutdown"] is None  # not replaced by false
        assert wo_audit_rows(db_session, record["id"]) == audit_before

        detail = assert_refused_and_untouched(client, headers, db_session, record, {"requires_shutdown": True})
        assert detail == final_record_detail(record["wo_number"], end, ["requires_shutdown"])
        assert stored_row(db_session, record["id"])["requires_shutdown"] is None


# ── 6. an API token gets the same refusal ────────────────────────────────


class TestAnApiTokenGetsTheSameRefusal:
    def test_a_token_re_ticking_a_completed_checklist_is_409(self, client, db_session, headers, token_headers):
        """Ticking is what a token MAY do on an open work order; on a final record it is refused like
        anyone's edit."""
        _, wo = start_pm(client, headers, db_session)
        done = client.post(
            f"{WORK_ORDERS}/{wo['id']}/complete",
            headers=headers,
            json={"checklist_results": ONE_TICKED, REASON_FIELD: "Coolant concentrate on back order"},
        )
        assert done.status_code == 200, done.text
        record = get_wo(client, headers, wo["id"])
        detail = assert_refused_and_untouched(
            client, token_headers, db_session, record, {"checklist_results": BOTH_TICKED}
        )
        assert detail == final_record_detail(wo["wo_number"], "completed", ["checklist_results"])
        assert stored_row(db_session, wo["id"])["checklist_results"] == ONE_TICKED

    def test_a_token_removing_items_gets_the_final_record_409_before_the_token_403(
        self, client, db_session, headers, token_headers, technician
    ):
        record = final_wo(client, headers, db_session, technician.id, "completed")
        detail = assert_refused_and_untouched(client, token_headers, db_session, record, {"checklist_results": None})
        assert detail == final_record_detail(record["wo_number"], "completed", ["checklist_results"])

    def test_a_token_echo_is_accepted(self, client, db_session, headers, token_headers, technician):
        record = final_wo(client, headers, db_session, technician.id, "completed")
        with writes_to_record(db_session) as writes:
            resp = put_wo(client, token_headers, record["id"], {"checklist_results": record["checklist_results"]})
        assert resp.status_code == 200, resp.text
        assert writes == [] and resp.json() == record


# ── 7. the channel the refusal points to ─────────────────────────────────


class TestALaterNoteIsALogEntry:
    @pytest.mark.parametrize("end", ENDS)
    def test_a_log_entry_on_the_final_work_order_is_accepted_and_audited(
        self, client, db_session, headers, supervisor, technician, end
    ):
        record = final_wo(client, headers, db_session, technician.id, end)
        resp = client.post(
            "/api/v1/maintenance/log",
            headers=headers,
            json={
                "work_center_id": record["work_center_id"],
                "maintenance_wo_id": record["id"],
                "event_type": "note",
                "description": "Wiper on the Y axis weeping slightly; recheck at next PM",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["maintenance_wo_id"] == record["id"]

        db_session.expire_all()
        [log] = (
            db_session.query(MaintenanceLog)
            .filter(MaintenanceLog.maintenance_wo_id == record["id"], MaintenanceLog.event_type == "note")
            .all()
        )
        assert log.company_id == 1 and log.performed_by == supervisor.id
        assert log.work_center_id == record["work_center_id"] and log.event_date is not None
        assert log.description == "Wiper on the Y axis weeping slightly; recheck at next PM"
        created = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.resource_type == "maintenance_log",
                AuditLog.resource_id == log.id,
                AuditLog.action == "CREATE",
            )
            .one()
        )
        assert created.user_id == supervisor.id
        # ...and the work order itself is still the record it was.
        assert get_wo(client, headers, record["id"]) == record


# ── 8. open work orders are edited as before ─────────────────────────────

OPEN_STATES = ["scheduled", "overdue", "on_hold", "in_progress", "on_hold_after_start"]
STARTED_STATES = {"in_progress", "on_hold_after_start"}


def open_wo(client, headers, db: Session, assignee_id: int, state: str) -> dict:
    wc = make_wc(db)
    wo = create_wo(client, headers, work_center_id=wc.id, assigned_to=assignee_id, **ORIGINAL)
    if state in STARTED_STATES:
        assert client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=headers).status_code == 200
    if state in ("overdue", "on_hold", "on_hold_after_start"):
        moved = put_wo(client, headers, wo["id"], {"status": "overdue" if state == "overdue" else "on_hold"})
        assert moved.status_code == 200, moved.text
    record = get_wo(client, headers, wo["id"])
    assert record["status"] == {"on_hold_after_start": "on_hold"}.get(state, state)
    return record


class TestOpenWorkOrdersAreUnaffected:
    @pytest.mark.parametrize("state", OPEN_STATES)
    def test_details_still_change_and_are_audited(
        self, client, db_session, headers, technician, other_technician, state
    ):
        record = open_wo(client, headers, db_session, technician.id, state)
        body = {field: value for field, value in CHANGED.items() if field != "requires_shutdown"}
        body["assigned_to"] = other_technician.id
        audit_before = wo_audit_rows(db_session, record["id"])

        with writes_to_record(db_session) as writes:
            resp = put_wo(client, headers, record["id"], body)
        assert resp.status_code == 200, resp.text
        # The watcher the final-record tests rely on does see a real edit's writes.
        assert any(s.lstrip().upper().startswith("UPDATE MAINTENANCE_WORK_ORDERS") for s in writes), writes
        assert any(s.lstrip().upper().startswith("INSERT INTO AUDIT_LOGS") for s in writes), writes
        for field, value in body.items():
            assert resp.json()[field] == value, field
        assert resp.json()["status"] == record["status"]  # untouched by a details edit

        row = stored_row(db_session, record["id"])
        assert row["priority"].value == "critical" and row["title"] == "Rewritten after the fact"
        assert row["description"] == "Rewritten description" and row["checklist_results"] == TWO_ITEMS_OPEN
        assert (row["scheduled_date"].isoformat(), row["due_date"].isoformat()) == ("2026-12-01", "2026-12-08")
        assert row["assigned_to"] == other_technician.id and row["notes"] == "Rewritten notes"
        assert row["requires_shutdown"] is True

        audit_after = wo_audit_rows(db_session, record["id"])
        assert audit_after[: len(audit_before)] == audit_before
        [(_, action, old_values, new_values, extra_data)] = audit_after[len(audit_before) :]
        assert action == "UPDATE"
        assert set(body) <= set(extra_data["changes"])
        assert old_values["title"] == "Synthetic PM" and new_values["title"] == "Rewritten after the fact"
        assert old_values["assigned_to"] == technician.id and new_values["assigned_to"] == other_technician.id

    @pytest.mark.parametrize("state", OPEN_STATES)
    def test_requires_shutdown_changes_until_started(self, client, db_session, headers, technician, state):
        record = open_wo(client, headers, db_session, technician.id, state)
        resp = put_wo(client, headers, record["id"], {"requires_shutdown": False})
        if state in STARTED_STATES:
            # The existing started-work-order rule, not the final-record one.
            assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
            assert "has been started" in resp.json()["detail"]
            assert "final record" not in resp.json()["detail"]
            assert stored_row(db_session, record["id"])["requires_shutdown"] is True
            [event] = events_for(db_session, maintenance_work_order_id=record["id"])
            assert event.end_time is None  # its downtime still runs
        else:
            assert resp.status_code == 200, resp.text
            assert stored_row(db_session, record["id"])["requires_shutdown"] is False

    def test_an_open_work_order_can_still_be_cancelled_with_its_notes(self, client, db_session, headers, technician):
        """The move INTO cancelled is an open work order's edit: it lands, and only afterwards is
        the record final."""
        record = open_wo(client, headers, db_session, technician.id, "in_progress")
        resp = put_wo(client, headers, record["id"], {"status": "cancelled", "notes": "Part never arrived"})
        assert resp.status_code == 200, resp.text
        row = stored_row(db_session, record["id"])
        assert row["status"] == MaintenanceStatus.CANCELLED and row["notes"] == "Part never arrived"
        [event] = events_for(db_session, maintenance_work_order_id=record["id"])
        assert event.end_time is not None  # cancelling settled the downtime
        refused = put_wo(client, headers, record["id"], {"notes": "Second thoughts"})
        assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
