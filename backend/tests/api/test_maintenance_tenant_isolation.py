"""Every maintenance endpoint is scoped to the caller's company, and the four writes stamp it.

``api/endpoints/maintenance.py`` had two defects sharing one root cause -- ten of its
sixteen handlers never asked which company was calling.

**A. Three write paths were 500ing in production, two of them AFTER committing.**
``MaintenanceLog`` carries ``TenantMixin.company_id`` (NOT NULL; migration 026 drops the
interim ``server_default``), and ``start_work_order`` / ``complete_work_order`` /
``create_log`` all constructed one without it. The first two committed the work-order
state change in their own ``db.commit()`` and only then hit the failing insert, so an
operator saw a 500, reloaded, and found the job running / closed anyway -- a silent
partial success that left the PM event history permanently empty. Nothing in the suite
touched those three handlers, which is exactly why nobody noticed. §1 is therefore
written as a reproducer first and a regression pin second: it asserts the STORED log row,
because a 200 alone would not prove the scope was written.

**B. Ten handlers read or wrote other tenants' rows.** ``start`` and ``complete`` resolved
the work order by bare id, so guessing an integer was enough to start or close another
company's maintenance -- and ``complete`` then advanced whatever ``MaintenanceSchedule``
that work order pointed at. ``dashboard``, ``calendar``, ``history`` and ``overdue`` took
no company argument at all.

Three things shaped these tests
-------------------------------
* **The write cases carry the weight.** For every mutating handler the assertion is on
  the victim's STORED row after the refusal, not on the caller's status code: a handler
  that 404s and mutates anyway would pass a status-only test.

* **Two predicates are only reachable through a MIS-TENANTED row.** ``complete_work_order``
  resolves ``wo.schedule_id`` and writes ``last_completed_date`` / ``next_due_date`` onto
  it. ``create_work_order`` now refuses a foreign ``schedule_id``, but rows written before
  the fix may already carry one, so the read at the consuming end has to survive that
  state -- ``test_complete_does_not_advance_another_companys_schedule`` builds it
  deliberately (``company_id`` is part of no FK, on SQLite or on Postgres).

* **A refusal must be indistinguishable from an absent id.** Every cross-tenant case
  asserts 404, never 403, and ``test_..._exactly_like_an_absent_one`` pins that the
  refusal body matches a genuinely missing id -- otherwise the status code is itself an
  existence oracle over another tenant's equipment list.

Three more sections were added after review (§7-§9)
---------------------------------------------------
* **§7 Egress.** Scoping the WRITES closes ingress and does nothing about rows written
  BEFORE the guards. Such a row is owned by the caller and passes every ``company_id``
  filter, while the serializer's relationship carries no predicate of its own -- so it
  still rendered the FOREIGN machine's name. The serializers now null the relation.

* **§8 RBAC.** There was none: all sixteen handlers were bare ``get_current_user``, so a
  VIEWER could create, start and close maintenance work orders.

* **§9 Audit rows.** Invariant 2: the router wrote none at all, so who started or closed
  an AS9100D-auditable PM job was unrecoverable.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.company import Company
from app.models.maintenance import (
    MaintenanceFrequency,
    MaintenanceLog,
    MaintenancePriority,
    MaintenanceSchedule,
    MaintenanceStatus,
    MaintenanceType,
    MaintenanceWorkOrder,
)
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1  # the seeded company -- plays the victim throughout
COMPANY_B = 2  # the caller that must never reach A's data

TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"

# Sentinels distinctive enough that a substring search over a whole response body is a
# real leak check. A competitor's machine list, PM programme and failure history are
# exactly the disclosure that matters here.
FOREIGN_WC_NAME = "ACME-SECRET Haas VF-4SS Cell 7"
FOREIGN_WC_CODE = "ACME-SECRET-WC-7"
FOREIGN_SCHEDULE_NAME = "ACME-SECRET spindle rebuild programme"
FOREIGN_WO_TITLE = "ACME-SECRET emergency spindle failure"
FOREIGN_LOG_TEXT = "ACME-SECRET bearing race spalling found"

SCHEDULES_URL = "/api/v1/maintenance/schedules"
WORK_ORDERS_URL = "/api/v1/maintenance/work-orders"
LOG_URL = "/api/v1/maintenance/log"

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True)
        db.add(company)
        db.commit()
    return company


def make_user(db: Session, *, company_id: int, role: UserRole = UserRole.ADMIN) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"mnt-iso-{n}@co{company_id}.test",
        employee_id=f"MNTISO-{n:05d}",
        first_name="Tenant",
        last_name="Isolation",
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


def make_work_center(db: Session, *, company_id: int, name: str = None, code: str = None) -> WorkCenter:
    _ensure_company(db, company_id)
    n = _next()
    work_center = WorkCenter(
        code=code or f"WC-MNT-{n}",
        name=name or f"Isolation work center {n}",
        work_center_type="milling",
        capacity_hours_per_day=8.0,
        is_active=True,
        company_id=company_id,
    )
    db.add(work_center)
    db.commit()
    db.refresh(work_center)
    return work_center


def make_schedule(db: Session, *, company_id: int, work_center_id: int = None, **overrides) -> MaintenanceSchedule:
    if work_center_id is None:
        work_center_id = make_work_center(db, company_id=company_id).id
    fields = {
        "company_id": company_id,
        "work_center_id": work_center_id,
        "name": f"PM schedule {_next()}",
        "maintenance_type": MaintenanceType.PREVENTIVE,
        "frequency": MaintenanceFrequency.MONTHLY,
        "priority": MaintenancePriority.MEDIUM,
        "estimated_duration_hours": 2.0,
        "next_due_date": date(2026, 12, 1),
        "last_completed_date": date(2026, 1, 1),
        "is_active": True,
    }
    fields.update(overrides)
    schedule = MaintenanceSchedule(**fields)
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return schedule


def make_wo(db: Session, *, company_id: int, work_center_id: int = None, **overrides) -> MaintenanceWorkOrder:
    if work_center_id is None:
        work_center_id = make_work_center(db, company_id=company_id).id
    n = _next()
    fields = {
        "company_id": company_id,
        "work_center_id": work_center_id,
        "wo_number": f"MWO-ISO-{n:05d}",
        "maintenance_type": MaintenanceType.PREVENTIVE,
        "priority": MaintenancePriority.MEDIUM,
        "status": MaintenanceStatus.SCHEDULED,
        "title": f"Isolation maintenance {n}",
        "scheduled_date": date.today(),
        "due_date": date.today(),
        "requires_shutdown": False,
        "downtime_minutes": 0,
        "labor_cost": 0,
        "parts_cost": 0,
        "total_cost": 0,
    }
    fields.update(overrides)
    wo = MaintenanceWorkOrder(**fields)
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def make_log(db: Session, *, company_id: int, work_center_id: int, **overrides) -> MaintenanceLog:
    fields = {
        "company_id": company_id,
        "work_center_id": work_center_id,
        "event_type": "observation",
        "description": f"log {_next()}",
        "event_date": datetime(2026, 6, 1, 12, 0, 0),
        "cost": 0,
    }
    fields.update(overrides)
    log = MaintenanceLog(**fields)
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


def foreign_work_center(db: Session) -> WorkCenter:
    """Company A's machine -- the one Company B must never read, write or name."""
    return make_work_center(db, company_id=COMPANY_A, name=FOREIGN_WC_NAME, code=FOREIGN_WC_CODE)


def assert_discloses_nothing(response) -> None:
    """No sentinel may appear ANYWHERE in the body -- detail, echo or field."""
    body = response.text
    for sentinel in (FOREIGN_WC_NAME, FOREIGN_WC_CODE, FOREIGN_SCHEDULE_NAME, FOREIGN_WO_TITLE, FOREIGN_LOG_TEXT):
        assert sentinel not in body, f"leaked {sentinel!r}: {body}"


def fresh(db: Session, model, pk: int):
    """The row as COMMITTED, not as the shared session last remembered it."""
    db.expire_all()
    return db.get(model, pk)


def snapshot(db: Session, model, pk: int) -> dict:
    """Every mapped column of a row, for a byte-identical before/after comparison.

    ``updated_at`` carries an ``onupdate``, so any write at all -- even one that
    sets a column back to the value it already held -- shows up here.
    """
    row = fresh(db, model, pk)
    assert row is not None
    return {c.key: getattr(row, c.key) for c in sa_inspect(model).mapper.column_attrs}


# ===========================================================================
# 1. The four company_id-omitting INSERTs — the production 500s
# ===========================================================================


def test_start_work_order_succeeds_and_writes_its_log_row(client: TestClient, db_session: Session):
    """THE production-bug reproducer for ``start``.

    ``maintenance_logs.company_id`` is NOT NULL with no server default and the handler
    never set it, so this call raised IntegrityError for every operator who pressed
    Start -- *after* ``wo.status = IN_PROGRESS`` had been committed by its own
    ``db.commit()``. The joint assertion is the point: the old code could satisfy the
    status half and the log half separately but never both, so a started work order
    always lost its history entry.
    """
    user_b = make_user(db_session, company_id=COMPANY_B)
    wo = make_wo(db_session, company_id=COMPANY_B)

    response = client.post(f"{WORK_ORDERS_URL}/{wo.id}/start", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["status"] == "in_progress"

    stored = fresh(db_session, MaintenanceWorkOrder, wo.id)
    assert stored.status == MaintenanceStatus.IN_PROGRESS
    assert stored.started_at is not None

    logs = db_session.query(MaintenanceLog).filter(MaintenanceLog.maintenance_wo_id == wo.id).all()
    assert len(logs) == 1, "a started work order must not lose its history entry"
    assert logs[0].company_id == COMPANY_B, "the write must stamp the CALLER's company"
    assert logs[0].event_type == "started"
    assert logs[0].performed_by == user_b.id


def test_complete_work_order_succeeds_and_writes_its_log_row(client: TestClient, db_session: Session):
    """Second production-bug reproducer: same NOT NULL omission, same after-the-commit
    shape -- the completion, its costs and the schedule's new due date were all persisted
    and only then did the log insert blow up."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    wo = make_wo(db_session, company_id=COMPANY_B, status=MaintenanceStatus.IN_PROGRESS)

    response = client.post(
        f"{WORK_ORDERS_URL}/{wo.id}/complete",
        headers=headers_for(user_b),
        json={"labor_cost": 120.0, "parts_cost": 30.0, "downtime_minutes": 45.0, "findings": "bearing replaced"},
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["total_cost"] == 150.0

    stored = fresh(db_session, MaintenanceWorkOrder, wo.id)
    assert stored.status == MaintenanceStatus.COMPLETED
    assert stored.completed_by == user_b.id

    logs = db_session.query(MaintenanceLog).filter(MaintenanceLog.maintenance_wo_id == wo.id).all()
    assert len(logs) == 1
    assert logs[0].company_id == COMPANY_B
    assert logs[0].event_type == "completed"
    assert logs[0].cost == 150.0


def test_create_log_succeeds_and_stamps_the_callers_company_id(client: TestClient, db_session: Session):
    """Third reproducer. ``POST /maintenance/log`` has no UI caller, so this one 500'd
    on every call with nothing committed first -- but it is a public API surface and the
    stamp is what makes the row belong to anyone at all."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    wc_b = make_work_center(db_session, company_id=COMPANY_B)

    response = client.post(
        LOG_URL,
        headers=headers_for(user_b),
        json={
            "work_center_id": wc_b.id,
            "event_type": "inspection",
            "description": "monthly walkaround",
            "cost": 12.5,
        },
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()

    stored = fresh(db_session, MaintenanceLog, body["id"])
    assert stored.company_id == COMPANY_B
    assert stored.work_center_id == wc_b.id
    assert stored.performed_by == user_b.id


def test_no_maintenance_log_is_ever_written_without_a_company(client: TestClient, db_session: Session):
    """The sweep over all three log-writing paths at once.

    A ``maintenance_logs`` row with a NULL ``company_id`` belongs to no tenant and is
    invisible to every scoped read -- it is the shape the NOT NULL constraint exists to
    prevent, and the shape all three handlers were trying to write.
    """
    user_b = make_user(db_session, company_id=COMPANY_B)
    wc_b = make_work_center(db_session, company_id=COMPANY_B)
    wo = make_wo(db_session, company_id=COMPANY_B, work_center_id=wc_b.id)
    headers = headers_for(user_b)

    assert client.post(f"{WORK_ORDERS_URL}/{wo.id}/start", headers=headers).status_code == status.HTTP_200_OK
    assert (
        client.post(f"{WORK_ORDERS_URL}/{wo.id}/complete", headers=headers, json={"labor_cost": 1.0}).status_code
        == status.HTTP_200_OK
    )
    assert (
        client.post(
            LOG_URL,
            headers=headers,
            json={"work_center_id": wc_b.id, "event_type": "repair", "description": "manual entry"},
        ).status_code
        == status.HTTP_200_OK
    )

    db_session.expire_all()
    logs = db_session.query(MaintenanceLog).all()
    assert len(logs) == 3
    assert {log.company_id for log in logs} == {COMPANY_B}


# ===========================================================================
# 2. Cross-tenant MUTATION — the cases that matter most
# ===========================================================================


def test_start_refuses_another_companys_work_order(client: TestClient, db_session: Session):
    """Guessing an integer was enough to put another company's machine into maintenance.

    The assertion that matters is the victim's STORED row, not the status code: a handler
    that 404s and mutates anyway would pass a status-only test.
    """
    user_b = make_user(db_session, company_id=COMPANY_B)
    wc_a = foreign_work_center(db_session)
    victim = make_wo(db_session, company_id=COMPANY_A, work_center_id=wc_a.id, title=FOREIGN_WO_TITLE)
    before = snapshot(db_session, MaintenanceWorkOrder, victim.id)

    response = client.post(f"{WORK_ORDERS_URL}/{victim.id}/start", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert_discloses_nothing(response)
    assert snapshot(db_session, MaintenanceWorkOrder, victim.id) == before
    db_session.expire_all()
    assert db_session.query(MaintenanceLog).count() == 0, "a refused start must write no history entry"


def test_complete_refuses_another_companys_work_order(client: TestClient, db_session: Session):
    """Closing another tenant's maintenance is the sharper half: it stamps a completion
    time, a completing user, costs and downtime onto an AS9100D equipment record."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    wc_a = foreign_work_center(db_session)
    victim = make_wo(
        db_session,
        company_id=COMPANY_A,
        work_center_id=wc_a.id,
        title=FOREIGN_WO_TITLE,
        status=MaintenanceStatus.IN_PROGRESS,
    )
    before = snapshot(db_session, MaintenanceWorkOrder, victim.id)

    response = client.post(
        f"{WORK_ORDERS_URL}/{victim.id}/complete",
        headers=headers_for(user_b),
        json={"labor_cost": 9999.0, "parts_cost": 9999.0, "notes": "closed by another tenant"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert_discloses_nothing(response)
    assert snapshot(db_session, MaintenanceWorkOrder, victim.id) == before
    db_session.expire_all()
    assert db_session.query(MaintenanceLog).count() == 0


def test_complete_does_not_advance_another_companys_schedule(client: TestClient, db_session: Session):
    """The laundered cross-tenant write, and the reason scoping only the work-order read
    is a silent partial fix.

    ``complete_work_order`` writes ``last_completed_date`` and ``next_due_date`` onto
    whatever ``MaintenanceSchedule`` the work order's ``schedule_id`` names. Company B
    completing its OWN, legitimately-owned work order therefore rewrote Company A's PM
    programme -- moving a real machine's next service date, with nothing in A's audit
    trail to explain it. ``create_work_order`` now refuses a foreign ``schedule_id``, but
    rows written before the fix still carry one, so the read at the consuming end has to
    survive that state; it is constructed here directly (``company_id`` is part of no FK).
    """
    user_b = make_user(db_session, company_id=COMPANY_B)
    victim_schedule = make_schedule(
        db_session,
        company_id=COMPANY_A,
        name=FOREIGN_SCHEDULE_NAME,
        next_due_date=date(2026, 12, 25),
        last_completed_date=date(2026, 1, 15),
    )
    # B's OWN work order, pointing at A's schedule -- the pre-fix state.
    wo_b = make_wo(
        db_session,
        company_id=COMPANY_B,
        status=MaintenanceStatus.IN_PROGRESS,
        schedule_id=victim_schedule.id,
    )
    before = snapshot(db_session, MaintenanceSchedule, victim_schedule.id)

    response = client.post(
        f"{WORK_ORDERS_URL}/{wo_b.id}/complete", headers=headers_for(user_b), json={"labor_cost": 10.0}
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert (
        snapshot(db_session, MaintenanceSchedule, victim_schedule.id) == before
    ), "Company B's completion advanced Company A's PM schedule"


def test_complete_still_advances_the_callers_own_schedule(client: TestClient, db_session: Session):
    """Positive control for the scoped schedule read: narrowing WHICH schedule may be
    advanced must not stop a work order advancing its own."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    own_schedule = make_schedule(
        db_session, company_id=COMPANY_B, frequency=MaintenanceFrequency.MONTHLY, next_due_date=date(2026, 2, 1)
    )
    wo_b = make_wo(db_session, company_id=COMPANY_B, status=MaintenanceStatus.IN_PROGRESS, schedule_id=own_schedule.id)

    response = client.post(
        f"{WORK_ORDERS_URL}/{wo_b.id}/complete", headers=headers_for(user_b), json={"labor_cost": 10.0}
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    # The handler stamps ``datetime.utcnow().date()``; the shop is Central, so a local
    # ``date.today()`` here is off by one for part of every day.
    completion_day = datetime.utcnow().date()
    stored = fresh(db_session, MaintenanceSchedule, own_schedule.id)
    assert stored.last_completed_date == completion_day
    assert stored.next_due_date == completion_day + timedelta(days=30)


# ===========================================================================
# 3. Cross-tenant FKs on the create paths
# ===========================================================================


def test_create_schedule_refuses_a_foreign_work_center(client: TestClient, db_session: Session):
    """A PM schedule opened against another tenant's machine. The response renders
    ``work_center_name`` straight back, so the write doubled as a read of the foreign
    machine's name."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    wc_a = foreign_work_center(db_session)

    response = client.post(
        SCHEDULES_URL,
        headers=headers_for(user_b),
        json={"work_center_id": wc_a.id, "name": "B monthly PM", "frequency": "monthly"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert_discloses_nothing(response)
    db_session.expire_all()
    assert db_session.query(MaintenanceSchedule).count() == 0, "a refused create must leave no row behind"


def test_create_work_order_refuses_a_foreign_work_center(client: TestClient, db_session: Session):
    user_b = make_user(db_session, company_id=COMPANY_B)
    wc_a = foreign_work_center(db_session)

    response = client.post(
        WORK_ORDERS_URL,
        headers=headers_for(user_b),
        json={"work_center_id": wc_a.id, "title": "B corrective"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert_discloses_nothing(response)
    db_session.expire_all()
    assert db_session.query(MaintenanceWorkOrder).count() == 0


def test_create_work_order_refuses_a_foreign_schedule(client: TestClient, db_session: Session):
    """``schedule_id`` was accepted verbatim, which is how a legitimately-owned work
    order became the vehicle for the cross-tenant schedule write above. Refused at the
    source as well as at the consuming end."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    own_wc = make_work_center(db_session, company_id=COMPANY_B)
    foreign_schedule = make_schedule(db_session, company_id=COMPANY_A, name=FOREIGN_SCHEDULE_NAME)

    response = client.post(
        WORK_ORDERS_URL,
        headers=headers_for(user_b),
        json={"work_center_id": own_wc.id, "title": "B PM run", "schedule_id": foreign_schedule.id},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert_discloses_nothing(response)
    db_session.expire_all()
    assert db_session.query(MaintenanceWorkOrder).count() == 0


def test_create_work_order_still_accepts_the_callers_own_fks(client: TestClient, db_session: Session):
    """Positive control: the validation narrows WHICH ids are acceptable, it does not
    break the create path."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    own_wc = make_work_center(db_session, company_id=COMPANY_B)
    own_schedule = make_schedule(db_session, company_id=COMPANY_B, work_center_id=own_wc.id)
    foreign_work_center(db_session)  # present and irrelevant

    response = client.post(
        WORK_ORDERS_URL,
        headers=headers_for(user_b),
        json={"work_center_id": own_wc.id, "title": "B PM run", "schedule_id": own_schedule.id},
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["schedule_id"] == own_schedule.id
    assert fresh(db_session, MaintenanceWorkOrder, body["id"]).company_id == COMPANY_B


def test_create_log_refuses_a_foreign_work_center(client: TestClient, db_session: Session):
    user_b = make_user(db_session, company_id=COMPANY_B)
    wc_a = foreign_work_center(db_session)

    response = client.post(
        LOG_URL,
        headers=headers_for(user_b),
        json={"work_center_id": wc_a.id, "event_type": "repair", "description": FOREIGN_LOG_TEXT},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    db_session.expire_all()
    assert db_session.query(MaintenanceLog).count() == 0


def test_create_log_refuses_a_foreign_maintenance_work_order(client: TestClient, db_session: Session):
    """``maintenance_wo_id`` was unvalidated too, so a log entry owned by B could hang
    off A's work order -- a false traceability pointer on an equipment record, and one
    that surfaces in nothing B is allowed to read."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    own_wc = make_work_center(db_session, company_id=COMPANY_B)
    wc_a = foreign_work_center(db_session)
    foreign_wo = make_wo(db_session, company_id=COMPANY_A, work_center_id=wc_a.id, title=FOREIGN_WO_TITLE)

    response = client.post(
        LOG_URL,
        headers=headers_for(user_b),
        json={
            "work_center_id": own_wc.id,
            "maintenance_wo_id": foreign_wo.id,
            "event_type": "repair",
            "description": "note",
        },
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    db_session.expire_all()
    assert db_session.query(MaintenanceLog).count() == 0


# ===========================================================================
# 4. GET /work-orders/overdue — a poll is not an actor
# ===========================================================================


def test_overdue_does_not_read_or_rewrite_another_tenants_rows(client: TestClient, db_session: Session):
    """The worst-shaped defect in the file: an unscoped SELECT followed by a committed
    SCHEDULED -> OVERDUE write across EVERY tenant, fired by a GET.

    Any authenticated user of any company, merely by loading the maintenance page,
    rewrote every other company's overdue work orders -- with no actor intent behind it
    and no audit row saying who or why. The victim row is compared column-by-column
    (``updated_at`` carries an ``onupdate``, so a write of any kind shows up).
    """
    user_a = make_user(db_session, company_id=COMPANY_A)
    wc_b = make_work_center(db_session, company_id=COMPANY_B)
    victim = make_wo(
        db_session,
        company_id=COMPANY_B,
        work_center_id=wc_b.id,
        status=MaintenanceStatus.SCHEDULED,
        scheduled_date=date.today() - timedelta(days=10),
        due_date=date.today() - timedelta(days=5),
    )
    before = snapshot(db_session, MaintenanceWorkOrder, victim.id)

    response = client.get(f"{WORK_ORDERS_URL}/overdue", headers=headers_for(user_a))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json() == [], "company A's poll returned company B's work orders"
    assert (
        snapshot(db_session, MaintenanceWorkOrder, victim.id) == before
    ), "company A's GET rewrote company B's work order"


def test_overdue_derives_the_label_without_persisting_it(client: TestClient, db_session: Session):
    """The caller's own rows keep their payload byte-for-byte -- the tile still reads
    OVERDUE -- but the transition is no longer committed from a GET. The persisted
    transition still happens on ``GET /maintenance/work-orders``, which is the call the
    page actually makes on load and is tenant-scoped."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    own = make_wo(
        db_session,
        company_id=COMPANY_B,
        status=MaintenanceStatus.SCHEDULED,
        scheduled_date=date.today() - timedelta(days=10),
        due_date=date.today() - timedelta(days=5),
    )

    response = client.get(f"{WORK_ORDERS_URL}/overdue", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert [row["id"] for row in body] == [own.id]
    assert [row["status"] for row in body] == ["overdue"], "the derived label must survive"

    assert (
        fresh(db_session, MaintenanceWorkOrder, own.id).status == MaintenanceStatus.SCHEDULED
    ), "a GET must not persist a status change"


def test_list_work_orders_is_a_pure_read_too(client: TestClient, db_session: Session):
    """The list endpoint used to be the "compensating path" that persisted the
    SCHEDULED -> OVERDUE transition ``/overdue`` stopped writing. It is now a pure read on
    the same grounds: a GET committing a status change has no actor behind it, records no
    reason, and wrote no ``AuditService`` row (invariant 2).

    The derived label still appears in the payload, so the response is unchanged."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    own = make_wo(
        db_session,
        company_id=COMPANY_B,
        status=MaintenanceStatus.SCHEDULED,
        scheduled_date=date.today() - timedelta(days=10),
        due_date=date.today() - timedelta(days=5),
    )

    response = client.get(WORK_ORDERS_URL, headers=headers_for(user_b))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert [row["status"] for row in response.json()] == ["overdue"], "the derived label must survive"
    assert (
        fresh(db_session, MaintenanceWorkOrder, own.id).status == MaintenanceStatus.SCHEDULED
    ), "a GET must not persist a status change"


def test_overdue_evidence_no_longer_depends_on_a_page_load(db_session: Session):
    """Now that no GET persists OVERDUE, the AS9100D evidence card has to derive it.

    ``auto_evidence_service._query_maintenance`` counted the STORED flag, so its health
    verdict silently depended on whether a human had loaded the Maintenance page. It now
    uses the same ``due_date`` predicate ``GET /maintenance/dashboard`` does.
    """
    from app.services.auto_evidence_service import _query_maintenance

    make_wo(
        db_session,
        company_id=COMPANY_B,
        status=MaintenanceStatus.SCHEDULED,
        scheduled_date=date.today() - timedelta(days=10),
        due_date=date.today() - timedelta(days=5),
    )

    evidence = _query_maintenance(db_session, COMPANY_B)

    assert evidence["health_status"] == "warning"
    assert "1 maintenance tasks overdue" in evidence["health_detail"]


# ===========================================================================
# 5. The aggregates — platform-wide until now
# ===========================================================================


def test_dashboard_counts_only_the_callers_own_work_orders(client: TestClient, db_session: Session):
    """Every count, the cost total and the per-machine MTBF/MTTR block spanned all
    tenants: a competitor's maintenance spend and failure rate, on your own dashboard."""
    user_a = make_user(db_session, company_id=COMPANY_A)
    user_b = make_user(db_session, company_id=COMPANY_B)
    wc_a = foreign_work_center(db_session)
    make_wo(
        db_session,
        company_id=COMPANY_A,
        work_center_id=wc_a.id,
        title=FOREIGN_WO_TITLE,
        status=MaintenanceStatus.COMPLETED,
        maintenance_type=MaintenanceType.EMERGENCY,
        completed_at=datetime.utcnow(),
        scheduled_date=date.today(),
        actual_duration_hours=6.0,
        downtime_minutes=360.0,
        total_cost=48000.0,
    )
    make_wo(
        db_session,
        company_id=COMPANY_A,
        work_center_id=wc_a.id,
        status=MaintenanceStatus.IN_PROGRESS,
        title=FOREIGN_WO_TITLE,
    )

    # Overdue and due-this-week are asserted alongside the rest: a mutation run found
    # that deleting the company_id predicate from either left the suite fully green,
    # because this test claimed "every count" and checked three of six. The sixth
    # (``total_this_month``, surfaced as ``completion_rate``) has its own test below --
    # it needs a completed row on BOTH sides to be discriminating.
    make_wo(
        db_session,
        company_id=COMPANY_A,
        work_center_id=wc_a.id,
        title=FOREIGN_WO_TITLE,
        status=MaintenanceStatus.SCHEDULED,
        scheduled_date=date.today() - timedelta(days=3),
        due_date=date.today() - timedelta(days=2),
    )
    make_wo(
        db_session,
        company_id=COMPANY_A,
        work_center_id=wc_a.id,
        title=FOREIGN_WO_TITLE,
        status=MaintenanceStatus.SCHEDULED,
        scheduled_date=date.today(),
        due_date=date.today() + timedelta(days=2),
    )

    seen_by_b = client.get("/api/v1/maintenance/dashboard", headers=headers_for(user_b))
    assert seen_by_b.status_code == status.HTTP_200_OK, seen_by_b.text
    body_b = seen_by_b.json()
    assert body_b["completed_this_month"] == 0
    assert body_b["in_progress"] == 0
    assert body_b["total_cost_month"] == 0
    assert body_b["overdue_count"] == 0
    assert body_b["due_this_week"] == 0
    assert body_b["work_center_metrics"] == []
    assert_discloses_nothing(seen_by_b)

    # The positive control in the other direction: A still sees its OWN, which is the
    # whole point of the endpoint and is what a refuse-everything fix would have broken.
    seen_by_a = client.get("/api/v1/maintenance/dashboard", headers=headers_for(user_a))
    assert seen_by_a.status_code == status.HTTP_200_OK, seen_by_a.text
    body_a = seen_by_a.json()
    assert body_a["completed_this_month"] == 1
    assert body_a["in_progress"] == 1
    assert body_a["total_cost_month"] == 48000.0
    assert body_a["overdue_count"] == 1
    assert body_a["due_this_week"] == 1
    assert [m["work_center_id"] for m in body_a["work_center_metrics"]] == [wc_a.id]


def test_dashboard_work_center_metrics_never_name_another_companys_machine(client: TestClient, db_session: Session):
    """The work-center list itself was unscoped, so a foreign machine could appear as a
    row on your dashboard by NAME even before any work order joined to it."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    wc_a = foreign_work_center(db_session)
    make_wo(
        db_session,
        company_id=COMPANY_A,
        work_center_id=wc_a.id,
        title=FOREIGN_WO_TITLE,
        status=MaintenanceStatus.COMPLETED,
        completed_at=datetime.utcnow(),
        actual_duration_hours=3.0,
        total_cost=1234.0,
    )

    response = client.get("/api/v1/maintenance/dashboard", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert_discloses_nothing(response)
    assert "1234" not in response.text


def test_calendar_returns_only_the_callers_own_work_orders(client: TestClient, db_session: Session):
    user_b = make_user(db_session, company_id=COMPANY_B)
    wc_a = foreign_work_center(db_session)
    make_wo(db_session, company_id=COMPANY_A, work_center_id=wc_a.id, title=FOREIGN_WO_TITLE)
    own = make_wo(db_session, company_id=COMPANY_B)

    response = client.get(
        "/api/v1/maintenance/calendar",
        headers=headers_for(user_b),
        params={
            "start_date": (date.today() - timedelta(days=30)).isoformat(),
            "end_date": (date.today() + timedelta(days=30)).isoformat(),
        },
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert [row["id"] for row in response.json()] == [own.id]
    assert_discloses_nothing(response)


def test_calendar_refuses_an_unbounded_window(client: TestClient, db_session: Session):
    """``start_date``/``end_date`` are caller-supplied and there was no ``limit``, so one
    request could serialize every maintenance work order a tenant has ever had. Same
    posture as the list/export bounds in #193."""
    user_b = make_user(db_session, company_id=COMPANY_B)

    too_wide = client.get(
        "/api/v1/maintenance/calendar",
        headers=headers_for(user_b),
        params={"start_date": "2000-01-01", "end_date": "2100-01-01"},
    )
    assert too_wide.status_code == status.HTTP_400_BAD_REQUEST, too_wide.text

    backwards = client.get(
        "/api/v1/maintenance/calendar",
        headers=headers_for(user_b),
        params={"start_date": "2026-06-01", "end_date": "2026-05-01"},
    )
    assert backwards.status_code == status.HTTP_400_BAD_REQUEST, backwards.text

    # The boundary itself is allowed, so a full-year calendar still works.
    at_the_cap = client.get(
        "/api/v1/maintenance/calendar",
        headers=headers_for(user_b),
        params={
            "start_date": (date.today() - timedelta(days=366)).isoformat(),
            "end_date": date.today().isoformat(),
        },
    )
    assert at_the_cap.status_code == status.HTTP_200_OK, at_the_cap.text


def test_history_refuses_another_companys_work_center(client: TestClient, db_session: Session):
    """A machine's full failure history, readable by guessing an integer."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    wc_a = foreign_work_center(db_session)
    make_log(db_session, company_id=COMPANY_A, work_center_id=wc_a.id, description=FOREIGN_LOG_TEXT)

    response = client.get(f"/api/v1/maintenance/history/{wc_a.id}", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert_discloses_nothing(response)


def test_history_returns_the_callers_own(client: TestClient, db_session: Session):
    """Positive control, and the second half of the history fix: even for an OWNED work
    center the log read is scoped, so a mis-tenanted log row on your own machine (the
    shape the pre-fix ``create_log`` could not write but a foreign one could) stays out."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    own_wc = make_work_center(db_session, company_id=COMPANY_B)
    make_log(db_session, company_id=COMPANY_B, work_center_id=own_wc.id, description="B own event")
    make_log(db_session, company_id=COMPANY_A, work_center_id=own_wc.id, description=FOREIGN_LOG_TEXT)

    response = client.get(f"/api/v1/maintenance/history/{own_wc.id}", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert [log["description"] for log in body["logs"]] == ["B own event"]
    assert_discloses_nothing(response)


# ===========================================================================
# 6. Sweep + the refusal is not an oracle
# ===========================================================================


def test_every_id_keyed_endpoint_refuses_a_foreign_id(client: TestClient, db_session: Session):
    """No per-endpoint exception survives, and none of them leaks a name.

    ``/schedules/{id}``, ``/work-orders/{id}`` and ``PUT``/``DELETE`` on schedules were
    already scoped before this change; they are swept here so a future edit cannot quietly
    unscope one of them while the rest stay covered.
    """
    user_b = make_user(db_session, company_id=COMPANY_B)
    wc_a = foreign_work_center(db_session)
    schedule_a = make_schedule(db_session, company_id=COMPANY_A, work_center_id=wc_a.id, name=FOREIGN_SCHEDULE_NAME)
    wo_a = make_wo(db_session, company_id=COMPANY_A, work_center_id=wc_a.id, title=FOREIGN_WO_TITLE)
    make_log(db_session, company_id=COMPANY_A, work_center_id=wc_a.id, description=FOREIGN_LOG_TEXT)
    headers = headers_for(user_b)

    gets = [
        f"{SCHEDULES_URL}/{schedule_a.id}",
        f"{WORK_ORDERS_URL}/{wo_a.id}",
        f"/api/v1/maintenance/history/{wc_a.id}",
    ]
    for url in gets:
        response = client.get(url, headers=headers)
        assert response.status_code == status.HTTP_404_NOT_FOUND, f"{url} -> {response.status_code}: {response.text}"
        assert_discloses_nothing(response)

    puts = [
        (f"{SCHEDULES_URL}/{schedule_a.id}", {"name": "owned by B now"}),
        (f"{WORK_ORDERS_URL}/{wo_a.id}", {"title": "owned by B now"}),
    ]
    for url, payload in puts:
        response = client.put(url, headers=headers, json=payload)
        assert response.status_code == status.HTTP_404_NOT_FOUND, f"{url} -> {response.status_code}: {response.text}"
        assert_discloses_nothing(response)

    posts = [
        (f"{WORK_ORDERS_URL}/{wo_a.id}/start", None),
        (f"{WORK_ORDERS_URL}/{wo_a.id}/complete", {"labor_cost": 1.0}),
    ]
    for url, payload in posts:
        response = client.post(url, headers=headers, json=payload)
        assert response.status_code == status.HTTP_404_NOT_FOUND, f"{url} -> {response.status_code}: {response.text}"
        assert_discloses_nothing(response)

    response = client.delete(f"{SCHEDULES_URL}/{schedule_a.id}", headers=headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text

    # Nothing above may have landed on A's rows.
    assert fresh(db_session, MaintenanceSchedule, schedule_a.id).name == FOREIGN_SCHEDULE_NAME
    assert fresh(db_session, MaintenanceSchedule, schedule_a.id).is_active is True
    stored_wo = fresh(db_session, MaintenanceWorkOrder, wo_a.id)
    assert stored_wo.title == FOREIGN_WO_TITLE
    assert stored_wo.status == MaintenanceStatus.SCHEDULED


def test_a_foreign_work_order_refuses_exactly_like_an_absent_one(client: TestClient, db_session: Session):
    """If the two answers differ at all, the status code is itself an existence oracle
    over another tenant's equipment records."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    wc_a = foreign_work_center(db_session)
    wo_a = make_wo(db_session, company_id=COMPANY_A, work_center_id=wc_a.id, title=FOREIGN_WO_TITLE)
    headers = headers_for(user_b)

    foreign = client.get(f"{WORK_ORDERS_URL}/{wo_a.id}", headers=headers)
    absent = client.get(f"{WORK_ORDERS_URL}/{wo_a.id + 900_000}", headers=headers)

    assert (foreign.status_code, foreign.json()) == (absent.status_code, absent.json())

    foreign_start = client.post(f"{WORK_ORDERS_URL}/{wo_a.id}/start", headers=headers)
    absent_start = client.post(f"{WORK_ORDERS_URL}/{wo_a.id + 900_000}/start", headers=headers)
    assert (foreign_start.status_code, foreign_start.json()) == (absent_start.status_code, absent_start.json())


def test_each_company_only_ever_lists_its_own(client: TestClient, db_session: Session):
    """End-to-end shape of the invariant across the two list endpoints.

    A PIN, NOT A REPRODUCER -- it passes against the pre-fix code too. ``list_schedules``
    and ``list_work_orders`` were among the six handlers that were already scoped, so this
    holds the line on them while the rest of the file closes the ten that were not.
    """
    user_a = make_user(db_session, company_id=COMPANY_A)
    user_b = make_user(db_session, company_id=COMPANY_B)
    wc_a = foreign_work_center(db_session)
    schedule_a = make_schedule(db_session, company_id=COMPANY_A, work_center_id=wc_a.id, name=FOREIGN_SCHEDULE_NAME)
    wo_a = make_wo(db_session, company_id=COMPANY_A, work_center_id=wc_a.id, title=FOREIGN_WO_TITLE)
    schedule_b = make_schedule(db_session, company_id=COMPANY_B, name="B monthly PM")
    wo_b = make_wo(db_session, company_id=COMPANY_B, title="B corrective")

    listed_b = client.get(SCHEDULES_URL, headers=headers_for(user_b))
    assert [row["id"] for row in listed_b.json()] == [schedule_b.id]
    assert_discloses_nothing(listed_b)

    wos_b = client.get(WORK_ORDERS_URL, headers=headers_for(user_b))
    assert [row["id"] for row in wos_b.json()] == [wo_b.id]
    assert_discloses_nothing(wos_b)

    listed_a = client.get(SCHEDULES_URL, headers=headers_for(user_a))
    assert [row["id"] for row in listed_a.json()] == [schedule_a.id]
    assert "B monthly PM" not in listed_a.text

    wos_a = client.get(WORK_ORDERS_URL, headers=headers_for(user_a))
    assert [row["id"] for row in wos_a.json()] == [wo_a.id]
    assert "B corrective" not in wos_a.text


def test_wo_numbers_stay_globally_unique_across_companies(client: TestClient, db_session: Session):
    """A PIN ON A DELIBERATE NON-CHANGE, not a defect reproducer -- it passes against the
    pre-fix code too.

    ``MaintenanceWorkOrder.wo_number`` carries a GLOBAL unique constraint, so
    ``_generate_wo_number`` must keep scanning across tenants. The obvious "finish the
    tenancy sweep" edit -- adding ``company_id == company_id`` to that scan -- would hand
    two companies the same number and make the second company's create 500. This test is
    what stops that edit.
    """
    user_a = make_user(db_session, company_id=COMPANY_A)
    user_b = make_user(db_session, company_id=COMPANY_B)
    wc_a = make_work_center(db_session, company_id=COMPANY_A)
    wc_b = make_work_center(db_session, company_id=COMPANY_B)

    first = client.post(WORK_ORDERS_URL, headers=headers_for(user_a), json={"work_center_id": wc_a.id, "title": "A"})
    second = client.post(WORK_ORDERS_URL, headers=headers_for(user_b), json={"work_center_id": wc_b.id, "title": "B"})

    assert first.status_code == status.HTTP_200_OK, first.text
    assert second.status_code == status.HTTP_200_OK, second.text
    assert first.json()["wo_number"] != second.json()["wo_number"]


def test_dashboard_completion_rate_only_counts_the_callers_own_denominator(client: TestClient, db_session: Session):
    """``total_this_month`` is the sixth aggregate. It is not returned directly -- it is
    the denominator of ``completion_rate`` -- and it needs a completed row on BOTH sides
    to be discriminating: with an empty numerator, leaking the denominator changes 0/0 to
    0/N and the rate stays 0 either way."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    wc_a = foreign_work_center(db_session)
    for _ in range(3):
        make_wo(
            db_session,
            company_id=COMPANY_A,
            work_center_id=wc_a.id,
            title=FOREIGN_WO_TITLE,
            status=MaintenanceStatus.SCHEDULED,
            scheduled_date=date.today(),
        )
    make_wo(
        db_session,
        company_id=COMPANY_B,
        status=MaintenanceStatus.COMPLETED,
        completed_at=datetime.utcnow(),
        scheduled_date=date.today(),
    )

    response = client.get("/api/v1/maintenance/dashboard", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["completion_rate"] == 100.0, "A's three rows leaked into B's denominator"
    assert_discloses_nothing(response)


# ===========================================================================
# 7. Egress — a LEGACY row pointing at another tenant's machine
# ===========================================================================
#
# The write paths now validate work_center_id, which stops NEW cross-tenant rows. It
# does nothing about rows written BEFORE that guard, and the relationship the serializers
# traverse carries no predicate of its own: such a row passes every company_id filter in
# the query (it really is the caller's row) and used to render the FOREIGN machine's name
# straight back. Ingress closed, egress open.


def test_a_legacy_work_order_never_names_another_companys_machine(client: TestClient, db_session: Session):
    user_b = make_user(db_session, company_id=COMPANY_B)
    wc_a = foreign_work_center(db_session)
    # The exact row the pre-fix create_work_order could write: owned by B, pointed at A's
    # machine. company_id is part of no FK, so the DB accepts it on SQLite and Postgres.
    legacy = make_wo(db_session, company_id=COMPANY_B, work_center_id=wc_a.id, title="B legacy")

    for response in (
        client.get(WORK_ORDERS_URL, headers=headers_for(user_b)),
        client.get(f"{WORK_ORDERS_URL}/{legacy.id}", headers=headers_for(user_b)),
    ):
        assert response.status_code == status.HTTP_200_OK, response.text
        assert_discloses_nothing(response)

    detail = client.get(f"{WORK_ORDERS_URL}/{legacy.id}", headers=headers_for(user_b)).json()
    assert detail["work_center_name"] is None, "a foreign relation must read as absent, not as a name"
    assert detail["work_center_id"] == wc_a.id, "the stored id stays visible so the row can be corrected"


def test_a_legacy_schedule_and_log_never_name_another_companys_machine(client: TestClient, db_session: Session):
    user_b = make_user(db_session, company_id=COMPANY_B)
    wc_a = foreign_work_center(db_session)
    make_schedule(db_session, company_id=COMPANY_B, work_center_id=wc_a.id, name="B legacy PM")
    own_wc = make_work_center(db_session, company_id=COMPANY_B)
    make_log(db_session, company_id=COMPANY_B, work_center_id=own_wc.id, description="B own event")

    schedules = client.get(SCHEDULES_URL, headers=headers_for(user_b))
    assert schedules.status_code == status.HTTP_200_OK, schedules.text
    assert_discloses_nothing(schedules)
    assert schedules.json()[0]["work_center_name"] is None

    history = client.get(f"/api/v1/maintenance/history/{own_wc.id}", headers=headers_for(user_b))
    assert history.status_code == status.HTTP_200_OK, history.text
    assert_discloses_nothing(history)


# ===========================================================================
# 8. RBAC — every handler was a bare get_current_user
# ===========================================================================


def test_a_viewer_cannot_change_any_maintenance_record(client: TestClient, db_session: Session):
    """A VIEWER could open, start and close maintenance work orders on any machine. Every
    endpoint in the file was ``Depends(get_current_user)`` with no role gate at all, while
    the sibling supplier-scorecard router gated its writes to Admin/Manager."""
    viewer = make_user(db_session, company_id=COMPANY_B, role=UserRole.VIEWER)
    headers = headers_for(viewer)
    wc = make_work_center(db_session, company_id=COMPANY_B)
    schedule = make_schedule(db_session, company_id=COMPANY_B, work_center_id=wc.id)
    wo = make_wo(db_session, company_id=COMPANY_B, work_center_id=wc.id)

    refusals = [
        client.post(SCHEDULES_URL, headers=headers, json={"work_center_id": wc.id, "name": "nope"}),
        client.put(f"{SCHEDULES_URL}/{schedule.id}", headers=headers, json={"name": "nope"}),
        client.delete(f"{SCHEDULES_URL}/{schedule.id}", headers=headers),
        client.post(WORK_ORDERS_URL, headers=headers, json={"work_center_id": wc.id, "title": "nope"}),
        client.put(f"{WORK_ORDERS_URL}/{wo.id}", headers=headers, json={"title": "nope"}),
        client.post(f"{WORK_ORDERS_URL}/{wo.id}/start", headers=headers),
        client.post(f"{WORK_ORDERS_URL}/{wo.id}/complete", headers=headers, json={}),
        client.post(LOG_URL, headers=headers, json={"work_center_id": wc.id, "event_type": "x", "description": "y"}),
    ]
    assert [r.status_code for r in refusals] == [status.HTTP_403_FORBIDDEN] * 8, [r.text for r in refusals]

    # Refused, and nothing moved.
    assert fresh(db_session, MaintenanceWorkOrder, wo.id).status == MaintenanceStatus.SCHEDULED
    assert fresh(db_session, MaintenanceSchedule, schedule.id).is_active is True

    # Reads stay open -- the /maintenance route is gated on work_orders:view, which a
    # viewer holds, so refusing reads too would break the page for its intended audience.
    assert client.get(WORK_ORDERS_URL, headers=headers).status_code == status.HTTP_200_OK
    assert client.get("/api/v1/maintenance/dashboard", headers=headers).status_code == status.HTTP_200_OK


def test_an_operator_performs_but_does_not_plan(client: TestClient, db_session: Session):
    """The split: the maintenance tech doing the work signs in as an OPERATOR and must be
    able to start/complete/log, mirroring ``work_orders:complete``. Planning verbs stay
    with Admin/Manager/Supervisor, mirroring ``work_orders:create``/``edit``."""
    operator = make_user(db_session, company_id=COMPANY_B, role=UserRole.OPERATOR)
    headers = headers_for(operator)
    wc = make_work_center(db_session, company_id=COMPANY_B)
    wo = make_wo(db_session, company_id=COMPANY_B, work_center_id=wc.id)

    assert client.post(f"{WORK_ORDERS_URL}/{wo.id}/start", headers=headers).status_code == status.HTTP_200_OK
    assert (
        client.post(f"{WORK_ORDERS_URL}/{wo.id}/complete", headers=headers, json={}).status_code == status.HTTP_200_OK
    )
    logged = client.post(
        LOG_URL, headers=headers, json={"work_center_id": wc.id, "event_type": "observation", "description": "ok"}
    )
    assert logged.status_code == status.HTTP_200_OK, logged.text

    planning = client.post(SCHEDULES_URL, headers=headers, json={"work_center_id": wc.id, "name": "nope"})
    assert planning.status_code == status.HTTP_403_FORBIDDEN, planning.text


# ===========================================================================
# 9. Audit rows — invariant 2, no write in this file recorded anything
# ===========================================================================


def _audit_rows(db: Session, resource_type: str) -> list:
    from app.models.audit_log import AuditLog

    db.expire_all()
    return db.query(AuditLog).filter(AuditLog.resource_type == resource_type).all()


def test_every_maintenance_write_now_leaves_an_audit_row(client: TestClient, db_session: Session):
    """Invariant 2. Fourteen state changes across this router and its sibling wrote ZERO
    ``audit_log`` rows -- PM schedules and maintenance records are AS9100D-auditable
    quality records, and who started or closed one was unrecoverable.

    Rows are asserted as COMMITTED, not merely flushed: ``AuditService.log()`` only
    flushes, so a call placed after ``db.commit()`` lands in a transaction that get_db
    teardown rolls back, and a plain query would still see it because the client fixture
    shares one open transaction with the endpoint."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    headers = headers_for(user_b)
    wc = make_work_center(db_session, company_id=COMPANY_B)

    created_schedule = client.post(
        SCHEDULES_URL, headers=headers, json={"work_center_id": wc.id, "name": "quarterly spindle check"}
    )
    assert created_schedule.status_code == status.HTTP_200_OK, created_schedule.text
    schedule_id = created_schedule.json()["id"]

    assert client.put(f"{SCHEDULES_URL}/{schedule_id}", headers=headers, json={"name": "renamed"}).status_code == 200
    assert client.delete(f"{SCHEDULES_URL}/{schedule_id}", headers=headers).status_code == 200

    created_wo = client.post(WORK_ORDERS_URL, headers=headers, json={"work_center_id": wc.id, "title": "job"})
    assert created_wo.status_code == status.HTTP_200_OK, created_wo.text
    wo_id = created_wo.json()["id"]

    assert client.put(f"{WORK_ORDERS_URL}/{wo_id}", headers=headers, json={"title": "job v2"}).status_code == 200
    assert client.post(f"{WORK_ORDERS_URL}/{wo_id}/start", headers=headers).status_code == 200
    assert client.post(f"{WORK_ORDERS_URL}/{wo_id}/complete", headers=headers, json={}).status_code == 200
    assert (
        client.post(
            LOG_URL, headers=headers, json={"work_center_id": wc.id, "event_type": "note", "description": "d"}
        ).status_code
        == 200
    )

    schedule_actions = sorted(r.action for r in _audit_rows(db_session, "maintenance_schedule"))
    assert schedule_actions == ["CREATE", "UPDATE", "UPDATE"], schedule_actions

    wo_rows = _audit_rows(db_session, "maintenance_work_order")
    assert sorted(r.action for r in wo_rows) == ["CREATE", "STATUS_CHANGE", "STATUS_CHANGE", "UPDATE"]
    assert all(r.user_id == user_b.id for r in wo_rows), "the audit row must name who"
    assert all(r.company_id == COMPANY_B for r in wo_rows)

    assert [r.action for r in _audit_rows(db_session, "maintenance_log")] == ["CREATE"]
