"""No scheduled job creates supply (plan mrp/6 step 1, 2026-11-23).

Standing ruling: nothing in this system executes supply without a named person's Accept. The
06:00 MRP cron used to run AUTO_DRAFT (draft purchase orders and work orders with nobody looking,
an AUTO_SUBMIT mode beside it that wrote them SENT / RELEASED, and a "first active vendor"
fallback for unsourced parts). It is now ``run_mrp_review_job``: a fresh run plus the
``mrp.review_needed`` notice, nothing else. These tests hold that line:

* **Runtime, the MRP cron.** Over a fixture that DOES produce an ORDER (a sourced part) and a
  MANUFACTURE (a make subassembly), running the cron coroutine constructs no ``PurchaseOrder``
  and no ``WorkOrder`` (an ORM ``init`` listener counts every construction anywhere), while it
  does write the MRP run and send ``mrp.review_needed``. The positive control runs the very same
  fixture through ``run_mrp_job(mode="AUTO_DRAFT")`` and sees a purchase order constructed -- so
  the listener and the fixture bite -- and still no work order (the creator is gone).
* **Static, every cron.** No cron coroutine in ``ALL_CRON_JOBS`` names the AUTO_DRAFT mode, and
  no module under ``app/jobs`` constructs a ``PurchaseOrder`` or a ``WorkOrder`` itself. A new
  cron that drafts supply fails here.
* **The removed modes stay removed.** ``MRPAutoMode`` knows REVIEW and AUTO_DRAFT only, and both
  ``run_mrp_task`` and ``MRPAutoService.process_actions`` refuse anything else (AUTO_SUBMIT).
"""

import ast
import asyncio
import inspect
import pathlib

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

import app.jobs.mrp_jobs as mrp_jobs
from app.models.mrp import MRPAction, MRPRun, PlanningAction
from app.models.purchasing import PurchaseOrder
from app.models.supplier_part import SupplierPartMapping
from app.models.user import UserRole
from app.models.work_order import WorkOrder
from app.services.mrp_auto_service import MRPAutoMode, MRPAutoService
from tests.api.test_backflush_lot_policy import COMPANY_A, make_bom, make_user, make_wo
from tests.api.test_customer_supplied_mrp import _line, _part, _vendor

pytestmark = [pytest.mark.requires_db]

JOBS_DIR = pathlib.Path(__file__).resolve().parents[1] / "app" / "jobs"


class _UnclosableSession:
    """The test session, handed to the job as if it were a fresh ``SessionLocal()``; the job's
    ``finally: db.close()`` must not end the test's session."""

    def __init__(self, db: Session):
        self._db = db

    def close(self) -> None:
        pass

    def __getattr__(self, name):
        return getattr(self._db, name)


@pytest.fixture
def constructions():
    """Count every ``PurchaseOrder`` / ``WorkOrder`` constructed while the test body runs."""
    seen = {"PurchaseOrder": 0, "WorkOrder": 0}

    def _po(target, args, kwargs):
        seen["PurchaseOrder"] += 1

    def _wo(target, args, kwargs):
        seen["WorkOrder"] += 1

    event.listen(PurchaseOrder, "init", _po)
    event.listen(WorkOrder, "init", _wo)
    try:
        yield seen
    finally:
        event.remove(PurchaseOrder, "init", _po)
        event.remove(WorkOrder, "init", _wo)


def _shortage_fixture(db: Session):
    """An IN_PROGRESS work order for an assembly needing a SOURCED purchased component (an ORDER
    an AUTO_DRAFT pass would draft) and a make subassembly (a MANUFACTURE)."""
    assembly = _part(db, part_type="manufactured")
    bought = _part(db)
    sub = _part(db, part_type="manufactured")
    bom = make_bom(db, assembly)
    _line(db, bom, bought, 2.0, item_number=10)
    _line(db, bom, sub, 1.0, item_number=20, item_type="make")
    make_wo(db, assembly, quantity_ordered=5)
    vendor = _vendor(db)
    db.add(
        SupplierPartMapping(
            company_id=COMPANY_A,
            vendor_id=vendor.id,
            part_id=bought.id,
            supplier_part_number=f"GUARD-SP-{bought.id}",
            is_active=True,
        )
    )
    make_user(db, role=UserRole.MANAGER)
    db.commit()
    return bought, sub


def _route_jobs_at(monkeypatch, db: Session) -> list:
    monkeypatch.setattr(mrp_jobs, "SessionLocal", lambda: _UnclosableSession(db))
    dispatched: list = []

    async def _fake_dispatch_direct(db_, *, event_key, company_id, recipients, **kwargs):
        dispatched.append(event_key)
        return 0

    monkeypatch.setattr(mrp_jobs, "dispatch_direct", _fake_dispatch_direct)
    return dispatched


def test_the_mrp_cron_writes_the_run_and_constructs_no_supply(db_session: Session, monkeypatch, constructions):
    import app.worker as worker

    bought, sub = _shortage_fixture(db_session)
    dispatched = _route_jobs_at(monkeypatch, db_session)
    constructions.update(PurchaseOrder=0, WorkOrder=0)  # the fixture's own work order does not count
    runs_before = db_session.query(MRPRun).count()
    pos_before = db_session.query(PurchaseOrder).count()
    wos_before = db_session.query(WorkOrder).count()

    result = asyncio.run(worker.run_mrp_review_job({"job_id": "guard"}))

    assert constructions == {"PurchaseOrder": 0, "WorkOrder": 0}
    db_session.expire_all()
    assert db_session.query(PurchaseOrder).count() == pos_before
    assert db_session.query(WorkOrder).count() == wos_before
    assert db_session.query(MRPRun).count() == runs_before + 1
    run = db_session.query(MRPRun).order_by(MRPRun.id.desc()).first()
    kinds = {a.part_id: a.action_type for a in db_session.query(MRPAction).filter(MRPAction.mrp_run_id == run.id)}
    # The fixture really does carry an ORDER and a MANUFACTURE, untouched for a planner to review.
    assert kinds[bought.id] == PlanningAction.ORDER and kinds[sub.id] == PlanningAction.MANUFACTURE
    assert not any(a.processed for a in db_session.query(MRPAction).filter(MRPAction.mrp_run_id == run.id))
    assert "mrp.review_needed" in dispatched
    assert "mrp.completed" not in dispatched
    assert all(r.get("mode") == "REVIEW" for r in result["runs"])


def test_the_review_notice_is_dispatched_even_with_no_active_manager(db_session: Session, monkeypatch):
    """An Admin-selected Email Recipients list is delivered inside ``dispatch_direct``; the cron must
    still call it when the automatic (Manager) audience is empty, as the preview promises."""
    import app.worker as worker
    from app.models.user import User

    _shortage_fixture(db_session)
    db_session.query(User).filter(User.role == UserRole.MANAGER).update(
        {User.is_active: False}, synchronize_session=False
    )
    db_session.commit()
    seen: list = []

    async def _fake_dispatch_direct(db_, *, event_key, company_id, recipients, **kwargs):
        seen.append((event_key, company_id, list(recipients)))
        return 0

    monkeypatch.setattr(mrp_jobs, "SessionLocal", lambda: _UnclosableSession(db_session))
    monkeypatch.setattr(mrp_jobs, "dispatch_direct", _fake_dispatch_direct)

    asyncio.run(worker.run_mrp_review_job({"job_id": "no-manager"}))

    assert ("mrp.review_needed", COMPANY_A, []) in seen


def test_positive_control_auto_draft_on_the_same_fixture_drafts_a_po_and_still_no_work_order(
    db_session: Session, monkeypatch, constructions
):
    import app.worker as worker

    bought, sub = _shortage_fixture(db_session)
    _route_jobs_at(monkeypatch, db_session)
    constructions.update(PurchaseOrder=0, WorkOrder=0)

    asyncio.run(worker.run_mrp_job({"job_id": "control"}, mode="AUTO_DRAFT", company_id=COMPANY_A))

    assert constructions["PurchaseOrder"] == 1, "the listener and the fixture must be able to see a PO"
    assert constructions["WorkOrder"] == 0, "no automatic pass drafts a work order"
    db_session.expire_all()
    action = db_session.query(MRPAction).filter(MRPAction.part_id == sub.id).order_by(MRPAction.id.desc()).first()
    assert action.processed is False and action.result_wo_id is None


def _cron_coroutines():
    from app.worker import ALL_CRON_JOBS

    return [job.coroutine for job in ALL_CRON_JOBS]


def test_no_cron_coroutine_names_an_auto_draft_mode():
    for coroutine in _cron_coroutines():
        source = inspect.getsource(coroutine)
        assert "AUTO_DRAFT" not in source, coroutine.__name__
        assert "AUTO_SUBMIT" not in source, coroutine.__name__


def test_no_job_module_constructs_a_purchase_order_or_a_work_order():
    offenders = []
    for path in sorted(JOBS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name in {"PurchaseOrder", "WorkOrder"}:
                    offenders.append(f"{path.name}:{node.lineno} {name}(...)")
    assert offenders == []


def test_the_removed_modes_are_refused():
    assert MRPAutoMode.ALL == frozenset({"REVIEW", "AUTO_DRAFT"})
    assert not hasattr(MRPAutoMode, "AUTO_SUBMIT")
    assert not hasattr(MRPAutoService, "_create_wo_from_action")
    assert not hasattr(MRPAutoService, "_generate_wo_number")
    with pytest.raises(ValueError, match="Unknown MRP mode"):
        asyncio.run(mrp_jobs.run_mrp_task("AUTO_SUBMIT", company_id=COMPANY_A))
    with pytest.raises(ValueError, match="Unknown MRP auto-processing mode"):
        MRPAutoService(None, COMPANY_A).process_actions([], "AUTO_SUBMIT")
