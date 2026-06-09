"""Behavior locks for the tenant-scoped skill-matrix READ endpoints
(fix/wo-remediation-followups, FIX 2).

The four skill-matrix reads in ``app/api/endpoints/operator_certifications.py`` were NOT
tenant-scoped: a caller could see another company's ``SkillMatrix`` rows (and, for
``list_skill_matrix``, another company's users/work-centers). The fix adds
``company_id = Depends(get_current_company_id)`` and filters ``SkillMatrix.company_id`` on:
  - ``GET /certifications/skill-matrix/check/{user_id}/{work_center_id}``  (check_operator_qualification)
  - ``GET /certifications/skill-matrix/user/{user_id}``                    (get_user_skills)
  - ``GET /certifications/skill-matrix/work-center/{work_center_id}``      (get_work_center_operators)
  - ``GET /certifications/skill-matrix/``                                  (list_skill_matrix)
``list_skill_matrix`` also scopes its returned ``users``/``work_centers``; ``create_skill_entry``
stamps ``company_id`` on insert and scopes its existing-entry lookup.

Each test authenticates as a COMPANY_A caller and proves a COMPANY_B skill row / user /
work-center is invisible, and that ``POST`` persists ``company_id`` = the caller's active company.
"""

from datetime import date

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.company import Company
from app.models.operator_certification import SkillMatrix
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter

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


def make_user(db: Session, *, role: UserRole = UserRole.ADMIN, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"skill-fu-{n}@co{company_id}.test",
        employee_id=f"SKILLFU-{n:05d}",
        first_name="Skill",
        last_name=f"Co{company_id}",
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


def make_work_center(db: Session, *, company_id: int = COMPANY_A) -> WorkCenter:
    _ensure_company(db, company_id)
    n = _next()
    wc = WorkCenter(
        name=f"SKILLFU-WC-{n}",
        code=f"SKILLFU-WC-{n}",
        work_center_type="welding",
        description="skill-matrix fixture work center",
        hourly_rate=100,
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def make_skill(
    db: Session,
    user: User,
    wc: WorkCenter,
    *,
    skill_level: int = 3,
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
    db.commit()
    db.refresh(sm)
    return sm


# ---------------------------------------------------------------------------
# check_operator_qualification: GET /skill-matrix/check/{user}/{wc}
# ---------------------------------------------------------------------------


def test_check_qualification_does_not_see_company_b_row(client: TestClient, db_session: Session):
    """A qualifying SkillMatrix row tagged COMPANY_B must NOT make a COMPANY_A caller see
    the operator as qualified -- the read returns the not-qualified default, not B's row."""
    admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
    op_b = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_B)
    wc_b = make_work_center(db_session, company_id=COMPANY_B)
    make_skill(db_session, op_b, wc_b, skill_level=5, company_id=COMPANY_B)
    db_session.commit()

    resp = client.get(
        f"/api/v1/certifications/skill-matrix/check/{op_b.id}/{wc_b.id}",
        headers=headers_for(admin_a),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body == {"qualified": False, "skill_level": 0, "detail": None}


def test_check_qualification_sees_own_company_row(client: TestClient, db_session: Session):
    admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
    op_a = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    wc_a = make_work_center(db_session, company_id=COMPANY_A)
    make_skill(db_session, op_a, wc_a, skill_level=4, company_id=COMPANY_A)
    db_session.commit()

    resp = client.get(
        f"/api/v1/certifications/skill-matrix/check/{op_a.id}/{wc_a.id}",
        headers=headers_for(admin_a),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["qualified"] is True
    assert body["skill_level"] == 4
    assert body["detail"] is not None


# ---------------------------------------------------------------------------
# get_user_skills: GET /skill-matrix/user/{user}
# ---------------------------------------------------------------------------


def test_get_user_skills_excludes_company_b_rows(client: TestClient, db_session: Session):
    admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
    op = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    wc_a = make_work_center(db_session, company_id=COMPANY_A)
    wc_b = make_work_center(db_session, company_id=COMPANY_B)
    make_skill(db_session, op, wc_a, skill_level=3, company_id=COMPANY_A)
    # A second row for the same user but tagged company B (different work center).
    make_skill(db_session, op, wc_b, skill_level=5, company_id=COMPANY_B)
    db_session.commit()

    resp = client.get(
        f"/api/v1/certifications/skill-matrix/user/{op.id}",
        headers=headers_for(admin_a),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["work_center_id"] == wc_a.id
    returned_wc_ids = {r["work_center_id"] for r in rows}
    assert wc_b.id not in returned_wc_ids


# ---------------------------------------------------------------------------
# get_work_center_operators: GET /skill-matrix/work-center/{wc}
# ---------------------------------------------------------------------------


def test_get_work_center_operators_excludes_company_b_rows(client: TestClient, db_session: Session):
    admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
    op_a = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    op_b = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_B)
    wc = make_work_center(db_session, company_id=COMPANY_A)
    make_skill(db_session, op_a, wc, skill_level=3, company_id=COMPANY_A)
    # A company-B skill row pointing at the SAME work-center id -- must not surface for A.
    make_skill(db_session, op_b, wc, skill_level=5, company_id=COMPANY_B)
    db_session.commit()

    resp = client.get(
        f"/api/v1/certifications/skill-matrix/work-center/{wc.id}",
        headers=headers_for(admin_a),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    rows = resp.json()
    returned_user_ids = {r["user_id"] for r in rows}
    assert op_a.id in returned_user_ids
    assert op_b.id not in returned_user_ids


# ---------------------------------------------------------------------------
# list_skill_matrix: GET /skill-matrix/  (entries + users + work_centers all scoped)
# ---------------------------------------------------------------------------


def test_list_skill_matrix_scopes_entries_users_and_work_centers(client: TestClient, db_session: Session):
    admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
    op_a = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    wc_a = make_work_center(db_session, company_id=COMPANY_A)
    make_skill(db_session, op_a, wc_a, skill_level=3, company_id=COMPANY_A)

    # Company-B noise: a user, a work center, and a skill row.
    op_b = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_B)
    wc_b = make_work_center(db_session, company_id=COMPANY_B)
    make_skill(db_session, op_b, wc_b, skill_level=5, company_id=COMPANY_B)
    db_session.commit()

    resp = client.get("/api/v1/certifications/skill-matrix/", headers=headers_for(admin_a))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()

    entry_wc_ids = {e["work_center_id"] for e in body["entries"]}
    assert wc_a.id in entry_wc_ids
    assert wc_b.id not in entry_wc_ids

    user_ids = {u["id"] for u in body["users"]}
    assert admin_a.id in user_ids and op_a.id in user_ids
    assert op_b.id not in user_ids

    wc_ids = {wc["id"] for wc in body["work_centers"]}
    assert wc_a.id in wc_ids
    assert wc_b.id not in wc_ids


# ---------------------------------------------------------------------------
# create_skill_entry: POST /skill-matrix/  stamps company_id = caller's active company
# ---------------------------------------------------------------------------


def test_create_skill_entry_persists_callers_company_id(client: TestClient, db_session: Session):
    admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
    op_a = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    wc_a = make_work_center(db_session, company_id=COMPANY_A)
    db_session.commit()

    resp = client.post(
        "/api/v1/certifications/skill-matrix/",
        json={
            "user_id": op_a.id,
            "work_center_id": wc_a.id,
            "skill_level": 3,
            "qualified_date": date.today().isoformat(),
        },
        headers=headers_for(admin_a),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    entry_id = resp.json()["id"]

    row = db_session.query(SkillMatrix).filter(SkillMatrix.id == entry_id).first()
    assert row is not None
    assert row.company_id == COMPANY_A
    assert row.user_id == op_a.id
    assert row.work_center_id == wc_a.id
