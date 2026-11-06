"""Eight tenant business keys are unique PER COMPANY (migration 116), seen from the API.

Until ``116_business_keys_per_company`` each of these columns carried a GLOBAL unique
index, so one tenant's value blocked every other tenant from using it:

- ``ncrs.ncr_number`` / ``cars.car_number`` are numbered per company by
  ``services/quality_numbering.py`` (``NCR-YYYYMMDD-NNN`` / ``CAR-YYYYMMDD-NNN``), so a
  second company's first NCR or CAR of the day drew the "-001" the first company already
  held and ``POST /quality/ncr`` / ``POST /quality/car`` failed on the insert (a 500);
- ``equipment.equipment_id``, ``tools.tool_id`` and ``downtime_reason_codes.code`` are
  user-entered, and the create routes' duplicate probe is already tenant-scoped, so the
  probe missed another company's identical value and the insert failed instead.

Each is now a ``(company_id, <column>)`` unique constraint. These tests hold both halves
of that at the API (status code + persisted row): another company's value no longer
blocks a create, and a SAME-company duplicate is still refused, by the route's existing
400 and by the constraint itself.

Covered elsewhere, not repeated here: the migration (``tests/test_migration_116_*``),
complaint / RMA numbering and the complaint escalation verbs' NCR / CAR
(``test_customer_complaints_tenant_isolation.py``), FAI numbering
(``test_fai_access_controls.py``), and the quality-metrics NCR count across tenants
(``test_soft_delete_read_sweep.py``).
"""

from datetime import datetime
from itertools import count

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.calibration import Equipment
from app.models.customer_complaint import CustomerComplaint, ReturnMaterialAuthorization
from app.models.downtime import DowntimeCategory, DowntimeReasonCode
from app.models.part import Part
from app.models.quality import CorrectiveActionRequest, FirstArticleInspection, NCRSource, NonConformanceReport
from app.models.tool_management import Tool
from app.models.user import UserRole
from tests.api.kiosk_test_helpers import COMPANY_A, COMPANY_B, ensure_company, make_user, user_headers

pytestmark = [pytest.mark.api, pytest.mark.requires_db]


def probe_misses_once(monkeypatch, module, model) -> None:
    """Make ``module``'s next tenant-scoped duplicate probe on ``model`` see nothing.

    Simulates the race the per-company constraint backstops: a same-company create of
    the same code commits between the router's probe and its flush. The probe passes,
    the flush hits ``uq_<table>_company_<col>``, and the router's ``IntegrityError``
    handler must answer 400 and leave the request session usable. Since migration 116
    nothing else reaches that handler -- another tenant's identical code is accepted.
    """
    real = module.tenant_query
    state = {"missed": False}

    def tenant_query(db, target, company_id):
        query = real(db, target, company_id)
        if target is model and not state["missed"]:
            state["missed"] = True
            return query.filter(sa.false())
        return query

    monkeypatch.setattr(module, "tenant_query", tenant_query)


# ---------------------------------------------------------------- quality numbering (NCR / CAR)

# kind -> (create URL, model, number column, number prefix, valid create body)
QUALITY_CREATES = {
    "ncr": (
        "/api/v1/quality/ncr",
        NonConformanceReport,
        "ncr_number",
        "NCR",
        {
            "source": "in_process",
            "title": "Burr on bore edge",
            "description": "Bore edge carries a burr outside the drawing's break-edge note.",
        },
    ),
    "car": (
        "/api/v1/quality/car",
        CorrectiveActionRequest,
        "car_number",
        "CAR",
        {
            "title": "Recurring bore-edge burrs",
            "problem_description": "Three lots this month shipped bore-edge burrs past final inspection.",
        },
    ),
}


def _numbers(db: Session, model, column: str, company_id: int) -> list:
    rows = db.query(model).filter(model.company_id == company_id).order_by(getattr(model, column)).all()
    return [getattr(row, column) for row in rows]


@pytest.mark.parametrize("kind", sorted(QUALITY_CREATES))
def test_quality_record_numbering_is_its_own_sequence_per_company(client: TestClient, db_session: Session, kind: str):
    """Company B already holds today's -001 and -002; company A's creates draw -001, -002.

    A global scan would hand A "-003"; a global unique refused A's "-001" outright.
    """
    url, model, column, prefix, body = QUALITY_CREATES[kind]
    today = datetime.now().strftime("%Y%m%d")
    headers_a = user_headers(make_user(db_session, company_id=COMPANY_A, role=UserRole.QUALITY))
    headers_b = user_headers(make_user(db_session, company_id=COMPANY_B, role=UserRole.QUALITY))

    for _ in range(2):
        held = client.post(url, json=body, headers=headers_b)
        assert held.status_code == 200, held.text

    first = client.post(url, json=body, headers=headers_a)
    second = client.post(url, json=body, headers=headers_a)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert [first.json()[column], second.json()[column]] == [f"{prefix}-{today}-001", f"{prefix}-{today}-002"]
    assert _numbers(db_session, model, column, COMPANY_A) == [f"{prefix}-{today}-001", f"{prefix}-{today}-002"]
    # Company B's records keep the same two numbers; nothing of A's landed in B.
    assert _numbers(db_session, model, column, COMPANY_B) == [f"{prefix}-{today}-001", f"{prefix}-{today}-002"]


def test_a_voided_ncr_keeps_its_number_and_is_never_reissued(client: TestClient, db_session: Session):
    """The NCR scan deliberately sees tombstones (invariant 3's per-site exception).

    uq_ncrs_company_ncr_number has no partial predicate, so a voided NCR keeps its number
    for good: if a future sweep added ``is_deleted == False`` to the scan, the next create
    would re-mint the voided "-001" and fail on the constraint.
    """
    url, _, column, prefix, body = QUALITY_CREATES["ncr"]
    today = datetime.now().strftime("%Y%m%d")
    user = make_user(db_session, company_id=COMPANY_A, role=UserRole.QUALITY)
    headers = user_headers(user)

    voided = client.post(url, json=body, headers=headers)
    assert voided.status_code == 200, voided.text
    record = db_session.get(NonConformanceReport, voided.json()["id"])
    record.soft_delete(user.id)
    db_session.commit()

    response = client.post(url, json=body, headers=headers)

    assert response.status_code == 200, response.text
    assert [voided.json()[column], response.json()[column]] == [f"{prefix}-{today}-001", f"{prefix}-{today}-002"]


# ---------------------------------------------------------------- user-entered identifiers

# kind -> (create URL, model, key column, existing duplicate detail, body builder)
# MANAGER is inside every one of these create routes' write gates (calibration:
# Admin/Manager/Quality; tools and downtime reason codes: Admin/Manager/Supervisor).
CATALOG_CREATES = {
    "equipment": (
        "/api/v1/calibration/equipment",
        Equipment,
        "equipment_id",
        "Equipment ID already exists",
        lambda key, name: {"equipment_id": key, "name": name},
    ),
    "tool": (
        "/api/v1/tool-management/tools/",
        Tool,
        "tool_id",
        "Tool ID already exists",
        lambda key, name: {"tool_id": key, "name": name},
    ),
    "downtime_reason_code": (
        "/api/v1/downtime/reason-codes",
        DowntimeReasonCode,
        "code",
        "Reason code already exists",
        lambda key, name: {"code": key, "name": name, "category": "mechanical"},
    ),
}
SHARED_KEY = "SHARED-0042"


def _rows_with_key(db: Session, model, column: str, key: str) -> list:
    return db.query(model).filter(getattr(model, column) == key).order_by(model.company_id).all()


@pytest.mark.parametrize("kind", sorted(CATALOG_CREATES))
def test_an_identifier_another_company_uses_can_be_created(client: TestClient, db_session: Session, kind: str):
    url, model, column, _detail, body = CATALOG_CREATES[kind]
    headers_a = user_headers(make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER))
    headers_b = user_headers(make_user(db_session, company_id=COMPANY_B, role=UserRole.MANAGER))
    theirs = client.post(url, json=body(SHARED_KEY, "Company B's"), headers=headers_b)
    assert theirs.status_code == 200, theirs.text

    ours = client.post(url, json=body(SHARED_KEY, "Company A's"), headers=headers_a)

    assert ours.status_code == 200, ours.text
    assert ours.json()[column] == SHARED_KEY
    rows = _rows_with_key(db_session, model, column, SHARED_KEY)
    assert [(row.company_id, row.name) for row in rows] == [(COMPANY_A, "Company A's"), (COMPANY_B, "Company B's")]


@pytest.mark.parametrize("kind", sorted(CATALOG_CREATES))
def test_a_same_company_duplicate_identifier_is_still_refused(client: TestClient, db_session: Session, kind: str):
    url, model, column, detail, body = CATALOG_CREATES[kind]
    headers_a = user_headers(make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER))
    original = client.post(url, json=body(SHARED_KEY, "Original"), headers=headers_a)
    assert original.status_code == 200, original.text

    duplicate = client.post(url, json=body(SHARED_KEY, "Duplicate"), headers=headers_a)

    assert duplicate.status_code == 400, duplicate.text
    assert duplicate.json()["detail"] == detail
    rows = _rows_with_key(db_session, model, column, SHARED_KEY)
    assert [(row.company_id, row.name) for row in rows] == [(COMPANY_A, "Original")]


# ---------------------------------------------------------------- the constraint itself


_part_numbers = count(1)


def _part(db: Session, company_id: int) -> Part:
    part = Part(
        part_number=f"BK-PART-{next(_part_numbers):04d}",
        name="Business-key part",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()
    return part


# table -> (model, key column, minimal valid row builder(db, company_id, key))
CONSTRAINED_MODELS = {
    "ncrs": (
        NonConformanceReport,
        "ncr_number",
        lambda db, company_id, key: NonConformanceReport(
            company_id=company_id,
            ncr_number=key,
            source=NCRSource.IN_PROCESS,
            title="Constraint NCR",
            description="Constraint NCR description",
        ),
    ),
    "cars": (
        CorrectiveActionRequest,
        "car_number",
        lambda db, company_id, key: CorrectiveActionRequest(
            company_id=company_id, car_number=key, title="Constraint CAR", problem_description="Constraint CAR"
        ),
    ),
    "fais": (
        FirstArticleInspection,
        "fai_number",
        lambda db, company_id, key: FirstArticleInspection(
            company_id=company_id, fai_number=key, part_id=_part(db, company_id).id
        ),
    ),
    "customer_complaints": (
        CustomerComplaint,
        "complaint_number",
        lambda db, company_id, key: CustomerComplaint(
            company_id=company_id,
            complaint_number=key,
            customer_name="Acme",
            title="Constraint complaint",
            description="Constraint complaint",
        ),
    ),
    "return_material_authorizations": (
        ReturnMaterialAuthorization,
        "rma_number",
        lambda db, company_id, key: ReturnMaterialAuthorization(
            company_id=company_id, rma_number=key, customer_name="Acme", quantity=1, reason="Constraint RMA"
        ),
    ),
    "equipment": (
        Equipment,
        "equipment_id",
        lambda db, company_id, key: Equipment(company_id=company_id, equipment_id=key, name="Constraint gauge"),
    ),
    "tools": (
        Tool,
        "tool_id",
        lambda db, company_id, key: Tool(company_id=company_id, tool_id=key, name="Constraint tool"),
    ),
    "downtime_reason_codes": (
        DowntimeReasonCode,
        "code",
        lambda db, company_id, key: DowntimeReasonCode(
            company_id=company_id, code=key, name="Constraint code", category=DowntimeCategory.MECHANICAL
        ),
    ),
}


@pytest.mark.parametrize("table", list(CONSTRAINED_MODELS))
def test_the_per_company_constraint_still_binds_within_a_company(db_session: Session, table: str):
    """Two companies may hold the same key; the same company may not, at the database.

    The routes' duplicate probe is a courtesy (a race slips past it); the constraint is
    what actually refuses a second ``(company_id, key)``. The refusal must name the
    tenant-scoped key, not a lingering single-column one.
    """
    model, column, build = CONSTRAINED_MODELS[table]
    ensure_company(db_session, COMPANY_A)
    ensure_company(db_session, COMPANY_B)
    key = "BK-0001"
    db_session.add(build(db_session, COMPANY_A, key))
    db_session.add(build(db_session, COMPANY_B, key))
    db_session.commit()  # the cross-company pair is accepted

    db_session.add(build(db_session, COMPANY_A, key))
    with pytest.raises(IntegrityError) as excinfo:
        db_session.flush()
    db_session.rollback()

    message = str(excinfo.value.orig)
    constraint = f"uq_{table}_company_{column}"
    sqlite_columns = f"{table}.company_id, {table}.{column}"
    assert constraint in message or sqlite_columns in message, message
    assert sorted(row.company_id for row in _rows_with_key(db_session, model, column, key)) == [COMPANY_A, COMPANY_B]
