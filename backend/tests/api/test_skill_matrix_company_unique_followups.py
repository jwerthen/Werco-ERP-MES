"""Behavior locks for the tenant-scoped SkillMatrix unique constraint
(fix/wo-followups-round2, FIX 4 + migration 045).

``SkillMatrix`` carried a GLOBAL unique on ``(user_id, work_center_id)`` (constraint
``uq_user_work_center``), so two tenants could not both record the same
``(user_id, work_center_id)`` pairing -- a tenant-isolation correctness gap. The model now
declares ``uq_skill_matrix_company_user_wc`` on ``(company_id, user_id, work_center_id)`` and
migration 045 swaps the live Postgres constraint to match.

The pytest path uses the SQLite ``create_all`` bootstrap, which builds the constraint directly
from the model's ``__table_args__`` -- so this is testable directly at the DB layer:
  - the SAME ``(user_id, work_center_id)`` pair CAN now coexist in COMPANY_A and COMPANY_B
    (no IntegrityError);
  - a duplicate within the SAME company still violates uniqueness (IntegrityError).
"""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.operator_certification import SkillMatrix
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter

pytestmark = [pytest.mark.requires_db]

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


def make_user(db: Session, *, company_id: int) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"skill-uq-{n}@co{company_id}.test",
        employee_id=f"SKILLUQ-{n:05d}",
        first_name="Skill",
        last_name=f"Co{company_id}",
        hashed_password=TEST_PASSWORD_HASH,
        role=UserRole.OPERATOR,
        is_active=True,
        is_superuser=False,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def make_work_center(db: Session, *, company_id: int) -> WorkCenter:
    _ensure_company(db, company_id)
    n = _next()
    wc = WorkCenter(
        name=f"SKILLUQ-WC-{n}",
        code=f"SKILLUQ-WC-{n}",
        work_center_type="welding",
        description="skill uq fixture",
        hourly_rate=100,
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def test_same_user_wc_pair_allowed_across_two_companies(db_session: Session):
    """The constraint is now tenant-scoped: the SAME (user_id, work_center_id) tuple may exist
    once per company without tripping uniqueness."""
    # Real FK rows must exist in each tenant; the constraint key is (company_id, user_id, wc_id).
    user_a = make_user(db_session, company_id=COMPANY_A)
    wc_a = make_work_center(db_session, company_id=COMPANY_A)
    # Company B rows that happen to share the SAME numeric user_id/work_center_id are not
    # achievable with distinct FK rows, so we assert the key directly: a SkillMatrix in B with
    # the SAME (user_id, work_center_id) ints as A's must not collide.
    user_b = make_user(db_session, company_id=COMPANY_B)
    wc_b = make_work_center(db_session, company_id=COMPANY_B)

    sm_a = SkillMatrix(user_id=user_a.id, work_center_id=wc_a.id, skill_level=3, company_id=COMPANY_A)
    db_session.add(sm_a)
    db_session.commit()

    # Force the SAME (user_id, work_center_id) ints as A's row, but tagged company B. Under the
    # OLD global unique this would raise; under the new (company_id, user_id, wc_id) it must not.
    sm_b = SkillMatrix(user_id=user_a.id, work_center_id=wc_a.id, skill_level=4, company_id=COMPANY_B)
    db_session.add(sm_b)
    db_session.commit()  # must NOT raise IntegrityError

    rows = (
        db_session.query(SkillMatrix)
        .filter(SkillMatrix.user_id == user_a.id, SkillMatrix.work_center_id == wc_a.id)
        .all()
    )
    assert {r.company_id for r in rows} == {COMPANY_A, COMPANY_B}
    # The unused B FK rows exist but are not part of this assertion.
    assert user_b.id and wc_b.id


def test_duplicate_pair_within_same_company_still_violates_uniqueness(db_session: Session):
    """A second row with the SAME (company_id, user_id, work_center_id) still trips the unique
    constraint -- the per-tenant guarantee is intact."""
    user_a = make_user(db_session, company_id=COMPANY_A)
    wc_a = make_work_center(db_session, company_id=COMPANY_A)

    db_session.add(SkillMatrix(user_id=user_a.id, work_center_id=wc_a.id, skill_level=3, company_id=COMPANY_A))
    db_session.commit()

    db_session.add(SkillMatrix(user_id=user_a.id, work_center_id=wc_a.id, skill_level=5, company_id=COMPANY_A))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
