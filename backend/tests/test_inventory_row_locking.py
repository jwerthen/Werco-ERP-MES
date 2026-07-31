"""FOR UPDATE row locks on the inventory read-modify-write paths (finding B2).

The lost-update race: two completions of DIFFERENT work orders drawing the same lot
both read ``quantity_on_hand``, both compute a new absolute value, and the second
write silently erases the first movement. The WO optimistic lock (invariant #4) only
serializes same-WO writers, so the fix is ``FOR UPDATE`` row locks on the
``InventoryItem`` rows before every read-modify-write:

* the consumption engines' candidate-lot selection (``consumable_source_items`` with
  ``for_update=True``) and both pinned-lot lookups;
* the return verb's source-row fetch;
* the manual ``/inventory`` endpoints (receive / issue / transfer / adjust) and the
  cycle-count completion's bulk load.

Lock-ordering rule (documented in the engine's concurrency docstring): the WO lock is
taken first, and each locking QUERY acquires its inventory rows in ascending-id order
-- the for-update branch orders by ``id`` and re-sorts to FIFO in Python. That removes
opposite-order acquisition within one candidate-lot set but is NOT a global ordering
across the several locking queries one completion issues; residual deadlocks are
handled by Postgres detection plus the savepoint/reconcile convergence, per the
engine docstring.

SQLite ignores ``FOR UPDATE`` entirely, so the behavioral suite cannot observe it.
These tests compile the queries for the POSTGRESQL dialect and assert the ``FOR
UPDATE`` clause is present (and absent on the pure-read branches), which is the only
honest way to pin the production SQL from this test backend. The FIFO re-sort is
covered behaviorally below since it runs in Python either way.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.api.endpoints.inventory import _load_inventory_items, _stock_row_query
from app.models.company import Company
from app.models.inventory import InventoryItem
from app.models.part import Part
from app.services.material_consumption_service import (
    _fifo_sort_key,
    consumable_source_items,
    consumable_source_query,
)

pytestmark = [pytest.mark.unit, pytest.mark.requires_db]

COMPANY_A = 1


def _compile_pg(query) -> str:
    return str(query.statement.compile(dialect=postgresql.dialect()))


# ---------------------------------------------------------------------------
# Dialect-compile assertions: the postgresql SQL carries FOR UPDATE
# ---------------------------------------------------------------------------


def test_consumable_source_query_for_update_compiles_to_for_update(db_session: Session):
    sql = _compile_pg(consumable_source_query(db_session, 1, COMPANY_A, for_update=True))
    assert "FOR UPDATE" in sql
    # Lock acquisition order is ascending id -- the per-query ordering rule.
    assert "ORDER BY inventory_items.id ASC" in sql


def test_consumable_source_query_plain_read_has_no_for_update(db_session: Session):
    """The preview / pure-read branch must NOT lock: a backflush preview is a read
    that writes nothing, and locking from it would serialize reads behind writers."""
    sql = _compile_pg(consumable_source_query(db_session, 1, COMPANY_A))
    assert "FOR UPDATE" not in sql
    # And it keeps the FIFO order in SQL.
    assert "received_date" in sql


def test_manual_endpoint_stock_row_query_for_update_compiles_to_for_update(db_session: Session):
    sql = _compile_pg(
        _stock_row_query(
            db_session,
            company_id=COMPANY_A,
            part_id=1,
            location_code="A1",
            lot_number=None,
            for_update=True,
        )
    )
    assert "FOR UPDATE" in sql
    # The NULL-lot branch must compile to IS NULL, never `lot_number = NULL`.
    assert "lot_number IS NULL" in sql


def test_stock_row_query_with_lot_uses_equality(db_session: Session):
    sql = _compile_pg(
        _stock_row_query(
            db_session,
            company_id=COMPANY_A,
            part_id=1,
            location_code="A1",
            lot_number="LOT-1",
            for_update=False,
        )
    )
    assert "FOR UPDATE" not in sql
    assert "lot_number IS NULL" not in sql
    assert "lot_number =" in sql


def test_engine_call_sites_pass_for_update():
    """The two consuming engines and the return verb actually TAKE the locks: the
    pinned-lot lookups and the return fetch carry ``with_for_update`` and the
    candidate selection is called with ``for_update=True``. Source-level, because the
    SQLite backend cannot observe a lock behaviorally."""
    import inspect

    from app.services import completion_inventory_service, material_consumption_service

    engine_src = inspect.getsource(material_consumption_service._consume_one_allocation)
    assert "for_update=True" in engine_src
    assert "with_for_update()" in engine_src  # the pinned-lot lookup

    backflush_src = inspect.getsource(completion_inventory_service._issue_one_component)
    assert "for_update=True" in backflush_src
    assert "with_for_update()" in backflush_src  # the pinned-lot lookup

    return_src = inspect.getsource(material_consumption_service._resolve_return_source_lots)
    assert "with_for_update()" in return_src


def test_manual_inventory_endpoints_take_row_locks():
    """The `/inventory` endpoint call sites actually TAKE the locks (same source-level
    rationale as above -- SQLite cannot observe them). Without this pin, dropping
    ``with_for_update()`` from issue/transfer/adjust or the ``for_update=True`` from
    receive / transfer-destination / cycle-count completion would fail no test."""
    import inspect

    from app.api.endpoints import inventory

    # Single-row read-modify-writes lock the row they mutate.
    assert "with_for_update()" in inspect.getsource(inventory.issue_inventory)
    assert "with_for_update()" in inspect.getsource(inventory.adjust_inventory)

    transfer_src = inspect.getsource(inventory.transfer_inventory)
    assert "with_for_update()" in transfer_src  # source row
    assert "for_update=True" in transfer_src  # destination row via _find_stock_row

    # Receive locks the existing (part, location, lot) row it increments.
    assert "for_update=True" in inspect.getsource(inventory.receive_inventory)

    # Cycle-count completion locks the bulk load exactly when it will write.
    assert "for_update=apply_adjustments" in inspect.getsource(inventory.complete_cycle_count)


# ---------------------------------------------------------------------------
# The for_update branch preserves the FIFO draw order (Python re-sort)
# ---------------------------------------------------------------------------


def _ensure_company(db: Session, company_id: int) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True))
        db.commit()


_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _make_part(db: Session) -> Part:
    _ensure_company(db, COMPANY_A)
    n = _next()
    part = Part(
        part_number=f"LOCK-P-{n:05d}",
        name=f"Lock test part {n}",
        part_type="purchased",
        unit_of_measure="each",
        is_active=True,
        company_id=COMPANY_A,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def _make_lot(db: Session, part: Part, *, qty: float, received_date) -> InventoryItem:
    n = _next()
    item = InventoryItem(
        part_id=part.id,
        location="LOCK-LOC",
        warehouse="MAIN",
        quantity_on_hand=qty,
        quantity_allocated=0.0,
        quantity_available=qty,
        lot_number=f"LOCK-LOT-{n:05d}",
        received_date=received_date,
        is_active=True,
        status="available",
        company_id=COMPANY_A,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def test_for_update_branch_returns_the_same_fifo_order_as_the_plain_branch(db_session: Session):
    """Lock in ascending-id order, DRAW in FIFO order: the two branches must hand the
    caller identical lists, or the locking change would silently re-heat draws."""
    part = _make_part(db_session)
    now = datetime(2026, 3, 1, 12, 0, 0)
    newest = _make_lot(db_session, part, qty=5.0, received_date=now)
    oldest = _make_lot(db_session, part, qty=5.0, received_date=now - timedelta(days=30))
    undated = _make_lot(db_session, part, qty=5.0, received_date=None)

    plain = consumable_source_items(db_session, part.id, COMPANY_A)
    locked = consumable_source_items(db_session, part.id, COMPANY_A, for_update=True)

    assert [i.id for i in plain] == [oldest.id, newest.id, undated.id]
    assert [i.id for i in locked] == [i.id for i in plain]


def test_fifo_sort_key_orders_null_received_dates_last_with_id_tiebreak():
    class Row:
        def __init__(self, id, received_date):
            self.id = id
            self.received_date = received_date

    now = datetime(2026, 3, 1)
    rows = [Row(4, None), Row(3, now), Row(2, None), Row(1, now - timedelta(days=1))]
    rows.sort(key=_fifo_sort_key)
    assert [r.id for r in rows] == [1, 3, 2, 4]


def test_cycle_count_bulk_load_for_update_sorts_ids_ascending(db_session: Session):
    """``_load_inventory_items(for_update=True)`` must acquire in ascending-id order.
    Behaviorally observable half: the ids are sorted before chunking (the FOR UPDATE
    itself is pinned by source inspection, same rationale as above)."""
    import inspect

    src = inspect.getsource(_load_inventory_items)
    assert "sorted(" in src
    assert "with_for_update()" in src

    part = _make_part(db_session)
    a = _make_lot(db_session, part, qty=1.0, received_date=None)
    b = _make_lot(db_session, part, qty=2.0, received_date=None)
    loaded = _load_inventory_items(db_session, COMPANY_A, [b.id, a.id], for_update=True)
    assert set(loaded) == {a.id, b.id}
