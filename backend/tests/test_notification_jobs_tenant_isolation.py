"""Behavior locks for the per-tenant notification digest jobs (fix/wo-remediation-followups, FIX 1).

The four daily-digest jobs in ``app/jobs/notification_jobs.py`` were leaking cross-tenant:
they queried entities globally and fanned out to every active user. The fix makes each job
iterate active companies via ``_active_company_ids(db)`` and, per company, (a) scope the
entity query by ``company_id`` and (b) pass ``company_id=cid`` to
``get_notification_recipients`` so a tenant's overdue work / low stock / due calibrations /
expiring quotes only ever notify that SAME tenant's users (invariant #1).

Two latent runtime bugs were fixed in passing and are guarded here:
- the calibration digest uses the ``Equipment`` model (a non-existent ``Calibration`` class
  with wrong fields was referenced before -> it crashed),
- the low-stock digest joins ``InventoryItem`` -> ``Part`` and compares
  ``quantity_on_hand <= Part.reorder_point`` (``InventoryItem.reorder_point`` does not exist).

Each test seeds COMPANY_A and COMPANY_B with a qualifying entity and a distinct user, runs the
job against the in-test session (monkeypatching the module-level ``SessionLocal``), and asserts:
  (a) the job runs to completion without raising (the calibration/low-stock fixes),
  (b) every ``send_notification`` recipient set is scoped to the SAME company as the entity
      that triggered it (no cross-tenant leak), and
  (c) one company's failure does not abort the others (best-effort per-company loop).

``NotificationService.send_notification`` is patched to a recorder so the test never touches
Redis (the real immediate-email path enqueues a job) and so we can inspect exactly which
recipients / related entity each call carried.
"""

import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

import app.jobs.notification_jobs as notification_jobs
from app.models.calibration import CalibrationStatus, Equipment
from app.models.company import Company
from app.models.inventory import InventoryItem
from app.models.part import Part
from app.models.quote import Quote, QuoteStatus
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus

pytestmark = [pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2

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


def _make_user(
    db: Session,
    *,
    company_id: int,
    role: UserRole = UserRole.MANAGER,
    department: str = None,
) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"notif-{n}@co{company_id}.test",
        employee_id=f"NOTIF-{n:05d}",
        first_name="Notif",
        last_name=f"Co{company_id}",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=role,
        department=department,
        is_active=True,
        is_superuser=False,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_part(db: Session, *, company_id: int, reorder_point: float = 100.0) -> Part:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"NOTIF-P-{n}",
        name=f"Part {n}",
        description="notification digest fixture part",
        part_type="manufactured",
        unit_of_measure="each",
        reorder_point=reorder_point,
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def _make_low_stock_item(db: Session, part: Part, *, company_id: int, on_hand: float = 1.0) -> InventoryItem:
    item = InventoryItem(
        part_id=part.id,
        location="MAIN",
        warehouse="MAIN",
        quantity_on_hand=on_hand,
        is_active=True,
        company_id=company_id,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _make_overdue_wo(db: Session, part: Part, *, company_id: int) -> WorkOrder:
    n = _next()
    wo = WorkOrder(
        work_order_number=f"NOTIF-WO-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=10,
        status=WorkOrderStatus.RELEASED,
        priority=5,
        due_date=datetime.utcnow().date() - timedelta(days=5),  # overdue
        company_id=company_id,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def _make_due_equipment(db: Session, *, company_id: int) -> Equipment:
    n = _next()
    eq = Equipment(
        equipment_id=f"NOTIF-EQ-{n}",
        name=f"Caliper {n}",
        next_calibration_date=datetime.utcnow().date() + timedelta(days=3),  # within 7-day window
        status=CalibrationStatus.ACTIVE,
        is_active=True,
        company_id=company_id,
    )
    db.add(eq)
    db.commit()
    db.refresh(eq)
    return eq


def _make_expiring_quote(db: Session, *, company_id: int) -> Quote:
    n = _next()
    quote = Quote(
        quote_number=f"NOTIF-Q-{n:05d}",
        customer_name="Acme",
        status=QuoteStatus.SENT,
        valid_until=datetime.utcnow().date() + timedelta(days=3),  # within 7-day window
        company_id=company_id,
    )
    db.add(quote)
    db.commit()
    db.refresh(quote)
    return quote


class _Recorder:
    """Captures every send_notification call so we can inspect recipient tenant scoping."""

    def __init__(self):
        self.calls = []

    async def record(self, *, users, related_type=None, related_id=None, event_type=None, **kwargs):
        # ``users`` is the recipient list the job built from get_notification_recipients.
        self.calls.append(
            {
                "users": list(users),
                "related_type": related_type,
                "related_id": related_id,
                "event_type": event_type,
            }
        )


def _install(monkeypatch, db_session, recorder):
    """Route the job at the in-test session and capture notifications instead of enqueuing."""
    monkeypatch.setattr(notification_jobs, "SessionLocal", lambda: db_session)

    async def fake_send(self, *args, **kwargs):
        return await recorder.record(**kwargs)

    monkeypatch.setattr(notification_jobs.NotificationService, "send_notification", fake_send, raising=True)


def _company_of_users(db: Session, user_ids) -> set:
    """The set of company_ids for the recipient users (recipients may be ids or User objects)."""
    ids = [u.id if isinstance(u, User) else u for u in user_ids]
    if not ids:
        return set()
    rows = db.query(User.company_id).filter(User.id.in_(ids)).all()
    return {c for (c,) in rows}


# ---------------------------------------------------------------------------
# Calibrations digest (latent bug fix: uses Equipment, not a non-existent Calibration)
# ---------------------------------------------------------------------------


def test_calibrations_digest_runs_and_is_tenant_scoped(db_session: Session, monkeypatch):
    a_user = _make_user(db_session, company_id=COMPANY_A, department="Quality")
    b_user = _make_user(db_session, company_id=COMPANY_B, department="Quality")
    eq_a = _make_due_equipment(db_session, company_id=COMPANY_A)
    eq_b = _make_due_equipment(db_session, company_id=COMPANY_B)
    db_session.commit()

    recorder = _Recorder()
    _install(monkeypatch, db_session, recorder)

    # (a) Runs to completion without raising -- the model/field fix means it no longer crashes.
    result = asyncio.run(notification_jobs.check_calibrations_task())
    assert result["calibrations_7day"] >= 2

    # Both equipments produced a notification.
    by_related = {(c["related_type"], c["related_id"]): c for c in recorder.calls}
    assert ("Equipment", eq_a.id) in by_related
    assert ("Equipment", eq_b.id) in by_related

    # (b) Each equipment's recipients belong ONLY to that equipment's company.
    assert _company_of_users(db_session, by_related[("Equipment", eq_a.id)]["users"]) == {COMPANY_A}
    assert _company_of_users(db_session, by_related[("Equipment", eq_b.id)]["users"]) == {COMPANY_B}
    assert b_user.id not in [u for u in by_related[("Equipment", eq_a.id)]["users"]]
    assert a_user.id not in [u for u in by_related[("Equipment", eq_b.id)]["users"]]


# ---------------------------------------------------------------------------
# Late work orders digest
# ---------------------------------------------------------------------------


def test_late_work_orders_digest_is_tenant_scoped(db_session: Session, monkeypatch):
    a_super = _make_user(db_session, company_id=COMPANY_A, role=UserRole.SUPERVISOR)
    a_mgr = _make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    b_super = _make_user(db_session, company_id=COMPANY_B, role=UserRole.SUPERVISOR)
    b_mgr = _make_user(db_session, company_id=COMPANY_B, role=UserRole.MANAGER)
    part_a = _make_part(db_session, company_id=COMPANY_A)
    part_b = _make_part(db_session, company_id=COMPANY_B)
    wo_a = _make_overdue_wo(db_session, part_a, company_id=COMPANY_A)
    wo_b = _make_overdue_wo(db_session, part_b, company_id=COMPANY_B)
    db_session.commit()

    recorder = _Recorder()
    _install(monkeypatch, db_session, recorder)

    result = asyncio.run(notification_jobs.check_late_work_orders_task())
    assert result["late_work_orders"] >= 2

    by_related = {(c["related_type"], c["related_id"]): c for c in recorder.calls}
    assert ("WorkOrder", wo_a.id) in by_related
    assert ("WorkOrder", wo_b.id) in by_related

    # Company A's overdue WO never targets a company-B user, and vice versa.
    a_recipients = by_related[("WorkOrder", wo_a.id)]["users"]
    b_recipients = by_related[("WorkOrder", wo_b.id)]["users"]
    assert _company_of_users(db_session, a_recipients) == {COMPANY_A}
    assert _company_of_users(db_session, b_recipients) == {COMPANY_B}
    a_ids = {u.id if isinstance(u, User) else u for u in a_recipients}
    b_ids = {u.id if isinstance(u, User) else u for u in b_recipients}
    assert b_super.id not in a_ids and b_mgr.id not in a_ids
    assert a_super.id not in b_ids and a_mgr.id not in b_ids


# ---------------------------------------------------------------------------
# Low-stock digest (latent bug fix: join Part, compare Part.reorder_point)
# ---------------------------------------------------------------------------


def test_low_stock_digest_runs_and_is_tenant_scoped(db_session: Session, monkeypatch):
    a_user = _make_user(db_session, company_id=COMPANY_A, department="Purchasing")
    b_user = _make_user(db_session, company_id=COMPANY_B, department="Purchasing")
    part_a = _make_part(db_session, company_id=COMPANY_A, reorder_point=100.0)
    part_b = _make_part(db_session, company_id=COMPANY_B, reorder_point=100.0)
    _make_low_stock_item(db_session, part_a, company_id=COMPANY_A, on_hand=1.0)
    _make_low_stock_item(db_session, part_b, company_id=COMPANY_B, on_hand=1.0)
    db_session.commit()

    recorder = _Recorder()
    _install(monkeypatch, db_session, recorder)

    # (a) Runs without raising -- the join/reorder_point fix means it no longer crashes.
    result = asyncio.run(notification_jobs.check_low_stock_task())
    assert result["low_stock_items"] >= 2

    # Exactly one low-stock notification per company (the digest sends one bundled call).
    by_company = {}
    for c in recorder.calls:
        companies = _company_of_users(db_session, c["users"])
        assert len(companies) == 1, "a low-stock notification mixed recipients across tenants"
        by_company.setdefault(next(iter(companies)), []).append(c)

    assert set(by_company) == {COMPANY_A, COMPANY_B}
    # No cross-tenant recipient ever appears.
    a_ids = {u.id if isinstance(u, User) else u for c in by_company[COMPANY_A] for u in c["users"]}
    b_ids = {u.id if isinstance(u, User) else u for c in by_company[COMPANY_B] for u in c["users"]}
    assert b_user.id not in a_ids
    assert a_user.id not in b_ids


def test_low_stock_excludes_items_above_part_reorder_point(db_session: Session, monkeypatch):
    """Guard the comparison target: an item with on-hand ABOVE the part's reorder point is
    NOT low stock (proves the threshold reads Part.reorder_point, not a phantom column)."""
    _make_user(db_session, company_id=COMPANY_A, department="Purchasing")
    part = _make_part(db_session, company_id=COMPANY_A, reorder_point=10.0)
    _make_low_stock_item(db_session, part, company_id=COMPANY_A, on_hand=500.0)  # well above reorder
    db_session.commit()

    recorder = _Recorder()
    _install(monkeypatch, db_session, recorder)

    result = asyncio.run(notification_jobs.check_low_stock_task())
    assert result["low_stock_items"] == 0
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Quote-expiry digest
# ---------------------------------------------------------------------------


def test_quote_expiry_digest_is_tenant_scoped(db_session: Session, monkeypatch):
    a_user = _make_user(db_session, company_id=COMPANY_A, department="Sales")
    b_user = _make_user(db_session, company_id=COMPANY_B, department="Sales")
    quote_a = _make_expiring_quote(db_session, company_id=COMPANY_A)
    quote_b = _make_expiring_quote(db_session, company_id=COMPANY_B)
    db_session.commit()

    recorder = _Recorder()
    _install(monkeypatch, db_session, recorder)

    result = asyncio.run(notification_jobs.check_quote_expiring_task())
    assert result["expiring_quotes"] >= 2

    by_related = {(c["related_type"], c["related_id"]): c for c in recorder.calls}
    assert ("Quote", quote_a.id) in by_related
    assert ("Quote", quote_b.id) in by_related
    assert _company_of_users(db_session, by_related[("Quote", quote_a.id)]["users"]) == {COMPANY_A}
    assert _company_of_users(db_session, by_related[("Quote", quote_b.id)]["users"]) == {COMPANY_B}
    a_ids = {u.id if isinstance(u, User) else u for u in by_related[("Quote", quote_a.id)]["users"]}
    b_ids = {u.id if isinstance(u, User) else u for u in by_related[("Quote", quote_b.id)]["users"]}
    assert b_user.id not in a_ids
    assert a_user.id not in b_ids


# ---------------------------------------------------------------------------
# (c) best-effort per-company loop: one tenant's failure doesn't abort the rest
# ---------------------------------------------------------------------------


def test_one_company_failure_does_not_abort_others(db_session: Session, monkeypatch):
    """Late-WO digest: make the FIRST company's send_notification blow up and assert the
    SECOND company still gets its notification (the per-company try/except is best-effort)."""
    _make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    _make_user(db_session, company_id=COMPANY_B, role=UserRole.MANAGER)
    part_a = _make_part(db_session, company_id=COMPANY_A)
    part_b = _make_part(db_session, company_id=COMPANY_B)
    wo_a = _make_overdue_wo(db_session, part_a, company_id=COMPANY_A)
    wo_b = _make_overdue_wo(db_session, part_b, company_id=COMPANY_B)
    db_session.commit()

    monkeypatch.setattr(notification_jobs, "SessionLocal", lambda: db_session)

    # Companies are iterated in id order (COMPANY_A first). Blow up only on company A's WO.
    seen = []

    async def flaky_send(self, *, related_id=None, **kwargs):
        if related_id == wo_a.id:
            raise RuntimeError("boom for company A")
        seen.append(related_id)

    monkeypatch.setattr(notification_jobs.NotificationService, "send_notification", flaky_send, raising=True)

    # The job swallows the per-company error and returns normally.
    result = asyncio.run(notification_jobs.check_late_work_orders_task())

    # Company B still got notified despite company A failing.
    assert wo_b.id in seen
    # The aggregate count still credits company B's processed WO (A's count is skipped on error).
    assert result["late_work_orders"] >= 1
