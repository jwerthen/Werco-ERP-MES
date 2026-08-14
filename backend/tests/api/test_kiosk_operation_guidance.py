"""Written guidance reaches the kiosk: WO notes + special instructions + op text.

An operator at the weld crew station could not see a "Unit #" the owner typed
into WO-20260807-006's **work order Notes** field. The data existed and
``GET /shop-floor/operations/{id}`` already returned it -- but the kiosk never
calls that endpoint, and a crew station holds only a station-type JWT before a
badge is scanned, so it *cannot*: ``get_kiosk_or_user`` is honored by the
work-center-queue route only. The guidance therefore had to ride the payloads the
kiosk already reads.

Five keys, identical on every kiosk surface, built once by
``shop_floor._job_guidance_fields``::

    work_order_notes
    work_order_special_instructions
    operation_description
    operation_setup_instructions
    operation_run_instructions

What this file pins:

SHAPE -- all five on the queue rows, on the ``held`` rows (inherited through
``_kiosk_job_row``, which is why the held list needs no code of its own), and on
``GET /shop-floor/my-active-job``'s job dict. Same names, same normalization,
one contract.

NORMALIZATION -- empty and whitespace-only collapse to ``None`` so the client
never has to decide whether ``""`` counts as a note; everything else is
**verbatim**, NOT stripped. Leading indentation and blank lines in a multi-line
work instruction are layout, and markup-ish characters pass through untouched:
this system has no ingest-time sanitizer on purpose (CLAUDE.md, "store request
bytes verbatim, escape at the sink") and the kiosk renders text, not HTML.

SCOPE -- work-order-level text rides EVERY operation row of that work order (the
motivating bug: the note was on the WO, the operator was on op 3 of 4), while
operation-level text stays on its own row.

DISCLOSURE -- these five ARE sent to a STATION principal, by the owner's explicit
decision, AND the blocker's free text is STILL withheld from that same principal
on that same response. Both halves are asserted together, in one test, because
the risk this feature carries is a later reader "harmonizing" the two.

TENANCY -- unchanged: a station reads only its own work center, and a
cross-tenant work center answers empty rather than leaking guidance.

COST -- no N+1. The queue's per-row cost is flat in the number of rows, and the
guidance block itself issues ZERO queries against an eager-loaded operation.
"""

from typing import Callable, List

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session, joinedload

from app.api.endpoints.shop_floor import _guidance_text, _job_guidance_fields
from app.models.time_entry import TimeEntry
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation
from app.models.work_order_blocker import (
    WorkOrderBlocker,
    WorkOrderBlockerCategory,
    WorkOrderBlockerSeverity,
    WorkOrderBlockerStatus,
)
from tests.api.kiosk_test_helpers import (
    COMPANY_A,
    COMPANY_B,
    bearer,
    kiosk_token_for,
    make_kiosk_station,
    make_user,
    make_wo_with_operation,
    make_work_center,
    queue_url,
    user_headers,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

MY_ACTIVE_JOB = "/api/v1/shop-floor/my-active-job"

# The contract. Named once so a partial implementation on any one surface fails
# loudly instead of passing whichever subset that surface happens to carry.
GUIDANCE_KEYS = {
    "work_order_notes",
    "work_order_special_instructions",
    "operation_description",
    "operation_setup_instructions",
    "operation_run_instructions",
}


def set_guidance(
    db: Session,
    wo: WorkOrder,
    op: WorkOrderOperation,
    *,
    notes="Unit #7 -- match to the tag on the fixture",
    special="Weld per WPS-114. Purge backside.",
    description="Fit and tack the housing halves",
    setup="Set fixture stops to 12.500 in",
    run="Run at 180 A, weave 1/8 in",
):
    wo.notes = notes
    wo.special_instructions = special
    op.description = description
    op.setup_instructions = setup
    op.run_instructions = run
    db.commit()


def queue_body(client: TestClient, headers: dict, work_center_id: int) -> dict:
    resp = client.get(queue_url(work_center_id), headers=headers)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()


def queue_row(client: TestClient, headers: dict, work_center_id: int, operation_id: int) -> dict:
    rows = [r for r in queue_body(client, headers, work_center_id)["queue"] if r["operation_id"] == operation_id]
    assert rows, f"operation {operation_id} missing from queue"
    return rows[0]


def clock_in(client: TestClient, headers: dict, wo, op) -> int:
    resp = client.post(
        "/api/v1/shop-floor/clock-in",
        headers=headers,
        json={"work_order_id": wo.id, "operation_id": op.id, "work_center_id": op.work_center_id},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()["id"]


def add_operation(
    db: Session,
    wo: WorkOrder,
    work_center,
    *,
    sequence: int,
    status_: OperationStatus = OperationStatus.READY,
    **guidance,
) -> WorkOrderOperation:
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=work_center.id,
        sequence=sequence,
        operation_number=f"OP{sequence}",
        name=f"Step {sequence}",
        status=status_,
        company_id=COMPANY_A,
        **guidance,
    )
    db.add(op)
    db.commit()
    db.refresh(op)
    return op


def count_queries(db: Session, fn: Callable) -> int:
    """Count statements on the session's OWN bind.

    ``db.get_bind()`` is load-bearing, not incidental -- see the identical helper
    in ``test_dispatch_nest_details.py``: this suite has two ``Engine`` objects
    over the same in-memory SQLite, and listening on the wrong one records ZERO
    statements, which makes every assertion below vacuously true.
    """
    statements: List[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    bind = db.get_bind()
    event.listen(bind, "after_cursor_execute", _record)
    try:
        fn()
    finally:
        event.remove(bind, "after_cursor_execute", _record)
    return len(statements)


class TestGuidanceOnQueueRows:
    def test_queue_row_carries_all_five_labeled_fields(self, client: TestClient, db_session: Session):
        operator = make_user(db_session)
        wc = make_work_center(db_session)
        wo, op = make_wo_with_operation(db_session, work_center=wc)
        set_guidance(db_session, wo, op)

        row = queue_row(client, user_headers(operator), wc.id, op.id)

        assert GUIDANCE_KEYS <= set(row), f"missing: {GUIDANCE_KEYS - set(row)}"
        assert row["work_order_notes"] == "Unit #7 -- match to the tag on the fixture"
        assert row["work_order_special_instructions"] == "Weld per WPS-114. Purge backside."
        assert row["operation_description"] == "Fit and tack the housing halves"
        assert row["operation_setup_instructions"] == "Set fixture stops to 12.500 in"
        assert row["operation_run_instructions"] == "Run at 180 A, weave 1/8 in"

    def test_keys_are_present_and_null_when_nothing_was_written(self, client: TestClient, db_session: Session):
        """Absent guidance is ``None``, never a missing key -- the client renders
        off one stable shape and hides the empties itself."""
        operator = make_user(db_session)
        wc = make_work_center(db_session)
        _wo, op = make_wo_with_operation(db_session, work_center=wc)

        row = queue_row(client, user_headers(operator), wc.id, op.id)

        assert GUIDANCE_KEYS <= set(row)
        for key in GUIDANCE_KEYS:
            assert row[key] is None, key

    def test_work_order_text_rides_every_operation_row_of_that_work_order(
        self, client: TestClient, db_session: Session
    ):
        """THE MOTIVATING BUG. The "Unit #" was typed on the WORK ORDER; the
        operator was standing at one operation of four. Work-order-level text is
        therefore repeated on every row, while operation-level text is not."""
        operator = make_user(db_session)
        wc = make_work_center(db_session)
        wo, op1 = make_wo_with_operation(db_session, work_center=wc, sequential_operations=False)
        set_guidance(db_session, wo, op1, description="Fit-up", setup=None, run=None)
        op2 = add_operation(db_session, wo, wc, sequence=20, description="Final weld")
        op3 = add_operation(db_session, wo, wc, sequence=30, description="Dress welds")

        rows = {r["operation_id"]: r for r in queue_body(client, user_headers(operator), wc.id)["queue"]}
        assert {op1.id, op2.id, op3.id} <= set(rows)

        for op_id in (op1.id, op2.id, op3.id):
            assert rows[op_id]["work_order_notes"] == "Unit #7 -- match to the tag on the fixture", op_id
            assert rows[op_id]["work_order_special_instructions"] == "Weld per WPS-114. Purge backside.", op_id

        # Operation-level text stays on its own operation.
        assert rows[op1.id]["operation_description"] == "Fit-up"
        assert rows[op2.id]["operation_description"] == "Final weld"
        assert rows[op3.id]["operation_description"] == "Dress welds"
        assert rows[op2.id]["operation_setup_instructions"] is None


class TestNormalization:
    @pytest.mark.parametrize("blank", ["", "   ", "\t", "\n", "  \n\t \r\n "])
    def test_whitespace_only_normalizes_to_none(self, client: TestClient, db_session: Session, blank: str):
        """The UI must never have to decide whether "" counts as a note."""
        operator = make_user(db_session)
        wc = make_work_center(db_session)
        wo, op = make_wo_with_operation(db_session, work_center=wc)
        set_guidance(db_session, wo, op, notes=blank, special=blank, description=blank, setup=blank, run=blank)

        row = queue_row(client, user_headers(operator), wc.id, op.id)
        for key in GUIDANCE_KEYS:
            assert row[key] is None, f"{key} should normalize {blank!r} to None"

    def test_text_is_returned_verbatim_not_stripped_or_escaped(self, client: TestClient, db_session: Session):
        """Whitespace-only collapses; real text does NOT get re-flowed.

        Leading indentation and blank lines are how a multi-line work instruction
        is laid out, so the value is the ORIGINAL string, not ``.strip()``. And
        markup-ish characters survive: ASME Y14.5 notation like ``<REF>`` is
        ordinary quality text, this system deliberately has no ingest-time
        sanitizer, and the kiosk renders text rather than HTML.
        """
        operator = make_user(db_session)
        wc = make_work_center(db_session)
        wo, op = make_wo_with_operation(db_session, work_center=wc)
        indented = "\n  1. Tack four corners\n\n  2. Full weld <REF> per WPS-114\n"
        set_guidance(db_session, wo, op, notes=indented, run="Hold 12.500 <TYP> & purge")
        db_session.commit()

        row = queue_row(client, user_headers(operator), wc.id, op.id)
        assert row["work_order_notes"] == indented, "must be byte-identical, not stripped"
        assert row["operation_run_instructions"] == "Hold 12.500 <TYP> & purge"

    def test_guidance_text_helper_directly(self):
        """The one normalizer, unit-tested: None passthrough, blank->None,
        verbatim otherwise (the returned object is the input, not a copy)."""
        assert _guidance_text(None) is None
        assert _guidance_text("") is None
        assert _guidance_text("   \n\t ") is None
        original = "  keep\n  my\n  indentation  "
        assert _guidance_text(original) is original


class TestGuidanceOnHeldRows:
    def test_held_row_inherits_the_same_five_keys(self, client: TestClient, db_session: Session):
        """``_held_job_row`` builds on ``_kiosk_job_row``, so the held list gets
        the guidance with no code of its own. Pinned because that inheritance is
        exactly what a future hand-rolled held row would silently drop."""
        operator = make_user(db_session)
        wc = make_work_center(db_session)
        wo, op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
        set_guidance(db_session, wo, op)

        body = queue_body(client, user_headers(operator), wc.id)
        held = [r for r in body["held"] if r["operation_id"] == op.id]
        assert held, f"held operation missing from held list: {body['held']}"
        row = held[0]

        assert GUIDANCE_KEYS <= set(row), f"missing: {GUIDANCE_KEYS - set(row)}"
        assert row["work_order_notes"] == "Unit #7 -- match to the tag on the fixture"
        assert row["operation_run_instructions"] == "Run at 180 A, weave 1/8 in"
        # Still a held row, not a queued one.
        assert row["startable"] is False
        assert not any(r["operation_id"] == op.id for r in body["queue"])


class TestGuidanceOnMyActiveJob:
    def test_running_job_panel_carries_all_five(self, client: TestClient, db_session: Session):
        """The screen an operator stares at after clocking in -- a separate
        hand-built dict, fed by the SAME builder so the shapes cannot drift."""
        operator = make_user(db_session)
        headers = user_headers(operator)
        wc = make_work_center(db_session)
        wo, op = make_wo_with_operation(db_session, work_center=wc)
        set_guidance(db_session, wo, op)
        clock_in(client, headers, wo, op)

        resp = client.get(MY_ACTIVE_JOB, headers=headers)
        assert resp.status_code == status.HTTP_200_OK, resp.text
        job = resp.json()["active_job"]

        assert GUIDANCE_KEYS <= set(job), f"missing: {GUIDANCE_KEYS - set(job)}"
        assert job["work_order_notes"] == "Unit #7 -- match to the tag on the fixture"
        assert job["work_order_special_instructions"] == "Weld per WPS-114. Purge backside."
        assert job["operation_description"] == "Fit and tack the housing halves"
        assert job["operation_setup_instructions"] == "Set fixture stops to 12.500 in"
        assert job["operation_run_instructions"] == "Run at 180 A, weave 1/8 in"

    def test_queue_row_and_active_job_agree_field_for_field(self, client: TestClient, db_session: Session):
        """One contract, two surfaces: the five values must be equal on both."""
        operator = make_user(db_session)
        headers = user_headers(operator)
        wc = make_work_center(db_session)
        wo, op = make_wo_with_operation(db_session, work_center=wc)
        set_guidance(db_session, wo, op, setup=None)
        clock_in(client, headers, wo, op)

        row = queue_row(client, headers, wc.id, op.id)
        job = client.get(MY_ACTIVE_JOB, headers=headers).json()["active_job"]

        assert {k: row[k] for k in GUIDANCE_KEYS} == {k: job[k] for k in GUIDANCE_KEYS}
        assert job["operation_setup_instructions"] is None

    def test_blank_normalizes_to_none_on_the_active_job_too(self, client: TestClient, db_session: Session):
        operator = make_user(db_session)
        headers = user_headers(operator)
        wc = make_work_center(db_session)
        wo, op = make_wo_with_operation(db_session, work_center=wc)
        set_guidance(db_session, wo, op, notes="   ", special="", description="\n", setup="\t", run="  ")
        clock_in(client, headers, wo, op)

        job = client.get(MY_ACTIVE_JOB, headers=headers).json()["active_job"]
        for key in GUIDANCE_KEYS:
            assert job[key] is None, key


class TestStationDisclosure:
    def test_station_gets_the_guidance_AND_still_loses_the_blocker_free_text(
        self, client: TestClient, db_session: Session
    ):
        """BOTH HALVES, DELIBERATELY IN ONE TEST.

        A crew station is an unattended, PIN-unlocked tablet. It receives the
        five planning fields -- the owner's explicit decision (2026-08-14), and
        the entire point of the feature, since the crew station is the screen
        that could not see the note. It must STILL NOT receive the blocker's
        ``note``/``title``, which ``_hold_blocker_payload`` withholds because
        that text is operator-authored incident detail, not planning text.

        Asserted together because the realistic failure is a later reader
        harmonizing the two rules in one direction or the other.
        """
        operator = make_user(db_session)
        wc = make_work_center(db_session)
        station = make_kiosk_station(db_session, work_center=wc)
        wo, op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
        set_guidance(db_session, wo, op)
        db_session.add(
            WorkOrderBlocker(
                company_id=COMPANY_A,
                work_order_id=wo.id,
                operation_id=op.id,
                category=WorkOrderBlockerCategory.QUALITY_HOLD,
                severity=WorkOrderBlockerSeverity.HIGH,
                status=WorkOrderBlockerStatus.OPEN,
                title="NCR-1042 cracked welds, ACME rejected lot",
                note="Customer will not take these; scrap and re-run.",
                reported_by=operator.id,
            )
        )
        db_session.commit()

        body = queue_body(client, bearer(kiosk_token_for(station)), wc.id)
        row = [r for r in body["held"] if r["operation_id"] == op.id][0]

        # Half 1: the planning text IS disclosed to the station.
        assert row["work_order_notes"] == "Unit #7 -- match to the tag on the fixture"
        assert row["work_order_special_instructions"] == "Weld per WPS-114. Purge backside."
        assert row["operation_run_instructions"] == "Run at 180 A, weave 1/8 in"

        # Half 2: the blocker's free text is NOT -- keys ABSENT, not blanked.
        blocker = row["hold"]["blocker"]
        assert "note" not in blocker
        assert "title" not in blocker
        assert blocker["free_text_withheld"] is True
        assert blocker["has_note"] is True
        assert blocker["category"] == WorkOrderBlockerCategory.QUALITY_HOLD.value

    def test_identified_user_gets_both(self, client: TestClient, db_session: Session):
        """The other side of the same gate: a user session keeps the blocker text
        it always had, and gains the guidance."""
        operator = make_user(db_session)
        wc = make_work_center(db_session)
        wo, op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
        set_guidance(db_session, wo, op)
        db_session.add(
            WorkOrderBlocker(
                company_id=COMPANY_A,
                work_order_id=wo.id,
                operation_id=op.id,
                category=WorkOrderBlockerCategory.QUALITY_HOLD,
                severity=WorkOrderBlockerSeverity.HIGH,
                status=WorkOrderBlockerStatus.OPEN,
                title="NCR-1042 cracked welds",
                note="Scrap and re-run.",
                reported_by=operator.id,
            )
        )
        db_session.commit()

        body = queue_body(client, user_headers(operator), wc.id)
        row = [r for r in body["held"] if r["operation_id"] == op.id][0]

        assert row["work_order_notes"] == "Unit #7 -- match to the tag on the fixture"
        assert row["hold"]["blocker"]["note"] == "Scrap and re-run."
        assert row["hold"]["blocker"]["free_text_withheld"] is False


class TestTenancyUnaffected:
    def test_station_cannot_read_another_work_centers_guidance(self, client: TestClient, db_session: Session):
        """A station is bound to one work center by its DB row, never the client
        -- adding guidance to the payload does not widen that."""
        wc_a = make_work_center(db_session)
        wc_other = make_work_center(db_session)
        station = make_kiosk_station(db_session, work_center=wc_a)
        wo, op = make_wo_with_operation(db_session, work_center=wc_other)
        set_guidance(db_session, wo, op)

        resp = client.get(queue_url(wc_other.id), headers=bearer(kiosk_token_for(station)))
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    def test_cross_tenant_work_center_yields_no_guidance(self, client: TestClient, db_session: Session):
        """Company B's guidance is unreachable from a Company A session, and the
        read still answers empty rather than 404-ing (which would confirm the id
        exists elsewhere)."""
        operator_a = make_user(db_session, company_id=COMPANY_A)
        wc_b = make_work_center(db_session, company_id=COMPANY_B)
        wo_b, op_b = make_wo_with_operation(db_session, company_id=COMPANY_B, work_center=wc_b)
        set_guidance(db_session, wo_b, op_b, notes="COMPANY B CONFIDENTIAL")

        body = queue_body(client, user_headers(operator_a), wc_b.id)
        assert body["queue"] == []
        assert body["held"] == []
        assert "COMPANY B CONFIDENTIAL" not in str(body)

    def test_active_job_guidance_is_scoped_to_the_operators_own_entries(self, client: TestClient, db_session: Session):
        """my-active-job is keyed on the CALLER's open entries; another
        operator's running job (and its notes) never appears."""
        alice = make_user(db_session)
        bob = make_user(db_session)
        wc = make_work_center(db_session)
        wo, op = make_wo_with_operation(db_session, work_center=wc)
        set_guidance(db_session, wo, op, notes="ALICE ONLY")
        clock_in(client, user_headers(alice), wo, op)

        body = client.get(MY_ACTIVE_JOB, headers=user_headers(bob)).json()
        assert body["active_jobs"] == []
        assert body["active_job"] is None
        assert "ALICE ONLY" not in str(body)


class TestNoNPlusOne:
    def test_queue_cost_is_flat_in_the_number_of_rows(self, client: TestClient, db_session: Session):
        """Five queued operations must cost exactly what one costs.

        ``work_order.notes`` / ``.special_instructions`` are read per row. They
        are free ONLY because the endpoint joinedloads ``work_order``; drop that
        eager load and this becomes one extra SELECT per card, at the 10-15s poll
        cadence, on every station in the shop.
        """
        operator = make_user(db_session)
        headers = user_headers(operator)
        wc = make_work_center(db_session)
        wo, op1 = make_wo_with_operation(db_session, work_center=wc, sequential_operations=False)
        set_guidance(db_session, wo, op1)

        db_session.expire_all()
        one_row = count_queries(db_session, lambda: queue_body(client, headers, wc.id))

        # DISTINCT work orders, so a lazy work_order load would fan out per row
        # rather than hitting one identity-map entry five times over.
        for _ in range(4):
            wo_n, op_n = make_wo_with_operation(db_session, work_center=wc, sequential_operations=False)
            set_guidance(db_session, wo_n, op_n)

        db_session.expire_all()
        assert len(queue_body(client, headers, wc.id)["queue"]) == 5
        db_session.expire_all()
        five_rows = count_queries(db_session, lambda: queue_body(client, headers, wc.id))

        assert five_rows == one_row, f"per-row N+1: 1 row cost {one_row}, 5 rows cost {five_rows}"

    def test_guidance_block_itself_issues_zero_queries(self, db_session: Session):
        """Attribution, isolated from the rest of the endpoint: against an
        operation loaded with the queue's ``work_order`` eager load, building the
        guidance block touches the DB ZERO times. All five are plain,
        non-deferred columns on already-loaded entities."""
        wc = make_work_center(db_session)
        wo, op = make_wo_with_operation(db_session, work_center=wc)
        set_guidance(db_session, wo, op)

        db_session.expire_all()
        operations = (
            db_session.query(WorkOrderOperation)
            .options(joinedload(WorkOrderOperation.work_order))
            .filter(WorkOrderOperation.work_center_id == wc.id)
            .all()
        )
        assert operations

        def _build():
            for operation in operations:
                fields = _job_guidance_fields(operation.work_order, operation)
                assert fields["work_order_notes"] == "Unit #7 -- match to the tag on the fixture"

        assert count_queries(db_session, _build) == 0

    def test_my_active_job_guidance_reads_cost_zero_queries(self, client: TestClient, db_session: Session):
        """Same attribution for the running-job panel, whose entities hang off
        ``TimeEntry`` rather than the operation.

        The endpoint joinedloads ``TimeEntry.operation`` and
        ``TimeEntry.work_order``, so the guidance block reads five plain columns
        off already-loaded entities. Reproduced here with the endpoint's own
        eager loads over TWO open entries, so a lazy relationship would fan out.
        """
        operator = make_user(db_session)
        headers = user_headers(operator)
        for _ in range(2):
            wc = make_work_center(db_session)
            wo, op = make_wo_with_operation(db_session, work_center=wc)
            set_guidance(db_session, wo, op)
            clock_in(client, headers, wo, op)

        db_session.expire_all()
        entries = (
            db_session.query(TimeEntry)
            .options(
                joinedload(TimeEntry.operation),
                joinedload(TimeEntry.work_order),
            )
            .filter(TimeEntry.user_id == operator.id, TimeEntry.clock_out.is_(None))
            .all()
        )
        assert len(entries) == 2

        def _build():
            for entry in entries:
                fields = _job_guidance_fields(entry.work_order, entry.operation)
                assert fields["operation_run_instructions"] == "Run at 180 A, weave 1/8 in"

        assert count_queries(db_session, _build) == 0
