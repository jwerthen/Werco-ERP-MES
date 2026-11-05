"""Dispatch v2: the floor NOTICE -- "the planner changed this machine's order".

Spec 3g / 4c. Every dispatch-order writer (the work-order plan verb, the
operation-order verb, the column rewrite, the four machine-move paths) emits ONE
``dispatch_order_changed`` OperationalEvent per work center whose ordered queue
actually changed, and the floor reads the newest one back on the polls it already
makes:

* emitted only for machines whose order changed -- none on a no-op save;
* the payload is frozen at write time, has exactly the documented keys, and none of
  them trips ``redact_event_payload``;
* ``actor_display`` is "First L.", ``next_up`` is the first READY job (a running head
  is skipped), ``moved_up`` is RELATIVE (an arrival or departure moves nobody up);
* the notice is best-effort: an emit failure (pre-flush or a DB flush failure) never
  500s the reorder, and restores the outbox ids that were already pending;
* a rolled-back write leaves no notice and enqueues no notification;
* the after-commit websocket nudge carries the company explicitly;
* the reads: the crew station sees exactly the documented keys on the queue,
  ``my-active-job`` only for the machines the caller holds an open LABOR entry at
  (via the operation's CURRENT machine), ``/shop-floor/operations`` only when
  filtered to one machine; a 12-hour window; tenant-scoped; one query;
* forgery and bad rows: ``POST /operational-events/`` refuses this event type (and
  writes / enqueues nothing); a future-dated row is never served or counted; a
  malformed row never 500s a floor read and never outranks a real notice -- the
  machine falls back to its newest VALID notice, inside the same single query.
"""

from datetime import datetime, timedelta
from typing import List

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import app.core.realtime as realtime
import app.services.notification_outbox as notification_outbox
from app.models.audit_log import AuditLog
from app.models.operational_event import OperationalEvent
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation
from app.schemas.dispatch import DispatchNotice, DispatchNoticeNextUp
from app.services import dispatch_service
from app.services.operational_event_service import OperationalEventService, redact_event_payload
from tests.api.dispatch_plan_helpers import (
    count_statements,
    make_plan_wo,
    operation_order_url,
    plan,
    ready,
    run_order_url,
)
from tests.api.export_audit_helpers import fresh_session
from tests.api.kiosk_test_helpers import (
    COMPANY_A,
    COMPANY_B,
    bearer,
    kiosk_token_for,
    make_kiosk_station,
    make_user,
    make_work_center,
    queue_url,
    user_headers,
)
from tests.api.test_dispatch_operation_order import MOVE_PATHS, _move

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

MY_ACTIVE_JOB_URL = "/api/v1/shop-floor/my-active-job"
OPERATIONS_URL = "/api/v1/shop-floor/operations"

PAYLOAD_KEYS = {
    "work_center_id",
    "work_center_code",
    "change_kind",
    "actor_display",
    "next_up_before",
    "next_up_after",
    "moved_up",
    "queue_length",
}
NEXT_UP_KEYS = {"operation_id", "work_order_number", "unit_number", "operation_number", "operation_name", "part_number"}
NOTICE_KEYS = {
    "id",
    "occurred_at",
    "work_center_id",
    "work_center_code",
    "change_kind",
    "actor_display",
    "next_up_before",
    "next_up_after",
    "moved_up",
    "updates_in_window",
}

# A dispatch notice payload the schema refuses: ``work_center_code`` must be a string.
# Before the reader skipped bad rows, ONE such row made every floor read at that
# machine (kiosk queue, my-active-job, filtered /shop-floor/operations) a 500.
MALFORMED_PAYLOAD = {"work_center_code": 123, "change_kind": "machine_order", "moved_up": []}


@pytest.fixture(autouse=True)
def _no_outbox_enqueue(monkeypatch):
    """Keep the after-commit outbox tee off Redis; record what it would have enqueued."""
    enqueued: List[int] = []
    monkeypatch.setattr(notification_outbox, "_enqueue_dispatch", lambda event_id: enqueued.append(event_id))
    return enqueued


def _manager(db: Session):
    return make_user(db, role=UserRole.MANAGER, first_name="Jon", last_name="werthen")


def _notices(db: Session, company_id: int = COMPANY_A) -> List[OperationalEvent]:
    db.expire_all()
    return (
        db.query(OperationalEvent)
        .filter(
            OperationalEvent.company_id == company_id,
            OperationalEvent.event_type == dispatch_service.DISPATCH_NOTICE_EVENT_TYPE,
        )
        .order_by(OperationalEvent.id)
        .all()
    )


def _column(client: TestClient, headers: dict, wc_id: int, ids: List[int]):
    return client.put(run_order_url(wc_id), json={"operation_ids": ids}, headers=headers)


def _clock_in(db: Session, user, operation: WorkOrderOperation, *, entry_type=TimeEntryType.RUN, work_center_id=None):
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=operation.work_order_id,
        operation_id=operation.id,
        work_center_id=work_center_id if work_center_id is not None else operation.work_center_id,
        entry_type=entry_type,
        clock_in=datetime.utcnow(),
        company_id=operation.company_id,
    )
    db.add(entry)
    db.commit()
    return entry


# --------------------------------------------------------------------------
# 1. Which machines get a notice
# --------------------------------------------------------------------------


class TestWhichMachinesGetANotice:
    def test_a_plan_change_notifies_only_the_machines_whose_order_changed(
        self, client: TestClient, db_session: Session
    ):
        manager = _manager(db_session)
        headers = user_headers(manager)
        brake, weld, saw = (make_work_center(db_session) for _ in range(3))
        a, (a_brake, a_weld) = make_plan_wo(db_session, [ready(brake, 10), ready(weld, 20)], sequential=False)
        b, (b_brake,) = make_plan_wo(db_session, [ready(brake)])
        make_plan_wo(db_session, [ready(saw)])  # never planned: its machine is not a candidate

        resp = plan(client, headers, [b.id, a.id], expected=[])
        assert resp.status_code == status.HTTP_200_OK, resp.text

        notices = _notices(db_session)
        # BRAKE flipped (A then B -> B then A); WELD holds only A's op, so its order is
        # unchanged; SAW holds no re-ranked work at all.
        assert [event.entity_id for event in notices] == [brake.id]
        event = notices[0]
        assert event.entity_type == "work_center"
        assert event.source_module == "dispatch"
        assert event.user_id == manager.id
        assert event.work_order_id is None and event.operation_id is None
        assert event.event_payload["change_kind"] == "work_order_order"
        assert event.event_payload["moved_up"] == [b_brake.id]
        assert event.event_payload["next_up_before"]["operation_id"] == a_brake.id
        assert event.event_payload["next_up_after"]["operation_id"] == b_brake.id
        assert event.event_payload["queue_length"] == 2

    def test_a_no_op_plan_save_emits_nothing(self, client: TestClient, db_session: Session):
        headers = user_headers(_manager(db_session))
        wc = make_work_center(db_session)
        a, _ = make_plan_wo(db_session, [ready(wc)], dispatch_rank=1)
        assert plan(client, headers, [a.id], expected=[a.id]).status_code == status.HTTP_200_OK
        assert _notices(db_session) == []

    def test_a_renumber_that_keeps_the_order_emits_nothing(self, client: TestClient, db_session: Session):
        """A rank write that does not change any machine's ORDER is not news to the floor."""
        headers = user_headers(_manager(db_session))
        wc = make_work_center(db_session)
        a, _ = make_plan_wo(db_session, [ready(wc)], dispatch_rank=3)
        b, _ = make_plan_wo(db_session, [ready(wc)], dispatch_rank=7)
        resp = plan(client, headers, [a.id, b.id], expected=[a.id, b.id])
        assert resp.status_code == status.HTTP_200_OK, resp.text
        db_session.expire_all()
        assert (db_session.get(WorkOrder, a.id).dispatch_rank, db_session.get(WorkOrder, b.id).dispatch_rank) == (1, 2)
        assert _notices(db_session) == []

    def test_a_column_rewrite_notifies_its_machine(self, client: TestClient, db_session: Session):
        headers = user_headers(_manager(db_session))
        wc = make_work_center(db_session)
        _, (a,) = make_plan_wo(db_session, [ready(wc)])
        _, (b,) = make_plan_wo(db_session, [ready(wc)])
        assert _column(client, headers, wc.id, [b.id, a.id]).status_code == status.HTTP_200_OK
        notices = _notices(db_session)
        assert [(event.entity_id, event.event_payload["change_kind"]) for event in notices] == [
            (wc.id, "machine_order")
        ]
        assert notices[0].event_payload["moved_up"] == [b.id]

    def test_a_column_rewrite_that_keeps_the_order_emits_nothing(self, client: TestClient, db_session: Session):
        headers = user_headers(_manager(db_session))
        wc = make_work_center(db_session)
        _, (a,) = make_plan_wo(db_session, [ready(wc, run_order=1)])
        _, (b,) = make_plan_wo(db_session, [ready(wc, run_order=2)])
        assert _column(client, headers, wc.id, [a.id, b.id]).status_code == status.HTTP_200_OK
        assert _notices(db_session) == []

    def test_an_operation_order_change_notifies_with_its_kind(self, client: TestClient, db_session: Session):
        headers = user_headers(_manager(db_session))
        brake = make_work_center(db_session)
        wo, (op10, op20) = make_plan_wo(
            db_session, [ready(brake, 10), ready(brake, 20)], sequential=False, dispatch_rank=1
        )
        resp = client.put(
            operation_order_url(wo.id),
            json={"operation_ids": [op20.id, op10.id], "expected_operation_ids": [op10.id, op20.id]},
            headers=headers,
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        notices = _notices(db_session)
        assert [(event.entity_id, event.event_payload["change_kind"]) for event in notices] == [
            (brake.id, "operation_order")
        ]
        assert notices[0].event_payload["moved_up"] == [op20.id]

    @pytest.mark.parametrize("route", MOVE_PATHS)
    def test_a_move_notifies_the_old_and_the_new_machine(self, client: TestClient, db_session: Session, route):
        headers = user_headers(_manager(db_session))
        source = make_work_center(db_session)
        target = make_work_center(db_session)
        _, (incumbent,) = make_plan_wo(db_session, [ready(target)])
        mover_wo, (mover,) = make_plan_wo(db_session, [ready(source)])

        resp = _move(client, headers, route, mover, mover_wo.id, target.id)
        assert resp.status_code == status.HTTP_200_OK, resp.text

        by_machine = {event.entity_id: event.event_payload for event in _notices(db_session)}
        assert set(by_machine) == {source.id, target.id}
        assert by_machine[source.id]["change_kind"] == "moved_out"
        assert by_machine[target.id]["change_kind"] == "moved_in"
        assert by_machine[source.id]["next_up_before"]["operation_id"] == mover.id
        assert by_machine[source.id]["next_up_after"] is None
        assert by_machine[source.id]["queue_length"] == 0
        assert by_machine[target.id]["queue_length"] == 2
        # An arrival / a departure alone moves nobody "up".
        assert by_machine[source.id]["moved_up"] == [] and by_machine[target.id]["moved_up"] == []

    def test_a_conflicting_save_emits_nothing(self, client: TestClient, db_session: Session):
        headers = user_headers(_manager(db_session))
        wc = make_work_center(db_session)
        a, _ = make_plan_wo(db_session, [ready(wc)], dispatch_rank=1)
        b, _ = make_plan_wo(db_session, [ready(wc)])
        resp = plan(client, headers, [b.id, a.id], expected=[])
        assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
        assert _notices(db_session) == []


# --------------------------------------------------------------------------
# 2. The payload
# --------------------------------------------------------------------------


class TestPayload:
    def test_exact_keys_redaction_safe_and_first_name_last_initial(self, client: TestClient, db_session: Session):
        headers = user_headers(_manager(db_session))
        wc = make_work_center(db_session)
        a_wo, (a,) = make_plan_wo(db_session, [ready(wc)], unit_number="U-7")
        _, (b,) = make_plan_wo(db_session, [ready(wc)])
        assert _column(client, headers, wc.id, [b.id, a.id]).status_code == status.HTTP_200_OK

        (event,) = _notices(db_session)
        payload = event.event_payload
        assert set(payload) == PAYLOAD_KEYS
        assert set(payload["next_up_before"]) == NEXT_UP_KEYS and set(payload["next_up_after"]) == NEXT_UP_KEYS
        # Nothing in it trips the emit-time redaction (a key containing "token", "cui", ...).
        assert redact_event_payload(payload) == payload
        assert payload["actor_display"] == "Jon W."
        assert payload["work_center_id"] == wc.id
        assert payload["work_center_code"] == wc.code
        assert payload["next_up_before"] == {
            "operation_id": a.id,
            "work_order_number": a_wo.work_order_number,
            "unit_number": "U-7",
            "operation_number": a.operation_number,
            "operation_name": a.name,
            "part_number": a_wo.part.part_number,
        }

    def test_next_up_skips_running_work_at_the_head(self, client: TestClient, db_session: Session):
        headers = user_headers(_manager(db_session))
        wc = make_work_center(db_session)
        _, (running,) = make_plan_wo(db_session, [(wc, OperationStatus.IN_PROGRESS, 10)])
        _, (a,) = make_plan_wo(db_session, [ready(wc)])
        _, (b,) = make_plan_wo(db_session, [ready(wc)])
        assert _column(client, headers, wc.id, [running.id, b.id, a.id]).status_code == status.HTTP_200_OK

        (event,) = _notices(db_session)
        assert event.event_payload["next_up_before"]["operation_id"] == a.id
        assert event.event_payload["next_up_after"]["operation_id"] == b.id
        assert event.event_payload["moved_up"] == [b.id]

    def test_moved_up_is_relative_order_and_capped(self):
        before = list(range(1, 16))
        after = before[1:] + [1]
        # The old head dropped to the bottom: every other op moved ahead of it. Capped, queue order.
        assert dispatch_service._moved_up(before, after) == after[: dispatch_service.MAX_NOTICE_MOVED_UP]
        # A full reversal: only the upper half improved its position.
        assert dispatch_service._moved_up(before, list(reversed(before))) == [15, 14, 13, 12, 11, 10, 9]
        # A departure shifts everyone below it, but nobody passed anybody.
        assert dispatch_service._moved_up([1, 2, 3], [2, 3]) == []
        # An arrival at the top does not mark the cards it pushed down, nor itself.
        assert dispatch_service._moved_up([1, 2], [9, 1, 2]) == []
        assert dispatch_service._moved_up([1, 2, 3], [1, 3, 2]) == [3]

    def test_the_payload_is_frozen_at_write_time(self, client: TestClient, db_session: Session):
        headers = user_headers(_manager(db_session))
        wc = make_work_center(db_session)
        _, (a,) = make_plan_wo(db_session, [ready(wc)])
        _, (b,) = make_plan_wo(db_session, [ready(wc)])
        assert _column(client, headers, wc.id, [b.id, a.id]).status_code == status.HTTP_200_OK
        db_session.get(WorkOrderOperation, b.id).name = "Renamed later"
        db_session.commit()
        body = client.get(queue_url(wc.id), headers=headers).json()
        assert body["dispatch_notice"]["next_up_after"]["operation_name"] != "Renamed later"


# --------------------------------------------------------------------------
# 3. Best-effort, transactional
# --------------------------------------------------------------------------


def _pre_flush_failure(self, **kwargs):
    raise ValueError("event store is down")


def _db_flush_failure(self, **kwargs):
    # A NOT NULL violation AT THE DATABASE -- the case that, without the savepoint,
    # poisons the outer transaction even though emit_best_effort swallows it.
    self.db.add(OperationalEvent(company_id=kwargs["company_id"], event_type=None, source_module="dispatch"))
    self.db.flush()


class TestBestEffort:
    @pytest.mark.parametrize("failure", [_pre_flush_failure, _db_flush_failure], ids=["pre_flush", "db_flush"])
    def test_an_emit_failure_never_fails_the_reorder(
        self, client: TestClient, db_session: Session, monkeypatch, failure, _no_outbox_enqueue
    ):
        headers = user_headers(_manager(db_session))
        wc = make_work_center(db_session)
        a, _ = make_plan_wo(db_session, [ready(wc)])
        b, _ = make_plan_wo(db_session, [ready(wc)])
        monkeypatch.setattr(OperationalEventService, "emit", failure)

        resp = plan(client, headers, [b.id, a.id], expected=[])
        assert resp.status_code == status.HTTP_200_OK, resp.text
        db_session.expire_all()
        assert (db_session.get(WorkOrder, b.id).dispatch_rank, db_session.get(WorkOrder, a.id).dispatch_rank) == (1, 2)
        assert db_session.query(AuditLog).filter(AuditLog.resource_type == "dispatch_board").count() == 1
        assert _notices(db_session) == []
        assert _no_outbox_enqueue == []

    def test_a_failed_notice_restores_the_outbox_ids_already_pending(self, db_session: Session, monkeypatch):
        manager = _manager(db_session)
        wc = make_work_center(db_session)
        _, (a,) = make_plan_wo(db_session, [ready(wc)])
        _, (b,) = make_plan_wo(db_session, [ready(wc)])
        before = dispatch_service.snapshot_queue_orders(db_session, COMPANY_A, [wc.id])
        db_session.get(WorkOrderOperation, b.id).run_order = 1
        db_session.flush()
        db_session.info[notification_outbox._PENDING_KEY] = [424242]
        monkeypatch.setattr(OperationalEventService, "emit", _db_flush_failure)

        changed = dispatch_service.record_dispatch_change(
            db_session, COMPANY_A, manager, before, dispatch_service.DispatchChangeKind.MACHINE_ORDER
        )
        assert changed == []
        # The savepoint rollback dropped only what was emitted inside it; the id that
        # belongs to the still-live outer transaction is still pending.
        assert db_session.info[notification_outbox._PENDING_KEY] == [424242]
        # ... and the outer transaction is still usable.
        assert db_session.get(WorkOrderOperation, b.id).run_order == 1
        db_session.info.pop(notification_outbox._PENDING_KEY, None)
        db_session.rollback()

    def test_a_rolled_back_write_leaves_no_notice_and_notifies_nobody(
        self, client: TestClient, db_session: Session, monkeypatch, _no_outbox_enqueue
    ):
        headers = user_headers(_manager(db_session))
        wc = make_work_center(db_session)
        _, (a,) = make_plan_wo(db_session, [ready(wc)])
        _, (b,) = make_plan_wo(db_session, [ready(wc)])
        seen = []
        real_record = dispatch_service.record_dispatch_change

        def _record_then_note(*args, **kwargs):
            changed = real_record(*args, **kwargs)
            seen.append(changed)
            return changed

        def _boom(*args, **kwargs):
            raise RuntimeError("projection failed after the notice")

        monkeypatch.setattr(dispatch_service, "record_dispatch_change", _record_then_note)
        # Runs AFTER the notice seam inside the column PUT, before its commit.
        monkeypatch.setattr(dispatch_service, "plan_positions_for", _boom)
        try:
            resp = _column(client, headers, wc.id, [b.id, a.id])
            assert resp.status_code >= 500
        except RuntimeError:
            pass
        assert seen == [[wc.id]]  # the notice WAS emitted (flushed) inside the request
        db_session.rollback()

        assert _notices(db_session) == []
        assert _no_outbox_enqueue == []
        assert db_session.info.get(notification_outbox._PENDING_KEY) is None

    def test_the_outbox_enqueues_at_the_real_commit_not_at_the_savepoint_release(
        self, db_session: Session, _no_outbox_enqueue
    ):
        """SQLAlchemy fires ``after_commit`` on a SAVEPOINT release too. The notice's own
        savepoint is released inside ``record_dispatch_change``, with no shield around it;
        the outbox must still wait for the outer commit to enqueue the fan-out."""
        manager = _manager(db_session)
        wc = make_work_center(db_session)
        _, (a,) = make_plan_wo(db_session, [ready(wc)])
        _, (b,) = make_plan_wo(db_session, [ready(wc)])
        before = dispatch_service.snapshot_queue_orders(db_session, COMPANY_A, [wc.id])
        db_session.get(WorkOrderOperation, b.id).run_order = 1
        db_session.flush()

        changed = dispatch_service.record_dispatch_change(
            db_session, COMPANY_A, manager, before, dispatch_service.DispatchChangeKind.MACHINE_ORDER
        )
        assert changed == [wc.id]
        assert _no_outbox_enqueue == []
        pending = list(db_session.info[notification_outbox._PENDING_KEY])
        assert len(pending) == 1

        db_session.commit()
        assert _no_outbox_enqueue == pending

    def test_a_committed_notice_reaches_the_outbox(self, client: TestClient, db_session: Session, monkeypatch):
        """Once, and only after the request's COMMIT. The run-order PUT audits first
        (``AuditService`` leaves its savepoint open) and then emits inside the notice's
        own savepoint, so ``db.commit()`` releases two savepoints before the real COMMIT;
        an enqueue at either release hands the worker an event it cannot load yet. The
        probe reads the event on a SEPARATE connection at the moment of the enqueue,
        which can only see committed rows."""
        headers = user_headers(_manager(db_session))
        wc = make_work_center(db_session)
        _, (a,) = make_plan_wo(db_session, [ready(wc)])
        _, (b,) = make_plan_wo(db_session, [ready(wc)])
        enqueued = []

        def _probe(event_id: int) -> None:
            try:
                with fresh_session() as verify:
                    committed = verify.get(OperationalEvent, event_id) is not None
            except OperationalError:  # shared-cache table lock: the writer has not committed
                committed = False
            enqueued.append((event_id, committed))

        monkeypatch.setattr(notification_outbox, "_enqueue_dispatch", _probe)

        assert _column(client, headers, wc.id, [b.id, a.id]).status_code == status.HTTP_200_OK

        (notice,) = _notices(db_session)
        assert enqueued == [(notice.id, True)]
        audit_rows = db_session.query(AuditLog).filter(
            AuditLog.resource_type == "work_center", AuditLog.resource_id == wc.id
        )
        assert audit_rows.count() == 1


class TestBroadcast:
    def test_each_changed_machine_is_nudged_with_the_company(
        self, client: TestClient, db_session: Session, monkeypatch
    ):
        calls = []
        monkeypatch.setattr(realtime, "safe_broadcast", lambda fn, *args, **kwargs: calls.append((fn, args, kwargs)))
        headers = user_headers(_manager(db_session))
        source = make_work_center(db_session)
        target = make_work_center(db_session)
        mover_wo, (mover,) = make_plan_wo(db_session, [ready(source)])
        resp = _move(client, headers, "scheduling_move", mover, mover_wo.id, target.id)
        assert resp.status_code == status.HTTP_200_OK, resp.text

        nudges = [
            (args[0], args[1], kwargs)
            for fn, args, kwargs in calls
            if fn.__name__ == "broadcast_shop_floor_update" and args[1].get("type") == "dispatch_order_changed"
        ]
        assert sorted(wc_id for wc_id, _, _ in nudges) == sorted([source.id, target.id])
        assert all(kwargs == {"company_id": COMPANY_A} for _, _, kwargs in nudges)

    def test_a_no_op_nudges_nobody(self, client: TestClient, db_session: Session, monkeypatch):
        calls = []
        monkeypatch.setattr(realtime, "safe_broadcast", lambda fn, *args, **kwargs: calls.append(args))
        headers = user_headers(_manager(db_session))
        wc = make_work_center(db_session)
        _, (a,) = make_plan_wo(db_session, [ready(wc, run_order=1)])
        assert _column(client, headers, wc.id, [a.id]).status_code == status.HTTP_200_OK
        assert calls == []


# --------------------------------------------------------------------------
# 4. The floor reads
# --------------------------------------------------------------------------


class TestFloorReads:
    def test_the_crew_station_sees_exactly_the_documented_keys(self, client: TestClient, db_session: Session):
        headers = user_headers(_manager(db_session))
        wc = make_work_center(db_session)
        station = make_kiosk_station(db_session, work_center=wc)
        _, (a,) = make_plan_wo(db_session, [ready(wc)])
        _, (b,) = make_plan_wo(db_session, [ready(wc)])
        assert _column(client, headers, wc.id, [b.id, a.id]).status_code == status.HTTP_200_OK

        resp = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station)))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        notice = resp.json()["dispatch_notice"]
        assert set(notice) == NOTICE_KEYS == set(DispatchNotice.model_fields)
        assert set(notice["next_up_after"]) == NEXT_UP_KEYS == set(DispatchNoticeNextUp.model_fields)
        assert notice["work_center_id"] == wc.id
        assert notice["change_kind"] == "machine_order"
        assert notice["actor_display"] == "Jon W."
        assert notice["moved_up"] == [b.id]
        assert notice["updates_in_window"] == 1
        assert notice["occurred_at"].endswith("Z")

    def test_the_queue_carries_null_when_nothing_changed(self, client: TestClient, db_session: Session):
        operator = make_user(db_session)
        wc = make_work_center(db_session)
        make_plan_wo(db_session, [ready(wc)])
        body = client.get(queue_url(wc.id), headers=user_headers(operator)).json()
        assert body["dispatch_notice"] is None

    def test_newest_notice_wins_and_the_window_is_counted(self, client: TestClient, db_session: Session):
        headers = user_headers(_manager(db_session))
        wc = make_work_center(db_session)
        _, (a,) = make_plan_wo(db_session, [ready(wc)])
        _, (b,) = make_plan_wo(db_session, [ready(wc)])
        assert _column(client, headers, wc.id, [b.id, a.id]).status_code == status.HTTP_200_OK
        assert _column(client, headers, wc.id, [a.id, b.id]).status_code == status.HTTP_200_OK
        newest = _notices(db_session)[-1]
        notice = client.get(queue_url(wc.id), headers=headers).json()["dispatch_notice"]
        assert notice["id"] == newest.id
        assert notice["updates_in_window"] == 2
        assert notice["moved_up"] == [a.id]

    def test_my_active_job_carries_notices_only_for_the_callers_labor_machines(
        self, client: TestClient, db_session: Session
    ):
        headers = user_headers(_manager(db_session))
        operator = make_user(db_session, first_name="Ana", last_name="Ruiz")
        mine, on_break, elsewhere = (make_work_center(db_session) for _ in range(3))
        _, (running,) = make_plan_wo(db_session, [(mine, OperationStatus.IN_PROGRESS, 10)])
        _, (mine_next,) = make_plan_wo(db_session, [ready(mine)])
        _, (mine_last,) = make_plan_wo(db_session, [ready(mine)])
        _, (break_op,) = make_plan_wo(db_session, [ready(on_break)])
        _, (break_other,) = make_plan_wo(db_session, [ready(on_break)])
        _, (x1,) = make_plan_wo(db_session, [ready(elsewhere)])
        _, (x2,) = make_plan_wo(db_session, [ready(elsewhere)])
        _clock_in(db_session, operator, running)
        _clock_in(db_session, operator, break_op, entry_type=TimeEntryType.BREAK)  # not labor

        for wc, ids in ((mine, [running.id, mine_last.id, mine_next.id]), (on_break, [break_other.id, break_op.id])):
            assert _column(client, headers, wc.id, ids).status_code == status.HTTP_200_OK
        assert _column(client, headers, elsewhere.id, [x2.id, x1.id]).status_code == status.HTTP_200_OK

        body = client.get(MY_ACTIVE_JOB_URL, headers=user_headers(operator)).json()
        assert [notice["work_center_id"] for notice in body["dispatch_notices"]] == [mine.id]
        notice = body["dispatch_notices"][0]
        assert set(notice) == NOTICE_KEYS
        assert notice["next_up_after"]["operation_id"] == mine_last.id

    def test_my_active_job_resolves_the_machine_through_the_operations_current_work_center(
        self, client: TestClient, db_session: Session
    ):
        headers = user_headers(_manager(db_session))
        operator = make_user(db_session)
        old_wc, new_wc = make_work_center(db_session), make_work_center(db_session)
        _, (running,) = make_plan_wo(db_session, [(new_wc, OperationStatus.IN_PROGRESS, 10)])
        _, (a,) = make_plan_wo(db_session, [ready(new_wc)])
        _, (b,) = make_plan_wo(db_session, [ready(new_wc)])
        # The entry still names the machine it was opened at; the operation lives at new_wc now.
        _clock_in(db_session, operator, running, work_center_id=old_wc.id)
        assert _column(client, headers, new_wc.id, [running.id, b.id, a.id]).status_code == status.HTTP_200_OK

        body = client.get(MY_ACTIVE_JOB_URL, headers=user_headers(operator)).json()
        assert [notice["work_center_id"] for notice in body["dispatch_notices"]] == [new_wc.id]

    def test_my_active_job_with_nothing_open_carries_an_empty_list(self, client: TestClient, db_session: Session):
        body = client.get(MY_ACTIVE_JOB_URL, headers=user_headers(make_user(db_session))).json()
        assert body["dispatch_notices"] == []

    def test_operations_carries_notices_only_when_filtered_to_one_machine(
        self, client: TestClient, db_session: Session
    ):
        headers = user_headers(_manager(db_session))
        operator_headers = user_headers(make_user(db_session))
        wc = make_work_center(db_session)
        _, (a,) = make_plan_wo(db_session, [ready(wc)])
        _, (b,) = make_plan_wo(db_session, [ready(wc)])
        assert _column(client, headers, wc.id, [b.id, a.id]).status_code == status.HTTP_200_OK

        filtered = client.get(OPERATIONS_URL, params={"work_center_id": wc.id}, headers=operator_headers).json()
        assert [notice["work_center_id"] for notice in filtered["dispatch_notices"]] == [wc.id]
        assert set(filtered["dispatch_notices"][0]) == NOTICE_KEYS
        assert filtered["server_time"].endswith("Z")

        unfiltered = client.get(OPERATIONS_URL, headers=operator_headers).json()
        assert "dispatch_notices" not in unfiltered and "server_time" not in unfiltered


class TestLatestNoticesHelper:
    def _event(self, db: Session, wc_id: int, hours_ago: float, company_id: int = COMPANY_A, **overrides):
        event = OperationalEvent(
            company_id=company_id,
            event_type=overrides.pop("event_type", dispatch_service.DISPATCH_NOTICE_EVENT_TYPE),
            source_module="dispatch",
            entity_type=overrides.pop("entity_type", "work_center"),
            entity_id=wc_id,
            severity="info",
            event_payload=overrides.pop(
                "event_payload", {"work_center_id": wc_id, "change_kind": "machine_order", "moved_up": []}
            ),
            # A negative ``hours_ago`` dates the row in the future.
            occurred_at=datetime.utcnow() - timedelta(hours=hours_ago),
        )
        db.add(event)
        db.commit()
        return event

    def test_the_window_is_twelve_hours(self, db_session: Session):
        wc = make_work_center(db_session)
        self._event(db_session, wc.id, hours_ago=13)
        inside = self._event(db_session, wc.id, hours_ago=11)
        notices = dispatch_service.latest_dispatch_notices(db_session, COMPANY_A, [wc.id])
        assert notices[wc.id]["id"] == inside.id
        assert notices[wc.id]["updates_in_window"] == 1
        assert dispatch_service.DISPATCH_NOTICE_WINDOW_HOURS == 12

    def test_a_notice_older_than_the_window_is_not_served(self, db_session: Session):
        wc = make_work_center(db_session)
        self._event(db_session, wc.id, hours_ago=13)
        assert dispatch_service.latest_dispatch_notices(db_session, COMPANY_A, [wc.id]) == {}

    def test_tenant_scoped_and_type_scoped(self, db_session: Session):
        wc_b = make_work_center(db_session, company_id=COMPANY_B)
        wc_a = make_work_center(db_session)
        self._event(db_session, wc_b.id, hours_ago=1, company_id=COMPANY_B)
        self._event(db_session, wc_a.id, hours_ago=1, event_type="some_other_event")
        self._event(db_session, wc_a.id, hours_ago=1, entity_type="work_order")
        assert dispatch_service.latest_dispatch_notices(db_session, COMPANY_A, [wc_b.id, wc_a.id]) == {}

    def test_one_query_however_many_machines(self, db_session: Session):
        wcs = [make_work_center(db_session) for _ in range(4)]
        for wc in wcs:
            self._event(db_session, wc.id, hours_ago=2)
            self._event(db_session, wc.id, hours_ago=1)
        ids = [wc.id for wc in wcs]
        result = {}
        assert (
            count_statements(
                db_session, lambda: result.update(dispatch_service.latest_dispatch_notices(db_session, COMPANY_A, ids))
            )
            == 1
        )
        assert sorted(result) == sorted(ids)
        assert all(notice["updates_in_window"] == 2 for notice in result.values())

    @pytest.mark.parametrize("machine_count", [1, 3])
    def test_still_one_query_when_every_machine_must_fall_back_past_bad_rows(
        self, db_session: Session, machine_count: int
    ):
        """The skip-and-fall-back is resolved inside the SAME single query (a fixed read
        depth per machine), never a second probe per machine with a bad newest row."""
        wcs = [make_work_center(db_session) for _ in range(machine_count)]
        valid = {}
        for wc in wcs:
            valid[wc.id] = self._event(db_session, wc.id, hours_ago=2).id
            self._event(db_session, wc.id, hours_ago=1, event_payload=MALFORMED_PAYLOAD)
            self._event(db_session, wc.id, hours_ago=0.5, event_payload=["not", "an", "object"])
            self._event(db_session, wc.id, hours_ago=-24)  # future-dated
        ids = [wc.id for wc in wcs]
        result = {}
        assert (
            count_statements(
                db_session, lambda: result.update(dispatch_service.latest_dispatch_notices(db_session, COMPANY_A, ids))
            )
            == 1
        )
        assert {wc_id: notice["id"] for wc_id, notice in result.items()} == valid

    def test_no_machines_costs_no_query(self, db_session: Session):
        assert (
            count_statements(db_session, lambda: dispatch_service.latest_dispatch_notices(db_session, COMPANY_A, []))
            == 0
        )

    def test_the_notice_query_cost_does_not_grow_with_machines(self, db_session: Session):
        """record_dispatch_change: a fixed number of SELECTs whether 1 or 3 machines changed."""
        manager = _manager(db_session)

        def selects_for(machine_count: int) -> int:
            wcs = [make_work_center(db_session) for _ in range(machine_count)]
            pairs = []
            for wc in wcs:
                _, (a,) = make_plan_wo(db_session, [ready(wc)])
                _, (b,) = make_plan_wo(db_session, [ready(wc)])
                pairs.append(b)
            before = dispatch_service.snapshot_queue_orders(db_session, COMPANY_A, [wc.id for wc in wcs])
            for b in pairs:
                db_session.get(WorkOrderOperation, b.id).run_order = 1
            db_session.flush()
            statements: List[str] = []
            from sqlalchemy import event as sa_event

            def _record(conn, cursor, statement, parameters, context, executemany):
                statements.append(statement)

            bind = db_session.get_bind()
            sa_event.listen(bind, "after_cursor_execute", _record)
            try:
                changed = dispatch_service.record_dispatch_change(
                    db_session, COMPANY_A, manager, before, dispatch_service.DispatchChangeKind.MACHINE_ORDER
                )
            finally:
                sa_event.remove(bind, "after_cursor_execute", _record)
            assert len(changed) == machine_count
            db_session.rollback()
            return sum(1 for statement in statements if statement.lstrip().upper().startswith("SELECT"))

        assert selects_for(1) == selects_for(3)


# --------------------------------------------------------------------------
# 5. Forged and malformed notice rows (BE-1)
# --------------------------------------------------------------------------

EVENTS_URL = "/api/v1/operational-events/"


def _insert_notice_row(db: Session, wc_id: int, payload, occurred_at: datetime) -> OperationalEvent:
    """A ``dispatch_order_changed`` row written straight to the table -- not by the board."""
    event = OperationalEvent(
        company_id=COMPANY_A,
        event_type=dispatch_service.DISPATCH_NOTICE_EVENT_TYPE,
        source_module="dispatch",
        entity_type="work_center",
        entity_id=wc_id,
        severity="info",
        event_payload=payload,
        occurred_at=occurred_at,
    )
    db.add(event)
    db.commit()
    return event


def _forged_payload(wc_id: int, fake_op_id: int) -> dict:
    return {
        "work_center_id": wc_id,
        "work_center_code": "FAKE",
        "change_kind": "machine_order",
        "actor_display": "Boss X.",
        "next_up_before": None,
        "next_up_after": {"operation_id": fake_op_id, "work_order_number": "WO-FAKE"},
        "moved_up": [],
        "queue_length": 1,
    }


class TestPostedNoticesAreRefused:
    @pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.ADMIN])
    @pytest.mark.parametrize("event_type", ["dispatch_order_changed", "  Dispatch_Order_Changed "])
    def test_a_posted_dispatch_notice_is_refused_and_writes_nothing(
        self, client: TestClient, db_session: Session, _no_outbox_enqueue, role, event_type
    ):
        from app.api.endpoints.operational_events import SERVER_ONLY_EVENT_TYPES, server_only_event_type_detail

        assert dispatch_service.DISPATCH_NOTICE_EVENT_TYPE in SERVER_ONLY_EVENT_TYPES
        user = make_user(db_session, role=role)
        wc = make_work_center(db_session)
        before = db_session.query(OperationalEvent).filter(OperationalEvent.company_id == COMPANY_A).count()

        resp = client.post(
            EVENTS_URL,
            json={
                "event_type": event_type,
                "source_module": "dispatch",
                "entity_type": "work_center",
                "entity_id": wc.id,
                "occurred_at": "2030-01-01T00:00:00Z",
                "event_payload": _forged_payload(wc.id, 999999),
            },
            headers=user_headers(user),
        )

        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        assert resp.json()["detail"] == server_only_event_type_detail(event_type)
        db_session.expire_all()
        assert db_session.query(OperationalEvent).filter(OperationalEvent.company_id == COMPANY_A).count() == before
        assert _notices(db_session) == []
        assert _no_outbox_enqueue == []
        # And nothing reached the floor.
        assert client.get(queue_url(wc.id), headers=user_headers(user)).json()["dispatch_notice"] is None

    def test_other_cataloged_event_types_are_still_accepted(self, client: TestClient, db_session: Session):
        """Only the board's own notice type is refused: ``operation_hold`` (posted by the
        hold-context flow and reachable through the MCP tool) still records."""
        user = make_user(db_session)
        wc = make_work_center(db_session)
        wo, (op,) = make_plan_wo(db_session, [(wc, OperationStatus.ON_HOLD, 10)])
        resp = client.post(
            EVENTS_URL,
            json={
                "event_type": "operation_hold",
                "source_module": "shop_floor",
                "work_order_id": wo.id,
                "operation_id": op.id,
                "event_payload": {"category": "material"},
            },
            headers=user_headers(user),
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["event_type"] == "operation_hold"


class TestBadRowsCannotBreakTheFloor:
    def test_malformed_and_future_rows_neither_500_nor_outrank_a_real_notice(
        self, client: TestClient, db_session: Session, caplog
    ):
        headers = user_headers(_manager(db_session))
        operator = make_user(db_session)
        wc = make_work_center(db_session)
        station = make_kiosk_station(db_session, work_center=wc)
        _, (a,) = make_plan_wo(db_session, [ready(wc)])
        _, (b,) = make_plan_wo(db_session, [ready(wc)])
        _clock_in(db_session, operator, a)

        # A well-formed FORGED notice dated in the future, written before the real one:
        # unbounded above, it would win every poll until 2030.
        _insert_notice_row(db_session, wc.id, _forged_payload(wc.id, b.id), datetime(2030, 1, 1))
        assert _column(client, headers, wc.id, [b.id, a.id]).status_code == status.HTTP_200_OK
        (real,) = _notices(db_session)[1:]
        assert real.event_payload["actor_display"] == "Jon W."
        # Two malformed rows NEWER than the real notice: a schema-invalid value and a
        # payload that is not a JSON object at all.
        _insert_notice_row(db_session, wc.id, MALFORMED_PAYLOAD, datetime.utcnow())
        _insert_notice_row(db_session, wc.id, ["not", "an", "object"], datetime.utcnow())

        def assert_real(notice: dict) -> None:
            assert notice["id"] == real.id
            assert notice["actor_display"] == "Jon W."
            assert notice["work_center_code"] == wc.code
            # Counted: the real row and the two malformed in-window rows. Not counted:
            # the future-dated row, which is outside the window.
            assert notice["updates_in_window"] == 3

        caplog.set_level("WARNING", logger="app.services.dispatch_service")
        station_view = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station)))
        assert station_view.status_code == status.HTTP_200_OK, station_view.text
        assert_real(station_view.json()["dispatch_notice"])

        operator_queue = client.get(queue_url(wc.id), headers=user_headers(operator))
        assert operator_queue.status_code == status.HTTP_200_OK, operator_queue.text
        assert_real(operator_queue.json()["dispatch_notice"])

        active = client.get(MY_ACTIVE_JOB_URL, headers=user_headers(operator))
        assert active.status_code == status.HTTP_200_OK, active.text
        (notice,) = active.json()["dispatch_notices"]
        assert_real(notice)

        filtered = client.get(OPERATIONS_URL, params={"work_center_id": wc.id}, headers=user_headers(operator))
        assert filtered.status_code == status.HTTP_200_OK, filtered.text
        (notice,) = filtered.json()["dispatch_notices"]
        assert_real(notice)

        assert any("dispatch notice row skipped" in record.getMessage() for record in caplog.records)

    def test_a_machine_with_only_bad_rows_shows_no_notice_rather_than_a_500(
        self, client: TestClient, db_session: Session
    ):
        operator = make_user(db_session)
        wc = make_work_center(db_session)
        _, (a,) = make_plan_wo(db_session, [ready(wc)])
        _clock_in(db_session, operator, a)
        _insert_notice_row(db_session, wc.id, MALFORMED_PAYLOAD, datetime.utcnow())
        _insert_notice_row(db_session, wc.id, _forged_payload(wc.id, a.id), datetime.utcnow() + timedelta(days=30))

        queue = client.get(queue_url(wc.id), headers=user_headers(operator))
        assert queue.status_code == status.HTTP_200_OK, queue.text
        assert queue.json()["dispatch_notice"] is None
        active = client.get(MY_ACTIVE_JOB_URL, headers=user_headers(operator))
        assert active.status_code == status.HTTP_200_OK, active.text
        assert active.json()["dispatch_notices"] == []
        filtered = client.get(OPERATIONS_URL, params={"work_center_id": wc.id}, headers=user_headers(operator))
        assert filtered.status_code == status.HTTP_200_OK, filtered.text
        assert filtered.json()["dispatch_notices"] == []


class TestNoticeReaderBounds:
    def test_a_future_dated_row_is_never_served_or_counted(self, db_session: Session):
        wc = make_work_center(db_session)
        real = _insert_notice_row(
            db_session, wc.id, {"change_kind": "machine_order"}, datetime.utcnow() - timedelta(minutes=5)
        )
        _insert_notice_row(db_session, wc.id, {"change_kind": "machine_order"}, datetime.utcnow() + timedelta(hours=1))
        notice = dispatch_service.latest_dispatch_notices(db_session, COMPANY_A, [wc.id])[wc.id]
        assert notice["id"] == real.id
        assert notice["updates_in_window"] == 1

    def test_a_malformed_newest_row_falls_back_to_the_newest_valid_one(self, db_session: Session):
        wc = make_work_center(db_session)
        now = datetime.utcnow()
        _insert_notice_row(db_session, wc.id, {"change_kind": "machine_order"}, now - timedelta(minutes=30))
        newest_valid = _insert_notice_row(
            db_session, wc.id, {"change_kind": "operation_order"}, now - timedelta(minutes=20)
        )
        _insert_notice_row(db_session, wc.id, MALFORMED_PAYLOAD, now - timedelta(minutes=10))
        notice = dispatch_service.latest_dispatch_notices(db_session, COMPANY_A, [wc.id])[wc.id]
        assert (notice["id"], notice["change_kind"]) == (newest_valid.id, "operation_order")

    def test_the_fall_back_is_bounded_by_the_read_depth(self, db_session: Session):
        """Only the newest ``DISPATCH_NOTICE_READ_DEPTH`` rows are considered: a valid row
        buried under that many bad ones yields no notice -- never an unbounded scan."""
        wc = make_work_center(db_session)
        now = datetime.utcnow()
        _insert_notice_row(db_session, wc.id, {"change_kind": "machine_order"}, now - timedelta(hours=1))
        for minutes in range(dispatch_service.DISPATCH_NOTICE_READ_DEPTH):
            _insert_notice_row(db_session, wc.id, MALFORMED_PAYLOAD, now - timedelta(minutes=minutes + 1))
        assert dispatch_service.latest_dispatch_notices(db_session, COMPANY_A, [wc.id]) == {}

    def test_moved_up_keeps_plain_ints_only_and_is_capped(self, db_session: Session):
        wc = make_work_center(db_session)
        ids = list(range(1, 40))
        _insert_notice_row(
            db_session,
            wc.id,
            {"moved_up": [True, "7", None, *ids]},
            datetime.utcnow() - timedelta(minutes=1),
        )
        notice = dispatch_service.latest_dispatch_notices(db_session, COMPANY_A, [wc.id])[wc.id]
        assert notice["moved_up"] == ids[: dispatch_service.MAX_NOTICE_MOVED_UP]
