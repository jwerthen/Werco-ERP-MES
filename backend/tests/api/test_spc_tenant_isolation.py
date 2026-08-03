"""Every SPC endpoint is scoped to the caller's company, and the three writes stamp it.

``api/endpoints/spc.py`` had two defects that share a single root cause -- the router never
asked which company was calling:

**A. Three write endpoints were 500ing in production.** ``SPCMeasurement``,
``SPCControlLimit`` and ``SPCProcessCapability`` all carry ``TenantMixin.company_id``
(NOT NULL, and migration 026 explicitly drops the interim ``server_default``), but
``add_measurements`` / ``calculate_control_limits`` / ``run_capability_study`` constructed
them without it, so every call raised NotNullViolation. Nothing in the suite touched those
three handlers, which is exactly why it was never caught -- so §1 and §3 below are written
as reproducers first and regression pins second: they fail with an IntegrityError against
the old code, and they assert the STORED ``company_id``, not the response, because the
response schemas do not expose it.

**B. Ten endpoints returned or wrote other tenants' data.** Eight were keyed on a
``characteristic_id`` that was resolved with no company filter, so guessing an integer was
enough to read another company's raw measurements, control limits and Cp/Cpk. The two
aggregates -- ``GET /spc/out-of-control`` and ``GET /spc/dashboard`` -- took no company
argument at all and returned platform-wide numbers to any authenticated user.

Three things shaped these tests
-------------------------------
* **The ownership check belongs on the CHARACTERISTIC, not on the leaf rows.** Filtering
  measurements by ``company_id`` alone would make a foreign id return an empty-but-200
  chart instead of refusing. Every §2 case therefore asserts a 404, and
  ``test_..._exactly_like_an_absent_one`` pins that the refusal is byte-identical to a
  genuinely missing id -- otherwise the status code itself is an existence oracle.

* **Two predicates are only reachable through a MIS-TENANTED row**, and both are silent
  partial-fix traps that a naive test cannot catch. ``calculate_control_limits`` issues two
  bulk UPDATEs keyed on ``characteristic_id``; ``get_chart_data`` and ``get_dashboard`` each
  pick a window / max-date in an INNER subquery and filter in the OUTER query. A row whose
  ``company_id`` disagrees with its characteristic's is constructible (``company_id`` is not
  part of any FK, on SQLite or on Postgres) and is precisely the corrupt state the predicate
  has to survive, so §3 and §4 build one deliberately.

* **The positive control is not decoration.** Every refusal test here also passes against an
  endpoint that refuses everything, so each group keeps a same-company case: the write
  succeeds, the read returns the tenant's own rows, and ``GET /control-limits`` still
  answers 200/``null`` for an owned characteristic that simply has no limits yet.
"""

from datetime import datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.part import Part
from app.models.spc import SPCCharacteristic, SPCControlLimit, SPCMeasurement, SPCProcessCapability
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1  # the seeded company -- plays the victim throughout
COMPANY_B = 2  # the caller that must never reach A's data

TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"

BASE_TS = datetime(2026, 7, 1, 12, 0, 0)

# Sentinels distinctive enough that a substring search over a whole response body is a real
# leak check. On this system a characteristic name and a lot number are customer-identifying
# quality data, which is the disclosure that matters.
FOREIGN_CHAR_NAME = "ACME-SECRET-77821 Bore Diameter"
FOREIGN_LOT = "LOT-ACME-CLASSIFIED-4417"
FOREIGN_PART_NUMBER = "ACME-SECRET-PN-77821"

MEASUREMENTS_URL = "/api/v1/spc/measurements"
CHARACTERISTICS_URL = "/api/v1/spc/characteristics"

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
        email=f"spc-iso-{n}@co{company_id}.test",
        employee_id=f"SPCISO-{n:05d}",
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


def make_part(db: Session, *, company_id: int, part_number: str = None) -> Part:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=part_number or f"SPC-ISO-P-{n}",
        name=f"Isolation part {n}",
        description="spc tenant-isolation fixture part",
        part_type="manufactured",
        unit_of_measure="each",
        standard_cost=5.0,
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_work_center(db: Session, *, company_id: int) -> WorkCenter:
    _ensure_company(db, company_id)
    n = _next()
    work_center = WorkCenter(
        code=f"WC-ISO-{n}",
        name=f"Isolation work center {n}",
        work_center_type="milling",
        is_active=True,
        company_id=company_id,
    )
    db.add(work_center)
    db.commit()
    db.refresh(work_center)
    return work_center


def make_work_order(db: Session, *, company_id: int) -> WorkOrder:
    _ensure_company(db, company_id)
    n = _next()
    work_order = WorkOrder(
        work_order_number=f"WO-ISO-{n}",
        part_id=make_part(db, company_id=company_id).id,
        quantity_ordered=10,
        status="draft",
        priority=2,
        company_id=company_id,
    )
    db.add(work_order)
    db.commit()
    db.refresh(work_order)
    return work_order


def make_characteristic(db: Session, *, company_id: int, part_id: int = None, **overrides) -> SPCCharacteristic:
    """A characteristic. ``part_id`` defaults to a fresh part of the SAME company."""
    if part_id is None:
        part_id = make_part(db, company_id=company_id).id
    fields = {
        "company_id": company_id,
        "name": f"Characteristic {_next()}",
        "part_id": part_id,
        "characteristic_type": "dimensional",
        "unit_of_measure": "mm",
        "specification_nominal": 10.0,
        "specification_usl": 10.5,
        "specification_lsl": 9.5,
        "subgroup_size": 3,
        "is_active": True,
    }
    fields.update(overrides)
    characteristic = SPCCharacteristic(**fields)
    db.add(characteristic)
    db.commit()
    db.refresh(characteristic)
    return characteristic


def _value(subgroup_number: int, sample_number: int) -> float:
    return 10.0 + (subgroup_number % 7) * 0.01 + sample_number * 0.001


def seed_subgroups(
    db: Session,
    characteristic: SPCCharacteristic,
    subgroup_numbers,
    *,
    company_id: int = None,
    samples_per_subgroup: int = 3,
    ooc_subgroups=(),
    lot_number: str = None,
) -> None:
    """Seed measurements.

    ``company_id`` defaults to the characteristic's own. Passing a DIFFERENT one builds the
    mis-tenanted row the bulk-UPDATE and subquery predicates have to survive.
    """
    owner = characteristic.company_id if company_id is None else company_id
    for position, subgroup_number in enumerate(subgroup_numbers):
        for sample_number in range(1, samples_per_subgroup + 1):
            db.add(
                SPCMeasurement(
                    company_id=owner,
                    characteristic_id=characteristic.id,
                    subgroup_number=subgroup_number,
                    sample_number=sample_number,
                    measurement_value=_value(subgroup_number, sample_number),
                    measured_at=BASE_TS + timedelta(minutes=position * 10 + sample_number),
                    is_out_of_control=subgroup_number in ooc_subgroups,
                    lot_number=lot_number,
                )
            )
    db.commit()


def make_control_limit(
    db: Session, characteristic: SPCCharacteristic, *, company_id: int = None, is_current: bool = True, **overrides
) -> SPCControlLimit:
    fields = {
        "company_id": characteristic.company_id if company_id is None else company_id,
        "characteristic_id": characteristic.id,
        "ucl": 10.4,
        "lcl": 9.6,
        "center_line": 10.0,
        "ucl_range": 0.2,
        "lcl_range": 0.0,
        "center_line_range": 0.1,
        "sample_count": 25,
        "is_current": is_current,
    }
    fields.update(overrides)
    control_limit = SPCControlLimit(**fields)
    db.add(control_limit)
    db.commit()
    db.refresh(control_limit)
    return control_limit


def make_capability(
    db: Session, characteristic: SPCCharacteristic, *, company_id: int = None, cpk: float = 1.5, study_date=None
) -> SPCProcessCapability:
    capability = SPCProcessCapability(
        company_id=characteristic.company_id if company_id is None else company_id,
        characteristic_id=characteristic.id,
        study_date=study_date or BASE_TS,
        sample_count=30,
        mean=10.0,
        std_dev=0.1,
        cp=cpk,
        cpk=cpk,
        pp=cpk,
        ppk=cpk,
        within_spec_pct=99.0,
        is_capable=cpk >= 1.33,
    )
    db.add(capability)
    db.commit()
    db.refresh(capability)
    return capability


def foreign_characteristic(db: Session) -> SPCCharacteristic:
    """Company A's characteristic -- the one Company B must never read, write or name."""
    part = make_part(db, company_id=COMPANY_A, part_number=FOREIGN_PART_NUMBER)
    return make_characteristic(db, company_id=COMPANY_A, part_id=part.id, name=FOREIGN_CHAR_NAME)


def assert_discloses_nothing(response) -> None:
    """No sentinel may appear ANYWHERE in the body -- detail, echo or field."""
    body = response.text
    assert FOREIGN_CHAR_NAME not in body, f"leaked the foreign characteristic name: {body}"
    assert FOREIGN_LOT not in body, f"leaked the foreign lot number: {body}"
    assert FOREIGN_PART_NUMBER not in body, f"leaked the foreign part number: {body}"


def fresh(db: Session, model, pk: int):
    """The row as COMMITTED, not as the shared session last remembered it."""
    db.expire_all()
    return db.get(model, pk)


def measurement_payload(characteristic_id: int, *, subgroup: int = 1, samples: int = 3, lot: str = None) -> dict:
    return {
        "measurements": [
            {
                "characteristic_id": characteristic_id,
                "subgroup_number": subgroup,
                "measurement_value": 10.0 + s * 0.01,
                "sample_number": s,
                "lot_number": lot,
            }
            for s in range(1, samples + 1)
        ]
    }


# ===========================================================================
# 1. POST /spc/measurements — the NOT NULL 500, and the cross-tenant write
# ===========================================================================


def test_add_measurements_succeeds_and_stamps_the_callers_company_id(client: TestClient, db_session: Session):
    """THE production-bug reproducer.

    ``spc_measurements.company_id`` is NOT NULL with no server default, and the handler
    never set it -- so this call raised NotNullViolation for every user of the SPC page.
    The stored row is asserted rather than the response because ``MeasurementResponse``
    does not expose ``company_id``: a 200 alone would not prove the scope was written.
    """
    user_b = make_user(db_session, company_id=COMPANY_B)
    char_b = make_characteristic(db_session, company_id=COMPANY_B)

    response = client.post(MEASUREMENTS_URL, headers=headers_for(user_b), json=measurement_payload(char_b.id))

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert len(body) == 3
    assert {row["characteristic_id"] for row in body} == {char_b.id}

    db_session.expire_all()
    stored = db_session.query(SPCMeasurement).filter(SPCMeasurement.characteristic_id == char_b.id).all()
    assert len(stored) == 3
    assert {m.company_id for m in stored} == {COMPANY_B}, "the write must stamp the CALLER's company"
    assert {m.measured_by for m in stored} == {user_b.id}


def test_add_measurements_refuses_another_companys_characteristic(client: TestClient, db_session: Session):
    """404 rather than 403: a foreign id must be indistinguishable from an absent one."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    stolen = foreign_characteristic(db_session)

    response = client.post(
        MEASUREMENTS_URL, headers=headers_for(user_b), json=measurement_payload(stolen.id, lot=FOREIGN_LOT)
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert_discloses_nothing(response)
    db_session.expire_all()
    assert db_session.query(SPCMeasurement).filter(SPCMeasurement.characteristic_id == stolen.id).count() == 0


def test_add_measurements_refuses_a_mixed_batch_whole(client: TestClient, db_session: Session):
    """A batch may name several characteristics, so EVERY distinct id is validated before
    the first ``db.add``. A batch that is half legitimate must leave nothing behind."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    char_b = make_characteristic(db_session, company_id=COMPANY_B)
    stolen = foreign_characteristic(db_session)

    payload = {
        "measurements": (
            measurement_payload(char_b.id, subgroup=1, samples=2)["measurements"]
            + measurement_payload(stolen.id, subgroup=1, samples=2, lot=FOREIGN_LOT)["measurements"]
        )
    }
    response = client.post(MEASUREMENTS_URL, headers=headers_for(user_b), json=payload)

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert_discloses_nothing(response)
    db_session.expire_all()
    assert db_session.query(SPCMeasurement).count() == 0, "a refused batch must not half-write its legitimate half"


def test_add_measurements_refuses_a_foreign_id_exactly_like_an_absent_one(client: TestClient, db_session: Session):
    user_b = make_user(db_session, company_id=COMPANY_B)
    stolen = foreign_characteristic(db_session)
    headers = headers_for(user_b)

    foreign = client.post(MEASUREMENTS_URL, headers=headers, json=measurement_payload(stolen.id))
    absent = client.post(MEASUREMENTS_URL, headers=headers, json=measurement_payload(stolen.id + 900_000))

    assert (foreign.status_code, foreign.json()) == (absent.status_code, absent.json())


# ===========================================================================
# 2. The characteristic-keyed reads
# ===========================================================================


def test_get_measurements_refuses_a_foreign_characteristic(client: TestClient, db_session: Session):
    """Raw measurement values, lot and serial numbers were readable by guessing an int."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    stolen = foreign_characteristic(db_session)
    seed_subgroups(db_session, stolen, range(1, 4), lot_number=FOREIGN_LOT)

    response = client.get(f"{MEASUREMENTS_URL}/{stolen.id}", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert_discloses_nothing(response)


def test_get_measurements_returns_the_callers_own_rows(client: TestClient, db_session: Session):
    """Positive control: scoping narrows WHICH rows are readable, it does not switch the
    endpoint off. Company A's rows are present the whole time and stay invisible."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    char_b = make_characteristic(db_session, company_id=COMPANY_B)
    seed_subgroups(db_session, char_b, range(1, 4), samples_per_subgroup=2)
    stolen = foreign_characteristic(db_session)
    seed_subgroups(db_session, stolen, range(1, 4), lot_number=FOREIGN_LOT)

    response = client.get(f"{MEASUREMENTS_URL}/{char_b.id}", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert len(body) == 6
    assert {row["characteristic_id"] for row in body} == {char_b.id}
    assert_discloses_nothing(response)


def test_chart_data_refuses_a_foreign_characteristic(client: TestClient, db_session: Session):
    user_b = make_user(db_session, company_id=COMPANY_B)
    stolen = foreign_characteristic(db_session)
    seed_subgroups(db_session, stolen, range(1, 6))
    make_control_limit(db_session, stolen)

    response = client.get(f"/api/v1/spc/chart-data/{stolen.id}", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert_discloses_nothing(response)


def test_chart_data_window_cannot_be_steered_by_a_foreign_row(client: TestClient, db_session: Session):
    """The INNER-subquery trap, and the reason scoping only the outer query is a silent
    partial fix.

    ``get_chart_data`` resolves the last-N distinct subgroup numbers in a subquery, then
    fetches those subgroups. With the predicate on the outer fetch alone, a mis-tenanted row
    carrying a high ``subgroup_number`` wins the window and the scoped outer query then
    matches nothing -- Company B's own chart silently renders EMPTY. Asserted with N=1 so
    exactly one subgroup can win.
    """
    user_b = make_user(db_session, company_id=COMPANY_B)
    char_b = make_characteristic(db_session, company_id=COMPANY_B)
    seed_subgroups(db_session, char_b, [1, 2, 3], samples_per_subgroup=2)
    # A row on B's characteristic that belongs to A (company_id is part of no FK, so nothing
    # in the schema prevents this state on SQLite or on Postgres).
    seed_subgroups(db_session, char_b, [999], company_id=COMPANY_A, samples_per_subgroup=2)

    response = client.get(
        f"/api/v1/spc/chart-data/{char_b.id}", headers=headers_for(user_b), params={"last_n_subgroups": 1}
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    points = response.json()["chart_points"]
    assert [p["subgroup_number"] for p in points] == [3], "the foreign row chose the window"


def test_get_control_limits_refuses_a_foreign_characteristic(client: TestClient, db_session: Session):
    user_b = make_user(db_session, company_id=COMPANY_B)
    stolen = foreign_characteristic(db_session)
    make_control_limit(db_session, stolen, ucl=42.4242, lcl=-42.4242)

    response = client.get(f"/api/v1/spc/control-limits/{stolen.id}", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert "42.4242" not in response.text, "leaked another company's control limits"
    assert_discloses_nothing(response)


def test_get_control_limits_still_answers_200_null_when_the_owned_characteristic_has_none(
    client: TestClient, db_session: Session
):
    """Shape preservation. The ownership check must not convert the established
    "no limits calculated yet" 200/``null`` into a 404 for the caller's OWN characteristic --
    the SPC chart depends on telling those two states apart."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    char_b = make_characteristic(db_session, company_id=COMPANY_B)

    response = client.get(f"/api/v1/spc/control-limits/{char_b.id}", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json() is None


def test_get_control_limits_returns_the_callers_own(client: TestClient, db_session: Session):
    user_b = make_user(db_session, company_id=COMPANY_B)
    char_b = make_characteristic(db_session, company_id=COMPANY_B)
    make_control_limit(db_session, char_b)

    response = client.get(f"/api/v1/spc/control-limits/{char_b.id}", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["ucl"] == 10.4


def test_get_capability_refuses_a_foreign_characteristic(client: TestClient, db_session: Session):
    """Cp/Cpk is the tenant's process performance -- a competitive disclosure on its own."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    stolen = foreign_characteristic(db_session)
    make_capability(db_session, stolen, cpk=0.4242)

    response = client.get(f"/api/v1/spc/capability/{stolen.id}", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert "0.4242" not in response.text
    assert_discloses_nothing(response)


def test_get_capability_returns_the_callers_own(client: TestClient, db_session: Session):
    user_b = make_user(db_session, company_id=COMPANY_B)
    char_b = make_characteristic(db_session, company_id=COMPANY_B)
    make_capability(db_session, char_b, cpk=1.77)

    response = client.get(f"/api/v1/spc/capability/{char_b.id}", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["cpk"] == 1.77


def test_check_violations_refuses_a_foreign_characteristic(client: TestClient, db_session: Session):
    """This response echoed the characteristic NAME plus UCL/LCL/center-line -- a full
    process disclosure for any id the caller could guess."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    stolen = foreign_characteristic(db_session)
    seed_subgroups(db_session, stolen, range(1, 6))
    make_control_limit(db_session, stolen, ucl=42.4242)

    response = client.get(f"/api/v1/spc/violations/{stolen.id}", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert "42.4242" not in response.text
    assert_discloses_nothing(response)


def test_check_violations_returns_the_callers_own(client: TestClient, db_session: Session):
    user_b = make_user(db_session, company_id=COMPANY_B)
    char_b = make_characteristic(db_session, company_id=COMPANY_B, name="B bore diameter")
    seed_subgroups(db_session, char_b, range(1, 6), samples_per_subgroup=2)
    make_control_limit(db_session, char_b)

    response = client.get(f"/api/v1/spc/violations/{char_b.id}", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["characteristic_id"] == char_b.id
    assert body["characteristic_name"] == "B bore diameter"
    assert body["total_subgroups"] == 5


def test_every_characteristic_keyed_endpoint_refuses_a_foreign_id(client: TestClient, db_session: Session):
    """Sweep: no per-endpoint exception survives, and none of them leaks the name."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    stolen = foreign_characteristic(db_session)
    seed_subgroups(db_session, stolen, range(1, 6), lot_number=FOREIGN_LOT)
    make_control_limit(db_session, stolen)
    make_capability(db_session, stolen)
    headers = headers_for(user_b)

    gets = [
        f"{MEASUREMENTS_URL}/{stolen.id}",
        f"/api/v1/spc/chart-data/{stolen.id}",
        f"/api/v1/spc/control-limits/{stolen.id}",
        f"/api/v1/spc/capability/{stolen.id}",
        f"/api/v1/spc/violations/{stolen.id}",
        f"{CHARACTERISTICS_URL}/{stolen.id}",
    ]
    for url in gets:
        response = client.get(url, headers=headers)
        assert response.status_code == status.HTTP_404_NOT_FOUND, f"{url} -> {response.status_code}: {response.text}"
        assert_discloses_nothing(response)

    posts = [
        f"/api/v1/spc/control-limits/{stolen.id}/calculate",
        f"/api/v1/spc/capability-study/{stolen.id}",
    ]
    for url in posts:
        response = client.post(url, headers=headers)
        assert response.status_code == status.HTTP_404_NOT_FOUND, f"{url} -> {response.status_code}: {response.text}"
        assert_discloses_nothing(response)


# ===========================================================================
# 3. The calculating writes — NOT NULL, and the two unscoped bulk UPDATEs
# ===========================================================================


def _calc_url(characteristic_id: int) -> str:
    return f"/api/v1/spc/control-limits/{characteristic_id}/calculate"


def test_calculate_control_limits_succeeds_and_stamps_the_callers_company_id(client: TestClient, db_session: Session):
    """Second production-bug reproducer: ``SPCControlLimit.company_id`` is NOT NULL and was
    never set, so this endpoint 500'd for every caller."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    char_b = make_characteristic(db_session, company_id=COMPANY_B)
    seed_subgroups(db_session, char_b, range(1, 6), samples_per_subgroup=3)

    response = client.post(_calc_url(char_b.id), headers=headers_for(user_b))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["sample_count"] == 5

    db_session.expire_all()
    stored = db_session.query(SPCControlLimit).filter(SPCControlLimit.characteristic_id == char_b.id).all()
    assert len(stored) == 1
    assert stored[0].company_id == COMPANY_B
    assert stored[0].is_current is True
    assert stored[0].calculated_by == user_b.id


def test_calculate_control_limits_refuses_a_foreign_characteristic(client: TestClient, db_session: Session):
    user_b = make_user(db_session, company_id=COMPANY_B)
    stolen = foreign_characteristic(db_session)
    seed_subgroups(db_session, stolen, range(1, 6))

    response = client.post(_calc_url(stolen.id), headers=headers_for(user_b))

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert_discloses_nothing(response)
    db_session.expire_all()
    assert db_session.query(SPCControlLimit).filter(SPCControlLimit.characteristic_id == stolen.id).count() == 0


def test_calculate_does_not_retire_another_companys_current_control_limit(client: TestClient, db_session: Session):
    """The first of two unscoped bulk UPDATEs.

    ``UPDATE spc_control_limits SET is_current = false WHERE characteristic_id = ?`` carried
    no tenant predicate, so Company B recalculating retired Company A's live limits for the
    same characteristic id -- A's chart would lose its control lines with no trace of who did
    it. Reachable through a mis-tenanted row (see the module note); asserted on the STORED
    row because B's own response looks perfectly correct either way.
    """
    user_b = make_user(db_session, company_id=COMPANY_B)
    char_b = make_characteristic(db_session, company_id=COMPANY_B)
    seed_subgroups(db_session, char_b, range(1, 6), samples_per_subgroup=3)
    victim_limit = make_control_limit(db_session, char_b, company_id=COMPANY_A, is_current=True, ucl=42.4242)

    response = client.post(_calc_url(char_b.id), headers=headers_for(user_b))
    assert response.status_code == status.HTTP_200_OK, response.text

    survivor = fresh(db_session, SPCControlLimit, victim_limit.id)
    assert survivor.is_current is True, "Company B retired Company A's current control limit"
    assert survivor.ucl == 42.4242
    assert survivor.company_id == COMPANY_A


def test_calculate_does_not_rewrite_another_companys_out_of_control_flags(client: TestClient, db_session: Session):
    """The second unscoped bulk UPDATE.

    The per-subgroup ``UPDATE spc_measurements SET is_out_of_control = ..., violation_rules
    = ...`` was keyed on ``characteristic_id`` and ``subgroup_number`` alone, so Company B's
    recalculation cleared Company A's Western-Electric findings. On an AS9100D system that is
    quality evidence being silently overwritten by another tenant.
    """
    user_b = make_user(db_session, company_id=COMPANY_B)
    char_b = make_characteristic(db_session, company_id=COMPANY_B)
    seed_subgroups(db_session, char_b, range(1, 6), samples_per_subgroup=3)
    # A's row sits on subgroup 1 -- exactly a subgroup B's recalculation will rewrite.
    seed_subgroups(db_session, char_b, [1], company_id=COMPANY_A, samples_per_subgroup=1, ooc_subgroups={1})
    db_session.expire_all()
    victim = (
        db_session.query(SPCMeasurement)
        .filter(SPCMeasurement.company_id == COMPANY_A, SPCMeasurement.characteristic_id == char_b.id)
        .one()
    )
    assert victim.is_out_of_control is True

    response = client.post(_calc_url(char_b.id), headers=headers_for(user_b))
    assert response.status_code == status.HTTP_200_OK, response.text

    survivor = fresh(db_session, SPCMeasurement, victim.id)
    assert survivor.is_out_of_control is True, "Company B cleared Company A's out-of-control flag"
    assert survivor.company_id == COMPANY_A


def test_calculate_ignores_another_companys_measurements_when_computing(client: TestClient, db_session: Session):
    """The read half: a foreign row must not be able to move B's own control limits.

    A wildly out-of-scale mis-tenanted measurement would drag the grand mean and R-bar if the
    measurement read were unscoped, so the calculation is compared against a run where the
    foreign row does not exist at all.
    """
    user_b = make_user(db_session, company_id=COMPANY_B)
    clean = make_characteristic(db_session, company_id=COMPANY_B)
    contaminated = make_characteristic(db_session, company_id=COMPANY_B)
    seed_subgroups(db_session, clean, range(1, 6), samples_per_subgroup=3)
    seed_subgroups(db_session, contaminated, range(1, 6), samples_per_subgroup=3)
    db_session.add(
        SPCMeasurement(
            company_id=COMPANY_A,
            characteristic_id=contaminated.id,
            subgroup_number=3,
            sample_number=9,
            measurement_value=9999.0,
            measured_at=BASE_TS,
        )
    )
    db_session.commit()

    headers = headers_for(user_b)
    clean_body = client.post(_calc_url(clean.id), headers=headers).json()
    contaminated_body = client.post(_calc_url(contaminated.id), headers=headers).json()

    assert contaminated_body["center_line"] == clean_body["center_line"]
    assert contaminated_body["ucl"] == clean_body["ucl"]
    assert contaminated_body["sample_count"] == clean_body["sample_count"]


def test_capability_study_succeeds_and_stamps_the_callers_company_id(client: TestClient, db_session: Session):
    """Third production-bug reproducer: ``SPCProcessCapability.company_id`` is NOT NULL."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    char_b = make_characteristic(db_session, company_id=COMPANY_B)
    seed_subgroups(db_session, char_b, range(1, 6), samples_per_subgroup=3)

    response = client.post(f"/api/v1/spc/capability-study/{char_b.id}", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["sample_count"] == 15

    db_session.expire_all()
    stored = db_session.query(SPCProcessCapability).filter(SPCProcessCapability.characteristic_id == char_b.id).all()
    assert len(stored) == 1
    assert stored[0].company_id == COMPANY_B
    assert stored[0].performed_by == user_b.id


def test_capability_study_refuses_a_foreign_characteristic(client: TestClient, db_session: Session):
    """Cross-tenant, this computed and PERSISTED a Cp/Cpk study against another company's
    spec limits and measurements."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    stolen = foreign_characteristic(db_session)
    seed_subgroups(db_session, stolen, range(1, 6))

    response = client.post(f"/api/v1/spc/capability-study/{stolen.id}", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert_discloses_nothing(response)
    db_session.expire_all()
    assert (
        db_session.query(SPCProcessCapability).filter(SPCProcessCapability.characteristic_id == stolen.id).count() == 0
    )


# ===========================================================================
# 4. The two aggregates — platform-wide until now
# ===========================================================================


def test_out_of_control_returns_only_the_callers_own_characteristics(client: TestClient, db_session: Session):
    """This endpoint took no company argument at all: every authenticated user got a
    platform-wide list of every tenant's out-of-control characteristics, by name."""
    user_a = make_user(db_session, company_id=COMPANY_A)
    user_b = make_user(db_session, company_id=COMPANY_B)
    stolen = foreign_characteristic(db_session)
    seed_subgroups(db_session, stolen, [1, 2], ooc_subgroups={1, 2}, lot_number=FOREIGN_LOT)
    char_b = make_characteristic(db_session, company_id=COMPANY_B, name="B critical bore")
    seed_subgroups(db_session, char_b, [1], ooc_subgroups={1})

    seen_by_b = client.get("/api/v1/spc/out-of-control", headers=headers_for(user_b))
    assert seen_by_b.status_code == status.HTTP_200_OK, seen_by_b.text
    assert [row["characteristic_id"] for row in seen_by_b.json()] == [char_b.id]
    assert_discloses_nothing(seen_by_b)

    # The positive control in the other direction: A still sees its OWN, which is the whole
    # point of the endpoint and is what a refuse-everything fix would have broken.
    seen_by_a = client.get("/api/v1/spc/out-of-control", headers=headers_for(user_a))
    assert seen_by_a.status_code == status.HTTP_200_OK, seen_by_a.text
    rows_a = seen_by_a.json()
    assert [row["characteristic_id"] for row in rows_a] == [stolen.id]
    assert rows_a[0]["characteristic_name"] == FOREIGN_CHAR_NAME
    assert rows_a[0]["ooc_count"] == 6
    assert "B critical bore" not in seen_by_a.text


def test_dashboard_counts_only_the_callers_own_characteristics(client: TestClient, db_session: Session):
    """``total_characteristics``, ``out_of_control_count`` and ``attention_needed`` all
    spanned every tenant."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    for _ in range(3):
        make_characteristic(db_session, company_id=COMPANY_A)
    stolen = foreign_characteristic(db_session)
    seed_subgroups(db_session, stolen, [1], ooc_subgroups={1})

    char_b = make_characteristic(db_session, company_id=COMPANY_B, name="B critical bore")
    seed_subgroups(db_session, char_b, [1], ooc_subgroups={1})

    response = client.get("/api/v1/spc/dashboard", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["total_characteristics"] == 1
    assert body["out_of_control_count"] == 1
    assert [row["id"] for row in body["attention_needed"]] == [char_b.id]
    assert_discloses_nothing(response)


def test_dashboard_average_cpk_excludes_other_companies(client: TestClient, db_session: Session):
    """``average_cpk`` and ``characteristics_below_cpk_threshold`` mixed tenants: a foreign
    company's poor process pulled this company's headline number down (and vice versa)."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    stolen = foreign_characteristic(db_session)
    make_capability(db_session, stolen, cpk=0.5)  # would drag the average and add a "below"
    char_b = make_characteristic(db_session, company_id=COMPANY_B)
    make_capability(db_session, char_b, cpk=2.0)

    response = client.get("/api/v1/spc/dashboard", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["average_cpk"] == 2.0
    assert body["characteristics_below_cpk_threshold"] == 0
    assert body["attention_needed"] == []
    assert_discloses_nothing(response)


def test_dashboard_latest_capability_cannot_be_hidden_by_a_foreign_study(client: TestClient, db_session: Session):
    """The second INNER-subquery trap, and the reason the ``latest_caps`` predicate matters.

    ``latest_caps`` picks MAX(study_date) per characteristic. Scoped only on the outer join,
    a later foreign study sets ``max_date`` and the outer (scoped) query then joins to
    nothing -- Company B's own latest Cpk vanishes from its dashboard entirely. That failure
    is invisible in every "B cannot see A's row" assertion, so it gets its own test.
    """
    user_b = make_user(db_session, company_id=COMPANY_B)
    char_b = make_characteristic(db_session, company_id=COMPANY_B)
    make_capability(db_session, char_b, cpk=1.9, study_date=BASE_TS)
    # A's study, later, sitting on B's characteristic id.
    make_capability(db_session, char_b, company_id=COMPANY_A, cpk=0.2, study_date=BASE_TS + timedelta(days=30))

    response = client.get("/api/v1/spc/dashboard", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["average_cpk"] == 1.9, "a foreign study set max_date and hid B's own"


# ===========================================================================
# 5. FK ownership on the characteristics CRUD
# ===========================================================================


def test_create_characteristic_refuses_a_foreign_part(client: TestClient, db_session: Session):
    """``part_id`` is a NOT NULL FK that was never checked, so a characteristic could be
    hung off another company's part -- a cross-tenant reference that later leaks that part's
    identity through every join and report that walks it."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    foreign_part = make_part(db_session, company_id=COMPANY_A, part_number=FOREIGN_PART_NUMBER)

    response = client.post(
        CHARACTERISTICS_URL,
        headers=headers_for(user_b),
        json={"name": "B bore", "part_id": foreign_part.id, "characteristic_type": "dimensional"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert_discloses_nothing(response)
    db_session.expire_all()
    assert db_session.query(SPCCharacteristic).filter(SPCCharacteristic.part_id == foreign_part.id).count() == 0


def test_create_characteristic_refuses_a_foreign_work_center(client: TestClient, db_session: Session):
    user_b = make_user(db_session, company_id=COMPANY_B)
    own_part = make_part(db_session, company_id=COMPANY_B)
    foreign_wc = make_work_center(db_session, company_id=COMPANY_A)

    response = client.post(
        CHARACTERISTICS_URL,
        headers=headers_for(user_b),
        json={
            "name": "B bore",
            "part_id": own_part.id,
            "characteristic_type": "dimensional",
            "work_center_id": foreign_wc.id,
        },
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    db_session.expire_all()
    assert db_session.query(SPCCharacteristic).count() == 0, "a refused create must leave no row behind"


def test_create_characteristic_still_works_for_the_callers_own_fks(client: TestClient, db_session: Session):
    """Positive control: the validation narrows WHICH ids are acceptable, it does not break
    the create path."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    own_part = make_part(db_session, company_id=COMPANY_B)
    own_wc = make_work_center(db_session, company_id=COMPANY_B)
    make_part(db_session, company_id=COMPANY_A, part_number=FOREIGN_PART_NUMBER)  # present and irrelevant

    response = client.post(
        CHARACTERISTICS_URL,
        headers=headers_for(user_b),
        json={
            "name": "B bore",
            "part_id": own_part.id,
            "characteristic_type": "dimensional",
            "work_center_id": own_wc.id,
        },
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["part_id"] == own_part.id
    assert body["work_center_id"] == own_wc.id
    db_session.expire_all()
    assert db_session.get(SPCCharacteristic, body["id"]).company_id == COMPANY_B


def test_update_characteristic_refuses_repointing_at_a_foreign_work_center(client: TestClient, db_session: Session):
    """The update path setattr-loops the payload, so ``work_center_id`` could be repointed
    at a foreign work center even though the characteristic itself was correctly scoped.
    The check runs BEFORE the loop, so a refusal leaves the row untouched."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    own_wc = make_work_center(db_session, company_id=COMPANY_B)
    char_b = make_characteristic(db_session, company_id=COMPANY_B, work_center_id=own_wc.id, name="B bore")
    foreign_wc = make_work_center(db_session, company_id=COMPANY_A)

    response = client.put(
        f"{CHARACTERISTICS_URL}/{char_b.id}",
        headers=headers_for(user_b),
        json={"work_center_id": foreign_wc.id, "name": "renamed by a refused request"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    stored = fresh(db_session, SPCCharacteristic, char_b.id)
    assert stored.work_center_id == own_wc.id
    assert stored.name == "B bore", "a refused update must not have applied its other fields"


def test_update_characteristic_still_accepts_the_callers_own_work_center(client: TestClient, db_session: Session):
    user_b = make_user(db_session, company_id=COMPANY_B)
    char_b = make_characteristic(db_session, company_id=COMPANY_B)
    own_wc = make_work_center(db_session, company_id=COMPANY_B)

    response = client.put(
        f"{CHARACTERISTICS_URL}/{char_b.id}",
        headers=headers_for(user_b),
        json={"work_center_id": own_wc.id, "name": "B bore renamed"},
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["work_center_id"] == own_wc.id
    assert response.json()["name"] == "B bore renamed"


def test_update_characteristic_refuses_another_companys_characteristic(client: TestClient, db_session: Session):
    user_b = make_user(db_session, company_id=COMPANY_B)
    stolen = foreign_characteristic(db_session)

    response = client.put(
        f"{CHARACTERISTICS_URL}/{stolen.id}", headers=headers_for(user_b), json={"name": "owned by B now"}
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert_discloses_nothing(response)
    assert fresh(db_session, SPCCharacteristic, stolen.id).name == FOREIGN_CHAR_NAME


# ===========================================================================
# 6. Paging bounds (this PR owns spc.py's own skip/limit hardening)
# ===========================================================================


@pytest.mark.parametrize(
    "params",
    [
        {"skip": -1},
        {"limit": 0},
        {"limit": 100_000},
    ],
)
def test_list_characteristics_rejects_out_of_range_paging(client: TestClient, db_session: Session, params: dict):
    user_b = make_user(db_session, company_id=COMPANY_B)
    response = client.get(CHARACTERISTICS_URL, headers=headers_for(user_b), params=params)
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, response.text


@pytest.mark.parametrize("limit", [0, 100_000])
def test_get_measurements_rejects_an_out_of_range_limit(client: TestClient, db_session: Session, limit: int):
    user_b = make_user(db_session, company_id=COMPANY_B)
    char_b = make_characteristic(db_session, company_id=COMPANY_B)
    response = client.get(f"{MEASUREMENTS_URL}/{char_b.id}", headers=headers_for(user_b), params={"limit": limit})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, response.text


# ===========================================================================
# 7. End-to-end shape of the invariant
# ===========================================================================


def test_each_company_only_ever_lists_its_own_characteristics(client: TestClient, db_session: Session):
    user_a = make_user(db_session, company_id=COMPANY_A)
    user_b = make_user(db_session, company_id=COMPANY_B)
    stolen = foreign_characteristic(db_session)
    char_b = make_characteristic(db_session, company_id=COMPANY_B, name="B bore diameter")

    listed_b = client.get(CHARACTERISTICS_URL, headers=headers_for(user_b))
    assert listed_b.status_code == status.HTTP_200_OK, listed_b.text
    assert [row["id"] for row in listed_b.json()] == [char_b.id]
    assert_discloses_nothing(listed_b)

    listed_a = client.get(CHARACTERISTICS_URL, headers=headers_for(user_a))
    assert listed_a.status_code == status.HTTP_200_OK, listed_a.text
    assert [row["id"] for row in listed_a.json()] == [stolen.id]
    assert "B bore diameter" not in listed_a.text


# ===========================================================================
# 8. work_order_id ownership on the measurement write
# ===========================================================================


def test_add_measurements_refuses_another_companys_work_order(client: TestClient, db_session: Session):
    """A measurement may not be attributed to a foreign work order.

    Nothing joins ``spc_measurements.work_order_id`` today, so this is not a live read
    leak -- it is a FALSE TRACEABILITY POINTER on an AS9100D quality record, which is why
    it is refused rather than merely filtered at read time.
    """
    user_b = make_user(db_session, company_id=COMPANY_B)
    char_b = make_characteristic(db_session, company_id=COMPANY_B)
    foreign_wo = make_work_order(db_session, company_id=COMPANY_A)

    payload = measurement_payload(char_b.id)
    for row in payload["measurements"]:
        row["work_order_id"] = foreign_wo.id
    response = client.post(MEASUREMENTS_URL, headers=headers_for(user_b), json=payload)

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    db_session.expire_all()
    assert db_session.query(SPCMeasurement).count() == 0, "a refused batch must write nothing"


def test_add_measurements_still_accepts_the_callers_own_work_order(client: TestClient, db_session: Session):
    """Positive control: the traceability pointer is a real feature, only narrowed."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    char_b = make_characteristic(db_session, company_id=COMPANY_B)
    own_wo = make_work_order(db_session, company_id=COMPANY_B)

    payload = measurement_payload(char_b.id)
    for row in payload["measurements"]:
        row["work_order_id"] = own_wo.id
    response = client.post(MEASUREMENTS_URL, headers=headers_for(user_b), json=payload)

    assert response.status_code == status.HTTP_200_OK, response.text
    db_session.expire_all()
    stored = db_session.query(SPCMeasurement).all()
    assert len(stored) == 3
    assert {m.work_order_id for m in stored} == {own_wo.id}
    assert {m.company_id for m in stored} == {COMPANY_B}


# ===========================================================================
# 9. The recalculation is role-gated and audited
# ===========================================================================


@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.VIEWER, UserRole.SHIPPING, UserRole.SUPERVISOR])
def test_recalculate_is_refused_for_roles_that_do_not_own_the_quality_system(
    client: TestClient, db_session: Session, role: UserRole
):
    """The endpoint rewrites out-of-control flags on historical measurements, so it is
    gated like the NCR void/restore verbs. ``/spc`` itself is only ``quality:view``, which
    operator and viewer both hold, so without this gate they could fire the rewrite."""
    user_b = make_user(db_session, company_id=COMPANY_B, role=role)
    char_b = make_characteristic(db_session, company_id=COMPANY_B)
    seed_subgroups(db_session, char_b, range(1, 6), samples_per_subgroup=3)

    response = client.post(_calc_url(char_b.id), headers=headers_for(user_b))

    assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
    db_session.expire_all()
    assert db_session.query(SPCControlLimit).count() == 0, "a refused recalculation must write no limits"


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY])
def test_recalculate_is_allowed_for_the_quality_owning_roles(client: TestClient, db_session: Session, role: UserRole):
    """The positive control for the gate: narrowing WHO may recalculate must not break
    the roles whose job it is."""
    user_b = make_user(db_session, company_id=COMPANY_B, role=role)
    char_b = make_characteristic(db_session, company_id=COMPANY_B)
    seed_subgroups(db_session, char_b, range(1, 6), samples_per_subgroup=3)

    response = client.post(_calc_url(char_b.id), headers=headers_for(user_b))

    assert response.status_code == status.HTTP_200_OK, response.text


def _recalc_audit_rows(db: Session):
    db.expire_all()
    return db.query(AuditLog).filter(AuditLog.resource_type == "spc_control_limit").all()


def test_recalculation_writes_an_audit_row_naming_the_window_and_the_limits(client: TestClient, db_session: Session):
    """Before this PR the endpoint was inert (the NOT NULL violation rolled the whole
    transaction back), so making it commit is what creates the obligation to audit it."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    char_b = make_characteristic(db_session, company_id=COMPANY_B, name="B bore diameter")
    seed_subgroups(db_session, char_b, range(1, 9), samples_per_subgroup=3)

    response = client.post(_calc_url(char_b.id), headers=headers_for(user_b), params={"last_n_subgroups": 5})
    assert response.status_code == status.HTTP_200_OK, response.text

    rows = _recalc_audit_rows(db_session)
    assert len(rows) == 1, "exactly one audit row per recalculation"
    row = rows[0]
    assert row.company_id == COMPANY_B
    assert row.user_id == user_b.id
    assert row.resource_id == response.json()["id"]

    extra = row.extra_data or {}
    # The caller-supplied window is what makes the rewrite steerable, so it must be on the row.
    assert extra["last_n_subgroups"] == 5
    assert extra["subgroups_used"] == 5
    assert extra["subgroup_numbers"] == [4, 5, 6, 7, 8]
    assert extra["characteristic_id"] == char_b.id
    assert extra["control_limits"]["ucl"] == response.json()["ucl"]
    assert extra["control_limits"]["center_line"] == response.json()["center_line"]


def test_recalculation_audit_records_a_cleared_violation_old_to_new(client: TestClient, db_session: Session):
    """THE reason this audit row exists.

    A narrower ``last_n_subgroups`` recomputes limits from a subset and resets
    ``is_out_of_control``/``violation_rules`` to False/None on subgroups that no longer
    violate -- erasing a Western Electric finding recorded against shipped material. The
    row must carry the BEFORE value, or the erasure is unreconstructable.
    """
    user_b = make_user(db_session, company_id=COMPANY_B)
    char_b = make_characteristic(db_session, company_id=COMPANY_B)
    # Every subgroup shares the SAME mean, so the recalculation finds no Western Electric
    # violation anywhere. The only flag in the data is the stale one seeded below, which
    # makes clearing it unambiguously an ERASURE rather than a recomputation.
    # (The module's usual `_value` ramp trips Rule 2 on the target subgroup, so the flag
    # would legitimately survive and the test would prove nothing.)
    for subgroup_number in range(1, 6):
        for sample_number in (1, 2, 3):
            db_session.add(
                SPCMeasurement(
                    company_id=COMPANY_B,
                    characteristic_id=char_b.id,
                    subgroup_number=subgroup_number,
                    sample_number=sample_number,
                    measurement_value=10.0 + sample_number * 0.001,
                    measured_at=BASE_TS + timedelta(minutes=subgroup_number),
                    # Subgroup 3 carries a violation previously recorded against material
                    # that has already shipped. This run will not reproduce it.
                    is_out_of_control=subgroup_number == 3,
                    violation_rules="Rule1" if subgroup_number == 3 else None,
                )
            )
    db_session.commit()

    response = client.post(_calc_url(char_b.id), headers=headers_for(user_b))
    assert response.status_code == status.HTTP_200_OK, response.text

    # The flag really was cleared on the stored row...
    db_session.expire_all()
    cleared = db_session.query(SPCMeasurement).filter(SPCMeasurement.subgroup_number == 3).all()
    assert all(m.is_out_of_control is False for m in cleared)
    assert all(m.violation_rules is None for m in cleared)

    # ...and the audit row records what it used to be.
    extra = _recalc_audit_rows(db_session)[0].extra_data or {}
    assert extra["violations_cleared_count"] == 1
    assert extra["violations_cleared_subgroups"] == [3]
    change = next(c for c in extra["measurement_flag_changes"] if c["subgroup_number"] == 3)
    assert change["old"] == {"is_out_of_control": True, "violation_rules": "Rule1"}
    assert change["new"] == {"is_out_of_control": False, "violation_rules": None}


def test_recalculation_audit_reports_no_cleared_violations_when_nothing_changed(
    client: TestClient, db_session: Session
):
    """A clean re-run must not claim it erased anything -- otherwise the cleared-count
    is noise and a reviewer learns to ignore it."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    char_b = make_characteristic(db_session, company_id=COMPANY_B)
    seed_subgroups(db_session, char_b, range(1, 6), samples_per_subgroup=3)

    headers = headers_for(user_b)
    assert client.post(_calc_url(char_b.id), headers=headers).status_code == status.HTTP_200_OK
    assert client.post(_calc_url(char_b.id), headers=headers).status_code == status.HTTP_200_OK

    rows = _recalc_audit_rows(db_session)
    assert len(rows) == 2
    second = rows[-1].extra_data or {}
    assert second["violations_cleared_count"] == 0
    assert second["measurement_flag_changes"] == []
    # The second run supersedes the first run's limit row, and says so.
    assert len(second["superseded_control_limit_ids"]) == 1
