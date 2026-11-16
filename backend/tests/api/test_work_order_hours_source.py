"""P1.4: where an operation's actual hours come from, derived at read time.

Pinned on ``GET /work-orders/{id}`` (and by-number, which now shares the enrichment):

* ``hours_source`` -- ``time_entries`` when closed TimeEntries measured hours; ``manual`` for
  stored hours with no TimeEntry hours behind them; ``none`` when nothing is recorded;
  ``standard`` never (no code copies standards into actuals).
* ``recorded_setup_hours`` / ``recorded_run_hours`` -- CLOSED entries only, split as clock-out
  credits them (SETUP -> setup; every other type, and a NULL type, -> run). They can exceed
  the stored actuals: an operation hold and the flag-off completion auto-close close entries
  without crediting the operation.
* ``hours_include_backfill`` -- a closed entry came from a paper back-entry or an import.
* Invariant 1: a foreign company's entry on a colliding operation id never counts.
"""

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import create_access_token
from app.models.company import Company
from app.models.part import Part
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services.work_order_state_service import OperationLaborEvidence, operation_hours_source

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> None:
    if db.get(Company, company_id) is None:
        db.add(Company(id=company_id, name=f"Hours Co {company_id}", slug=f"hours-co-{company_id}", is_active=True))
        db.commit()


def make_user(db: Session, *, role: UserRole = UserRole.ADMIN, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"p14-{n}@co{company_id}.test",
        employee_id=f"P14-{n:05d}",
        first_name="P14",
        last_name="User",
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


def make_wo_op(db: Session, *, quantity_ordered: float = 10) -> tuple[WorkOrder, WorkOrderOperation]:
    n = _next()
    part = Part(
        part_number=f"P14-P-{n}",
        name=f"Part {n}",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=COMPANY_A,
    )
    wc = WorkCenter(
        name=f"P14-WC-{n}",
        code=f"P14-WC-{n}",
        work_center_type="welding",
        hourly_rate=100.0,
        is_active=True,
        company_id=COMPANY_A,
    )
    db.add_all([part, wc])
    db.flush()
    wo = WorkOrder(
        work_order_number=f"P14-WO-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=quantity_ordered,
        status=WorkOrderStatus.IN_PROGRESS,
        priority=5,
        due_date=date.today() + timedelta(days=30),
        company_id=COMPANY_A,
    )
    db.add(wo)
    db.flush()
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        sequence=10,
        operation_number="10",
        name="Weld",
        status=OperationStatus.IN_PROGRESS,
        quantity_complete=0,
        company_id=COMPANY_A,
    )
    db.add(op)
    db.commit()
    return wo, op


def entry(
    db: Session,
    user: User,
    wo: WorkOrder,
    op: WorkOrderOperation,
    *,
    hours: float,
    closed: bool = True,
    entry_type=TimeEntryType.RUN,
    source=None,
    company_id: int = COMPANY_A,
) -> TimeEntry:
    now = datetime.utcnow()
    row = TimeEntry(
        user_id=user.id,
        work_order_id=wo.id,
        operation_id=op.id,
        work_center_id=op.work_center_id,
        entry_type=entry_type,
        clock_in=now - timedelta(hours=hours),
        clock_out=now if closed else None,
        duration_hours=hours if closed else None,
        source=source,
        company_id=company_id,
    )
    db.add(row)
    db.commit()
    return row


def read_op(client, user, wo: WorkOrder) -> dict:
    resp = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    [op] = resp.json()["operations"]
    return op


def hours_fields(op: dict) -> dict:
    return {
        key: op[key]
        for key in (
            "hours_source",
            "recorded_setup_hours",
            "recorded_run_hours",
            "hours_include_backfill",
            "actual_setup_hours",
            "actual_run_hours",
        )
    }


# ---------------------------------------------------------------------------
# The pure rule
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "evidence, stored, expected",
    [
        (None, (0, 0), "none"),
        (OperationLaborEvidence(entries=1), (0, 0), "none"),  # open entry only
        (OperationLaborEvidence(entries=1, closed_entries=1, closed_run_hours=1.5), (0, 1.5), "time_entries"),
        (OperationLaborEvidence(entries=1, closed_entries=1, closed_setup_hours=0.5), (0, 0), "time_entries"),
        (OperationLaborEvidence(entries=1, closed_entries=1), (0, 0), "time_entries"),  # a measured zero
        (OperationLaborEvidence(entries=1, closed_entries=1), (0, 2.0), "manual"),
        (None, (1.0, 0), "manual"),
        (None, (None, None), "none"),
    ],
)
def test_operation_hours_source_rule(evidence, stored, expected):
    op = SimpleNamespace(actual_setup_hours=stored[0], actual_run_hours=stored[1])
    assert operation_hours_source(op, evidence) == expected


@pytest.mark.unit
def test_standard_is_reserved_and_never_derived():
    """Every branch of the rule, over every reachable input shape, returns one of three."""
    results = {
        operation_hours_source(SimpleNamespace(actual_setup_hours=s, actual_run_hours=r), e)
        for s in (None, 0, 1.0)
        for r in (None, 0, 2.0)
        for e in (
            None,
            OperationLaborEvidence(entries=1),
            OperationLaborEvidence(entries=1, closed_entries=1),
            OperationLaborEvidence(entries=2, closed_entries=2, closed_setup_hours=1.0, closed_run_hours=1.0),
        )
    }
    assert results == {"time_entries", "manual", "none"}


@pytest.mark.unit
def test_the_evidence_read_is_one_tenant_scoped_grouped_query_on_postgresql():
    """DIALECT-COMPILED for PostgreSQL rather than trusting SQLite's execution: one statement,
    scoped on company_id, closed-only hour legs, SETUP vs (NULL or not SETUP), and the
    backfill/import sources -- no approval filter (a measurement, not a cost)."""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.orm import Query

    from app.services.work_order_state_service import operation_labor_evidence

    captured: list = []

    class _CapturingQuery(Query):
        def all(self):
            captured.append(
                " ".join(
                    str(
                        self.statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
                    ).split()
                )
            )
            return []

    class _StubSession:
        def query(self, *entities):
            return _CapturingQuery(list(entities))

    assert operation_labor_evidence(_StubSession(), 7, [3, None, 1]) == {}
    [sql] = captured
    assert "WHERE time_entries.company_id = 7 AND time_entries.operation_id IN (1, 3)" in sql
    assert "GROUP BY time_entries.operation_id" in sql
    assert (
        "CASE WHEN (time_entries.clock_out IS NOT NULL AND time_entries.entry_type = 'SETUP') "
        "THEN coalesce(time_entries.duration_hours, 0.0)"
    ) in sql
    assert (
        "CASE WHEN (time_entries.clock_out IS NOT NULL AND (time_entries.entry_type IS NULL OR "
        "time_entries.entry_type != 'SETUP')) THEN coalesce(time_entries.duration_hours, 0.0)"
    ) in sql
    assert "time_entries.source IN ('import', 'backfill')" in sql
    assert "approved" not in sql


# ---------------------------------------------------------------------------
# On the work-order read
# ---------------------------------------------------------------------------


def test_kiosk_clock_in_and_out_reads_time_entries_with_recorded_equal_to_stored(client, db_session):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wo, op = make_wo_op(db_session)
    clock_in = client.post(
        "/api/v1/shop-floor/clock-in",
        json={
            "work_order_id": wo.id,
            "operation_id": op.id,
            "work_center_id": op.work_center_id,
            "entry_type": "run",
            "source": "kiosk",
        },
        headers=headers_for(operator),
    )
    assert clock_in.status_code == status.HTTP_200_OK, clock_in.text
    entry_id = clock_in.json()["id"]
    # Backdate the clock-in so the measured duration is a real 1.5 h.
    row = db_session.get(TimeEntry, entry_id)
    row.clock_in = datetime.utcnow() - timedelta(hours=1.5)
    db_session.commit()

    clock_out = client.post(
        f"/api/v1/shop-floor/clock-out/{entry_id}",
        json={"quantity_produced": 2, "source": "kiosk"},
        headers=headers_for(operator),
    )
    assert clock_out.status_code == status.HTTP_200_OK, clock_out.text

    fields = hours_fields(read_op(client, make_user(db_session), wo))
    assert fields["hours_source"] == "time_entries"
    assert fields["recorded_run_hours"] == pytest.approx(1.5, abs=0.01)
    assert fields["recorded_run_hours"] == pytest.approx(fields["actual_run_hours"])
    assert fields["recorded_setup_hours"] == 0.0
    assert fields["hours_include_backfill"] is False


def test_no_entries_reads_none(client, db_session):
    admin = make_user(db_session)
    wo, _ = make_wo_op(db_session)
    assert hours_fields(read_op(client, admin, wo)) == {
        "hours_source": "none",
        "recorded_setup_hours": 0.0,
        "recorded_run_hours": 0.0,
        "hours_include_backfill": False,
        "actual_setup_hours": 0.0,
        "actual_run_hours": 0.0,
    }


def test_an_open_entry_contributes_nothing_until_clock_out(client, db_session):
    admin = make_user(db_session)
    wo, op = make_wo_op(db_session)
    entry(db_session, admin, wo, op, hours=3.0, closed=False)
    fields = hours_fields(read_op(client, admin, wo))
    assert fields["hours_source"] == "none"
    assert fields["recorded_run_hours"] == 0.0


def test_stored_hours_with_no_time_entry_read_manual(client, db_session):
    admin = make_user(db_session)
    wo, op = make_wo_op(db_session)
    op.actual_setup_hours = 0.75
    op.actual_run_hours = 2.0
    db_session.commit()
    fields = hours_fields(read_op(client, admin, wo))
    assert fields["hours_source"] == "manual"
    assert (fields["recorded_setup_hours"], fields["recorded_run_hours"]) == (0.0, 0.0)


def test_setup_and_run_split_the_way_clock_out_credits_them(client, db_session):
    """SETUP -> setup; RUN, REWORK, INSPECTION, DOWNTIME, BREAK and a NULL type -> run."""
    admin = make_user(db_session)
    wo, op = make_wo_op(db_session)
    entry(db_session, admin, wo, op, hours=1.0, entry_type=TimeEntryType.SETUP)
    for entry_type in (
        TimeEntryType.RUN,
        TimeEntryType.REWORK,
        TimeEntryType.INSPECTION,
        TimeEntryType.DOWNTIME,
        TimeEntryType.BREAK,
    ):
        entry(db_session, admin, wo, op, hours=0.5, entry_type=entry_type)
    null_type = entry(db_session, admin, wo, op, hours=0.25)
    null_type.entry_type = None
    db_session.commit()
    assert db_session.get(TimeEntry, null_type.id).entry_type is None

    fields = hours_fields(read_op(client, admin, wo))
    assert fields["hours_source"] == "time_entries"
    assert fields["recorded_setup_hours"] == pytest.approx(1.0)
    assert fields["recorded_run_hours"] == pytest.approx(2.75)


def test_a_backfill_entry_is_flagged(client, db_session):
    admin = make_user(db_session)
    wo, op = make_wo_op(db_session)
    entry(db_session, admin, wo, op, hours=1.0, source="kiosk")
    first = hours_fields(read_op(client, admin, wo))
    assert first["hours_include_backfill"] is False

    entry(db_session, admin, wo, op, hours=0.1, source="backfill")
    fields = hours_fields(read_op(client, admin, wo))
    assert fields["hours_source"] == "time_entries"
    assert fields["hours_include_backfill"] is True
    assert fields["recorded_run_hours"] == pytest.approx(1.1)


def test_an_open_backfill_entry_does_not_flag_recorded_hours(client, db_session):
    admin = make_user(db_session)
    wo, op = make_wo_op(db_session)
    entry(db_session, admin, wo, op, hours=1.0, closed=False, source="backfill")
    assert read_op(client, admin, wo)["hours_include_backfill"] is False


def test_a_foreign_company_entry_on_a_colliding_operation_id_never_counts(client, db_session):
    admin = make_user(db_session)
    foreign_user = make_user(db_session, company_id=COMPANY_B)
    wo, op = make_wo_op(db_session)
    entry(db_session, foreign_user, wo, op, hours=5.0, source="backfill", company_id=COMPANY_B)
    fields = hours_fields(read_op(client, admin, wo))
    assert fields["hours_source"] == "none"
    assert fields["recorded_run_hours"] == 0.0
    assert fields["hours_include_backfill"] is False
    assert read_op(client, admin, wo)["has_labor_evidence"] is False


def test_flag_off_shop_floor_complete_auto_close_leaves_recorded_above_stored(client, db_session, monkeypatch):
    """With LABOR_COST_ROLLUP_ENABLED off the close auto-closes open entries without
    crediting the operation: the stored actuals stay 0 while the recorded hours are ~5."""
    monkeypatch.setattr(settings, "LABOR_COST_ROLLUP_ENABLED", False)
    admin = make_user(db_session)
    welder = make_user(db_session, role=UserRole.OPERATOR)
    wo, op = make_wo_op(db_session, quantity_ordered=5)
    entry(db_session, admin, wo, op, hours=2.0, closed=False)
    entry(db_session, welder, wo, op, hours=3.0, closed=False)

    reported = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/production",
        json={"quantity_complete_delta": 5},
        headers=headers_for(welder),
    )
    assert reported.status_code == status.HTTP_200_OK, reported.text
    closed = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/complete", json={"quantity_complete": 5}, headers=headers_for(admin)
    )
    assert closed.status_code == status.HTTP_200_OK, closed.text

    fields = hours_fields(read_op(client, admin, wo))
    assert fields["hours_source"] == "time_entries"
    assert fields["actual_run_hours"] == 0.0
    assert fields["recorded_run_hours"] == pytest.approx(5.0, abs=0.05)


def test_an_operation_hold_closes_entries_without_crediting_the_operation(client, db_session):
    admin = make_user(db_session, role=UserRole.SUPERVISOR)
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wo, op = make_wo_op(db_session)
    entry(db_session, operator, wo, op, hours=2.0, closed=False, source="kiosk")

    held = client.put(
        f"/api/v1/shop-floor/operations/{op.id}/hold", json={"source": "desktop"}, headers=headers_for(admin)
    )
    assert held.status_code == status.HTTP_200_OK, held.text

    fields = hours_fields(read_op(client, admin, wo))
    assert fields["hours_source"] == "time_entries"
    assert fields["actual_run_hours"] == 0.0
    assert fields["recorded_run_hours"] == pytest.approx(2.0, abs=0.05)


def test_by_number_carries_the_same_hours_fields(client, db_session):
    admin = make_user(db_session)
    wo, op = make_wo_op(db_session)
    entry(db_session, admin, wo, op, hours=1.25, entry_type=TimeEntryType.SETUP, source="backfill")
    by_id = read_op(client, admin, wo)
    resp = client.get(f"/api/v1/work-orders/by-number/{wo.work_order_number}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    [by_number] = resp.json()["operations"]
    assert hours_fields(by_number) == hours_fields(by_id)
    assert by_id["hours_source"] == "time_entries"
    assert by_id["recorded_setup_hours"] == pytest.approx(1.25)
    assert by_id["hours_include_backfill"] is True
