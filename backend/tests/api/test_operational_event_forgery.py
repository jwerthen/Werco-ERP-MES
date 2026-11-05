"""``POST /operational-events/`` cannot forge a server event, and no reader trusts a bad row.

The route depends only on ``get_current_user``, so every signed-in principal -- an
OPERATOR, a VIEWER, an API token -- reaches it. Before this was closed, any of them
could post any ``event_type`` the server writes:

* every type in ``notification_catalog.SOURCE_EVENT_TYPE_TO_KEY`` is tagged for the
  notification outbox at emit, so a forged ``ncr_created`` / ``work_order_released`` /
  ``operation_hold`` fanned real in-app, email and SMS notifications out to other users;
* readers select some types by name and present them as fact (RUN NEXT banners, who
  held an operation, flow and adoption metrics, the work-order timeline);
* a future ``occurred_at`` stayed "the newest" row in every reader ordering by it.

What is pinned here:

* COMPOSITION -- ``SERVER_ONLY_EVENT_TYPES`` holds every cataloged source type and every
  reader-trusted type, and a static scan of ``app/`` proves every
  ``OperationalEvent.event_type`` filter names a refused type (so a new trusting reader
  cannot quietly reopen the hole);
* REFUSAL -- each refused type is 400 for any role and for an API token, in any case or
  padding, and writes no row and enqueues no notification;
* A CUSTOM TYPE STILL WORKS -- and never reaches the outbox;
* THE CLAMP -- a future ``occurred_at`` is stored as now; a past one is kept;
* READERS -- hold context, AI work-order context, the timeline, adoption and flow metrics
  each ignore a future-dated row, and the adoption report survives a non-object payload.
"""

import ast
import importlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.services.notification_outbox as notification_outbox
from app.api.endpoints.operational_events import (
    READER_TRUSTED_EVENT_TYPES,
    SERVER_ONLY_EVENT_TYPES,
    clamp_occurred_at,
    is_server_only_event_type,
    server_only_event_type_detail,
)
from app.core.time_utils import ensure_utc
from app.models.operational_event import OperationalEvent
from app.models.quality import NonConformanceReport
from app.models.user import UserRole
from app.models.work_order import OperationStatus
from app.models.work_order_blocker import WorkOrderBlocker, WorkOrderBlockerStatus
from app.services.adoption_metrics_service import get_adoption_metrics
from app.services.ai_context_service import AIContextService
from app.services.flow_metrics_service import _ready_times_by_operation
from app.services.notification_catalog import SOURCE_EVENT_TYPE_TO_KEY
from app.services.operation_hold_view import HOLD_EVENT_TYPE, hold_contexts_for_operations
from app.services.operational_event_service import OperationalEventService
from tests.api.kiosk_test_helpers import COMPANY_A, make_user, user_headers
from tests.api.test_scheduling_impact import fixture_job

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

EVENTS_URL = "/api/v1/operational-events/"
API_TOKENS_URL = "/api/v1/api-tokens/"
APP_DIR = Path(__file__).resolve().parents[2] / "app"
# Far enough ahead that the future-dated rows below stay in the future. A near date
# (2030) would make every reader test fail once the calendar reached it, since the row
# would no longer be in the future.
FUTURE = datetime(2999, 1, 1)
FUTURE_ISO = "2999-01-01T00:00:00Z"


@pytest.fixture(autouse=True)
def _no_outbox_enqueue(monkeypatch):
    """Keep the after-commit outbox tee off Redis; record what it would have enqueued."""
    enqueued: List[int] = []
    monkeypatch.setattr(notification_outbox, "_enqueue_dispatch", lambda event_id: enqueued.append(event_id))
    return enqueued


def _post(client: TestClient, headers: dict, event_type: str, **extra):
    return client.post(
        EVENTS_URL,
        json={"event_type": event_type, "source_module": "shop_floor", **extra},
        headers=headers,
    )


def _rows(db: Session, company_id: int = COMPANY_A) -> List[OperationalEvent]:
    db.expire_all()
    return (
        db.query(OperationalEvent).filter(OperationalEvent.company_id == company_id).order_by(OperationalEvent.id).all()
    )


def _insert(db: Session, *, event_type: str, occurred_at: datetime, **fields) -> OperationalEvent:
    """A row written straight to the table -- the shape a legacy or out-of-band writer leaves."""
    fields.setdefault("company_id", COMPANY_A)
    fields.setdefault("source_module", "shop_floor")
    fields.setdefault("severity", "info")
    fields.setdefault("event_payload", {})
    event = OperationalEvent(event_type=event_type, occurred_at=occurred_at, **fields)
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


# --------------------------------------------------------------------------
# 1. What is refused
# --------------------------------------------------------------------------


class TestRefusedSetComposition:
    def test_every_cataloged_and_reader_trusted_type_is_refused(self):
        assert set(SOURCE_EVENT_TYPE_TO_KEY) <= SERVER_ONLY_EVENT_TYPES
        assert READER_TRUSTED_EVENT_TYPES <= SERVER_ONLY_EVENT_TYPES
        assert SERVER_ONLY_EVENT_TYPES == READER_TRUSTED_EVENT_TYPES | set(SOURCE_EVENT_TYPE_TO_KEY)
        # The route lower-cases and trims before matching, so a member with an
        # upper-case letter or padding could never match anything.
        assert all(member == member.strip().lower() for member in SERVER_ONLY_EVENT_TYPES)

    @pytest.mark.parametrize("variant", ["operation_hold", "  Operation_Hold ", "OPERATION_HOLD", "\tncr_created\n"])
    def test_spelling_variants_are_refused(self, variant):
        assert is_server_only_event_type(variant)

    @pytest.mark.parametrize("custom", ["agent_observation", "operation_hold_note", "ncr_created_v2", "labor_note"])
    def test_custom_types_are_not_refused(self, custom):
        assert not is_server_only_event_type(custom)

    def test_every_reader_filter_names_a_refused_type(self):
        """Static guard: a reader that selects ``OperationalEvent.event_type`` by name trusts
        that type, so the type must be refused at the route. Fails when a new reader is added
        without extending ``READER_TRUSTED_EVENT_TYPES``."""
        # Filters whose value is not a fixed type name, each with the reason it is safe.
        not_a_type_name = {
            # GET /operational-events: the caller's own filter over the raw stream.
            ("services/operational_event_service.py", "event_type"),
            # The relay sweeper reads exactly the catalog, which is refused wholesale.
            ("jobs/notification_jobs.py", "list(SOURCE_EVENT_TYPE_TO_KEY.keys())"),
        }

        def is_event_type_column(node: ast.AST) -> bool:
            return (
                isinstance(node, ast.Attribute)
                and node.attr == "event_type"
                and isinstance(node.value, ast.Name)
                and node.value.id == "OperationalEvent"
            )

        found = set()
        unresolved = set()
        for path in sorted(APP_DIR.rglob("*.py")):
            source = path.read_text()
            if "OperationalEvent.event_type" not in source:
                continue
            rel = path.relative_to(APP_DIR).as_posix()
            module = importlib.import_module("app." + rel[: -len(".py")].replace("/", "."))

            def resolve(expr: ast.AST) -> None:
                if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
                    found.add(expr.value)
                elif isinstance(expr, ast.Name) and isinstance(getattr(module, expr.id, None), str):
                    found.add(getattr(module, expr.id))
                elif isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
                    for element in expr.elts:
                        resolve(element)
                else:
                    unresolved.add((rel, ast.unparse(expr)))

            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Compare) and is_event_type_column(node.left):
                    for comparator in node.comparators:
                        resolve(comparator)
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "in_"
                    and is_event_type_column(node.func.value)
                ):
                    for arg in node.args:
                        resolve(arg)

        assert unresolved == not_a_type_name
        # Non-vacuity: the scan sees the readers this change was written against.
        assert {
            "dispatch_order_changed",
            "operation_hold",
            "operation_ready",
            "operation_completed",
            "ncr_updated",
            "fai_updated",
        } <= found
        assert found <= READER_TRUSTED_EVENT_TYPES, sorted(found - READER_TRUSTED_EVENT_TYPES)


# --------------------------------------------------------------------------
# 2. Refusal at the route
# --------------------------------------------------------------------------


class TestForgedPostsAreRefused:
    @pytest.mark.parametrize("event_type", sorted(SERVER_ONLY_EVENT_TYPES))
    def test_each_refused_type_is_400_and_writes_and_enqueues_nothing(
        self, client: TestClient, db_session: Session, _no_outbox_enqueue, event_type
    ):
        user = make_user(db_session, role=UserRole.OPERATOR)
        wo, op, _ = fixture_job(db_session)
        before = len(_rows(db_session))

        resp = _post(
            client,
            user_headers(user),
            event_type,
            entity_type="work_order_operation",
            entity_id=op.id,
            work_order_id=wo.id,
            operation_id=op.id,
            severity="high",
            occurred_at=FUTURE_ISO,
            event_payload={"category": "quality", "note": "forged"},
        )

        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        assert resp.json()["detail"] == server_only_event_type_detail(event_type)
        assert len(_rows(db_session)) == before
        assert _no_outbox_enqueue == []
        assert not db_session.info.get("pending_notification_event_ids")

    @pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.OPERATOR, UserRole.MANAGER, UserRole.ADMIN])
    @pytest.mark.parametrize("event_type", ["  Operation_Hold ", "WORK_ORDER_RELEASED"])
    def test_refused_for_every_role_and_every_spelling(
        self, client: TestClient, db_session: Session, _no_outbox_enqueue, role, event_type
    ):
        user = make_user(db_session, role=role)
        resp = _post(client, user_headers(user), event_type)
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        assert resp.json()["detail"] == server_only_event_type_detail(event_type)
        assert _rows(db_session) == []
        assert _no_outbox_enqueue == []

    def test_refused_for_an_api_token(
        self, client: TestClient, db_session: Session, admin_headers: dict, _no_outbox_enqueue
    ):
        bot = make_user(db_session, role=UserRole.OPERATOR)
        issued = client.post(API_TOKENS_URL, json={"user_id": bot.id, "label": "forgery probe"}, headers=admin_headers)
        assert issued.status_code == status.HTTP_201_CREATED, issued.text
        token_headers = {"Authorization": f"Bearer {issued.json()['token']}", "X-Requested-With": "XMLHttpRequest"}

        refused = _post(client, token_headers, "ncr_created", entity_type="ncr", entity_id=1)
        assert refused.status_code == status.HTTP_400_BAD_REQUEST, refused.text
        assert _rows(db_session) == []
        assert _no_outbox_enqueue == []

        # The same credential can still record a custom event.
        accepted = _post(client, token_headers, "agent_observation", event_payload={"summary": "ok"})
        assert accepted.status_code == status.HTTP_200_OK, accepted.text
        assert accepted.json()["user_id"] == bot.id

    def test_the_dispatch_notice_keeps_its_board_hint_and_others_do_not(self):
        assert server_only_event_type_detail("dispatch_order_changed").endswith(
            "To change a machine's run order, use the Dispatch Board."
        )
        assert "Dispatch Board" not in server_only_event_type_detail("operation_hold")
        assert server_only_event_type_detail("operation_hold").startswith(
            "Event type 'operation_hold' is recorded by the system itself and cannot be posted."
        )


class TestCustomTypesStillRecord:
    def test_a_custom_type_records_and_never_reaches_the_outbox(
        self, client: TestClient, db_session: Session, _no_outbox_enqueue
    ):
        user = make_user(db_session, role=UserRole.VIEWER)
        wo, op, _ = fixture_job(db_session)

        resp = _post(
            client,
            user_headers(user),
            "agent_observation",
            work_order_id=wo.id,
            operation_id=op.id,
            event_payload={"summary": "material staged early"},
        )

        assert resp.status_code == status.HTTP_200_OK, resp.text
        body = resp.json()
        assert body["event_type"] == "agent_observation"
        assert body["user_id"] == user.id
        (row,) = _rows(db_session)
        assert row.event_type == "agent_observation"
        assert _no_outbox_enqueue == []

        # Positive control: the fixture really captures an enqueue, so the empty list
        # above means "nothing queued", not "the spy was never wired".
        server_event = OperationalEventService(db_session).emit(
            company_id=COMPANY_A, event_type="ncr_created", source_module="quality"
        )
        db_session.commit()
        assert _no_outbox_enqueue == [server_event.id]

    def test_another_companys_work_order_is_still_404(self, client: TestClient, db_session: Session):
        user = make_user(db_session)
        foreign_wo, _, _ = fixture_job(db_session, company_id=2)
        resp = _post(client, user_headers(user), "agent_observation", work_order_id=foreign_wo.id)
        assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
        assert _rows(db_session) == []


# --------------------------------------------------------------------------
# 3. The occurred_at clamp
# --------------------------------------------------------------------------


class TestOccurredAtClamp:
    def test_a_future_occurred_at_is_stored_as_now(self, client: TestClient, db_session: Session):
        user = make_user(db_session)
        before = datetime.now(timezone.utc)
        resp = _post(client, user_headers(user), "agent_observation", occurred_at=FUTURE_ISO)
        after = datetime.now(timezone.utc)

        assert resp.status_code == status.HTTP_200_OK, resp.text
        served = datetime.fromisoformat(resp.json()["occurred_at"].replace("Z", "+00:00"))
        assert before - timedelta(seconds=1) <= served <= after
        (row,) = _rows(db_session)
        assert before - timedelta(seconds=1) <= ensure_utc(row.occurred_at) <= after

    @pytest.mark.parametrize(
        "sent, expected",
        [
            ("2026-01-15T08:30:00Z", datetime(2026, 1, 15, 8, 30, tzinfo=timezone.utc)),
            # An offset is the same instant, stored as UTC.
            ("2026-01-15T08:30:00-05:00", datetime(2026, 1, 15, 13, 30, tzinfo=timezone.utc)),
        ],
    )
    def test_a_past_occurred_at_is_kept(self, client: TestClient, db_session: Session, sent, expected):
        user = make_user(db_session)
        resp = _post(client, user_headers(user), "agent_observation", occurred_at=sent)
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert datetime.fromisoformat(resp.json()["occurred_at"].replace("Z", "+00:00")) == expected
        (row,) = _rows(db_session)
        assert ensure_utc(row.occurred_at) == expected

    def test_an_omitted_occurred_at_is_stamped_by_the_server(self, client: TestClient, db_session: Session):
        user = make_user(db_session)
        resp = _post(client, user_headers(user), "agent_observation")
        assert resp.status_code == status.HTTP_200_OK, resp.text
        served = datetime.fromisoformat(resp.json()["occurred_at"].replace("Z", "+00:00"))
        assert abs(served - datetime.now(timezone.utc)) < timedelta(minutes=1)

    def test_clamp_helper(self):
        now = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
        assert clamp_occurred_at(None, now) is None
        assert clamp_occurred_at(now + timedelta(seconds=1), now) == now
        assert clamp_occurred_at(datetime(2026, 11, 5, 13, 0), now) == now  # naive is UTC
        assert clamp_occurred_at(datetime(2026, 11, 5, 11, 0), now) == datetime(2026, 11, 5, 11, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# 4. Readers tolerate future-dated and malformed rows
# --------------------------------------------------------------------------


def _held_operation(db: Session):
    wo, op, _ = fixture_job(db)
    op.status = OperationStatus.ON_HOLD
    db.commit()
    return wo, op


class TestHoldContextReader:
    def test_a_future_dated_hold_event_does_not_beat_a_real_one(self, db_session: Session):
        real_holder = make_user(db_session, first_name="Real", last_name="Holder")
        forger = make_user(db_session, first_name="Forged", last_name="Holder")
        wo, op = _held_operation(db_session)
        real_at = datetime.utcnow() - timedelta(minutes=10)
        _insert(
            db_session,
            event_type=HOLD_EVENT_TYPE,
            occurred_at=real_at,
            work_order_id=wo.id,
            operation_id=op.id,
            user_id=real_holder.id,
        )
        _insert(
            db_session,
            event_type=HOLD_EVENT_TYPE,
            occurred_at=FUTURE,
            work_order_id=wo.id,
            operation_id=op.id,
            user_id=forger.id,
            event_payload={"category": "quality", "note": "forged"},
        )

        context = hold_contexts_for_operations(db_session, company_id=COMPANY_A, operation_ids=[op.id])[op.id]

        assert context.held_by_user_id == real_holder.id
        assert ensure_utc(context.held_at) == ensure_utc(real_at)
        assert context.note is None

    def test_a_future_dated_hold_event_does_not_displace_an_open_blocker(self, db_session: Session):
        reporter = make_user(db_session)
        forger = make_user(db_session)
        wo, op = _held_operation(db_session)
        blocker = WorkOrderBlocker(
            company_id=COMPANY_A,
            work_order_id=wo.id,
            operation_id=op.id,
            category="material",
            severity="high",
            status=WorkOrderBlockerStatus.OPEN.value,
            title="Waiting on sheet",
            note="Sheet late",
            reported_by=reporter.id,
            reported_at=datetime.utcnow() - timedelta(hours=1),
        )
        db_session.add(blocker)
        db_session.commit()
        _insert(
            db_session,
            event_type=HOLD_EVENT_TYPE,
            occurred_at=FUTURE,
            work_order_id=wo.id,
            operation_id=op.id,
            user_id=forger.id,
        )

        context = hold_contexts_for_operations(db_session, company_id=COMPANY_A, operation_ids=[op.id])[op.id]

        assert context.held_by_user_id == reporter.id
        assert context.blocker is not None and context.blocker.id == blocker.id

    def test_only_a_future_dated_hold_event_reads_as_unrecorded(self, db_session: Session):
        forger = make_user(db_session)
        wo, op = _held_operation(db_session)
        _insert(
            db_session,
            event_type=HOLD_EVENT_TYPE,
            occurred_at=FUTURE,
            work_order_id=wo.id,
            operation_id=op.id,
            user_id=forger.id,
        )
        context = hold_contexts_for_operations(db_session, company_id=COMPANY_A, operation_ids=[op.id])[op.id]
        assert context.held_by_user_id is None and context.held_at is None


class TestAIWorkOrderContextReader:
    def test_future_dated_rows_never_crowd_out_real_events(self, db_session: Session):
        wo, op, _ = fixture_job(db_session)
        real = _insert(
            db_session,
            event_type="operation_completed",
            occurred_at=datetime.utcnow() - timedelta(minutes=5),
            work_order_id=wo.id,
            operation_id=op.id,
            event_payload={"source": "kiosk"},
        )
        for _ in range(25):  # more than the context's 20-row window
            _insert(
                db_session,
                event_type="agent_observation",
                occurred_at=FUTURE,
                work_order_id=wo.id,
                event_payload={"summary": "pinned"},
            )

        context = AIContextService(db_session).work_order_context(company_id=COMPANY_A, work_order_id=wo.id)

        assert [event["event_type"] for event in context["recent_events"]] == [real.event_type]


class TestTimelineReader:
    def test_a_future_dated_quality_event_is_not_served(self, client: TestClient, db_session: Session):
        user = make_user(db_session)
        wo, _, _ = fixture_job(db_session)
        ncr = NonConformanceReport(
            company_id=COMPANY_A,
            work_order_id=wo.id,
            ncr_number=f"FORGERY-{wo.id}",
            title="Timeline fixture",
            source="in_process",
            description="fixture",
        )
        db_session.add(ncr)
        db_session.flush()
        for occurred_at in (datetime.utcnow() - timedelta(minutes=5), FUTURE):
            _insert(
                db_session,
                event_type="ncr_updated",
                source_module="quality",
                occurred_at=occurred_at,
                entity_type="ncr",
                entity_id=ncr.id,
                work_order_id=wo.id,
            )

        resp = client.get(f"/api/v1/work-orders/{wo.id}/timeline?category=quality", headers=user_headers(user))

        assert resp.status_code == status.HTTP_200_OK, resp.text
        telemetry = [row for row in resp.json()["items"] if row["evidence"] == "telemetry"]
        assert len(telemetry) == 1
        served = datetime.fromisoformat(telemetry[0]["occurred_at"].replace("Z", "+00:00"))
        assert served < datetime.now(timezone.utc)


class TestMetricsReaders:
    def test_adoption_survives_a_non_object_payload_and_skips_future_rows(self, db_session: Session):
        wo, op, _ = fixture_job(db_session)
        now = datetime.utcnow()
        for payload in ({"source": "kiosk"}, ["not", "an", "object"], "a string", None):
            _insert(
                db_session,
                event_type="operation_completed",
                occurred_at=now - timedelta(minutes=1),
                work_order_id=wo.id,
                operation_id=op.id,
                event_payload=payload,
            )
        # Dated later today, inside a window that ends today -- but not yet happened.
        _insert(
            db_session,
            event_type="operation_completed",
            occurred_at=now + timedelta(hours=1),
            work_order_id=wo.id,
            operation_id=op.id,
            event_payload={"source": "kiosk"},
        )

        today = now.date()
        report = get_adoption_metrics(db_session, COMPANY_A, today - timedelta(days=7), today + timedelta(days=1))

        # Four in-window rows; the future-dated one is not counted.
        assert (report.live_completions, report.backfill_completions, report.unknown_completions) == (1, 0, 3)

    def test_flow_metrics_ignore_a_future_ready_event(self, db_session: Session):
        wo, op, _ = fixture_job(db_session)
        _, other, _ = fixture_job(db_session)
        past = datetime.utcnow() - timedelta(hours=2)
        _insert(db_session, event_type="operation_ready", occurred_at=past, work_order_id=wo.id, operation_id=op.id)
        _insert(db_session, event_type="operation_ready", occurred_at=FUTURE, work_order_id=wo.id, operation_id=op.id)
        _insert(
            db_session,
            event_type="operation_ready",
            occurred_at=FUTURE,
            work_order_id=other.work_order_id,
            operation_id=other.id,
        )

        ready = _ready_times_by_operation(db_session, COMPANY_A, [op.id, other.id])

        assert set(ready) == {op.id}
        assert ensure_utc(ready[op.id]) == ensure_utc(past)


def test_the_raw_stream_still_lists_every_stored_row(client: TestClient, db_session: Session):
    """``GET /operational-events/`` is the raw store, not a trusting reader: a future-dated
    row written some other way is still listed (newest first), so whoever investigates a
    bad row can see it."""
    user = make_user(db_session)
    real = _insert(db_session, event_type="agent_observation", occurred_at=datetime.utcnow() - timedelta(minutes=1))
    future = _insert(db_session, event_type="agent_observation", occurred_at=FUTURE)
    resp = client.get(EVENTS_URL, headers=user_headers(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert [row["id"] for row in resp.json()] == [future.id, real.id]
