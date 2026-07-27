"""Material ties on the KIOSK payloads -- the operator-facing half of PR 2.

The ties ride ``GET /shop-floor/work-center-queue/{id}`` and
``GET /shop-floor/my-active-job`` **on purpose**. The tie API
(``/work-orders/{id}/material-allocations``) sits OUTSIDE the kiosk path fence, so a
badge-minted ``scope="kiosk"`` token is 403 there (asserted in
``test_kiosk_scope_fence.py``); carrying the data on an already-authorized,
already-tenant-scoped read is the same precedent ``scrap_reason_codes`` set, and it is
what keeps the fence from being widened for a display feature.

Pinned here:

* **Tenant scope comes from ``principal.company_id``** -- the STATION'S OWN company row
  for a station principal, the active company for a user -- never from client input.
* **ONE batched read for the whole queue.** This endpoint is polled every 10-15 seconds
  per station, shop-wide; a per-card query would be that cadence times the card count.
* **OPEN and OPERATION-scoped only**, matching the board, so the manager's chip and the
  operator's line can never describe the same work differently.
* **``[]`` on an untied operation** -- the kiosk renders nothing at all, no placeholder
  and no "not tied" nag.
* **``operation_quantity_scrapped`` is a DISTINCT key** from the time entry's own
  ``quantity_scrapped``: the material prediction scales on (complete + scrapped) at the
  OPERATION level, so reading the session figure would under-state the deduction on any
  job worked across two shifts.
* **The read writes nothing.** A poll is not an actor, has no intent and records no
  reason, so it must never move stock.
"""

from datetime import datetime, timedelta
from typing import List

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.inventory import InventoryItem, InventoryTransaction
from app.models.part import Part
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User, UserRole
from app.models.work_order import WorkOrderOperation
from app.models.work_order_material import AllocationSource, AllocationStatus, WorkOrderMaterialAllocation
from tests.api.kiosk_test_helpers import (
    COMPANY_A,
    COMPANY_B,
    bearer,
    ensure_company,
    kiosk_token_for,
    make_kiosk_station,
    make_user,
    make_wo_with_operation,
    make_work_center,
    queue_url,
    user_headers,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

ACTIVE_JOB_URL = "/api/v1/shop-floor/my-active-job"

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def make_sheet(db: Session, *, company_id: int = COMPANY_A, on_hand: float = 100.0, uom: str = "sheets") -> Part:
    """A raw-material part with one available lot."""
    ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"MTK-SHEET-{n:05d}",
        name=f"Sheet {n}",
        part_type="raw_material",
        unit_of_measure=uom,
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()
    db.add(
        InventoryItem(
            part_id=part.id,
            location="RAW-A",
            warehouse="MAIN",
            quantity_on_hand=on_hand,
            quantity_allocated=0.0,
            quantity_available=on_hand,
            lot_number=f"MTK-LOT-{n:05d}",
            unit_cost=80.0,
            status="available",
            is_active=True,
            company_id=company_id,
        )
    )
    db.commit()
    db.refresh(part)
    return part


def tie(
    db: Session,
    operation: WorkOrderOperation,
    part: Part,
    *,
    qty_per_run: float = 1.0,
    qty_planned: float = 5.0,
    qty_consumed: float = 0.0,
    status_: AllocationStatus = AllocationStatus.OPEN,
    work_order_scoped: bool = False,
    company_id: int = COMPANY_A,
) -> WorkOrderMaterialAllocation:
    allocation = WorkOrderMaterialAllocation(
        company_id=company_id,
        work_order_id=operation.work_order_id,
        work_order_operation_id=None if work_order_scoped else operation.id,
        part_id=part.id,
        source=AllocationSource.NEST,
        status=status_,
        qty_per_run=None if work_order_scoped else qty_per_run,
        qty_planned=qty_planned,
        unit_of_measure=getattr(part.unit_of_measure, "value", part.unit_of_measure) or "each",
        qty_consumed=qty_consumed,
    )
    db.add(allocation)
    db.commit()
    db.refresh(allocation)
    return allocation


def clock_in(db: Session, user: User, operation: WorkOrderOperation) -> TimeEntry:
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=operation.work_order_id,
        operation_id=operation.id,
        work_center_id=operation.work_center_id,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow() - timedelta(minutes=20),
        company_id=user.company_id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def statements_during(db: Session, fn) -> List[str]:
    """Every statement ``fn`` runs against the session's OWN engine.

    Bound to ``db.get_bind()`` deliberately: ``tests/`` has no ``__init__.py``, so
    ``tests.conftest`` and pytest's own ``conftest`` are two module objects with two
    independent engines, and listening on the wrong one silently records nothing (see
    the same note in ``test_dispatch_nest_details._count_select_queries``).
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
    return statements


def allocation_reads(statements: List[str]) -> int:
    """How many statements touched ``work_order_material_allocations``.

    Counting the TIE table specifically -- rather than the endpoint's total query
    count -- is what makes the batching assertions precise. These endpoints do other
    genuinely per-row work (nest sync, rosters, step counts, last-report, blocker
    clocks), so a total-count comparison measures that noise instead of the thing
    under test.
    """
    return sum(1 for statement in statements if "work_order_material_allocations" in statement)


def _queue_row(payload: dict, operation_id: int) -> dict:
    for row in payload["queue"]:
        if row["operation_id"] == operation_id:
            return row
    raise AssertionError(f"operation {operation_id} missing from the queue")


# ---------------------------------------------------------------------------
# Work-center queue
# ---------------------------------------------------------------------------


def test_queue_row_carries_the_open_tie_with_stock_and_shortage(client: TestClient, db_session: Session):
    wc = make_work_center(db_session)
    station = make_kiosk_station(db_session, work_center=wc)
    _, operation = make_wo_with_operation(db_session, work_center=wc)
    sheet = make_sheet(db_session, on_hand=4.0)
    allocation = tie(db_session, operation, sheet, qty_per_run=2.0, qty_planned=10.0, qty_consumed=3.0)

    resp = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station)))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    [line] = _queue_row(resp.json(), operation.id)["material_ties"]

    assert line["allocation_id"] == allocation.id
    assert line["part_id"] == sheet.id
    assert line["part_number"] == sheet.part_number
    assert line["part_name"] == sheet.name
    assert line["unit_of_measure"] == "sheets"
    assert line["qty_per_run"] == 2.0
    assert line["qty_planned"] == 10.0
    assert line["qty_consumed"] == 3.0
    assert line["qty_remaining"] == 7.0
    assert line["on_hand"] == 4.0
    assert line["short_by"] == 3.0
    assert line["pinned_lot_number"] is None
    # The lot ID is deliberately NOT on the kiosk payload: an operator reads the
    # lot NUMBER off a tag and the kiosk has no verb that takes the id.
    assert "pinned_inventory_item_id" not in line


def test_untied_queue_row_carries_an_empty_list(client: TestClient, db_session: Session):
    """The rendering contract: nothing at all is drawn for an untied operation."""
    wc = make_work_center(db_session)
    station = make_kiosk_station(db_session, work_center=wc)
    _, operation = make_wo_with_operation(db_session, work_center=wc)

    resp = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station)))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert _queue_row(resp.json(), operation.id)["material_ties"] == []


def test_queue_shows_only_open_operation_scoped_ties(client: TestClient, db_session: Session):
    """A CANCELLED tie must not paint a line on an operator's screen, and a
    work-order-scoped tie drains through a different mechanism entirely (leg 2 of
    the completion backflush, reconciled against ``qty_planned`` for the whole
    job), so per-operation numbers would be meaningless for it."""
    wc = make_work_center(db_session)
    station = make_kiosk_station(db_session, work_center=wc)
    _, operation = make_wo_with_operation(db_session, work_center=wc)
    live = tie(db_session, operation, make_sheet(db_session))
    tie(db_session, operation, make_sheet(db_session), status_=AllocationStatus.CANCELLED)
    tie(db_session, operation, make_sheet(db_session), work_order_scoped=True)

    resp = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station)))
    lines = _queue_row(resp.json(), operation.id)["material_ties"]

    assert [line["allocation_id"] for line in lines] == [live.id]


def test_station_tenant_scope_comes_from_the_station_row_not_the_client(client: TestClient, db_session: Session):
    """Invariant 1 on an unattended terminal.

    The leak shape is an allocation row stamped with Company B pointing at Company
    A's operation. A Company-A station must see only its own tie -- and a Company-B
    station bound to the same operation id must see only B's.
    """
    ensure_company(db_session, COMPANY_B)
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station_a = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)
    _, operation = make_wo_with_operation(db_session, company_id=COMPANY_A, work_center=wc)

    mine = tie(db_session, operation, make_sheet(db_session, company_id=COMPANY_A))
    theirs = tie(
        db_session,
        operation,
        make_sheet(db_session, company_id=COMPANY_B),
        company_id=COMPANY_B,
    )

    resp = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station_a)))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    lines = _queue_row(resp.json(), operation.id)["material_ties"]

    assert [line["allocation_id"] for line in lines] == [mine.id]
    assert theirs.id not in {line["allocation_id"] for line in lines}


def test_a_station_token_with_a_forged_company_claim_never_returns_ties(client: TestClient, db_session: Session):
    """``material_ties`` widens what an unattended PIN terminal can read -- it adds
    material part numbers and ON-HAND STOCK -- so the claim-vs-row check that
    guards the queue is now guarding that disclosure too.

    ``get_kiosk_or_user`` compares the token's ``company_id`` against the station's
    OWN row and refuses on a mismatch, so a forged claim cannot reach a 200 at all.
    The positive control below proves the same station reads its queue fine once
    the claim is honest.
    """
    ensure_company(db_session, COMPANY_B)
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)
    _, operation = make_wo_with_operation(db_session, company_id=COMPANY_A, work_center=wc)
    tie(db_session, operation, make_sheet(db_session, company_id=COMPANY_A))

    forged = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station, company_id=COMPANY_B)))
    assert forged.status_code == status.HTTP_401_UNAUTHORIZED, forged.text

    honest = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station)))
    assert honest.status_code == status.HTTP_200_OK, honest.text
    assert _queue_row(honest.json(), operation.id)["material_ties"], "the control must really carry a tie"


def test_queue_tie_read_is_batched_across_a_full_queue(client: TestClient, db_session: Session):
    """The whole reason the queue takes a batched map: this endpoint is polled
    every 10-15s per station, all day, shop-wide. A per-card tie query would be
    that cadence times the card count."""
    wc = make_work_center(db_session)
    _, first = make_wo_with_operation(db_session, work_center=wc)
    tie(db_session, first, make_sheet(db_session))
    operator = make_user(db_session, role=UserRole.OPERATOR)
    headers = user_headers(operator)

    db_session.expire_all()
    one_row = statements_during(db_session, lambda: client.get(queue_url(wc.id), headers=headers))

    for _ in range(19):
        _, operation = make_wo_with_operation(db_session, work_center=wc)
        # A DISTINCT part per tie so both the label lookup and the stock aggregate
        # would fan out if either were per-row.
        tie(db_session, operation, make_sheet(db_session))

    db_session.expire_all()
    twenty_rows = statements_during(db_session, lambda: client.get(queue_url(wc.id), headers=headers))

    # EXACTLY ONE allocations SELECT, whether the queue holds 1 tied card or 20.
    assert allocation_reads(one_row) == 1
    assert allocation_reads(twenty_rows) == 1, (
        "the tie read fanned out per card -- at a 10-15s poll per station, shop-wide, "
        "that is the queue length times the cadence"
    )


def test_queue_read_of_an_open_tie_writes_nothing(client: TestClient, db_session: Session):
    """A poll is not an actor: it has no intent and records no reason, so it must
    never move stock -- even though the operation carries completed runs and a
    genuine positive consumption delta is sitting there."""
    wc = make_work_center(db_session)
    station = make_kiosk_station(db_session, work_center=wc)
    _, operation = make_wo_with_operation(db_session, work_center=wc)
    operation.quantity_complete = 4
    operation.quantity_scrapped = 1
    db_session.commit()
    sheet = make_sheet(db_session, on_hand=40.0)
    allocation = tie(db_session, operation, sheet, qty_per_run=1.0, qty_planned=5.0)

    txns_before = db_session.query(InventoryTransaction).count()

    resp = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station)))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert _queue_row(resp.json(), operation.id)["material_ties"], "the read must have seen the tie"

    db_session.expire_all()
    assert db_session.query(InventoryTransaction).count() == txns_before
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 0.0
    assert db_session.query(InventoryItem).filter(InventoryItem.part_id == sheet.id).one().quantity_on_hand == 40.0
    assert db_session.query(AuditLog).filter(AuditLog.resource_type == "work_order_material_allocation").count() == 0


# ---------------------------------------------------------------------------
# my-active-job
# ---------------------------------------------------------------------------


def test_active_job_carries_ties_and_the_operation_scrap_total(client: TestClient, db_session: Session):
    """``operation_quantity_scrapped`` is a DISTINCT key from ``quantity_scrapped``.

    The latter is THIS TIME ENTRY's session scrap; the material prediction scales
    on (complete + scrapped) at the OPERATION level -- a scrapped run still ate its
    sheet -- so a job worked across two shifts would under-state its deduction if
    the client read the session figure. The fixture makes the two DIFFER, or the
    assertion would pass on either key.
    """
    wc = make_work_center(db_session)
    operator = make_user(db_session, role=UserRole.OPERATOR)
    _, operation = make_wo_with_operation(db_session, work_center=wc)
    operation.quantity_complete = 6
    operation.quantity_scrapped = 5  # accumulated across sessions
    db_session.commit()
    entry = clock_in(db_session, operator, operation)
    entry.quantity_scrapped = 2  # THIS session only
    db_session.commit()
    sheet = make_sheet(db_session, on_hand=30.0)
    allocation = tie(db_session, operation, sheet, qty_per_run=1.0, qty_planned=12.0)

    resp = client.get(ACTIVE_JOB_URL, headers=user_headers(operator))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    [job] = resp.json()["active_jobs"]

    assert job["operation_quantity_scrapped"] == 5.0
    assert job["quantity_scrapped"] == 2.0, "the session key must stay untouched (additive change)"
    assert job["quantity_complete"] == 6.0
    [line] = job["material_ties"]
    assert line["allocation_id"] == allocation.id
    assert line["part_number"] == sheet.part_number
    assert line["qty_remaining"] == 12.0
    assert line["on_hand"] == 30.0


def test_active_job_on_an_untied_operation_carries_an_empty_list(client: TestClient, db_session: Session):
    wc = make_work_center(db_session)
    operator = make_user(db_session, role=UserRole.OPERATOR)
    _, operation = make_wo_with_operation(db_session, work_center=wc)
    clock_in(db_session, operator, operation)

    resp = client.get(ACTIVE_JOB_URL, headers=user_headers(operator))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    [job] = resp.json()["active_jobs"]

    assert job["material_ties"] == []
    assert job["operation_quantity_scrapped"] == 0.0


def test_active_job_ties_are_scoped_to_the_active_company(client: TestClient, db_session: Session):
    """An operator holding an entry whose operation belongs to another tenant
    simply gets no ties rather than leaking one."""
    ensure_company(db_session, COMPANY_B)
    wc = make_work_center(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A, role=UserRole.OPERATOR)
    _, operation = make_wo_with_operation(db_session, company_id=COMPANY_A, work_center=wc)
    clock_in(db_session, operator, operation)
    # The tie exists, but stamped for company B.
    tie(db_session, operation, make_sheet(db_session, company_id=COMPANY_B), company_id=COMPANY_B)

    resp = client.get(ACTIVE_JOB_URL, headers=user_headers(operator))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    [job] = resp.json()["active_jobs"]

    assert job["material_ties"] == []


def test_active_job_tie_read_is_one_batch_across_several_open_entries(client: TestClient, db_session: Session):
    """An operator can hold several open entries; the ties for all of them come
    back in one batched read."""
    wc = make_work_center(db_session)
    operator = make_user(db_session, role=UserRole.OPERATOR)
    headers = user_headers(operator)

    _, first = make_wo_with_operation(db_session, work_center=wc)
    clock_in(db_session, operator, first)
    tie(db_session, first, make_sheet(db_session))
    db_session.expire_all()
    one_job = statements_during(db_session, lambda: client.get(ACTIVE_JOB_URL, headers=headers))

    for _ in range(4):
        _, operation = make_wo_with_operation(db_session, work_center=wc)
        clock_in(db_session, operator, operation)
        tie(db_session, operation, make_sheet(db_session))
    db_session.expire_all()
    five_jobs = statements_during(db_session, lambda: client.get(ACTIVE_JOB_URL, headers=headers))

    assert allocation_reads(one_job) == 1
    assert allocation_reads(five_jobs) == 1, "the tie read fanned out per active entry"
