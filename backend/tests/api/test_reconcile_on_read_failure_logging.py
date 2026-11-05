"""Reconcile-on-read side-effect legs must fail VISIBLY, without changing what the read does.

The read handlers in ``shop_floor.py`` and ``work_orders.py`` each carry the same six
best-effort legs after ``reconcile_work_orders_from_completion_evidence`` (audit,
in-process events, G1 parent advance, scheduling refresh, FG receipt / backflush, cost
rollup). Each swallows its own failure so a GET can never 500 on one -- and each used to
swallow it SILENTLY, ``except Exception: pass``, so a reconcile that committed status
flips without their audit rows, or without the inventory movement a completion owed,
left no log line and no Sentry event anywhere.

These pin the new contract for all twelve blocks: the failure is still absorbed (the
helper returns normally, the GET still answers 200, the reconcile still commits exactly
what it committed before), and it now produces one ERROR record -- traceback attached,
work order / operation / company ids in the context -- plus a Sentry event tagged
``reconcile_step``.

What these deliberately do NOT pin is the transaction behaviour of a failed leg, which
is unchanged here and is a known gap (a non-database failure in the audit leg lets the
flips commit unaudited). Moving each leg into a savepoint is a separate follow-up.
"""

import logging
from types import SimpleNamespace
from typing import Callable

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.api.endpoints.shop_floor as sf_module
import app.api.endpoints.work_orders as wo_module
from app.core.config import settings
from app.models.work_order import OperationStatus, WorkOrderOperation, WorkOrderStatus
from app.services.work_order_state_service import StatusTransition
from tests.api.test_completion_signals_dispatch import (
    COMPANY_A,
    headers_for,
    make_closed_time_entry,
    make_op,
    make_user,
    make_wo,
    make_work_center,
)

pytestmark = [pytest.mark.api]

ROUTER_MODULES = {"shop_floor": sf_module, "work_orders": wo_module}

COMPANY_ID = 42
USER_ID = 7
WORK_ORDER_ID = 101
OPERATION_ID = 501


class _Boom:
    """Stands in for a service class whose construction fails hard."""

    def __init__(self, *args, **kwargs):
        raise RuntimeError("leg subsystem down")


def _transitions() -> list[StatusTransition]:
    return [
        StatusTransition(
            resource_type="work_order_operation",
            resource_id=OPERATION_ID,
            resource_identifier="10",
            old_status="in_progress",
            new_status="complete",
            work_order_number="WO-RECON-1",
            work_order_id=WORK_ORDER_ID,
            time_entry_ids=[9],
        ),
        StatusTransition(
            resource_type="work_order",
            resource_id=WORK_ORDER_ID,
            resource_identifier="WO-RECON-1",
            old_status="in_progress",
            new_status="complete",
            work_order_number="WO-RECON-1",
            work_order_id=WORK_ORDER_ID,
            work_center_ids=[3],
        ),
    ]


# step name -> (service name patched in the router module, how to call the leg)
LEGS: dict[str, tuple[str, Callable]] = {
    "audit": (
        "AuditService",
        lambda m, db, user, trs: m._audit_reconcile_transitions(db, user, trs, COMPANY_ID),
    ),
    "events": (
        "OperationalEventService",
        lambda m, db, user, trs: m._emit_reconcile_events(db, COMPANY_ID, user, trs),
    ),
    "parent_advance": (
        "AuditService",
        lambda m, db, user, trs: m._emit_reconcile_parent_advance(db, COMPANY_ID, user, trs),
    ),
    "scheduling": (
        "SchedulingService",
        lambda m, db, user, trs: m._refresh_reconcile_scheduling(db, COMPANY_ID, trs),
    ),
    "inventory_effects": (
        "AuditService",
        lambda m, db, user, trs: m._apply_reconcile_inventory_effects(
            db, COMPANY_ID, user, [SimpleNamespace(id=WORK_ORDER_ID)], trs
        ),
    ),
    "cost_rollup": (
        "AuditService",
        lambda m, db, user, trs: m._apply_reconcile_cost_rollup(
            db, COMPANY_ID, user, [SimpleNamespace(id=WORK_ORDER_ID)], trs
        ),
    ),
}


class _SentrySpy:
    def __init__(self) -> None:
        self.captured: list[tuple[BaseException, dict]] = []

    def capture_exception(self, exc, **scope_kwargs) -> None:
        self.captured.append((exc, scope_kwargs))


@pytest.fixture
def sentry_spy(monkeypatch: pytest.MonkeyPatch) -> _SentrySpy:
    import sentry_sdk

    spy = _SentrySpy()
    monkeypatch.setattr(settings, "SENTRY_DSN", "https://public@sentry.invalid/1")
    monkeypatch.setattr(sentry_sdk, "capture_exception", spy.capture_exception)
    return spy


def _reconcile_records(caplog: pytest.LogCaptureFixture, module) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == module.__name__ and r.levelno == logging.ERROR and "Reconcile-on-read" in r.getMessage()
    ]


@pytest.mark.parametrize("router", sorted(ROUTER_MODULES))
@pytest.mark.parametrize("step", sorted(LEGS))
def test_each_reconcile_leg_logs_and_reports_its_failure_without_raising(
    router: str,
    step: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    sentry_spy: _SentrySpy,
):
    """All twelve blocks: absorbed (no raise), one ERROR record with the traceback and
    the reconcile's ids, one Sentry event tagged with the step and company."""
    module = ROUTER_MODULES[router]
    patched_name, call_leg = LEGS[step]
    monkeypatch.setattr(module, patched_name, _Boom)
    user = SimpleNamespace(id=USER_ID)
    # The leg fails before it touches the session; a bare sentinel proves that.
    db = object()

    with caplog.at_level(logging.ERROR, logger=module.__name__):
        call_leg(module, db, user, _transitions())  # must not raise

    records = _reconcile_records(caplog, module)
    assert len(records) == 1, [r.getMessage() for r in records]
    record = records[0]
    assert f"Reconcile-on-read {step} leg failed" in record.getMessage()
    assert f"company_id={COMPANY_ID}" in record.getMessage()
    assert record.exc_info is not None and isinstance(record.exc_info[1], RuntimeError)
    assert record.extra_data == {
        "reconcile_step": step,
        "company_id": COMPANY_ID,
        # The scheduling leg is not handed the user.
        "user_id": None if step == "scheduling" else USER_ID,
        "work_order_ids": [WORK_ORDER_ID],
        "operation_ids": [OPERATION_ID],
    }

    assert len(sentry_spy.captured) == 1
    exc, scope_kwargs = sentry_spy.captured[0]
    assert isinstance(exc, RuntimeError)
    assert scope_kwargs["tags"]["reconcile_step"] == step
    assert scope_kwargs["tags"]["company_id"] == str(COMPANY_ID)
    # List-valued context rides as extras, never as a high-cardinality tag.
    assert "work_order_ids" not in scope_kwargs["tags"]
    assert scope_kwargs["extras"]["work_order_ids"] == [WORK_ORDER_ID]


def test_reporter_survives_a_user_that_cannot_be_read(caplog: pytest.LogCaptureFixture):
    """The leg may have failed on a database error, leaving the requesting user an
    expired instance on a session that cannot reload it. Reading ``user.id`` there
    raises; the report must still go out rather than turning into a second failure."""
    from app.services.work_order_state_service import report_reconcile_leg_failure

    class _UnreadableUser:
        @property
        def id(self):
            raise RuntimeError("PendingRollbackError stand-in")

    log = logging.getLogger("tests.reconcile_reporter")
    with caplog.at_level(logging.ERROR, logger=log.name):
        try:
            raise ValueError("original leg failure")
        except ValueError as exc:
            report_reconcile_leg_failure(
                log, "audit", exc, company_id=COMPANY_ID, user=_UnreadableUser(), transitions=_transitions()
            )

    [record] = [r for r in caplog.records if r.name == log.name]
    assert record.extra_data["user_id"] is None
    assert isinstance(record.exc_info[1], ValueError)


# ---------------------------------------------------------------------------
# End to end: a forced leg failure on a real GET is logged, and the GET is unchanged.
# ---------------------------------------------------------------------------


def _stranded_completion(db_session: Session):
    """An IN_PROGRESS op whose closed labor already covers the ordered quantity, so the
    next reconciling read drives it COMPLETE (and runs the side-effect legs)."""
    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=4)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    make_closed_time_entry(db_session, user=admin, wo=wo, op=op, wc=wc, quantity_produced=4)
    return admin, wo, op


@pytest.mark.requires_db
@pytest.mark.parametrize(
    "router,path_for",
    [
        ("work_orders", lambda wo, op: f"/api/v1/work-orders/{wo.id}"),
        ("shop_floor", lambda wo, op: f"/api/v1/shop-floor/operations/{op.id}"),
    ],
)
def test_forced_leg_failure_on_a_reconciling_get_is_logged_and_the_get_still_succeeds(
    router: str,
    path_for: Callable,
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    module = ROUTER_MODULES[router]
    admin, wo, op = _stranded_completion(db_session)
    monkeypatch.setattr(module, "OperationalEventService", _Boom)

    with caplog.at_level(logging.ERROR, logger=module.__name__):
        response = client.get(path_for(wo, op), headers=headers_for(admin))

    assert response.status_code == status.HTTP_200_OK, response.text

    records = [r for r in _reconcile_records(caplog, module) if r.extra_data["reconcile_step"] == "events"]
    assert len(records) == 1, [r.getMessage() for r in _reconcile_records(caplog, module)]
    context = records[0].extra_data
    assert context["company_id"] == COMPANY_A
    assert context["user_id"] == admin.id
    assert op.id in context["operation_ids"]
    assert wo.id in context["work_order_ids"]
    assert records[0].exc_info is not None

    # Control flow unchanged: the reconcile still committed the op COMPLETE; only the
    # in-process event was lost.
    db_session.rollback()
    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op.id).status == OperationStatus.COMPLETE


# ---------------------------------------------------------------------------
# The audit leg on a real GET: the company id is threaded from the call site.
#
# ``_audit_reconcile_transitions`` grew ``company_id`` as an OPTIONAL trailing
# parameter (default None) purely for this report. The parametrized test above
# passes it explicitly, so it cannot tell whether ``_reconcile_and_commit`` does;
# only a real read through each router's call site can.
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
@pytest.mark.parametrize(
    "router,path_for",
    [
        ("work_orders", lambda wo, op: f"/api/v1/work-orders/{wo.id}"),
        ("shop_floor", lambda wo, op: f"/api/v1/shop-floor/operations/{op.id}"),
    ],
)
def test_audit_leg_failure_on_a_reconciling_get_is_reported_with_the_active_company(
    router: str,
    path_for: Callable,
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    module = ROUTER_MODULES[router]
    admin, wo, op = _stranded_completion(db_session)

    def _inspection_hook_down(*_args, **_kwargs):
        raise RuntimeError("inspection-exception hook down")

    # Called only from inside the audit leg, so no other leg is disturbed.
    monkeypatch.setattr(module, "record_reconcile_inspection_exception", _inspection_hook_down)

    with caplog.at_level(logging.ERROR, logger=module.__name__):
        response = client.get(path_for(wo, op), headers=headers_for(admin))

    assert response.status_code == status.HTTP_200_OK, response.text
    records = _reconcile_records(caplog, module)
    assert [r.extra_data["reconcile_step"] for r in records] == ["audit"], [r.getMessage() for r in records]
    context = records[0].extra_data
    assert context["company_id"] == COMPANY_A
    assert f"company_id={COMPANY_A}" in records[0].getMessage()
    assert context["user_id"] == admin.id
    assert op.id in context["operation_ids"]
    assert isinstance(records[0].exc_info[1], RuntimeError)


# ---------------------------------------------------------------------------
# The reporter's own "never raises" contract.
#
# It runs inside an ``except`` on a GET; ``_reconcile_and_commit`` only catches
# ``SQLAlchemyError``, so anything the reporter itself raised would 500 the read --
# the exact outcome the twelve blocks exist to prevent.
# ---------------------------------------------------------------------------


def test_reporter_still_logs_when_sentry_itself_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    import sentry_sdk

    from app.services.work_order_state_service import report_reconcile_leg_failure

    def _sentry_down(*_args, **_kwargs):
        raise RuntimeError("sentry transport broken")

    monkeypatch.setattr(settings, "SENTRY_DSN", "https://public@sentry.invalid/1")
    monkeypatch.setattr(sentry_sdk, "capture_exception", _sentry_down)
    log = logging.getLogger("tests.reconcile_reporter.sentry_down")

    with caplog.at_level(logging.ERROR, logger=log.name):
        try:
            raise ValueError("original leg failure")
        except ValueError as exc:
            report_reconcile_leg_failure(
                log, "events", exc, company_id=COMPANY_ID, user=SimpleNamespace(id=USER_ID), transitions=_transitions()
            )

    [record] = [r for r in caplog.records if r.name == log.name]
    assert isinstance(record.exc_info[1], ValueError)
    assert record.extra_data["reconcile_step"] == "events"


def test_reporter_survives_a_logger_that_raises(sentry_spy: _SentrySpy):
    from app.services.work_order_state_service import report_reconcile_leg_failure

    class _BrokenLogger(logging.LoggerAdapter):
        def error(self, *_args, **_kwargs):
            raise OSError("log sink unavailable")

    broken = _BrokenLogger(logging.getLogger("tests.reconcile_reporter.broken"), {})
    try:
        raise ValueError("original leg failure")
    except ValueError as exc:
        report_reconcile_leg_failure(
            broken, "scheduling", exc, company_id=COMPANY_ID, user=None, transitions=_transitions()
        )

    # Sentry still got it -- it is reported BEFORE the log line.
    assert len(sentry_spy.captured) == 1


# ---------------------------------------------------------------------------
# Against a REAL Sentry client: the event that survives dedupe is the tagged one.
#
# Production runs the SDK's logging integration (ERROR lines become events) and its
# dedupe integration (a second event for the SAME exception object is dropped). The
# reporter both captures AND logs the same exception, so exactly one event arrives
# and which one depends on the order: logged first, the surviving event is the
# logging integration's -- with no ``reconcile_step`` / ``company_id`` tags at all.
# The spy above cannot see that; a real client can. It is bound to an isolation
# scope for this test only, so the process-global client is never touched.
# ---------------------------------------------------------------------------


def test_the_one_sentry_event_that_survives_dedupe_carries_the_reconcile_tags(monkeypatch: pytest.MonkeyPatch):
    import sentry_sdk
    from sentry_sdk.integrations.dedupe import DedupeIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration

    from app.services.work_order_state_service import report_reconcile_leg_failure

    events: list[dict] = []

    class _CollectingTransport(sentry_sdk.transport.Transport):
        def capture_envelope(self, envelope):
            events.extend(item.payload.json for item in envelope.items if item.type == "event")

    dsn = "https://public@sentry.invalid/1"
    monkeypatch.setattr(settings, "SENTRY_DSN", dsn)
    sentry_client = sentry_sdk.Client(
        dsn=dsn,
        transport=_CollectingTransport,
        default_integrations=False,
        integrations=[LoggingIntegration(event_level=logging.ERROR), DedupeIntegration()],
    )
    log = logging.getLogger("tests.reconcile_reporter.real_sdk")

    with sentry_sdk.isolation_scope() as scope:
        scope.set_client(sentry_client)
        try:
            raise RuntimeError("leg subsystem down")
        except RuntimeError as exc:
            report_reconcile_leg_failure(
                log, "audit", exc, company_id=COMPANY_ID, user=SimpleNamespace(id=USER_ID), transitions=_transitions()
            )
        sentry_client.flush()

    assert len(events) == 1, [e.get("logger") for e in events]
    [event] = events
    assert event["tags"]["reconcile_step"] == "audit"
    assert event["tags"]["company_id"] == str(COMPANY_ID)
    assert event["tags"]["user_id"] == str(USER_ID)
    assert event["extra"]["operation_ids"] == [OPERATION_ID]
    assert event["extra"]["work_order_ids"] == [WORK_ORDER_ID]
    assert event["exception"]["values"][-1]["type"] == "RuntimeError"


# ---------------------------------------------------------------------------
# The KNOWN GAP, recorded as a strict xfail so it cannot be forgotten.
#
# Measured on this branch (2026-11-05), both routers: with a NON-database failure
# in the audit leg, the GET returns 200, the leg is logged -- and the reconcile
# commits the work order's IN_PROGRESS -> COMPLETE with NO audit row (only the
# operation's row, written before the failure, exists). Invariant 2. The assertion
# is fix-agnostic: every status flip that DID commit must carry its reconcile audit
# row; a fix that aborts the reconcile instead (nothing flips) satisfies it too.
# ``strict=True``: when the per-leg-savepoint follow-up lands this XPASSes, which
# fails the run until the marker is removed.
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN GAP (invariant 2, see _audit_reconcile_transitions): a non-database failure in the "
        "reconcile audit leg is absorbed and the caller still commits, so the work order's status "
        "flip commits unaudited. Follow-up: a savepoint per leg, an audit failure aborting the reconcile."
    ),
)
@pytest.mark.parametrize(
    "router,path_for",
    [
        ("work_orders", lambda wo, op: f"/api/v1/work-orders/{wo.id}"),
        ("shop_floor", lambda wo, op: f"/api/v1/shop-floor/operations/{op.id}"),
    ],
)
def test_no_reconcile_status_flip_commits_without_its_audit_row(
    router: str,
    path_for: Callable,
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.models.audit_log import AuditLog
    from app.models.work_order import WorkOrder

    module = ROUTER_MODULES[router]
    admin, wo, op = _stranded_completion(db_session)

    def _inspection_hook_down(*_args, **_kwargs):
        raise RuntimeError("inspection-exception hook down")

    monkeypatch.setattr(module, "record_reconcile_inspection_exception", _inspection_hook_down)

    response = client.get(path_for(wo, op), headers=headers_for(admin))
    assert response.status_code == status.HTTP_200_OK, response.text

    db_session.rollback()
    db_session.expire_all()
    for model, resource_type, pk, before in (
        (WorkOrderOperation, "work_order_operation", op.id, OperationStatus.IN_PROGRESS),
        (WorkOrder, "work_order", wo.id, WorkOrderStatus.IN_PROGRESS),
    ):
        committed = db_session.get(model, pk).status
        if committed == before:
            continue
        audit_rows = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.company_id == COMPANY_A,
                AuditLog.resource_type == resource_type,
                AuditLog.resource_id == pk,
                AuditLog.description.like("Reconciled %"),
            )
            .count()
        )
        assert audit_rows == 1, f"{resource_type} {pk} committed {before.value} -> {committed.value} unaudited"
