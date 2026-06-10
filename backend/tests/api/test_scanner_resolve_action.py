"""Behavior locks for A0.4 ``POST /api/v1/scanner/resolve-action``.

The resolver is the keystone every scan surface builds on; contracts locked:

1. All four result kinds resolve with their documented shapes -- operation,
   work_order, employee, and the STRUCTURED MISS (kind="unknown", HTTP 200).
2. ``legal_actions`` / ``blockers`` are derived from the SAME gate predicates the
   shop-floor write endpoints enforce: for the clock-in cases asserted here, the
   resolver verdict is checked AGAINST the real ``POST /shop-floor/clock-in``
   response (parity, including the exact error text).
3. Tenant isolation: a company-B operation / work order / badge resolves to
   kind="unknown" for a company-A caller -- indistinguishable from nonexistence.
4. Resolution is read-only: no audit_log rows, no OperationalEvents.
5. Routing staleness: warning="routing_revision_changed" appears iff the part's
   current released routing post-dates the work order baseline (documented proxy
   -- WOs carry no routing-revision snapshot).
6. Badge resolution is lookup-only (no tokens) -- login stays on /auth/employee-login.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.routing import Routing, RoutingOperation
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
RESOLVE = "/api/v1/scanner/resolve-action"
CLOCK_IN = "/api/v1/shop-floor/clock-in"
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


def make_user(
    db: Session,
    *,
    role: UserRole = UserRole.OPERATOR,
    company_id: int = COMPANY_A,
    employee_id: str = None,
) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"a04-scan-{n}@co{company_id}.test",
        employee_id=employee_id or f"A04SCAN-{n:05d}",
        first_name="Scan",
        last_name="Tester",
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


def make_wo(
    db: Session,
    *,
    company_id: int = COMPANY_A,
    wo_status: WorkOrderStatus = WorkOrderStatus.IN_PROGRESS,
    op_statuses: list = None,
) -> tuple:
    """A work order with one operation per entry in op_statuses (default one READY op)."""
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"A04SCAN-P-{n}",
        name=f"Scan Part {n}",
        description="A0.4 scan fixture part",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()
    wc = WorkCenter(
        name=f"A04SCAN-WC-{n}",
        code=f"A04SCAN-WC-{n}",
        work_center_type="welding",
        description="A0.4 scan fixture work center",
        hourly_rate=100.0,
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.flush()
    wo = WorkOrder(
        work_order_number=f"A04SCAN-WO-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=10,
        status=wo_status,
        priority=5,
        due_date=date.today() + timedelta(days=30),
        company_id=company_id,
    )
    db.add(wo)
    db.flush()
    ops = []
    for i, status in enumerate(op_statuses or [OperationStatus.READY]):
        op = WorkOrderOperation(
            work_order_id=wo.id,
            work_center_id=wc.id,
            sequence=(i + 1) * 10,
            operation_number=f"OP{(i + 1) * 10}",
            name=f"Op {(i + 1) * 10}",
            status=status,
            quantity_complete=0,
            company_id=company_id,
        )
        db.add(op)
        ops.append(op)
    db.commit()
    for op in ops:
        db.refresh(op)
    db.refresh(wo)
    return wo, ops, wc, part


def make_open_entry(db: Session, user: User, wo: WorkOrder, op: WorkOrderOperation) -> TimeEntry:
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=wo.id,
        operation_id=op.id,
        work_center_id=op.work_center_id,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow() - timedelta(hours=1),
        clock_out=None,
        company_id=user.company_id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def resolve(client: TestClient, user: User, code: str, work_center_id: int = None) -> dict:
    payload = {"code": code}
    if work_center_id is not None:
        payload["work_center_id"] = work_center_id
    response = client.post(RESOLVE, json=payload, headers=headers_for(user))
    assert response.status_code == 200, response.text
    return response.json()


# ===========================================================================
# kind="operation"
# ===========================================================================


def test_operation_scan_ready_op_legal_actions(client: TestClient, db_session: Session):
    """READY op, no predecessors, user not clocked in: clock_in/complete/hold legal;
    report_production and resume blocked with the endpoints' exact error text."""
    user = make_user(db_session)
    wo, (op,), wc, part = make_wo(db_session)

    body = resolve(client, user, f"OP:{op.id}")

    assert body["kind"] == "operation"
    assert body["operation"]["id"] == op.id
    assert body["operation"]["sequence"] == 10
    assert body["operation"]["work_order_number"] == wo.work_order_number
    assert body["operation"]["part_number"] == part.part_number
    assert body["operation"]["status"] == "ready"
    assert body["operation"]["work_center_match"] is None  # no station provided
    assert sorted(body["legal_actions"]) == ["clock_in", "complete", "hold"]
    assert body["blockers"]["report_production"] == [
        "Operation must be in progress to add completed quantity",
        "You must be clocked in to add completed quantity",
    ]
    assert body["blockers"]["resume"] == ["Operation is not on hold"]
    assert "clock_in" not in body["blockers"]


def test_operation_scan_work_center_match_flag(client: TestClient, db_session: Session):
    user = make_user(db_session)
    _, (op,), wc, _ = make_wo(db_session)

    assert resolve(client, user, f"OP:{op.id}", work_center_id=wc.id)["operation"]["work_center_match"] is True
    assert resolve(client, user, f"OP:{op.id}", work_center_id=wc.id + 999)["operation"]["work_center_match"] is False


def test_clock_in_gate_parity_incomplete_predecessor(client: TestClient, db_session: Session):
    """Resolver blocker text for a predecessor-blocked op == the real clock-in 400 detail.

    Different work centers per op so allow_same_work_center does not bypass the gate.
    """
    user = make_user(db_session)
    wo, (op1, op2), wc, _ = make_wo(db_session, op_statuses=[OperationStatus.IN_PROGRESS, OperationStatus.PENDING])
    # Move op2 to its own work center so the predecessor gate applies.
    wc2 = WorkCenter(
        name=f"A04SCAN-WC2-{_next()}",
        code=f"A04SCAN-WC2-{_next()}",
        work_center_type="welding",
        hourly_rate=100.0,
        is_active=True,
        company_id=COMPANY_A,
    )
    db_session.add(wc2)
    db_session.flush()
    op2.work_center_id = wc2.id
    db_session.commit()

    body = resolve(client, user, f"OP:{op2.id}")
    assert "clock_in" not in body["legal_actions"]
    assert "Previous operations must be completed first" in body["blockers"]["clock_in"]

    real = client.post(
        CLOCK_IN,
        json={"work_order_id": wo.id, "operation_id": op2.id, "work_center_id": wc2.id, "entry_type": "run"},
        headers=headers_for(user),
    )
    assert real.status_code == 400
    assert real.json()["detail"] == "Previous operations must be completed first"
    assert real.json()["detail"] in body["blockers"]["clock_in"]


def test_clock_in_gate_parity_on_hold(client: TestClient, db_session: Session):
    """ON_HOLD op: resolver blocks clock_in with the endpoint's text; resume is legal."""
    user = make_user(db_session)
    wo, (op,), wc, _ = make_wo(db_session, op_statuses=[OperationStatus.ON_HOLD])

    body = resolve(client, user, f"OP:{op.id}")
    assert "clock_in" not in body["legal_actions"]
    assert "resume" in body["legal_actions"]
    assert "Operation is not ready to start" in body["blockers"]["clock_in"]
    assert body["blockers"]["complete"] == ["Operation is on hold and cannot be completed"]

    real = client.post(
        CLOCK_IN,
        json={"work_order_id": wo.id, "operation_id": op.id, "work_center_id": wc.id, "entry_type": "run"},
        headers=headers_for(user),
    )
    assert real.status_code == 400
    assert real.json()["detail"] == "Operation is not ready to start"
    assert real.json()["detail"] in body["blockers"]["clock_in"]


def test_clock_in_gate_parity_already_clocked_in(client: TestClient, db_session: Session):
    """User already clocked in: clock_in blocked (endpoint text), report_production legal."""
    user = make_user(db_session)
    wo, (op,), wc, _ = make_wo(db_session, op_statuses=[OperationStatus.IN_PROGRESS])
    make_open_entry(db_session, user, wo, op)

    body = resolve(client, user, f"OP:{op.id}")
    assert "clock_in" not in body["legal_actions"]
    assert body["blockers"]["clock_in"] == ["You are already clocked in to this operation."]
    assert "report_production" in body["legal_actions"]
    assert "complete" in body["legal_actions"]

    real = client.post(
        CLOCK_IN,
        json={"work_order_id": wo.id, "operation_id": op.id, "work_center_id": wc.id, "entry_type": "run"},
        headers=headers_for(user),
    )
    assert real.status_code == 400
    assert real.json()["detail"] == body["blockers"]["clock_in"][0]


def test_clock_in_gate_parity_happy_path(client: TestClient, db_session: Session):
    """When the resolver says clock_in is legal, the real endpoint accepts it."""
    user = make_user(db_session)
    wo, (op,), wc, _ = make_wo(db_session)

    body = resolve(client, user, f"OP:{op.id}")
    assert "clock_in" in body["legal_actions"]

    real = client.post(
        CLOCK_IN,
        json={"work_order_id": wo.id, "operation_id": op.id, "work_center_id": wc.id, "entry_type": "run"},
        headers=headers_for(user),
    )
    assert real.status_code == 200, real.text


def test_clock_in_gate_parity_wrong_work_center(client: TestClient, db_session: Session):
    """Station-aware resolve: a mismatched work_center_id blocks clock_in with the real
    endpoint's exact 400 text; with no station id (or the right one) behavior is unchanged."""
    user = make_user(db_session)
    wo, (op,), wc, _ = make_wo(db_session)
    other_wc = WorkCenter(
        name=f"A04SCAN-WC-OTHER-{_next()}",
        code=f"A04SCAN-WC-OTHER-{_next()}",
        work_center_type="welding",
        hourly_rate=100.0,
        is_active=True,
        company_id=COMPANY_A,
    )
    db_session.add(other_wc)
    db_session.commit()

    body = resolve(client, user, f"OP:{op.id}", work_center_id=other_wc.id)
    assert body["operation"]["work_center_match"] is False
    assert "clock_in" not in body["legal_actions"]

    real = client.post(
        CLOCK_IN,
        json={"work_order_id": wo.id, "operation_id": op.id, "work_center_id": other_wc.id, "entry_type": "run"},
        headers=headers_for(user),
    )
    assert real.status_code == 400
    assert real.json()["detail"] == "Operation does not belong to this work center"
    assert body["blockers"]["clock_in"] == [real.json()["detail"]]

    # No station id provided: the gate must not fire (behavior unchanged).
    assert "clock_in" in resolve(client, user, f"OP:{op.id}")["legal_actions"]
    # The matching station stays legal.
    assert "clock_in" in resolve(client, user, f"OP:{op.id}", work_center_id=wc.id)["legal_actions"]


def test_complete_blocked_on_terminal_work_order(client: TestClient, db_session: Session):
    user = make_user(db_session)
    _, (op,), _, _ = make_wo(db_session, wo_status=WorkOrderStatus.CANCELLED, op_statuses=[OperationStatus.IN_PROGRESS])

    body = resolve(client, user, f"OP:{op.id}")
    assert "complete" not in body["legal_actions"]
    assert body["blockers"]["complete"] == ["cannot complete operation: work order is cancelled"]


def test_completed_operation_only_resume_and_hold_blocked(client: TestClient, db_session: Session):
    user = make_user(db_session)
    _, (op,), _, _ = make_wo(db_session, op_statuses=[OperationStatus.COMPLETE])

    body = resolve(client, user, f"OP:{op.id}")
    assert body["legal_actions"] == []
    assert body["blockers"]["hold"] == ["Cannot put completed operation on hold"]
    assert "Operation is already complete" in body["blockers"]["complete"]


# ===========================================================================
# kind="work_order"
# ===========================================================================


def test_work_order_scan_lists_operations_and_current_op(client: TestClient, db_session: Session):
    user = make_user(db_session)
    wo, (op1, op2), _, part = make_wo(db_session, op_statuses=[OperationStatus.COMPLETE, OperationStatus.READY])

    body = resolve(client, user, f"WO:{wo.work_order_number}")

    assert body["kind"] == "work_order"
    assert body["work_order"]["work_order_number"] == wo.work_order_number
    assert body["work_order"]["status"] == "in_progress"
    assert body["work_order"]["quantity_ordered"] == 10.0
    assert body["work_order"]["part_number"] == part.part_number
    assert [o["id"] for o in body["operations"]] == [op1.id, op2.id]
    assert body["operations"][0]["status"] == "complete"
    # current op = first non-complete by sequence, so a WO-level scan can jump there.
    assert body["work_order"]["current_operation_id"] == op2.id


def test_work_order_current_op_is_canonical_active_operation(client: TestClient, db_session: Session):
    """current_operation_id uses the canonical rule (first IN_PROGRESS, else first READY,
    else first non-COMPLETE) -- NOT 'first non-complete by sequence', so a seq-10 ON_HOLD
    op does not mask the seq-20 op actually being run."""
    user = make_user(db_session)
    wo, (op1, op2), _, _ = make_wo(db_session, op_statuses=[OperationStatus.ON_HOLD, OperationStatus.IN_PROGRESS])

    body = resolve(client, user, f"WO:{wo.work_order_number}")
    assert body["work_order"]["current_operation_id"] == op2.id


def test_work_order_scan_rejects_sql_wildcards(client: TestClient, db_session: Session):
    """'WO:%' (and '_' as any-char) must NOT resolve: the case-insensitive fallback is an
    exact comparison, not ilike, so scanned wildcard characters cannot match arbitrary WOs."""
    user = make_user(db_session)
    wo, _, _, _ = make_wo(db_session)  # a real WO that ilike('%') would have matched

    body = resolve(client, user, "WO:%")
    assert body["kind"] == "unknown"
    assert body["reason"] == "No work order matches this code"

    # '_' must not act as a single-character wildcard against an existing number.
    probe = wo.work_order_number[:-1] + "_"
    assert probe != wo.work_order_number
    assert resolve(client, user, f"WO:{probe}")["kind"] == "unknown"


def test_work_order_number_with_literal_underscore_resolves(client: TestClient, db_session: Session):
    """A literal '_' in a real WO number still resolves through the case-insensitive
    fallback (lowercased scan forces the fallback path past the exact-match probe)."""
    user = make_user(db_session)
    wo, _, _, _ = make_wo(db_session)
    n = _next()
    wo.work_order_number = f"A04SCAN_WO_{n:05d}"
    db_session.commit()

    body = resolve(client, user, f"WO:a04scan_wo_{n:05d}")
    assert body["kind"] == "work_order"
    assert body["work_order"]["work_order_number"] == wo.work_order_number


# ===========================================================================
# kind="employee"
# ===========================================================================


def test_employee_badge_scan_is_lookup_only(client: TestClient, db_session: Session):
    caller = make_user(db_session)
    badge_user = make_user(db_session, employee_id="40231")

    body = resolve(client, caller, "40231")

    assert body == {
        "kind": "employee",
        "code": "40231",
        "employee_id": "40231",
        "first_name": badge_user.first_name,
        "last_initial": badge_user.last_name[:1].upper(),
    }
    # Lookup only: no tokens, no auth side effects in the payload.
    assert "access_token" not in body and "refresh_token" not in body


def test_inactive_employee_badge_does_not_resolve(client: TestClient, db_session: Session):
    caller = make_user(db_session)
    badge_user = make_user(db_session, employee_id="40232")
    badge_user.is_active = False
    db_session.commit()

    body = resolve(client, caller, "40232")
    assert body["kind"] == "unknown"
    assert body["reason"] == "No employee badge matches this id"


def test_alphanumeric_employee_id_resolves_exact_match(client: TestClient, db_session: Session):
    """Badge sheets print users.employee_id verbatim -- legacy alphanumeric ids resolve too."""
    caller = make_user(db_session)
    badge_user = make_user(db_session, employee_id="EMP-00339")

    body = resolve(client, caller, "EMP-00339")
    assert body["kind"] == "employee"
    assert body["employee_id"] == "EMP-00339"
    assert body["first_name"] == badge_user.first_name


# ===========================================================================
# kind="unknown" -- the structured miss
# ===========================================================================


def test_unknown_code_returns_structured_miss_with_200(client: TestClient, db_session: Session):
    user = make_user(db_session)

    body = resolve(client, user, "TOTALLY-NOT-A-CODE-??")
    assert body == {"kind": "unknown", "code": "TOTALLY-NOT-A-CODE-??", "reason": "Unrecognized code"}

    assert resolve(client, user, "OP:999999")["kind"] == "unknown"
    assert resolve(client, user, "OP:notanumber")["reason"] == "Malformed operation code (expected OP:<id>)"
    assert resolve(client, user, "WO:NO-SUCH-WO")["reason"] == "No work order matches this code"
    assert resolve(client, user, "999999999")["reason"] == "No employee badge matches this id"


def test_malformed_op_ids_are_structured_misses_not_500(client: TestClient, db_session: Session):
    """OP:² passes str.isdigit() but int() raises; a 21-digit id overflows the DB int
    range. Both must come back as kind="unknown" with HTTP 200 (asserted in resolve())."""
    user = make_user(db_session)

    for code in ["OP:²", "OP:999999999999999999999"]:
        body = resolve(client, user, code)
        assert body["kind"] == "unknown", code
        assert body["reason"] == "Malformed operation code (expected OP:<id>)"

    # Boundary: 18 ASCII digits is still parsed and looked up (a clean miss, not malformed).
    assert resolve(client, user, "OP:" + "9" * 18)["reason"] == "No operation matches this code"


# ===========================================================================
# Tenant isolation
# ===========================================================================


def test_tenant_isolation_other_company_codes_resolve_unknown(client: TestClient, db_session: Session):
    user_a = make_user(db_session, company_id=COMPANY_A)
    wo_b, (op_b,), _, _ = make_wo(db_session, company_id=COMPANY_B)
    make_user(db_session, company_id=COMPANY_B, employee_id="70707")

    assert resolve(client, user_a, f"OP:{op_b.id}")["kind"] == "unknown"
    assert resolve(client, user_a, f"WO:{wo_b.work_order_number}")["kind"] == "unknown"
    assert resolve(client, user_a, "70707")["kind"] == "unknown"

    # Same codes resolve fine for a company-B caller.
    user_b = make_user(db_session, company_id=COMPANY_B)
    assert resolve(client, user_b, f"OP:{op_b.id}")["kind"] == "operation"
    assert resolve(client, user_b, f"WO:{wo_b.work_order_number}")["kind"] == "work_order"
    assert resolve(client, user_b, "70707")["kind"] == "employee"


def test_soft_deleted_work_order_does_not_resolve(client: TestClient, db_session: Session):
    user = make_user(db_session)
    wo, (op,), _, _ = make_wo(db_session)
    wo.soft_delete(user.id)
    db_session.commit()

    assert resolve(client, user, f"WO:{wo.work_order_number}")["kind"] == "unknown"
    assert resolve(client, user, f"OP:{op.id}")["kind"] == "unknown"


# ===========================================================================
# Read-only: no audit rows, no OperationalEvents
# ===========================================================================


def test_resolve_writes_no_audit_rows_and_no_events(client: TestClient, db_session: Session):
    user = make_user(db_session)
    wo, (op,), wc, _ = make_wo(db_session)
    make_user(db_session, employee_id="50505")

    audit_before = db_session.query(AuditLog).count()
    events_before = db_session.query(OperationalEvent).count()

    resolve(client, user, f"OP:{op.id}", work_center_id=wc.id)
    resolve(client, user, f"WO:{wo.work_order_number}")
    resolve(client, user, "50505")
    resolve(client, user, "GARBAGE")

    db_session.expire_all()
    assert db_session.query(AuditLog).count() == audit_before
    assert db_session.query(OperationalEvent).count() == events_before


# ===========================================================================
# Routing-revision staleness (documented proxy -- no snapshot linkage exists)
# ===========================================================================


def _make_released_routing(db: Session, part: Part, wc: WorkCenter, *, approved_at: datetime, revision: str = "B"):
    routing = Routing(
        part_id=part.id,
        revision=revision,
        status="released",
        is_active=True,
        effective_date=approved_at,
        approved_at=approved_at,
        company_id=part.company_id,
    )
    db.add(routing)
    db.flush()
    db.add(
        RoutingOperation(
            routing_id=routing.id,
            sequence=10,
            operation_number="OP10",
            name="Routed Op 10",
            work_center_id=wc.id,
            is_active=True,
            company_id=part.company_id,
        )
    )
    db.commit()
    return routing


def test_routing_released_after_wo_creation_warns(client: TestClient, db_session: Session):
    user = make_user(db_session)
    _, (op,), wc, part = make_wo(db_session)
    _make_released_routing(db_session, part, wc, approved_at=datetime.utcnow() + timedelta(hours=1))

    body = resolve(client, user, f"OP:{op.id}")
    assert body["warning"] == "routing_revision_changed"
    check = body["routing_revision_check"]
    assert check["current_released_revision"] == "B"
    assert check["released_routing_changed_after_wo_creation"] is True
    assert check["checked_against"] is not None


def test_routing_released_before_wo_creation_does_not_warn(client: TestClient, db_session: Session):
    user = make_user(db_session)
    _, (op,), wc, part = make_wo(db_session)
    _make_released_routing(db_session, part, wc, approved_at=datetime.utcnow() - timedelta(days=30))

    body = resolve(client, user, f"OP:{op.id}")
    assert body["warning"] is None
    assert body["routing_revision_check"]["released_routing_changed_after_wo_creation"] is False


def test_routing_check_picks_latest_release_not_highest_id(client: TestClient, db_session: Session):
    """Two released routings: the LATER-approved one is the routing in force, even though
    it has the LOWER id (the staleness query orders by approved_at, id only as tiebreak)."""
    user = make_user(db_session)
    _, (op,), wc, part = make_wo(db_session)
    # Lower id, later release -- this is the revision in force.
    _make_released_routing(db_session, part, wc, approved_at=datetime.utcnow() + timedelta(hours=2), revision="B")
    # Higher id, earlier release.
    _make_released_routing(db_session, part, wc, approved_at=datetime.utcnow() - timedelta(days=30), revision="C")

    body = resolve(client, user, f"OP:{op.id}")
    assert body["routing_revision_check"]["current_released_revision"] == "B"
    assert body["warning"] == "routing_revision_changed"


def test_no_released_routing_means_no_check(client: TestClient, db_session: Session):
    user = make_user(db_session)
    _, (op,), _, _ = make_wo(db_session)

    body = resolve(client, user, f"OP:{op.id}")
    assert body["warning"] is None
    assert body["routing_revision_check"] is None


# ===========================================================================
# Auth required
# ===========================================================================


def test_resolve_requires_authentication(client: TestClient, db_session: Session):
    response = client.post(RESOLVE, json={"code": "OP:1"})
    assert response.status_code in (401, 403)


# ===========================================================================
# Per-route rate limit declaration
# ===========================================================================


def test_scanner_resolve_action_rate_limit_declared(client: TestClient):
    """resolve-action carries a 60/minute per-route limit in main.py's central
    path -> limit map (the same declaration style as the auth limits).

    NOTE: like AUTH_RATE_LIMITS, this map feeds get_rate_limit_for_path, which is
    not yet wired into the slowapi Limiter -- enforcement today is the global
    default. This locks the declaration so the wiring fix has the value in place.
    """
    import app.main as app_main

    endpoint_limits = getattr(app_main, "ENDPOINT_RATE_LIMITS", None)
    if endpoint_limits is None:
        pytest.skip("Rate limiting disabled in this environment (settings.RATE_LIMIT_ENABLED=False)")
    assert endpoint_limits[RESOLVE] == "60/minute"
