"""Behavior locks for the Batch-11C operator-qualification gate (G5-B).

POSTURE: warn-and-record (mirrors Batch-4 / G5-B). Clock-in and operation-start
SUCCEED even when the operator is not qualified, but every unqualified start leaves a
tamper-evident audit row (action ``OPERATOR_QUALIFICATION_EXCEPTION``) + a warning
OperationalEvent and surfaces the exceptions on the response ``qualification_exceptions``.
The gate NEVER blocks.

"Qualified" = BOTH legs pass:
- SKILL: an active ``SkillMatrix`` entry for (operator, work_center) at
  ``skill_level >= MIN_SKILL_LEVEL`` (2 = Basic). No entry / below-Basic -> exception.
- CERT: where ``WorkCenter.required_certification_type`` is set, the operator must hold a
  current (active / expiring_soon) ``OperatorCertification`` of that type. Otherwise an
  exception. No required cert type (the common case) -> leg skipped.

The new evaluator IS company-scoped (unlike the legacy ``check_operator_qualification``
endpoint helper).

Covered:
- Unit: ``evaluate_operator_qualification`` matrix (no entry / below-Basic / >= Basic;
  cert missing / expired / revoked / current) + ``_effective_cert_status`` expiry logic
  + tenant isolation.
- (a) clock-in / start with NO active skill entry -> operator_not_skill_qualified, still
  creates the TimeEntry (200), surfaced on the response.
- (b) skill_level >= 2 active entry -> no exception; level 1 -> exception.
- (c) work center with required_certification_type: no/expired/revoked cert ->
  operator_certification_missing_or_expired; current cert -> clear.
- (d) tenant isolation -- skill/cert rows in another company don't satisfy the gate.
"""

from datetime import date, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.operational_event import OperationalEvent
from app.models.operator_certification import (
    CertificationStatus,
    CertificationType,
    OperatorCertification,
    SkillMatrix,
)
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services.operator_qualification_service import (
    MIN_SKILL_LEVEL,
    _effective_cert_status,
    evaluate_operator_qualification,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
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
        email=f"b11c-g5b-{n}@co{company_id}.test",
        employee_id=f"B11CG5B-{n:05d}",
        first_name="B11C",
        last_name="G5B",
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


def make_part(db: Session, *, company_id: int = COMPANY_A) -> Part:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"B11CG5B-P-{n}",
        name=f"Part {n}",
        description="batch11c G5B fixture part",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_work_center(
    db: Session,
    *,
    required_certification_type: CertificationType = None,
    company_id: int = COMPANY_A,
) -> WorkCenter:
    _ensure_company(db, company_id)
    n = _next()
    wc = WorkCenter(
        name=f"B11CG5B-WC-{n}",
        code=f"B11CG5B-WC-{n}",
        work_center_type="welding",
        description="batch11c G5B fixture work center",
        hourly_rate=100,
        is_active=True,
        required_certification_type=required_certification_type,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def make_wo(db: Session, part: Part, *, company_id: int = COMPANY_A) -> WorkOrder:
    n = _next()
    wo = WorkOrder(
        work_order_number=f"B11CG5B-WO-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=10,
        status=WorkOrderStatus.RELEASED,
        priority=5,
        due_date=date.today() + timedelta(days=30),
        company_id=company_id,
    )
    db.add(wo)
    db.flush()
    return wo


def make_op(
    db: Session,
    wo: WorkOrder,
    wc: WorkCenter,
    *,
    sequence: int = 10,
    status_: OperationStatus = OperationStatus.READY,
    company_id: int = COMPANY_A,
) -> WorkOrderOperation:
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        sequence=sequence,
        operation_number=f"OP{sequence}",
        name=f"Op {sequence}",
        status=status_,
        quantity_complete=0,
        quantity_scrapped=0,
        company_id=company_id,
    )
    db.add(op)
    db.flush()
    return op


def make_skill(
    db: Session,
    user: User,
    wc: WorkCenter,
    *,
    skill_level: int = 2,
    is_active: bool = True,
    company_id: int = COMPANY_A,
) -> SkillMatrix:
    sm = SkillMatrix(
        user_id=user.id,
        work_center_id=wc.id,
        skill_level=skill_level,
        is_active=is_active,
        company_id=company_id,
    )
    db.add(sm)
    db.flush()
    return sm


def make_cert(
    db: Session,
    user: User,
    cert_type: CertificationType,
    *,
    status_: CertificationStatus = CertificationStatus.ACTIVE,
    expiration_date=None,
    company_id: int = COMPANY_A,
) -> OperatorCertification:
    n = _next()
    cert = OperatorCertification(
        user_id=user.id,
        certification_type=cert_type,
        certification_name=f"Cert {n}",
        status=status_,
        expiration_date=expiration_date,
        company_id=company_id,
    )
    db.add(cert)
    db.flush()
    return cert


def _qualification_audit(db: Session, company_id: int = COMPANY_A) -> list[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(AuditLog.action == "OPERATOR_QUALIFICATION_EXCEPTION", AuditLog.company_id == company_id)
        .all()
    )


def clock_in(client: TestClient, user: User, wo: WorkOrder, op: WorkOrderOperation, wc: WorkCenter):
    return client.post(
        "/api/v1/shop-floor/clock-in",
        json={"work_order_id": wo.id, "operation_id": op.id, "work_center_id": wc.id},
        headers=headers_for(user),
    )


def start_operation(client: TestClient, user: User, op: WorkOrderOperation):
    return client.put(f"/api/v1/shop-floor/operations/{op.id}/start", headers=headers_for(user))


# ---------------------------------------------------------------------------
# Unit: evaluate_operator_qualification (skill leg)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_eval_no_skill_entry_is_not_skill_qualified(db_session: Session):
    user = make_user(db_session)
    wc = make_work_center(db_session)
    db_session.commit()
    exc = evaluate_operator_qualification(db_session, user_id=user.id, work_center_id=wc.id, company_id=COMPANY_A)
    codes = {e.code for e in exc}
    assert "operator_not_skill_qualified" in codes


@pytest.mark.unit
def test_eval_below_basic_skill_is_not_qualified(db_session: Session):
    user = make_user(db_session)
    wc = make_work_center(db_session)
    make_skill(db_session, user, wc, skill_level=1)
    db_session.commit()
    exc = evaluate_operator_qualification(db_session, user_id=user.id, work_center_id=wc.id, company_id=COMPANY_A)
    assert {e.code for e in exc} == {"operator_not_skill_qualified"}


@pytest.mark.unit
def test_eval_basic_skill_clears_skill_leg(db_session: Session):
    user = make_user(db_session)
    wc = make_work_center(db_session)
    make_skill(db_session, user, wc, skill_level=MIN_SKILL_LEVEL)
    db_session.commit()
    exc = evaluate_operator_qualification(db_session, user_id=user.id, work_center_id=wc.id, company_id=COMPANY_A)
    assert exc == []


@pytest.mark.unit
def test_eval_inactive_skill_entry_does_not_qualify(db_session: Session):
    user = make_user(db_session)
    wc = make_work_center(db_session)
    make_skill(db_session, user, wc, skill_level=4, is_active=False)
    db_session.commit()
    exc = evaluate_operator_qualification(db_session, user_id=user.id, work_center_id=wc.id, company_id=COMPANY_A)
    assert {e.code for e in exc} == {"operator_not_skill_qualified"}


# ---------------------------------------------------------------------------
# Unit: evaluate_operator_qualification (cert leg) + _effective_cert_status
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_eval_required_cert_missing(db_session: Session):
    user = make_user(db_session)
    wc = make_work_center(db_session, required_certification_type=CertificationType.WELDING)
    make_skill(db_session, user, wc, skill_level=3)  # skill leg clear, isolate the cert leg
    db_session.commit()
    exc = evaluate_operator_qualification(db_session, user_id=user.id, work_center_id=wc.id, company_id=COMPANY_A)
    assert {e.code for e in exc} == {"operator_certification_missing_or_expired"}


@pytest.mark.unit
def test_eval_required_cert_expired_or_revoked(db_session: Session):
    user = make_user(db_session)
    wc = make_work_center(db_session, required_certification_type=CertificationType.NDT)
    make_skill(db_session, user, wc, skill_level=3)
    make_cert(
        db_session,
        user,
        CertificationType.NDT,
        status_=CertificationStatus.ACTIVE,
        expiration_date=date.today() - timedelta(days=1),  # expired by date
    )
    make_cert(db_session, user, CertificationType.NDT, status_=CertificationStatus.REVOKED)
    db_session.commit()
    exc = evaluate_operator_qualification(db_session, user_id=user.id, work_center_id=wc.id, company_id=COMPANY_A)
    assert {e.code for e in exc} == {"operator_certification_missing_or_expired"}


@pytest.mark.unit
def test_eval_current_cert_clears_cert_leg(db_session: Session):
    user = make_user(db_session)
    wc = make_work_center(db_session, required_certification_type=CertificationType.INSPECTION)
    make_skill(db_session, user, wc, skill_level=3)
    # An active cert expiring well in the future + a no-expiry active cert both qualify.
    make_cert(
        db_session,
        user,
        CertificationType.INSPECTION,
        status_=CertificationStatus.ACTIVE,
        expiration_date=date.today() + timedelta(days=365),
    )
    db_session.commit()
    exc = evaluate_operator_qualification(db_session, user_id=user.id, work_center_id=wc.id, company_id=COMPANY_A)
    assert exc == []


@pytest.mark.unit
def test_eval_expiring_soon_cert_still_qualifies(db_session: Session):
    user = make_user(db_session)
    wc = make_work_center(db_session, required_certification_type=CertificationType.SAFETY)
    make_skill(db_session, user, wc, skill_level=3)
    make_cert(
        db_session,
        user,
        CertificationType.SAFETY,
        status_=CertificationStatus.ACTIVE,
        expiration_date=date.today() + timedelta(days=10),  # within 30 days -> expiring_soon
    )
    db_session.commit()
    exc = evaluate_operator_qualification(db_session, user_id=user.id, work_center_id=wc.id, company_id=COMPANY_A)
    assert exc == []


@pytest.mark.unit
def test_effective_cert_status_matrix(db_session: Session):
    user = make_user(db_session)
    today = date.today()

    revoked = make_cert(db_session, user, CertificationType.OTHER, status_=CertificationStatus.REVOKED)
    pending = make_cert(db_session, user, CertificationType.OTHER, status_=CertificationStatus.PENDING)
    no_expiry = make_cert(
        db_session, user, CertificationType.OTHER, status_=CertificationStatus.ACTIVE, expiration_date=None
    )
    expired = make_cert(
        db_session,
        user,
        CertificationType.OTHER,
        status_=CertificationStatus.ACTIVE,
        expiration_date=today - timedelta(days=1),
    )
    expiring = make_cert(
        db_session,
        user,
        CertificationType.OTHER,
        status_=CertificationStatus.ACTIVE,
        expiration_date=today + timedelta(days=15),
    )
    future = make_cert(
        db_session,
        user,
        CertificationType.OTHER,
        status_=CertificationStatus.ACTIVE,
        expiration_date=today + timedelta(days=400),
    )
    db_session.commit()

    assert _effective_cert_status(revoked) == "revoked"
    assert _effective_cert_status(pending) == "pending"
    assert _effective_cert_status(no_expiry) == "active"
    assert _effective_cert_status(expired) == "expired"
    assert _effective_cert_status(expiring) == "expiring_soon"
    assert _effective_cert_status(future) == "active"


@pytest.mark.unit
def test_eval_tenant_isolation_skill_and_cert(db_session: Session):
    """Skill + cert rows in company B must NOT satisfy the company-A gate."""
    user = make_user(db_session, company_id=COMPANY_A)
    wc = make_work_center(db_session, required_certification_type=CertificationType.WELDING, company_id=COMPANY_A)
    # The qualifying rows exist, but tagged company B.
    make_skill(db_session, user, wc, skill_level=5, company_id=COMPANY_B)
    make_cert(
        db_session,
        user,
        CertificationType.WELDING,
        status_=CertificationStatus.ACTIVE,
        expiration_date=date.today() + timedelta(days=365),
        company_id=COMPANY_B,
    )
    db_session.commit()
    exc = evaluate_operator_qualification(db_session, user_id=user.id, work_center_id=wc.id, company_id=COMPANY_A)
    codes = {e.code for e in exc}
    assert codes == {"operator_not_skill_qualified", "operator_certification_missing_or_expired"}


# ---------------------------------------------------------------------------
# (a) clock-in with NO skill entry warns + still creates the TimeEntry
# ---------------------------------------------------------------------------


def test_clock_in_unqualified_warns_not_blocks(client: TestClient, db_session: Session):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    part = make_part(db_session)
    wo = make_wo(db_session, part)
    wc = make_work_center(db_session)  # no required cert
    op = make_op(db_session, wo, wc, sequence=10)
    db_session.commit()

    resp = clock_in(client, operator, wo, op, wc)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    codes = [e["code"] for e in body["qualification_exceptions"]]
    assert "operator_not_skill_qualified" in codes
    # The TimeEntry was still created (the clock-in is not blocked).
    assert body["id"] is not None and body["operation_id"] == op.id

    audit = _qualification_audit(db_session)
    assert any(
        "operator_not_skill_qualified" in (a.new_values or {}).get("qualification_exceptions", []) for a in audit
    )
    events = (
        db_session.query(OperationalEvent)
        .filter(OperationalEvent.event_type == "operator_qualification_exception")
        .all()
    )
    assert events and events[0].severity == "warning"


def test_clock_in_qualified_no_exceptions(client: TestClient, db_session: Session):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    part = make_part(db_session)
    wo = make_wo(db_session, part)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10)
    make_skill(db_session, operator, wc, skill_level=MIN_SKILL_LEVEL)
    db_session.commit()

    resp = clock_in(client, operator, wo, op, wc)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["qualification_exceptions"] == []
    assert _qualification_audit(db_session) == []


def test_clock_in_below_basic_skill_warns(client: TestClient, db_session: Session):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    part = make_part(db_session)
    wo = make_wo(db_session, part)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10)
    make_skill(db_session, operator, wc, skill_level=1)
    db_session.commit()

    resp = clock_in(client, operator, wo, op, wc)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    codes = [e["code"] for e in resp.json()["qualification_exceptions"]]
    assert codes == ["operator_not_skill_qualified"]


# ---------------------------------------------------------------------------
# (c) certification leg on the live path
# ---------------------------------------------------------------------------


def test_clock_in_missing_required_cert_warns(client: TestClient, db_session: Session):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    part = make_part(db_session)
    wo = make_wo(db_session, part)
    wc = make_work_center(db_session, required_certification_type=CertificationType.WELDING)
    op = make_op(db_session, wo, wc, sequence=10)
    make_skill(db_session, operator, wc, skill_level=4)  # skill clear; only the cert leg fails
    db_session.commit()

    resp = clock_in(client, operator, wo, op, wc)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    codes = [e["code"] for e in resp.json()["qualification_exceptions"]]
    assert codes == ["operator_certification_missing_or_expired"]


def test_clock_in_with_current_cert_and_skill_clears(client: TestClient, db_session: Session):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    part = make_part(db_session)
    wo = make_wo(db_session, part)
    wc = make_work_center(db_session, required_certification_type=CertificationType.CNC_OPERATION)
    op = make_op(db_session, wo, wc, sequence=10)
    make_skill(db_session, operator, wc, skill_level=3)
    make_cert(
        db_session,
        operator,
        CertificationType.CNC_OPERATION,
        status_=CertificationStatus.ACTIVE,
        expiration_date=date.today() + timedelta(days=200),
    )
    db_session.commit()

    resp = clock_in(client, operator, wo, op, wc)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["qualification_exceptions"] == []


# ---------------------------------------------------------------------------
# (a) start_operation path also evaluates the gate
# ---------------------------------------------------------------------------


def test_start_operation_unqualified_warns_not_blocks(client: TestClient, db_session: Session):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    part = make_part(db_session)
    wo = make_wo(db_session, part)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.READY)
    db_session.commit()

    resp = start_operation(client, operator, op)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    codes = [e["code"] for e in body["qualification_exceptions"]]
    assert "operator_not_skill_qualified" in codes
    # Start succeeded -- the operation moved to IN_PROGRESS.
    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op.id).status == OperationStatus.IN_PROGRESS
    assert _qualification_audit(db_session)


def test_start_operation_qualified_no_exceptions(client: TestClient, db_session: Session):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    part = make_part(db_session)
    wo = make_wo(db_session, part)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.READY)
    make_skill(db_session, operator, wc, skill_level=3)
    db_session.commit()

    resp = start_operation(client, operator, op)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["qualification_exceptions"] == []


# ---------------------------------------------------------------------------
# (d) the NEW evaluator is company-scoped; the LEGACY endpoint is NOT
# ---------------------------------------------------------------------------


def test_legacy_check_endpoint_and_new_evaluator_are_both_company_scoped(client: TestClient, db_session: Session):
    """Regression lock (fix/wo-remediation-followups): a SkillMatrix row tagged company B is
    invisible to BOTH the legacy ``/skill-matrix/check`` endpoint (now company-scoped) and the
    new ``evaluate_operator_qualification`` when the caller is in company A. Before the
    tenant-scoping fix the legacy endpoint leaked the company-B row as ``qualified=True``; it is
    now scoped, so it agrees with the evaluator and both deny qualification cross-tenant."""
    admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
    operator = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    wc = make_work_center(db_session, company_id=COMPANY_A)
    # A qualifying skill row tagged COMPANY_B for the same (user, work_center).
    make_skill(db_session, operator, wc, skill_level=4, company_id=COMPANY_B)
    db_session.commit()

    # Legacy endpoint: NOW company-scoped -> the company-B row is invisible -> not qualified.
    legacy = client.get(
        f"/api/v1/certifications/skill-matrix/check/{operator.id}/{wc.id}",
        headers=headers_for(admin_a),
    )
    assert legacy.status_code == status.HTTP_200_OK, legacy.text
    assert legacy.json()["qualified"] is False

    # New evaluator: company-scoped -> the company-B row is invisible -> not qualified.
    exc = evaluate_operator_qualification(db_session, user_id=operator.id, work_center_id=wc.id, company_id=COMPANY_A)
    assert {e.code for e in exc} == {"operator_not_skill_qualified"}
