"""Customer-supplied parts create no MRP demand (owner decision 2026-11-22, AS9100D 8.5.3).

A part the customer furnishes is not something the shop buys or makes, so MRP must not plan
supply for it. The flag is ``Part.customer_supplied`` and the predicate is
``mrp_service.is_customer_supplied``; this file pins every seam that reads it:

* **The MRP run.** ``MRPService.explode_bom_for_mrp`` skips a flagged component: no
  requirement, hence no shortage and no action, even with zero stock and a safety stock.
  An unflagged sibling in the same BOM keeps its shortage and its action. A flagged MAKE
  subassembly is not recursed into, so its own BOM children create nothing either; a
  flagged part deeper in the tree is skipped at that depth. Clearing the flag restores
  demand, which doubles as the positive control that the fixtures DO create demand.
* **An AUTO_DRAFT pass.** ``_run_mrp_for_company`` in AUTO_DRAFT (no longer scheduled: the
  06:00 cron is REVIEW-only) drafts nothing for a flagged part while still drafting its
  sibling. An action minted BEFORE the part was flagged is refused by
  ``MRPAutoService.process_actions`` ahead of any supply call -- ORDER never reaches
  ``_get_preferred_vendor``, MANUFACTURE is refused rather than counted as left for review
  (no automatic pass drafts a work order at all), EXPEDITE never reaches
  ``_flag_for_expedite`` / the ``mrp.expedite_required`` dispatch -- and the refusal text
  lands on ``action.error_message`` and in ``errors``. Each refusal is paired with an
  unflagged sibling that DOES reach the call, so a guard that refused everything fails.
* **The reviewed draft and the purchase batch.** ``MRPSupplyService.review`` reports
  ``blocked_reason == CUSTOMER_SUPPLIED_SUPPLY_DETAIL``, and both ``POST
  /mrp/actions/{id}/supply-draft`` (purchase and work-order kinds) and ``POST
  /mrp/purchase-batch/drafts`` answer 409 with that text and write nothing. The request is
  built from a review taken AFTER the flag, so its token is current and the flag is the
  only thing refusing it. Without the review's own check the recomputed shortage reads 0
  and the refusal would carry the "inventory covers this shortage" text instead -- so the
  exact-text assertions are what make these tests bite.
* **The write door.** ``PUT /parts/{id}`` and ``PUT /materials/{id}`` set and clear the
  flag for Admin / Manager / Supervisor and refuse other roles (403); the detail reads and
  the hand-built LIST responses report it; an explicit ``null`` is 422 and leaves the row
  (and every other field in the request) untouched; each flip writes a ``part`` audit row
  whose ``changes`` map carries old and new; ``POST /parts/``, ``POST /materials/`` and
  both CSV importers cannot set it; another company's part is 404 and untouched.
* **No flag, no change.** With nothing flagged a run over a mixed BOM (scrap factor,
  safety stock, partial stock, an allocation, a MAKE subassembly, an expedite) produces
  exactly the requirements and actions computed by hand below.

The flag is never inferred from stock ownership: a part whose only stock is a customer's
lot is still unflagged and still planned (``test_owned_property_planning.py`` owns the
netting half of that). No test here orders lots, so THE FIFO TRAP in
``test_backflush_lot_policy.py`` does not apply. Every name is invented.
"""

import asyncio
from datetime import date, timedelta
from io import BytesIO
from types import SimpleNamespace
from typing import Dict, List, Tuple

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.db.session as db_session_module
import app.jobs.mrp_jobs as mrp_jobs
import app.services.notification_dispatch as notification_dispatch
from app.models.audit_log import AuditLog
from app.models.bom import BOM, BOMItem
from app.models.mrp import MRPAction, MRPRequirement, MRPRun, MRPRunStatus, MRPSupplyLink, PlanningAction
from app.models.part import Part
from app.models.purchasing import PurchaseOrder, PurchaseOrderLine, Vendor
from app.models.routing import Routing, RoutingOperation
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder
from app.schemas.part import PartBase, PartCreate, PartResponse, PartUpdate
from app.services.mrp_auto_service import MRPAutoMode, MRPAutoService
from app.services.mrp_service import CUSTOMER_SUPPLIED_SUPPLY_DETAIL, MRPService, is_customer_supplied
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
from tests.services.test_mrp_service import _seed_make_wo_with_purchased_component

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_B = 2
REFUSAL = CUSTOMER_SUPPLIED_SUPPLY_DETAIL
WRITE_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]
# (url prefix, a part type that door resolves). Both doors write the same ``parts`` rows.
DOORS = [("/api/v1/parts", "manufactured"), ("/api/v1/materials", "raw_material")]

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def test_the_refusal_text_is_real():
    """Every exact-text assertion below compares against this constant, so it must not be
    something two empty values could agree on."""
    assert isinstance(REFUSAL, str) and "customer-supplied" in REFUSAL
    assert is_customer_supplied(SimpleNamespace(customer_supplied=True)) is True
    assert is_customer_supplied(SimpleNamespace(customer_supplied=False)) is False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _part(
    db: Session,
    *,
    part_type: str = "purchased",
    safety_stock: float = 0.0,
    lead_time_days: int = 0,
    customer_supplied: bool = False,
    company_id: int = COMPANY_A,
) -> Part:
    part = make_part(db, part_type=part_type, company_id=company_id)
    part.safety_stock = safety_stock
    part.lead_time_days = lead_time_days
    part.customer_supplied = customer_supplied
    db.commit()
    db.refresh(part)
    return part


def _line(
    db: Session,
    bom: BOM,
    component: Part,
    quantity: float,
    *,
    item_number: int,
    item_type: str = "buy",
    scrap_factor: float = 0.0,
) -> BOMItem:
    item = add_bom_item(db, bom, component, quantity=quantity, item_number=item_number)
    item.item_type = item_type
    item.scrap_factor = scrap_factor
    db.commit()
    db.refresh(item)
    return item


def _flag(db: Session, part: Part, value: bool) -> None:
    part.customer_supplied = value
    db.commit()
    db.refresh(part)


def _vendor(db: Session, *, company_id: int = COMPANY_A) -> Vendor:
    n = _next()
    vendor = Vendor(code=f"CSM-V-{n}", name=f"Invented Supply {n}", company_id=company_id, is_active=True)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return vendor


def _map(db: Session, vendor: Vendor, part: Part) -> None:
    """Source ``part`` from ``vendor``: an AUTO_DRAFT pass drafts only for a part with sourcing."""
    from app.models.supplier_part import SupplierPartMapping

    db.add(
        SupplierPartMapping(
            company_id=vendor.company_id,
            vendor_id=vendor.id,
            part_id=part.id,
            supplier_part_number=f"CSM-SP-{_next()}",
            is_active=True,
        )
    )
    db.commit()


def _run(db: Session, *, include_safety_stock: bool = True) -> MRPRun:
    run = MRPService(db, COMPANY_A).run_mrp(user_id=None, include_safety_stock=include_safety_stock)
    assert run.status == MRPRunStatus.COMPLETE, run.error_message
    db.expire_all()
    return run


def _requirements(db: Session, run: MRPRun) -> Dict[int, Tuple[float, float, float, float, float, float]]:
    """part_id -> (required, on hand, allocated, available, on order, shortage). Every
    fixture here has one due date per part, so one requirement row per part."""
    out = {}
    for row in db.query(MRPRequirement).filter(MRPRequirement.mrp_run_id == run.id).all():
        assert row.part_id not in out, "one due date per part in these fixtures"
        out[row.part_id] = (
            float(row.quantity_required),
            float(row.quantity_on_hand),
            float(row.quantity_allocated),
            float(row.quantity_available),
            float(row.quantity_on_order),
            float(row.quantity_shortage),
        )
    return out


def _actions(db: Session, run: MRPRun) -> Dict[int, Tuple[PlanningAction, float, int]]:
    out = {}
    for row in db.query(MRPAction).filter(MRPAction.mrp_run_id == run.id).all():
        assert row.part_id not in out, "one action per part in these fixtures"
        out[row.part_id] = (row.action_type, float(row.quantity), row.priority)
    return out


def _action_for(db: Session, run: MRPRun, part: Part) -> MRPAction:
    return db.query(MRPAction).filter(MRPAction.mrp_run_id == run.id, MRPAction.part_id == part.id).one()


def _po_line_part_ids(db: Session) -> List[int]:
    db.expire_all()
    return sorted(line.part_id for line in db.query(PurchaseOrderLine).all())


def _furnished_assembly(db: Session, *, demand: float = 5) -> SimpleNamespace:
    """One IN_PROGRESS work order (due in 30 days) for an assembly whose BOM holds:

    * ``furnished`` -- purchased, 2 per, safety stock 7, no stock, FLAGGED;
    * ``sibling``   -- purchased, 3 per, never flagged;
    * ``sub``       -- manufactured MAKE subassembly, 1 per, FLAGGED, whose own BOM holds
    * ``inner``     -- purchased, 4 per, never flagged.

    Unflagged, this would plan furnished 2*5+7, sibling 3*5, sub 1*5 and inner 4*5.
    """
    assembly = _part(db, part_type="manufactured")
    furnished = _part(db, safety_stock=7.0, customer_supplied=True)
    sibling = _part(db)
    sub = _part(db, part_type="manufactured", customer_supplied=True)
    inner = _part(db)
    bom = make_bom(db, assembly)
    _line(db, bom, furnished, 2.0, item_number=10)
    _line(db, bom, sibling, 3.0, item_number=20)
    _line(db, bom, sub, 1.0, item_number=30, item_type="make")
    _line(db, make_bom(db, sub), inner, 4.0, item_number=10)
    make_wo(db, assembly, quantity_ordered=demand)
    return SimpleNamespace(furnished=furnished, sibling=sibling, sub=sub, inner=inner)


# ===========================================================================
# 1. The MRP run
# ===========================================================================


def test_a_flagged_component_has_no_requirement_even_with_zero_stock_and_safety_stock(db_session: Session):
    assembly = _part(db_session, part_type="manufactured")
    furnished = _part(db_session, safety_stock=7.0, customer_supplied=True)
    sibling = _part(db_session, safety_stock=1.0)
    bom = make_bom(db_session, assembly)
    _line(db_session, bom, furnished, 2.0, item_number=10)
    _line(db_session, bom, sibling, 3.0, item_number=20)
    make_wo(db_session, assembly, quantity_ordered=5)

    run = _run(db_session)

    # The sibling, by hand: 3 per x 5 = 15 required, + 1 safety stock, nothing on hand.
    assert _requirements(db_session, run) == {sibling.id: (15.0, 0.0, 0.0, 0.0, 0.0, 16.0)}
    assert _actions(db_session, run) == {sibling.id: (PlanningAction.ORDER, 16.0, 5)}
    assert (run.total_parts_analyzed, run.total_requirements, run.total_actions) == (1, 1, 1)


def test_a_flagged_make_subassembly_suppresses_its_own_bom(db_session: Session):
    """The customer furnishes the subassembly, so the shop neither buys nor makes what is in it."""
    assembly = _part(db_session, part_type="manufactured")
    sub = _part(db_session, part_type="manufactured", customer_supplied=True)
    inner = _part(db_session)
    deeper_sub = _part(db_session, part_type="manufactured")
    deepest = _part(db_session)
    sibling = _part(db_session)
    bom = make_bom(db_session, assembly)
    _line(db_session, bom, sub, 1.0, item_number=10, item_type="make")
    _line(db_session, bom, sibling, 1.0, item_number=20)
    sub_bom = make_bom(db_session, sub)
    _line(db_session, sub_bom, inner, 4.0, item_number=10)
    _line(db_session, sub_bom, deeper_sub, 1.0, item_number=20, item_type="make")
    _line(db_session, make_bom(db_session, deeper_sub), deepest, 2.0, item_number=10)
    make_wo(db_session, assembly, quantity_ordered=5)

    run = _run(db_session)

    assert _requirements(db_session, run) == {sibling.id: (5.0, 0.0, 0.0, 0.0, 0.0, 5.0)}
    assert set(_actions(db_session, run)) == {sibling.id}


def test_a_flagged_part_deeper_in_the_tree_is_skipped_at_that_depth(db_session: Session):
    """The skip is per BOM line at every level, not only at the top."""
    assembly = _part(db_session, part_type="manufactured")
    sub = _part(db_session, part_type="manufactured")
    furnished = _part(db_session, customer_supplied=True)
    inner = _part(db_session)
    _line(db_session, make_bom(db_session, assembly), sub, 1.0, item_number=10, item_type="make")
    sub_bom = make_bom(db_session, sub)
    _line(db_session, sub_bom, furnished, 6.0, item_number=10)
    _line(db_session, sub_bom, inner, 2.0, item_number=20)
    make_wo(db_session, assembly, quantity_ordered=3)

    run = _run(db_session)

    assert _requirements(db_session, run) == {
        sub.id: (3.0, 0.0, 0.0, 0.0, 0.0, 3.0),
        inner.id: (6.0, 0.0, 0.0, 0.0, 0.0, 6.0),
    }
    assert _actions(db_session, run) == {
        sub.id: (PlanningAction.MANUFACTURE, 3.0, 5),
        inner.id: (PlanningAction.ORDER, 6.0, 5),
    }


def test_clearing_the_flag_restores_demand(db_session: Session):
    """The same fixture before and after: flagged, only the sibling plans; cleared, all four
    plan exactly what the fixture docstring computes. The second half is also the proof
    that the first half's silence comes from the flag and not from a broken fixture."""
    f = _furnished_assembly(db_session, demand=5)

    flagged = _run(db_session)
    assert _requirements(db_session, flagged) == {f.sibling.id: (15.0, 0.0, 0.0, 0.0, 0.0, 15.0)}
    assert _actions(db_session, flagged) == {f.sibling.id: (PlanningAction.ORDER, 15.0, 5)}

    _flag(db_session, f.furnished, False)
    _flag(db_session, f.sub, False)
    cleared = _run(db_session)

    assert _requirements(db_session, cleared) == {
        f.furnished.id: (10.0, 0.0, 0.0, 0.0, 0.0, 17.0),  # 2 x 5, + 7 safety stock
        f.sibling.id: (15.0, 0.0, 0.0, 0.0, 0.0, 15.0),
        f.sub.id: (5.0, 0.0, 0.0, 0.0, 0.0, 5.0),
        f.inner.id: (20.0, 0.0, 0.0, 0.0, 0.0, 20.0),  # 4 x the sub's 5
    }
    assert _actions(db_session, cleared) == {
        f.furnished.id: (PlanningAction.ORDER, 17.0, 5),
        f.sibling.id: (PlanningAction.ORDER, 15.0, 5),
        f.sub.id: (PlanningAction.MANUFACTURE, 5.0, 5),
        f.inner.id: (PlanningAction.ORDER, 20.0, 5),
    }


def test_the_flag_is_not_inferred_from_customer_owned_stock(db_session: Session):
    """Who owns a stock row is the row's own ``ownership``; a customer's lot on the rack does
    not make the PART customer-supplied, so it is still planned (and, owned stock covering
    no demand, for the whole quantity)."""
    assembly = _part(db_session, part_type="manufactured")
    component = _part(db_session)
    _line(db_session, make_bom(db_session, assembly), component, 1.0, item_number=10)
    make_wo(db_session, assembly, quantity_ordered=8)
    _own(db_session, make_lot(db_session, component, qty=50, lot=f"CSM-OWN-{_next()}"), "customer")

    run = _run(db_session)

    db_session.refresh(component)
    assert component.customer_supplied is False
    assert _actions(db_session, run) == {component.id: (PlanningAction.ORDER, 8.0, 5)}


def test_the_flag_set_through_the_write_door_drives_the_planner(client: TestClient, db_session: Session):
    """End to end: the audited PUT is what turns demand off, and back on."""
    user = make_user(db_session, role=UserRole.SUPERVISOR)
    assembly = _part(db_session, part_type="manufactured")
    component = _part(db_session, part_type="raw_material")
    _line(db_session, make_bom(db_session, assembly), component, 2.0, item_number=10)
    make_wo(db_session, assembly, quantity_ordered=4)

    on = client.put(
        f"/api/v1/materials/{component.id}",
        headers=headers_for(user),
        json={"version": 0, "customer_supplied": True},
    )
    assert on.status_code == status.HTTP_200_OK, on.text
    assert _requirements(db_session, _run(db_session)) == {}

    off = client.put(
        f"/api/v1/materials/{component.id}",
        headers=headers_for(user),
        json={"version": 0, "customer_supplied": False},
    )
    assert off.status_code == status.HTTP_200_OK, off.text
    run = _run(db_session)
    assert _requirements(db_session, run) == {component.id: (8.0, 0.0, 0.0, 0.0, 0.0, 8.0)}
    assert _actions(db_session, run) == {component.id: (PlanningAction.ORDER, 8.0, 5)}


# ===========================================================================
# 2. The auto-draft cron path
# ===========================================================================


def _record_dispatch(monkeypatch, module) -> List[dict]:
    calls: List[dict] = []

    async def _fake_dispatch_direct(db, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(module, "dispatch_direct", _fake_dispatch_direct)
    return calls


def _spy(monkeypatch, name: str, *, key) -> List:
    """Wrap ``MRPAutoService.<name>``, recording ``key(args)`` and delegating to the original."""
    calls: List = []
    original = getattr(MRPAutoService, name)

    def _wrapper(self, *args, **kwargs):
        calls.append(key(args))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(MRPAutoService, name, _wrapper)
    return calls


def test_an_auto_draft_pass_drafts_nothing_for_a_flagged_part(db_session: Session, monkeypatch):
    """An AUTO_DRAFT pass per company: a fresh run, then AUTO_DRAFT over its actions. The
    sibling is sourced from an active vendor and gets its draft PO, and the flagged component,
    the flagged subassembly and that subassembly's child get no PO line, no work order and no
    action."""
    f = _furnished_assembly(db_session, demand=5)
    vendor = _vendor(db_session)
    _map(db_session, vendor, f.sibling)
    planner_dispatches = _record_dispatch(monkeypatch, mrp_jobs)
    vendor_lookups = _spy(monkeypatch, "_get_preferred_vendor", key=lambda args: args[0])

    result = asyncio.run(
        mrp_jobs._run_mrp_for_company(
            db=db_session, company_id=COMPANY_A, mode=MRPAutoMode.AUTO_DRAFT, planning_horizon_days=90
        )
    )

    assert result["auto_processing"] == {
        "pos_created": 1,
        "manufacture_left_for_review": 0,
        "actions_processed": 1,
        "errors": 0,
    }
    assert vendor_lookups == [f.sibling.id]
    assert _po_line_part_ids(db_session) == [f.sibling.id]
    assert db_session.query(PurchaseOrder).one().vendor_id == vendor.id
    furnished_ids = [f.furnished.id, f.sub.id, f.inner.id]
    assert db_session.query(WorkOrder).filter(WorkOrder.part_id.in_(furnished_ids)).count() == 0
    assert db_session.query(MRPAction).filter(MRPAction.part_id.in_(furnished_ids)).count() == 0
    assert all(call["event_key"] != "mrp.expedite_required" for call in planner_dispatches)


def test_an_order_minted_before_the_flag_is_refused_before_a_vendor_is_chosen(db_session: Session, monkeypatch):
    assembly = _part(db_session, part_type="manufactured")
    furnished = _part(db_session)
    sibling = _part(db_session)
    bom = make_bom(db_session, assembly)
    _line(db_session, bom, furnished, 2.0, item_number=10)
    _line(db_session, bom, sibling, 1.0, item_number=20)
    make_wo(db_session, assembly, quantity_ordered=5)
    vendor = _vendor(db_session)
    _map(db_session, vendor, furnished)
    _map(db_session, vendor, sibling)
    run = _run(db_session)
    stale = _action_for(db_session, run, furnished)
    control = _action_for(db_session, run, sibling)
    assert stale.action_type == control.action_type == PlanningAction.ORDER

    _flag(db_session, furnished, True)  # flagged AFTER the action was minted
    vendor_lookups = _spy(monkeypatch, "_get_preferred_vendor", key=lambda args: args[0])
    po_drafts = _spy(monkeypatch, "_create_po_from_action", key=lambda args: args[0].id)

    results = MRPAutoService(db_session, COMPANY_A).process_actions([stale, control], MRPAutoMode.AUTO_DRAFT)

    assert results == {"pos_created": 1, "manufacture_left_for_review": 0, "actions_processed": 1, "errors": 1}
    assert vendor_lookups == [sibling.id], "no vendor may be chosen for a customer-supplied part"
    assert po_drafts == [control.id]
    db_session.expire_all()
    stale = db_session.get(MRPAction, stale.id)
    assert stale.error_message == REFUSAL
    assert stale.processed is False and stale.result_po_id is None
    assert db_session.get(MRPAction, control.id).result_po_id is not None
    assert _po_line_part_ids(db_session) == [sibling.id]


def test_a_manufacture_minted_before_the_flag_is_refused_and_no_work_order_is_drafted(db_session: Session, monkeypatch):
    assembly = _part(db_session, part_type="manufactured")
    furnished = _part(db_session, part_type="manufactured")
    sibling = _part(db_session, part_type="manufactured")
    bom = make_bom(db_session, assembly)
    _line(db_session, bom, furnished, 1.0, item_number=10, item_type="make")
    _line(db_session, bom, sibling, 1.0, item_number=20, item_type="make")
    make_wo(db_session, assembly, quantity_ordered=5)
    run = _run(db_session)
    stale = _action_for(db_session, run, furnished)
    control = _action_for(db_session, run, sibling)
    assert stale.action_type == control.action_type == PlanningAction.MANUFACTURE

    _flag(db_session, furnished, True)

    refused = MRPAutoService(db_session, COMPANY_A).process_actions([stale], MRPAutoMode.AUTO_DRAFT)

    assert refused == {"pos_created": 0, "manufacture_left_for_review": 0, "actions_processed": 0, "errors": 1}
    db_session.expire_all()
    stale = db_session.get(MRPAction, stale.id)
    assert stale.error_message == REFUSAL
    assert stale.processed is False and stale.result_wo_id is None
    assert db_session.query(WorkOrder).filter(WorkOrder.part_id == furnished.id).count() == 0

    # Positive control: the unflagged sibling's MANUFACTURE is not refused -- it is left for the
    # planner's review, untouched, and no automatic pass drafts a work order for it either.
    left = MRPAutoService(db_session, COMPANY_A).process_actions([control], MRPAutoMode.AUTO_DRAFT)
    assert left == {"pos_created": 0, "manufacture_left_for_review": 1, "actions_processed": 0, "errors": 0}
    db_session.expire_all()
    control = db_session.get(MRPAction, control.id)
    assert control.processed is False and control.result_wo_id is None and control.error_message is None
    assert db_session.query(WorkOrder).filter(WorkOrder.part_id == sibling.id).count() == 0


def test_an_expedite_minted_before_the_flag_sends_no_expedite(db_session: Session, monkeypatch):
    """A 60-day lead time against a work order due in 30 puts the order date in the past, so
    both components EXPEDITE. A Purchasing user exists, so the sibling's expedite is really
    dispatched -- through a borrowed session (the closure opens ``SessionLocal()``) to a
    recorder -- and the flagged component's is not."""
    assembly = _part(db_session, part_type="manufactured")
    furnished = _part(db_session, lead_time_days=60)
    sibling = _part(db_session, lead_time_days=60)
    bom = make_bom(db_session, assembly)
    _line(db_session, bom, furnished, 1.0, item_number=10)
    _line(db_session, bom, sibling, 1.0, item_number=20)
    make_wo(db_session, assembly, quantity_ordered=5)
    buyer = make_user(db_session, role=UserRole.MANAGER)
    buyer.department = "Purchasing"
    db_session.commit()
    run = _run(db_session)
    stale = _action_for(db_session, run, furnished)
    control = _action_for(db_session, run, sibling)
    assert stale.action_type == control.action_type == PlanningAction.EXPEDITE

    _flag(db_session, furnished, True)
    monkeypatch.setattr(
        db_session_module, "SessionLocal", lambda: SimpleNamespace(query=db_session.query, close=lambda: None)
    )
    dispatches = _record_dispatch(monkeypatch, notification_dispatch)
    expedites = _spy(monkeypatch, "_flag_for_expedite", key=lambda args: args[0].id)

    results = MRPAutoService(db_session, COMPANY_A).process_actions([stale, control], MRPAutoMode.AUTO_DRAFT)

    assert results == {"pos_created": 0, "manufacture_left_for_review": 0, "actions_processed": 1, "errors": 1}
    assert expedites == [control.id]
    assert [(call["event_key"], call["related_id"]) for call in dispatches] == [("mrp.expedite_required", sibling.id)]
    db_session.expire_all()
    stale = db_session.get(MRPAction, stale.id)
    assert stale.error_message == REFUSAL and stale.processed is False
    assert db_session.get(MRPAction, control.id).processed is True
    assert _po_line_part_ids(db_session) == []


# ===========================================================================
# 3. The reviewed supply draft and the purchase batch
# ===========================================================================


def _supply_shortage(db: Session, *, kind: str, work_center_id: int) -> Tuple[Part, Vendor, MRPAction]:
    """A shortage of 10 on one component (2 per x 5), from a real run. ``kind`` picks a
    purchased component (ORDER) or a manufactured one with a released routing (MANUFACTURE)."""
    part = _seed_make_wo_with_purchased_component(db, COMPANY_A, f"CSM{_next()}")
    vendor = _vendor(db)
    part.primary_supplier_id = vendor.id
    part.standard_cost = 6.0
    if kind == "work_order":
        part.part_type = "manufactured"
        route = Routing(company_id=COMPANY_A, part_id=part.id, revision="A", status="released", is_active=True)
        db.add(route)
        db.flush()
        db.add(
            RoutingOperation(
                company_id=COMPANY_A,
                routing_id=route.id,
                sequence=10,
                name="Invented supply step",
                work_center_id=work_center_id,
                setup_hours=1,
                run_hours_per_unit=0.5,
                is_active=True,
            )
        )
    db.commit()
    run = MRPService(db, COMPANY_A).run_mrp(None, include_safety_stock=False)
    action = db.query(MRPAction).filter_by(mrp_run_id=run.id, part_id=part.id).one()
    expected = PlanningAction.ORDER if kind == "purchase_order" else PlanningAction.MANUFACTURE
    assert action.action_type == expected and action.quantity == 10
    return part, vendor, action


def _review(client: TestClient, headers: dict, action: MRPAction) -> dict:
    response = client.get(f"/api/v1/mrp/actions/{action.id}/supply-review", headers=headers)
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()


def _draft_payload(review: dict, key: str) -> dict:
    # ``source_quantity``: a flagged review recomputes the shortage as 0, which the schema's
    # ``gt=0`` would refuse as a 422 before the flag is ever consulted.
    return dict(
        request_key=key,
        review_token=review["review_token"],
        quantity=review["source_quantity"],
        due_date=review["due_date"],
        vendor_id=review["vendor_id"],
        unit_price=review["unit_price"],
        work_center_id=review["work_center_id"],
        notes="Planner reviewed",
    )


def _supply_documents(db: Session, part: Part) -> Tuple[int, int, int]:
    db.rollback()
    db.expire_all()
    return (
        db.query(PurchaseOrderLine).filter(PurchaseOrderLine.part_id == part.id).count(),
        db.query(WorkOrder).filter(WorkOrder.part_id == part.id).count(),
        db.query(MRPSupplyLink).count(),
    )


@pytest.mark.parametrize("kind", ["purchase_order", "work_order"])
def test_the_supply_review_and_draft_refuse_a_flagged_part_and_create_nothing(
    client: TestClient, admin_headers: dict, db_session: Session, test_work_center, kind
):
    part, _vendor_row, action = _supply_shortage(db_session, kind=kind, work_center_id=test_work_center.id)
    before = _review(client, admin_headers, action)
    assert before["kind"] == kind and before["blocked_reason"] is None and before["quantity"] == 10

    _flag(db_session, part, True)  # after the action was minted
    review = _review(client, admin_headers, action)
    assert review["blocked_reason"] == REFUSAL

    endpoint = f"/api/v1/mrp/actions/{action.id}/supply-draft"
    refused = client.post(endpoint, headers=admin_headers, json=_draft_payload(review, "csm-flagged-draft"))
    assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
    assert refused.json()["detail"] == {"message": REFUSAL, "code": "MRP_REVIEW_STALE"}
    assert _supply_documents(db_session, part) == (0, 0, 0)
    action = db_session.get(MRPAction, action.id)
    assert action.result_po_id is None and action.result_wo_id is None

    # Positive control: the same action, reviewed again once the flag is cleared, drafts.
    _flag(db_session, part, False)
    cleared = _review(client, admin_headers, action)
    assert cleared["blocked_reason"] is None
    created = client.post(endpoint, headers=admin_headers, json=_draft_payload(cleared, "csm-cleared-draft"))
    assert created.status_code == status.HTTP_200_OK, created.text
    assert created.json()["kind"] == kind
    expected = (1, 0, 1) if kind == "purchase_order" else (0, 1, 1)
    assert _supply_documents(db_session, part) == expected


def test_the_purchase_batch_refuses_a_flagged_line_and_drafts_nothing(
    client: TestClient, admin_headers: dict, db_session: Session
):
    vendor = _vendor(db_session)
    flagged = _seed_make_wo_with_purchased_component(db_session, COMPANY_A, f"CSMB{_next()}")
    other = _seed_make_wo_with_purchased_component(db_session, COMPANY_A, f"CSMB{_next()}")
    for part in (flagged, other):
        part.primary_supplier_id = vendor.id
        part.standard_cost = 2.0
    db_session.commit()
    run = MRPService(db_session, COMPANY_A).run_mrp(None, include_safety_stock=False)
    actions = db_session.query(MRPAction).filter_by(mrp_run_id=run.id).order_by(MRPAction.id).all()
    assert {a.part_id for a in actions} == {flagged.id, other.id}

    _flag(db_session, flagged, True)
    review = client.get(
        "/api/v1/mrp/purchase-batch/review",
        headers=admin_headers,
        params=[("action_ids", a.id) for a in actions],
    )
    assert review.status_code == status.HTTP_200_OK, review.text
    lines = {row["part_id"]: row for row in review.json()["lines"]}
    assert lines[flagged.id]["blocked_reason"] == REFUSAL
    assert lines[other.id]["blocked_reason"] is None

    def _line_payload(row: dict) -> dict:
        return dict(
            action_id=row["action_id"],
            review_token=row["review_token"],
            quantity=row["source_quantity"],
            due_date=row["due_date"],
            vendor_id=row["vendor_id"],
            unit_price=row["unit_price"],
            notes="Reviewed",
        )

    request = {"request_key": "csm-batch-flagged", "lines": [_line_payload(row) for row in lines.values()]}
    refused = client.post("/api/v1/mrp/purchase-batch/drafts", headers=admin_headers, json=request)
    assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
    assert refused.json()["detail"] == {"message": REFUSAL, "code": "MRP_REVIEW_STALE"}
    db_session.rollback()
    assert db_session.query(PurchaseOrder).count() == 0
    assert db_session.query(MRPSupplyLink).count() == 0

    # Positive control: the unflagged line alone is drafted.
    alone = {"request_key": "csm-batch-other", "lines": [_line_payload(lines[other.id])]}
    created = client.post("/api/v1/mrp/purchase-batch/drafts", headers=admin_headers, json=alone)
    assert created.status_code == status.HTTP_200_OK, created.text
    assert _po_line_part_ids(db_session) == [other.id]


# ===========================================================================
# 4. The write door
# ===========================================================================


def _audit_changes(db: Session, part: Part) -> List[dict]:
    """The ``changes`` map of every ``part`` audit row for ``part``, oldest first. Both update
    doors log ``resource_type="part"`` (see ``update_material``)."""
    db.rollback()
    db.expire_all()
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.company_id == COMPANY_A, AuditLog.resource_type == "part", AuditLog.resource_id == part.id)
        .order_by(AuditLog.id)
        .all()
    )
    return [(row.extra_data or {}).get("changes", {}) for row in rows]


def _stored_flag(db: Session, part: Part) -> bool:
    db.rollback()
    db.expire_all()
    return db.get(Part, part.id).customer_supplied


def _listed(client: TestClient, user: User, prefix: str) -> Dict[int, dict]:
    response = client.get(f"{prefix}/?limit=500", headers=headers_for(user))
    assert response.status_code == status.HTTP_200_OK, response.text
    return {row["id"]: row for row in response.json()}


def test_the_flag_lives_on_update_and_response_schemas_only():
    """The posture of ``backflush_components``: ``PartCreate`` (and so both create routes and
    both importers, which splat it) cannot carry the flag."""
    assert "customer_supplied" in PartUpdate.model_fields
    assert "customer_supplied" in PartResponse.model_fields
    assert "customer_supplied" not in PartBase.model_fields
    assert "customer_supplied" not in PartCreate.model_fields


@pytest.mark.parametrize("role", WRITE_ROLES, ids=lambda r: r.value)
@pytest.mark.parametrize("prefix, part_type", DOORS)
def test_put_sets_and_clears_the_flag_and_every_read_reports_it(
    client: TestClient, db_session: Session, prefix, part_type, role
):
    user = make_user(db_session, role=role)
    part = _part(db_session, part_type=part_type)
    assert _listed(client, user, prefix)[part.id]["customer_supplied"] is False

    for value in (True, False):
        response = client.put(
            f"{prefix}/{part.id}", headers=headers_for(user), json={"version": 0, "customer_supplied": value}
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.json()["customer_supplied"] is value
        assert _stored_flag(db_session, part) is value
        detail = client.get(f"{prefix}/{part.id}", headers=headers_for(user))
        assert detail.status_code == status.HTTP_200_OK, detail.text
        assert detail.json()["customer_supplied"] is value
        listed = _listed(client, user, prefix)
        assert part.id in listed, "the part must not vanish from the hand-built list response"
        assert listed[part.id]["customer_supplied"] is value, "the list must not report a stale default"

    flips = [changes["customer_supplied"] for changes in _audit_changes(db_session, part)]
    assert flips == [{"old": False, "new": True}, {"old": True, "new": False}]


@pytest.mark.parametrize("start", [False, True])
@pytest.mark.parametrize("prefix, part_type", DOORS)
def test_explicit_null_is_422_and_leaves_the_row_unchanged(
    client: TestClient, db_session: Session, prefix, part_type, start
):
    user = make_user(db_session, role=UserRole.SUPERVISOR)
    part = _part(db_session, part_type=part_type, customer_supplied=start)
    name = part.name

    response = client.put(
        f"{prefix}/{part.id}",
        headers=headers_for(user),
        json={"version": 0, "name": "Must not land", "customer_supplied": None},
    )

    assert response.status_code == 422, response.text
    assert _stored_flag(db_session, part) is start
    assert db_session.get(Part, part.id).name == name, "the whole request is refused"
    assert _audit_changes(db_session, part) == []


@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.QUALITY, UserRole.VIEWER], ids=lambda r: r.value)
@pytest.mark.parametrize("prefix, part_type", DOORS)
def test_roles_outside_the_write_set_are_refused(client: TestClient, db_session: Session, prefix, part_type, role):
    user = make_user(db_session, role=role)
    part = _part(db_session, part_type=part_type)

    response = client.put(
        f"{prefix}/{part.id}", headers=headers_for(user), json={"version": 0, "customer_supplied": True}
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
    assert _stored_flag(db_session, part) is False
    assert _audit_changes(db_session, part) == []


@pytest.mark.parametrize("prefix, part_type", DOORS)
def test_create_cannot_set_the_flag(client: TestClient, db_session: Session, prefix, part_type):
    user = make_user(db_session)
    part_number = f"CSM-CREATE-{_next()}"

    response = client.post(
        f"{prefix}/",
        headers=headers_for(user),
        json={
            "part_number": part_number,
            "name": "Invented created part",
            "part_type": part_type,
            "unit_of_measure": "each",
            "customer_supplied": True,
        },
    )

    assert response.status_code == status.HTTP_201_CREATED, response.text
    assert response.json()["customer_supplied"] is False
    db_session.expire_all()
    assert db_session.query(Part).filter(Part.part_number == part_number).one().customer_supplied is False


@pytest.mark.parametrize("prefix, part_type", DOORS)
def test_csv_import_cannot_set_the_flag(client: TestClient, db_session: Session, prefix, part_type):
    user = make_user(db_session)
    part_number = f"CSM-IMP-{_next()}"
    text = f"part_number,name,part_type,customer_supplied\n{part_number},Invented imported part,{part_type},true\n"

    response = client.post(
        f"{prefix}/import-csv",
        headers=headers_for(user),
        files={"file": ("import.csv", BytesIO(text.encode("utf-8")), "text/csv")},
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["imported_count"] == 1, response.text
    db_session.expire_all()
    assert db_session.query(Part).filter(Part.part_number == part_number).one().customer_supplied is False


@pytest.mark.parametrize("prefix, part_type", DOORS)
def test_another_companys_part_is_404_and_untouched(client: TestClient, db_session: Session, prefix, part_type):
    user = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
    foreign = _part(db_session, part_type=part_type, company_id=COMPANY_B)

    response = client.put(
        f"{prefix}/{foreign.id}", headers=headers_for(user), json={"version": 0, "customer_supplied": True}
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert _stored_flag(db_session, foreign) is False
    assert client.get(f"{prefix}/{foreign.id}", headers=headers_for(user)).status_code == status.HTTP_404_NOT_FOUND
    assert foreign.id not in _listed(client, user, prefix)
    db_session.expire_all()
    assert db_session.query(AuditLog).filter(AuditLog.resource_id == foreign.id).count() == 0


# ===========================================================================
# 5. Nothing flagged: the plan is what it always was
# ===========================================================================


def test_with_no_flagged_part_the_plan_matches_the_hand_computed_one(db_session: Session):
    """A mixed BOM, every figure written out by hand. One IN_PROGRESS work order for 4, due in
    30 days; ``include_safety_stock`` on, as the cron runs it.

    * ``scrapped`` (purchased) 2 per, scrap 0.25: 2 x 4 x 1.25 = 10 required; 3 on hand;
      safety stock 1 -> shortage 10 + 1 - 3 = 8, ORDER 8.
    * ``covered`` (raw material) 1.5 per: 6 required; 10 on hand, 2 allocated -> 8 available;
      shortage max(0, 6 - 8) = 0, no action.
    * ``sub`` (manufactured, MAKE) 1 per: 4 required; 1 on hand -> MANUFACTURE 3.
    * ``slow`` (purchased, under ``sub``) 3 per of the sub's GROSS 4 = 12 required; lead time
      45 days against a due date 30 days out -> EXPEDITE 12, priority 1.
    """
    assembly = _part(db_session, part_type="manufactured")
    scrapped = _part(db_session, safety_stock=1.0)
    covered = _part(db_session, part_type="raw_material")
    sub = _part(db_session, part_type="manufactured")
    slow = _part(db_session, lead_time_days=45)
    bom = make_bom(db_session, assembly)
    _line(db_session, bom, scrapped, 2.0, item_number=10, scrap_factor=0.25)
    _line(db_session, bom, covered, 1.5, item_number=20)
    _line(db_session, bom, sub, 1.0, item_number=30, item_type="make")
    _line(db_session, make_bom(db_session, sub), slow, 3.0, item_number=10)
    make_wo(db_session, assembly, quantity_ordered=4)
    make_lot(db_session, scrapped, qty=3, lot=f"CSM-L-{_next()}")
    covered_lot = make_lot(db_session, covered, qty=10, lot=f"CSM-L-{_next()}")
    covered_lot.quantity_allocated = 2.0
    db_session.commit()
    make_lot(db_session, sub, qty=1, lot=f"CSM-L-{_next()}")

    run = _run(db_session)

    assert _requirements(db_session, run) == {
        scrapped.id: (10.0, 3.0, 0.0, 3.0, 0.0, 8.0),
        covered.id: (6.0, 10.0, 2.0, 8.0, 0.0, 0.0),
        sub.id: (4.0, 1.0, 0.0, 1.0, 0.0, 3.0),
        slow.id: (12.0, 0.0, 0.0, 0.0, 0.0, 12.0),
    }
    assert _actions(db_session, run) == {
        scrapped.id: (PlanningAction.ORDER, 8.0, 5),
        sub.id: (PlanningAction.MANUFACTURE, 3.0, 5),
        slow.id: (PlanningAction.EXPEDITE, 12.0, 1),
    }
    expedite = _action_for(db_session, run, slow)
    assert expedite.suggested_order_date == date.today()
    assert expedite.required_date == date.today() + timedelta(days=30)
    assert (run.total_parts_analyzed, run.total_requirements, run.total_actions) == (4, 4, 3)
