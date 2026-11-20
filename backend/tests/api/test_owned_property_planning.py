"""Owned property and planning / reorder readers (AS9100D 8.5.3 x MRP and low stock).

Customer- and supplier-owned stock is physically on the rack but is not the shop's supply:
automatic consumption never draws it (``test_owned_property_consumption.py``), so a planner
that nets demand against it, or a reorder check that counts it, promises material the floor
can never take. The rule is one shared clause,
``owned_property_service.COMPANY_OWNED_ITEM_CLAUSES``, splatted into each reader NEXT TO
that reader's own status rule. This file pins each half of it:

* **Owned stock covers no MRP demand.** ``MRPService.get_inventory_summary`` -- the one
  netting seam behind ``run_mrp`` and the reviewed supply draft's fingerprint -- reports
  on hand and allocated over company-owned rows only. A component whose only stock is a
  customer's or supplier's lot shows its full shortage with an action for it; mixed stock
  nets only the company quantity; a reservation on an owned row does not reduce the
  shop's available.
* **Owned stock keeps no reorder alert quiet**, on every low-stock reader:
  ``GET /inventory/low-stock``, the ``stock.low`` cron (``check_low_stock_task``, which
  compares PER ROW), the operations inbox's ``low_stock`` source, the AI inventory-risk
  sensor, the consumable crib's ``low_stock`` filter and ``is_low_stock`` (the owned rows
  themselves are still listed) and the stockout prediction's ``current_stock``.
* **A company with no owned stock is byte-identical.** Each reader kept its OWN status
  rule, and that is what the ``*_company_only_*`` tests pin, so a future "tidy-up" that
  swaps in ``AUTO_DRAW_ITEM_CLAUSES`` (which drops held stock and widens NULL status to
  available) fails here: the low-stock route, cron, inbox, sensor, crib and prediction
  still COUNT an ``on_hold`` company lot; MRP still counts only ``status == 'available'``
  on hand (an ``on_hold`` lot and a NULL-status lot are NOT counted) while its allocated
  figure still reads active rows in any status. Each expected figure is computed by hand
  in the test.
* **One spelling.** ``AUTO_DRAW_ITEM_CLAUSES`` still ends with the shared ownership clause,
  so "company-owned" cannot drift between the draw and the planners.

Valuation (``GET /reports/inventory-value``, the analytics value figures) only swapped an
identical inline clause for the shared constant; its existing tests cover it. No test here
orders lots, so THE FIFO TRAP in ``test_backflush_lot_policy.py`` does not apply. Every name
is invented.
"""

import asyncio
from typing import Dict, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.jobs.notification_jobs as notification_jobs
from app.models.ai_learning import AIRecommendation
from app.models.inventory import InventoryItem
from app.models.mrp import MRPAction, MRPRequirement, MRPRunStatus, PlanningAction
from app.models.part import Part
from app.models.user import User
from app.schemas.consumables import ConsumableStock
from app.services.ai_sensors.inventory import run_inventory_risk_sensor
from app.services.consumable_service import list_stock
from app.services.material_consumption_service import AUTO_DRAW_ITEM_CLAUSES
from app.services.mrp_service import MRPService
from app.services.operations_inbox_service import OperationalInboxService
from app.services.prediction_service import PredictionService
from tests.api.test_backflush_lot_policy import (
    COMPANY_A,
    add_bom_item,
    headers_for,
    make_bom,
    make_lot,
    make_part,
    make_user,
    make_wo,
)
from tests.api.test_owned_property_consumption import _own
from tests.test_notification_jobs_tenant_isolation import _install, _Recorder

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

OWNER_KINDS = ["customer", "supplier"]

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lot(
    db: Session,
    part: Part,
    qty: float,
    *,
    status_: Optional[str] = "available",
    is_active: bool = True,
    allocated: float = 0.0,
    owner: Optional[str] = None,
) -> InventoryItem:
    """One stock row. ``status_=None`` writes the legacy NULL-status shape; ``owner`` re-owns
    the row as 'customer' / 'supplier' (creating the owner) via ``_own``."""
    item = make_lot(db, part, qty=qty, lot=f"PLAN-{_next():05d}", status_=status_ or "available", is_active=is_active)
    if status_ is None:
        item.status = None
    item.quantity_allocated = allocated
    db.commit()
    if owner is not None:
        _own(db, item, owner)
    db.refresh(item)
    if status_ is None:
        assert item.status is None, "the fixture must really be NULL"
    return item


def _reorder_part(
    db: Session, *, reorder_point: float, safety_stock: float = 0.0, part_type: str = "purchased"
) -> Part:
    part = make_part(db, part_type=part_type)
    part.reorder_point = reorder_point
    part.safety_stock = safety_stock
    db.commit()
    db.refresh(part)
    return part


def _mrp_component(db: Session, *, demand: float) -> Part:
    """A purchased component carrying exactly ``demand`` units of MRP demand.

    One IN_PROGRESS work order (due in 30 days, inside the default 90-day horizon) for an
    assembly whose active BOM needs one component per unit. No PO and no make work order
    produces the component, so on order is 0 and the netting is on hand - allocated alone.
    """
    assembly = make_part(db)
    component = make_part(db, part_type="purchased")
    add_bom_item(db, make_bom(db, assembly), component, quantity=1.0)
    make_wo(db, assembly, quantity_ordered=demand)
    return component


def _summary(db: Session, part: Part) -> Tuple[float, float, float]:
    return MRPService(db, COMPANY_A).get_inventory_summary(part.id)


def _run_mrp(db: Session, component: Part) -> Tuple[MRPRequirement, List[MRPAction]]:
    run = MRPService(db, COMPANY_A).run_mrp(user_id=None, include_safety_stock=False)
    assert run.status == MRPRunStatus.COMPLETE, run.error_message
    db.expire_all()
    requirements = (
        db.query(MRPRequirement)
        .filter(MRPRequirement.mrp_run_id == run.id, MRPRequirement.part_id == component.id)
        .all()
    )
    actions = db.query(MRPAction).filter(MRPAction.mrp_run_id == run.id, MRPAction.part_id == component.id).all()
    assert len(requirements) == 1, "one work order, one due date: one requirement row"
    return requirements[0], actions


def _low_stock_alerts(client: TestClient, user: User) -> Dict[int, dict]:
    resp = client.get("/api/v1/inventory/low-stock", headers=headers_for(user))
    assert resp.status_code == 200, resp.text
    return {row["part_id"]: row for row in resp.json()}


def _run_low_stock_cron(db: Session, monkeypatch) -> Tuple[dict, list]:
    recorder = _Recorder()
    _install(monkeypatch, db, recorder)
    result = asyncio.run(notification_jobs.check_low_stock_task())
    return result, recorder.calls


def _inbox_low_stock(db: Session, user: User) -> Dict[int, object]:
    items, _truncated = OperationalInboxService(db, user, COMPANY_A).collect(only_kind="low_stock")
    return {item.source_id: item for item, _record in items}


def _risk_recs(db: Session, part: Part) -> List[AIRecommendation]:
    db.expire_all()
    return (
        db.query(AIRecommendation)
        .filter(
            AIRecommendation.company_id == COMPANY_A,
            AIRecommendation.recommendation_type == "inventory_risk",
            AIRecommendation.target_entity_id == part.id,
        )
        .all()
    )


def _crib(db: Session, part: Part, *, low_stock: bool = False) -> List[ConsumableStock]:
    return [row for row in list_stock(db, COMPANY_A, low_stock=low_stock)["items"] if row.part_id == part.id]


def _predicted_stock(db: Session) -> Dict[int, float]:
    predictions = PredictionService(db, COMPANY_A).predict_inventory_demand().predictions
    return {p.part_id: float(p.current_stock) for p in predictions}


# ===========================================================================
# 1. MRP: owned stock covers no demand
# ===========================================================================


@pytest.mark.parametrize("kind", OWNER_KINDS)
def test_mrp_owned_only_stock_leaves_the_full_shortage(db_session: Session, kind):
    """The component's only stock is an owned lot five times the demand: MRP still sees a
    shortage of the whole demand and recommends buying it."""
    component = _mrp_component(db_session, demand=20)
    _lot(db_session, component, 100, owner=kind)

    assert _summary(db_session, component) == (0.0, 0.0, 0.0)

    requirement, actions = _run_mrp(db_session, component)
    assert requirement.quantity_required == 20
    assert requirement.quantity_on_hand == 0
    assert requirement.quantity_available == 0
    assert requirement.quantity_shortage == 20
    assert [(a.action_type, a.quantity) for a in actions] == [(PlanningAction.ORDER, 20)]


@pytest.mark.parametrize("kind", OWNER_KINDS)
def test_mrp_mixed_stock_nets_only_the_company_quantity(db_session: Session, kind):
    component = _mrp_component(db_session, demand=20)
    _lot(db_session, component, 8)
    _lot(db_session, component, 50, owner=kind)

    assert _summary(db_session, component) == (8.0, 0.0, 0.0)

    requirement, actions = _run_mrp(db_session, component)
    assert requirement.quantity_on_hand == 8
    assert requirement.quantity_shortage == 12
    assert [a.quantity for a in actions] == [12]


@pytest.mark.parametrize("kind", OWNER_KINDS)
def test_mrp_allocation_on_an_owned_row_does_not_reduce_available(db_session: Session, kind):
    """A reservation against someone else's material is not a claim on the shop's supply.

    The triple pins BOTH halves: an implementation that filtered on hand but forgot
    allocated would read (30, 10, 0) and leave 20 available against 25 of demand.
    """
    component = _mrp_component(db_session, demand=25)
    _lot(db_session, component, 30)
    _lot(db_session, component, 10, allocated=10, owner=kind)

    assert _summary(db_session, component) == (30.0, 0.0, 0.0)

    requirement, actions = _run_mrp(db_session, component)
    assert requirement.quantity_allocated == 0
    assert requirement.quantity_available == 30
    assert requirement.quantity_shortage == 0
    assert actions == []


def test_mrp_company_only_status_rule_is_unchanged(db_session: Session):
    """MRP's OWN status rule, by hand: on hand is active AND ``status == 'available'``
    (a held lot and a legacy NULL-status lot are NOT counted); allocated is every active
    row in any status. ``AUTO_DRAW_ITEM_CLAUSES`` would read on hand 17 (NULL counted)
    and allocated 0 (held dropped), so a swap fails here."""
    component = _mrp_component(db_session, demand=20)
    _lot(db_session, component, 10)
    _lot(db_session, component, 50, status_="on_hold", allocated=4)
    _lot(db_session, component, 70, status_="quarantine")
    _lot(db_session, component, 7, status_=None)
    _lot(db_session, component, 90, is_active=False, allocated=3)

    on_hand = 10.0  # the one active, 'available' row
    allocated = 4.0  # the held row's reservation; the inactive row's 3 is not counted
    assert _summary(db_session, component) == (on_hand, allocated, 0.0)

    requirement, actions = _run_mrp(db_session, component)
    assert requirement.quantity_on_hand == on_hand
    assert requirement.quantity_allocated == allocated
    assert requirement.quantity_available == on_hand - allocated
    assert requirement.quantity_shortage == 20 - (on_hand - allocated)
    assert [a.quantity for a in actions] == [14]


# ===========================================================================
# 2. GET /inventory/low-stock
# ===========================================================================


@pytest.mark.parametrize("kind", OWNER_KINDS)
def test_low_stock_route_flags_parts_held_up_only_by_owned_stock(client: TestClient, db_session: Session, kind):
    user = make_user(db_session)
    owned_only = _reorder_part(db_session, reorder_point=50, safety_stock=5)
    _lot(db_session, owned_only, 200, owner=kind)
    mixed = _reorder_part(db_session, reorder_point=50, safety_stock=5)
    _lot(db_session, mixed, 10)
    _lot(db_session, mixed, 200, owner=kind)

    alerts = _low_stock_alerts(client, user)

    assert owned_only.id in alerts, "an owned lot above the reorder point must not hide the shortage"
    assert alerts[owned_only.id]["quantity_on_hand"] == 0
    assert alerts[owned_only.id]["shortage"] == 50
    assert alerts[owned_only.id]["is_critical"] is True
    assert mixed.id in alerts
    assert alerts[mixed.id]["quantity_on_hand"] == 10
    assert alerts[mixed.id]["shortage"] == 40


def test_low_stock_route_company_only_still_counts_held_stock(client: TestClient, db_session: Session):
    """The route's own rule, by hand: every ACTIVE row in ANY status. A part whose held lot
    alone is above the reorder point stays off the list."""
    user = make_user(db_session)
    mixed = _reorder_part(db_session, reorder_point=50, safety_stock=5)
    _lot(db_session, mixed, 10)
    _lot(db_session, mixed, 15, status_="on_hold")
    _lot(db_session, mixed, 5, status_="quarantine")
    _lot(db_session, mixed, 3, status_=None)
    _lot(db_session, mixed, 100, is_active=False)
    held_cover = _reorder_part(db_session, reorder_point=50)
    _lot(db_session, held_cover, 60, status_="on_hold")

    alerts = _low_stock_alerts(client, user)

    on_hand = 10 + 15 + 5 + 3
    assert alerts[mixed.id]["quantity_on_hand"] == on_hand
    assert alerts[mixed.id]["shortage"] == 50 - on_hand
    assert alerts[mixed.id]["is_critical"] is False
    assert held_cover.id not in alerts, "held company stock still counts toward on hand"


# ===========================================================================
# 3. The stock.low cron (per-row comparison)
# ===========================================================================


@pytest.mark.parametrize("kind", OWNER_KINDS)
def test_stock_low_cron_counts_company_rows_not_owned_rows(db_session: Session, monkeypatch, kind):
    """Owned rows below and AT the reorder point are no longer counted; the company row is."""
    part = _reorder_part(db_session, reorder_point=50)
    _lot(db_session, part, 5)
    _lot(db_session, part, 5, owner=kind)
    _lot(db_session, part, 50, owner=kind)
    owned_only = _reorder_part(db_session, reorder_point=50)
    _lot(db_session, owned_only, 1, owner=kind)

    result, calls = _run_low_stock_cron(db_session, monkeypatch)

    assert result == {"low_stock_items": 1}
    assert [(c["event_key"], c["company_id"]) for c in calls] == [("stock.low", COMPANY_A)]


@pytest.mark.parametrize("kind", OWNER_KINDS)
def test_stock_low_cron_sends_nothing_for_owned_rows_alone(db_session: Session, monkeypatch, kind):
    part = _reorder_part(db_session, reorder_point=50)
    _lot(db_session, part, 5, owner=kind)

    result, calls = _run_low_stock_cron(db_session, monkeypatch)

    assert result == {"low_stock_items": 0}
    assert calls == []


def test_stock_low_cron_company_only_still_counts_held_rows(db_session: Session, monkeypatch):
    """The cron's own rule, by hand: each row at or below its part's reorder point, in ANY
    status. ``AUTO_DRAW_ITEM_CLAUSES`` would keep only the available and NULL rows (2)."""
    part = _reorder_part(db_session, reorder_point=50)
    _lot(db_session, part, 5)  # counted
    _lot(db_session, part, 7, status_="on_hold")  # counted
    _lot(db_session, part, 50, status_="quarantine")  # counted: at the point
    _lot(db_session, part, 1, status_=None)  # counted
    _lot(db_session, part, 51)  # above the point

    result, calls = _run_low_stock_cron(db_session, monkeypatch)

    assert result == {"low_stock_items": 4}
    assert [c["event_key"] for c in calls] == ["stock.low"]


# ===========================================================================
# 4. The operations inbox's low_stock source
# ===========================================================================


@pytest.mark.parametrize("kind", OWNER_KINDS)
def test_inbox_raises_low_stock_for_parts_held_up_only_by_owned_stock(db_session: Session, kind):
    user = make_user(db_session)
    owned_only = _reorder_part(db_session, reorder_point=50, safety_stock=5)
    _lot(db_session, owned_only, 200, owner=kind)
    mixed = _reorder_part(db_session, reorder_point=50, safety_stock=5)
    _lot(db_session, mixed, 10)
    _lot(db_session, mixed, 200, owner=kind)

    items = _inbox_low_stock(db_session, user)

    assert owned_only.id in items, "an owned lot above the reorder point must keep no low_stock item quiet"
    assert items[owned_only.id].detail.startswith("0 on hand;")
    assert items[owned_only.id].severity == "high"
    assert mixed.id in items
    assert items[mixed.id].detail.startswith("10 on hand;")


def test_inbox_company_only_still_counts_held_stock(db_session: Session):
    """The inbox's own rule, by hand: every ACTIVE row in ANY status, strictly below the
    reorder point."""
    user = make_user(db_session)
    mixed = _reorder_part(db_session, reorder_point=50, safety_stock=5)
    _lot(db_session, mixed, 10)
    _lot(db_session, mixed, 15, status_="on_hold")
    _lot(db_session, mixed, 3, status_=None)
    _lot(db_session, mixed, 100, is_active=False)
    held_cover = _reorder_part(db_session, reorder_point=50)
    _lot(db_session, held_cover, 60, status_="on_hold")

    items = _inbox_low_stock(db_session, user)

    assert items[mixed.id].detail.startswith(f"{10 + 15 + 3} on hand;")
    assert items[mixed.id].severity == "medium"
    assert held_cover.id not in items, "held company stock still counts toward on hand"


# ===========================================================================
# 5. The AI inventory-risk sensor
# ===========================================================================


@pytest.mark.parametrize("kind", OWNER_KINDS)
def test_inventory_risk_sensor_mints_for_parts_held_up_only_by_owned_stock(db_session: Session, kind):
    owned_only = _reorder_part(db_session, reorder_point=50, safety_stock=5)
    _lot(db_session, owned_only, 200, owner=kind)
    mixed = _reorder_part(db_session, reorder_point=50, safety_stock=5)
    _lot(db_session, mixed, 10)
    _lot(db_session, mixed, 200, owner=kind)

    created = run_inventory_risk_sensor(db_session, COMPANY_A)
    db_session.commit()

    assert created == 2
    (owned_rec,) = _risk_recs(db_session, owned_only)
    assert owned_rec.evidence[0]["on_hand"] == 0
    assert owned_rec.priority == "high"
    (mixed_rec,) = _risk_recs(db_session, mixed)
    assert mixed_rec.evidence[0]["on_hand"] == 10


def test_inventory_risk_sensor_company_only_still_counts_held_stock(db_session: Session):
    """The sensor's own rule, by hand: every ACTIVE row in ANY status."""
    mixed = _reorder_part(db_session, reorder_point=50, safety_stock=5)
    _lot(db_session, mixed, 10)
    _lot(db_session, mixed, 15, status_="on_hold")
    _lot(db_session, mixed, 3, status_=None)
    _lot(db_session, mixed, 100, is_active=False)
    held_cover = _reorder_part(db_session, reorder_point=50)
    _lot(db_session, held_cover, 60, status_="on_hold")

    created = run_inventory_risk_sensor(db_session, COMPANY_A)
    db_session.commit()

    assert created == 1
    (rec,) = _risk_recs(db_session, mixed)
    assert rec.evidence[0]["on_hand"] == 10 + 15 + 3
    assert _risk_recs(db_session, held_cover) == [], "held company stock still counts toward on hand"


# ===========================================================================
# 6. The consumable crib's low-stock judgment
# ===========================================================================


@pytest.mark.parametrize("kind", OWNER_KINDS)
def test_crib_low_stock_ignores_owned_stock_but_still_lists_the_rows(db_session: Session, kind):
    owned_only = _reorder_part(db_session, reorder_point=20, part_type="consumable")
    owned_row = _lot(db_session, owned_only, 100, owner=kind)
    mixed = _reorder_part(db_session, reorder_point=20, part_type="consumable")
    company_row = _lot(db_session, mixed, 5)
    mixed_owned_row = _lot(db_session, mixed, 100, owner=kind)

    rows = _crib(db_session, owned_only)
    assert [(r.inventory_item_id, r.quantity_on_hand) for r in rows] == [(owned_row.id, 100)], "still listed"
    assert {(r.part_quantity_on_hand, r.is_low_stock) for r in rows} == {(0, True)}

    rows = _crib(db_session, mixed)
    assert sorted((r.inventory_item_id, r.quantity_on_hand) for r in rows) == sorted(
        [(company_row.id, 5), (mixed_owned_row.id, 100)]
    )
    assert {(r.part_quantity_on_hand, r.is_low_stock) for r in rows} == {(5, True)}

    assert {r.inventory_item_id for r in _crib(db_session, owned_only, low_stock=True)} == {owned_row.id}
    assert {r.inventory_item_id for r in _crib(db_session, mixed, low_stock=True)} == {
        company_row.id,
        mixed_owned_row.id,
    }


def test_crib_company_only_still_counts_held_stock(db_session: Session):
    """The crib's own rule, by hand: every ACTIVE row in ANY status, at or below the
    reorder point. A part whose held lot alone covers the point is not low."""
    mixed = _reorder_part(db_session, reorder_point=20, part_type="consumable")
    _lot(db_session, mixed, 5)
    _lot(db_session, mixed, 10, status_="on_hold")
    _lot(db_session, mixed, 2, status_=None)
    _lot(db_session, mixed, 50, is_active=False)
    held_cover = _reorder_part(db_session, reorder_point=20, part_type="consumable")
    _lot(db_session, held_cover, 50, status_="on_hold")

    assert {(r.part_quantity_on_hand, r.is_low_stock) for r in _crib(db_session, mixed)} == {(5 + 10 + 2, True)}
    assert {(r.part_quantity_on_hand, r.is_low_stock) for r in _crib(db_session, held_cover)} == {(50, False)}
    assert _crib(db_session, mixed, low_stock=True), "the mixed part is low"
    assert _crib(db_session, held_cover, low_stock=True) == [], "held company stock still counts toward on hand"


# ===========================================================================
# 7. The stockout prediction
# ===========================================================================


@pytest.mark.parametrize("kind", OWNER_KINDS)
def test_stockout_prediction_counts_only_company_stock(db_session: Session, kind):
    owned_only = make_part(db_session, part_type="purchased")
    _lot(db_session, owned_only, 200, owner=kind)
    mixed = make_part(db_session, part_type="purchased")
    _lot(db_session, mixed, 10)
    _lot(db_session, mixed, 200, owner=kind)

    stock = _predicted_stock(db_session)

    assert stock[owned_only.id] == 0
    assert stock[mixed.id] == 10


def test_stockout_prediction_company_only_still_counts_held_stock(db_session: Session):
    """The prediction's own rule, by hand: every ACTIVE row in ANY status."""
    part = make_part(db_session, part_type="raw_material")
    _lot(db_session, part, 10)
    _lot(db_session, part, 15, status_="on_hold")
    _lot(db_session, part, 5, status_="quarantine")
    _lot(db_session, part, 3, status_=None)
    _lot(db_session, part, 100, is_active=False)

    assert _predicted_stock(db_session)[part.id] == 10 + 15 + 5 + 3


# ===========================================================================
# 8. One spelling of "company-owned"
# ===========================================================================


def test_auto_draw_clauses_end_with_the_shared_ownership_clause():
    """The automatic draw and the planning / reorder readers share ONE ownership clause.

    Imported here rather than at module top so the rest of this file still runs against a
    tree that predates the constant.
    """
    from app.services.owned_property_service import COMPANY_OWNED_ITEM_CLAUSES

    def sql(clause) -> str:
        return str(clause.compile(compile_kwargs={"literal_binds": True}))

    assert [sql(c) for c in COMPANY_OWNED_ITEM_CLAUSES] == ["inventory_items.ownership = 'company'"]
    tail = AUTO_DRAW_ITEM_CLAUSES[-len(COMPANY_OWNED_ITEM_CLAUSES) :]
    assert all(a is b for a, b in zip(tail, COMPANY_OWNED_ITEM_CLAUSES)), "built from the shared tuple, not a copy"
    assert sql(AUTO_DRAW_ITEM_CLAUSES[-1]) == "inventory_items.ownership = 'company'"
