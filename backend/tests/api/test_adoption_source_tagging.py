"""Behavior locks for A0.1 adoption-telemetry source tagging.

The go-live adoption dashboard needs to distinguish an operator clocking in at a
kiosk from a clerk back-filling from paper at 4pm. Contracts locked here:

1. ``POST /shop-floor/clock-in`` accepts an optional ``source`` channel
   (kiosk/desktop/scanner/import/backfill), persists it on the created TimeEntry,
   and tags the ``labor_clock_in`` OperationalEvent payload with it.
2. Omitted ``source`` stays NULL everywhere -- the server NEVER guesses a channel.
3. An unknown ``source`` value is rejected with 422 (Pydantic enum validation) on
   ALL FOUR write endpoints -- each request schema declares its own enum-typed
   field, so each is locked independently -- and a rejected write mutates nothing.
4. ``POST /shop-floor/clock-out/{id}`` records the clock-out channel when sent and
   tags ``labor_clock_out``; when omitted, the entry keeps the clock-in channel.
   A clock-out that COMPLETES the operation/work order also tags the
   ``operation_completed`` / ``work_order_completed`` events it emits (the second
   call site of the shared completion emitters).
5. ``POST /shop-floor/operations/{id}/production`` records the reporting channel on
   the active TimeEntry when sent.
6. ``POST /shop-floor/operations/{id}/complete`` only FILLS a missing channel on the
   open entries it auto-closes (never overwrites another operator's recorded
   clock-in channel) and tags ``operation_completed`` / ``work_order_completed``.
   With ``source`` omitted it fills NOTHING: NULL stays NULL and the events carry
   ``source: None``.
7. (A0.3) ``PUT /shop-floor/operations/{id}/hold`` accepts the same optional
   ``source``: fill-only-if-NULL on the open entries it auto-closes, the channel
   tags whichever event the hold emits (``operation_hold`` or
   ``work_order_blocker_created`` via the blocker branch), and an unknown value
   is a 422 that mutates nothing.
8. (A0.3) ``POST /shop-floor/operations/{id}/production`` carries a structured
   ``scrap_reason`` that persists onto the active TimeEntry like clock-out's --
   only when the report carries scrap, and never clobbered back to None. A
   reason-less clock-out (the kiosk COMPLETE flow) preserves it too, and the
   field is capped at the column width (255) with a 422 beyond it.
"""

from datetime import date, datetime, timedelta
from typing import Optional

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.company import Company
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True)
        db.add(company)
        db.commit()
    return company


def make_user(db: Session, *, role: UserRole = UserRole.OPERATOR, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"a01-src-{n}@co{company_id}.test",
        employee_id=f"A01SRC-{n:05d}",
        first_name="A01",
        last_name="Source",
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=True,
        is_superuser=False,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def make_wo_op(db: Session, *, company_id: int = COMPANY_A) -> tuple[WorkOrder, WorkOrderOperation, WorkCenter]:
    """One IN_PROGRESS work order with a single IN_PROGRESS operation (qty 10)."""
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"A01SRC-P-{n}",
        name=f"Part {n}",
        description="A0.1 source-tagging fixture part",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()
    wc = WorkCenter(
        name=f"A01SRC-WC-{n}",
        code=f"A01SRC-WC-{n}",
        work_center_type="welding",
        description="A0.1 source-tagging fixture work center",
        hourly_rate=100.0,
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.flush()
    wo = WorkOrder(
        work_order_number=f"A01SRC-WO-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=10,
        status=WorkOrderStatus.IN_PROGRESS,
        priority=5,
        due_date=date.today() + timedelta(days=30),
        company_id=company_id,
    )
    db.add(wo)
    db.flush()
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        sequence=10,
        operation_number="OP10",
        name="Op 10",
        status=OperationStatus.IN_PROGRESS,
        quantity_complete=0,
        company_id=company_id,
    )
    db.add(op)
    db.commit()
    return wo, op, wc


def make_open_entry(
    db: Session,
    user: User,
    wo: WorkOrder,
    op: WorkOrderOperation,
    *,
    source: Optional[str] = None,
    company_id: int = COMPANY_A,
) -> TimeEntry:
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=wo.id,
        operation_id=op.id,
        work_center_id=op.work_center_id,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow() - timedelta(hours=1),
        clock_out=None,
        source=source,
        company_id=company_id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def latest_event(
    db: Session, event_type: str, operation_id: Optional[int] = None, work_order_id: Optional[int] = None
) -> Optional[OperationalEvent]:
    query = db.query(OperationalEvent).filter(OperationalEvent.event_type == event_type)
    if operation_id is not None:
        query = query.filter(OperationalEvent.operation_id == operation_id)
    if work_order_id is not None:
        query = query.filter(OperationalEvent.work_order_id == work_order_id)
    return query.order_by(OperationalEvent.id.desc()).first()


def clock_in_payload(wo: WorkOrder, op: WorkOrderOperation, wc: WorkCenter, **extra) -> dict:
    return {"work_order_id": wo.id, "operation_id": op.id, "work_center_id": wc.id, "entry_type": "run", **extra}


# ===========================================================================
# Clock-in
# ===========================================================================


def test_clock_in_persists_source_and_tags_event(client: TestClient, db_session: Session):
    """``source`` from the client lands on the TimeEntry row AND the labor_clock_in event."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)

    resp = client.post(
        "/api/v1/shop-floor/clock-in",
        json=clock_in_payload(wo, op, wc, source="kiosk"),
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["source"] == "kiosk"

    db_session.expire_all()
    entry = db_session.get(TimeEntry, body["id"])
    assert entry.source == "kiosk"

    event = latest_event(db_session, "labor_clock_in", operation_id=op.id)
    assert event is not None, "clock-in must emit labor_clock_in"
    assert event.event_payload.get("source") == "kiosk"


def test_clock_in_without_source_stays_null(client: TestClient, db_session: Session):
    """Omitted source means UNKNOWN: NULL on the row, None in the event -- never guessed."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)

    resp = client.post("/api/v1/shop-floor/clock-in", json=clock_in_payload(wo, op, wc), headers=headers_for(operator))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["source"] is None

    db_session.expire_all()
    assert db_session.get(TimeEntry, body["id"]).source is None

    event = latest_event(db_session, "labor_clock_in", operation_id=op.id)
    assert event.event_payload.get("source") is None


def test_clock_in_rejects_unknown_source(client: TestClient, db_session: Session):
    """An unknown channel value is a 422 via Pydantic enum validation (no row created)."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)

    resp = client.post(
        "/api/v1/shop-floor/clock-in",
        json=clock_in_payload(wo, op, wc, source="fax"),
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text


def test_clock_out_production_and_complete_reject_unknown_source(client: TestClient, db_session: Session):
    """The other three write endpoints each declare their OWN enum-typed ``source``
    field (ClockOut / ProductionReportRequest / OperationCompleteRequest), so the
    422 contract is locked per-schema -- retyping any one as plain ``str`` would
    silently accept garbage channels. A rejected write must mutate nothing."""
    operator = make_user(db_session)
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    wo, op, wc = make_wo_op(db_session)
    entry = make_open_entry(db_session, operator, wo, op)
    entry_id, op_id = entry.id, op.id

    resp = client.post(
        f"/api/v1/shop-floor/clock-out/{entry_id}",
        json={"quantity_produced": 0, "quantity_scrapped": 0, "source": "fax"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op_id}/production",
        json={"quantity_complete_delta": 1.0, "quantity_scrapped_delta": 0.0, "source": "fax"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op_id}/complete",
        json={"quantity_complete": 10, "source": "fax"},
        headers=headers_for(supervisor),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text

    db_session.expire_all()
    entry = db_session.get(TimeEntry, entry_id)
    op = db_session.get(WorkOrderOperation, op_id)
    assert entry.clock_out is None and entry.source is None, "a 422 write must not touch the entry"
    assert op.status == OperationStatus.IN_PROGRESS, "a 422 write must not advance the operation"


# ===========================================================================
# Clock-out
# ===========================================================================


def test_clock_out_records_source_and_tags_event(client: TestClient, db_session: Session):
    """A clock-out that sends ``source`` records it on the entry and tags labor_clock_out."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    entry = make_open_entry(db_session, operator, wo, op)  # NULL source (pre-A0.1 row)
    entry_id = entry.id

    resp = client.post(
        f"/api/v1/shop-floor/clock-out/{entry_id}",
        json={"quantity_produced": 0, "quantity_scrapped": 0, "source": "backfill"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["source"] == "backfill"

    db_session.expire_all()
    assert db_session.get(TimeEntry, entry_id).source == "backfill"

    event = latest_event(db_session, "labor_clock_out", operation_id=op.id)
    assert event is not None, "clock-out must emit labor_clock_out"
    assert event.event_payload.get("source") == "backfill"


def test_clock_out_without_source_keeps_clock_in_channel(client: TestClient, db_session: Session):
    """Omitting source on clock-out preserves the channel recorded at clock-in."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    entry = make_open_entry(db_session, operator, wo, op, source="kiosk")
    entry_id = entry.id

    resp = client.post(
        f"/api/v1/shop-floor/clock-out/{entry_id}",
        json={"quantity_produced": 0, "quantity_scrapped": 0},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    assert db_session.get(TimeEntry, entry_id).source == "kiosk"

    event = latest_event(db_session, "labor_clock_out", operation_id=op.id)
    assert event.event_payload.get("source") is None, "the EVENT reflects this write's channel: not reported"


def test_clock_out_completing_operation_tags_completion_events(client: TestClient, db_session: Session):
    """clock_out is the second call site of the shared completion emitters: a
    clock-out whose produced quantity completes the operation (and thereby the
    single-op work order) must pass its channel through to the
    ``operation_completed`` and ``work_order_completed`` payloads too."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    entry = make_open_entry(db_session, operator, wo, op, source="scanner")
    entry_id, op_id, wo_id = entry.id, op.id, wo.id

    resp = client.post(
        f"/api/v1/shop-floor/clock-out/{entry_id}",
        json={"quantity_produced": 10, "quantity_scrapped": 0, "source": "scanner"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    op = db_session.get(WorkOrderOperation, op_id)
    wo = db_session.get(WorkOrder, wo_id)
    assert op.status == OperationStatus.COMPLETE, "qty 10/10 clock-out must complete the operation"
    assert wo.status == WorkOrderStatus.COMPLETE, "completing the only operation must complete the WO"

    op_event = latest_event(db_session, "operation_completed", operation_id=op_id)
    assert op_event is not None, "completion via clock-out must emit operation_completed"
    assert op_event.event_payload.get("source") == "scanner"

    wo_event = latest_event(db_session, "work_order_completed", work_order_id=wo_id)
    assert wo_event is not None, "completion via clock-out must emit work_order_completed"
    assert wo_event.event_payload.get("source") == "scanner"


# ===========================================================================
# Production reporting
# ===========================================================================


def test_production_report_records_source(client: TestClient, db_session: Session):
    """Reporting production with ``source`` stamps the active entry's channel."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    entry = make_open_entry(db_session, operator, wo, op)
    entry_id = entry.id

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/production",
        json={"quantity_complete_delta": 2.0, "quantity_scrapped_delta": 0.0, "source": "scanner"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    assert db_session.get(TimeEntry, entry_id).source == "scanner"


def test_production_report_persists_scrap_reason(client: TestClient, db_session: Session):
    """A0.3: /production carries a structured scrap reason that lands on the active
    TimeEntry exactly like clock-out's, and a later reason-less report never
    clobbers the recorded reason back to None."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    entry = make_open_entry(db_session, operator, wo, op)
    entry_id = entry.id

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/production",
        json={"quantity_complete_delta": 1.0, "quantity_scrapped_delta": 2.0, "scrap_reason": "Material defect"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    assert db_session.get(TimeEntry, entry_id).scrap_reason == "Material defect"

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/production",
        json={"quantity_complete_delta": 1.0, "quantity_scrapped_delta": 0.0},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    assert (
        db_session.get(TimeEntry, entry_id).scrap_reason == "Material defect"
    ), "a report without scrap must not clobber the recorded reason"


def test_clock_out_without_reason_preserves_mid_shift_scrap_reason(client: TestClient, db_session: Session):
    """Kiosk COMPLETE regression: a clock-out with zero scrap and NO reason must not
    null a scrap reason recorded mid-shift via /production. Clock-out only writes
    ``scrap_reason`` when it actually carries one."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    entry = make_open_entry(db_session, operator, wo, op)
    entry_id = entry.id

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/production",
        json={"quantity_complete_delta": 1.0, "quantity_scrapped_delta": 2.0, "scrap_reason": "Material defect"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    assert db_session.get(TimeEntry, entry_id).scrap_reason == "Material defect"

    resp = client.post(
        f"/api/v1/shop-floor/clock-out/{entry_id}",
        json={"quantity_produced": 0, "quantity_scrapped": 0, "source": "kiosk"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    entry = db_session.get(TimeEntry, entry_id)
    assert entry.clock_out is not None, "the clock-out itself must still land"
    assert entry.scrap_reason == "Material defect", "a reason-less clock-out must not null the mid-shift scrap reason"


def test_production_report_rejects_overlong_scrap_reason(client: TestClient, db_session: Session):
    """``scrap_reason`` is capped at the TimeEntry column width (String(255)):
    a 300-char reason is a 422 via Pydantic max_length, and nothing is written."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    entry = make_open_entry(db_session, operator, wo, op)
    entry_id = entry.id

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/production",
        json={"quantity_complete_delta": 0.0, "quantity_scrapped_delta": 1.0, "scrap_reason": "x" * 300},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text

    db_session.expire_all()
    entry = db_session.get(TimeEntry, entry_id)
    assert entry.scrap_reason is None and entry.clock_out is None, "a 422 report must not touch the entry"


# ===========================================================================
# Operation hold
# ===========================================================================


def test_hold_fills_missing_source_without_overwriting(client: TestClient, db_session: Session):
    """A0.3: a hold auto-closes every open entry on the operation; like /complete it
    only FILLS a missing channel from the hold's own ``source`` -- another
    operator's recorded clock-in channel is the adoption signal and must never
    be clobbered."""
    operator_kiosk = make_user(db_session)
    operator_unknown = make_user(db_session)
    holder = make_user(db_session, role=UserRole.SUPERVISOR)
    wo, op, wc = make_wo_op(db_session)
    kiosk_entry = make_open_entry(db_session, operator_kiosk, wo, op, source="kiosk")
    unknown_entry = make_open_entry(db_session, operator_unknown, wo, op)
    kiosk_entry_id, unknown_entry_id = kiosk_entry.id, unknown_entry.id

    resp = client.put(
        f"/api/v1/shop-floor/operations/{op.id}/hold",
        json={"source": "desktop"},
        headers=headers_for(holder),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    kiosk_entry = db_session.get(TimeEntry, kiosk_entry_id)
    unknown_entry = db_session.get(TimeEntry, unknown_entry_id)
    assert kiosk_entry.clock_out is not None and unknown_entry.clock_out is not None, "hold closes open entries"
    assert kiosk_entry.source == "kiosk", "a recorded clock-in channel must never be overwritten"
    assert unknown_entry.source == "desktop", "a missing channel is filled from the holding write"
    assert db_session.get(WorkOrderOperation, op.id).status == OperationStatus.ON_HOLD


def test_hold_event_payload_carries_source(client: TestClient, db_session: Session):
    """Both hold event branches pass the channel through: a bare hold emits
    ``operation_hold``; a hold WITH blocker details (the kiosk always sends a
    category) routes through WorkOrderBlockerService and must tag
    ``work_order_blocker_created`` instead."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)

    resp = client.put(
        f"/api/v1/shop-floor/operations/{op.id}/hold",
        json={"source": "kiosk"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    event = latest_event(db_session, "operation_hold", operation_id=op.id)
    assert event is not None, "a hold without blocker details must emit operation_hold"
    assert event.event_payload.get("source") == "kiosk"

    wo2, op2, wc2 = make_wo_op(db_session)
    resp = client.put(
        f"/api/v1/shop-floor/operations/{op2.id}/hold",
        json={"category": "machine_down", "severity": "medium", "source": "kiosk"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    event = latest_event(db_session, "work_order_blocker_created", operation_id=op2.id)
    assert event is not None, "a hold with blocker details must emit work_order_blocker_created"
    assert event.event_payload.get("source") == "kiosk"


def test_hold_rejects_unknown_source(client: TestClient, db_session: Session):
    """OperationHoldRequest declares its own enum-typed ``source`` field, so an
    unknown channel is a 422 via Pydantic validation and the rejected hold
    mutates nothing: no status change, no entry closed, no channel written."""
    operator = make_user(db_session)
    wo, op, wc = make_wo_op(db_session)
    entry = make_open_entry(db_session, operator, wo, op)
    entry_id, op_id = entry.id, op.id

    resp = client.put(
        f"/api/v1/shop-floor/operations/{op_id}/hold",
        json={"source": "fax"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text

    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op_id).status == OperationStatus.IN_PROGRESS
    entry = db_session.get(TimeEntry, entry_id)
    assert entry.clock_out is None and entry.source is None, "a 422 hold must not touch the entry"


# ===========================================================================
# Operation complete
# ===========================================================================


def test_complete_fills_missing_source_without_overwriting(client: TestClient, db_session: Session):
    """Complete tags its events with the completer's channel and only FILLS missing
    channels on the open entries it auto-closes -- another operator's recorded
    kiosk clock-in is the adoption signal and must never be clobbered."""
    operator_kiosk = make_user(db_session)
    operator_unknown = make_user(db_session)
    completer = make_user(db_session, role=UserRole.SUPERVISOR)
    wo, op, wc = make_wo_op(db_session)
    kiosk_entry = make_open_entry(db_session, operator_kiosk, wo, op, source="kiosk")
    unknown_entry = make_open_entry(db_session, operator_unknown, wo, op)
    kiosk_entry_id, unknown_entry_id = kiosk_entry.id, unknown_entry.id

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/complete",
        json={"quantity_complete": 10, "source": "desktop"},
        headers=headers_for(completer),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["is_fully_complete"] is True

    db_session.expire_all()
    kiosk_entry = db_session.get(TimeEntry, kiosk_entry_id)
    unknown_entry = db_session.get(TimeEntry, unknown_entry_id)
    assert kiosk_entry.clock_out is not None and unknown_entry.clock_out is not None
    assert kiosk_entry.source == "kiosk", "a recorded clock-in channel must never be overwritten"
    assert unknown_entry.source == "desktop", "a missing channel is filled from the completing write"

    op_event = latest_event(db_session, "operation_completed", operation_id=op.id)
    assert op_event is not None
    assert op_event.event_payload.get("source") == "desktop"

    wo_event = latest_event(db_session, "work_order_completed", work_order_id=wo.id)
    assert wo_event is not None
    assert wo_event.event_payload.get("source") == "desktop"


def test_complete_without_source_never_fills_or_guesses(client: TestClient, db_session: Session):
    """Fill-only-if-NULL with NOTHING to fill from: a completion that does not
    report a channel leaves every auto-closed entry's channel exactly as recorded
    (kiosk stays kiosk, NULL stays NULL -- never defaulted) and the completion
    events carry ``source: None``."""
    operator_kiosk = make_user(db_session)
    operator_unknown = make_user(db_session)
    completer = make_user(db_session, role=UserRole.SUPERVISOR)
    wo, op, wc = make_wo_op(db_session)
    kiosk_entry = make_open_entry(db_session, operator_kiosk, wo, op, source="kiosk")
    unknown_entry = make_open_entry(db_session, operator_unknown, wo, op)
    kiosk_entry_id, unknown_entry_id = kiosk_entry.id, unknown_entry.id

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/complete",
        json={"quantity_complete": 10},
        headers=headers_for(completer),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["is_fully_complete"] is True

    db_session.expire_all()
    kiosk_entry = db_session.get(TimeEntry, kiosk_entry_id)
    unknown_entry = db_session.get(TimeEntry, unknown_entry_id)
    assert kiosk_entry.clock_out is not None and unknown_entry.clock_out is not None
    assert kiosk_entry.source == "kiosk", "an unreported completion channel must not disturb a recorded one"
    assert unknown_entry.source is None, "NULL stays NULL -- the server never guesses a channel"

    op_event = latest_event(db_session, "operation_completed", operation_id=op.id)
    assert op_event is not None
    assert op_event.event_payload.get("source") is None

    wo_event = latest_event(db_session, "work_order_completed", work_order_id=wo.id)
    assert wo_event is not None
    assert wo_event.event_payload.get("source") is None
