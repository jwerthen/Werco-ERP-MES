"""Multi-level BOM reads are tenant-scoped (invariant #1).

``GET /bom/{id}/explode``, ``GET /bom/{id}/flatten`` and ``GET /bom/{id}/where-used`` took
only ``get_current_user`` — any authenticated user of any tenant could read any other
tenant's full multi-level BOM structure (part numbers ARE customer design identity here).
These tests are mutation-verified: each cross-tenant case seeds a real, distinctive foreign
structure and asserts the refusal is a flat 404 whose body carries none of it, and each has
a same-tenant positive control so a refuse-everything endpoint cannot pass.

The recursive walk is covered too: ``explode_bom_recursive`` and
``would_create_circular_reference`` now carry ``company_id`` on every BOM lookup, so even a
corrupt/mis-parented row (constructible — SQLite doesn't enforce FKs, and no FK carries
``company_id`` on Postgres either) cannot make the recursion cross a tenant boundary.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.bom import BOM, BOMItem
from app.models.company import Company
from app.models.part import Part
from app.models.user import User, UserRole

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"

# Sentinels distinctive enough that a substring search over the whole response body is a
# real leak check rather than a coincidence.
FOREIGN_ASSY_NUMBER = "ACME-TOPLEVEL-90311"
FOREIGN_SUB_NUMBER = "ACME-SUBASSY-90312"
FOREIGN_LEAF_NUMBER = "ACME-LEAF-90313"

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


def make_user(db: Session, *, company_id: int, role: UserRole = UserRole.ADMIN) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"bom-ml-iso-{n}@co{company_id}.test",
        employee_id=f"BOMML-{n:05d}",
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


def make_part(db: Session, *, company_id: int, part_number: str = None, name: str = None) -> Part:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=part_number or f"MLISO-P-{n}",
        name=name or f"Multi-level isolation part {n}",
        description="multi-level tenant-isolation fixture part",
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


def make_bom(db: Session, *, part_id: int, company_id: int) -> BOM:
    bom = BOM(part_id=part_id, revision="A", status="draft", is_active=True, company_id=company_id)
    db.add(bom)
    db.commit()
    db.refresh(bom)
    return bom


def add_item(db: Session, *, bom_id: int, component_part_id: int, company_id: int, qty: float = 2.0) -> BOMItem:
    item = BOMItem(
        bom_id=bom_id,
        component_part_id=component_part_id,
        company_id=company_id,
        item_number=10,
        quantity=qty,
        item_type="make",
        unit_of_measure="each",
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def build_foreign_two_level_structure(db: Session):
    """Company A: top assembly -> sub-assembly (own BOM) -> leaf. Returns the top BOM."""
    assy = make_part(db, company_id=COMPANY_A, part_number=FOREIGN_ASSY_NUMBER, name="A top assembly")
    sub = make_part(db, company_id=COMPANY_A, part_number=FOREIGN_SUB_NUMBER, name="A sub assembly")
    leaf = make_part(db, company_id=COMPANY_A, part_number=FOREIGN_LEAF_NUMBER, name="A leaf component")
    top_bom = make_bom(db, part_id=assy.id, company_id=COMPANY_A)
    sub_bom = make_bom(db, part_id=sub.id, company_id=COMPANY_A)
    add_item(db, bom_id=top_bom.id, component_part_id=sub.id, company_id=COMPANY_A)
    add_item(db, bom_id=sub_bom.id, component_part_id=leaf.id, company_id=COMPANY_A)
    return top_bom, assy, sub, leaf


def assert_discloses_nothing(response) -> None:
    body = response.text
    assert FOREIGN_ASSY_NUMBER not in body, f"leaked the foreign assembly number: {body}"
    assert FOREIGN_SUB_NUMBER not in body, f"leaked the foreign sub-assembly number: {body}"
    assert FOREIGN_LEAF_NUMBER not in body, f"leaked the foreign leaf number: {body}"


# ===========================================================================
# Cross-tenant: each endpoint answers a flat 404 and discloses nothing
# ===========================================================================


def test_explode_refuses_another_companys_bom_with_a_flat_404(client: TestClient, db_session: Session):
    top_bom, *_ = build_foreign_two_level_structure(db_session)
    user_b = make_user(db_session, company_id=COMPANY_B)

    response = client.get(f"/api/v1/bom/{top_bom.id}/explode", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert_discloses_nothing(response)


def test_flatten_refuses_another_companys_bom_with_a_flat_404(client: TestClient, db_session: Session):
    top_bom, *_ = build_foreign_two_level_structure(db_session)
    user_b = make_user(db_session, company_id=COMPANY_B)

    response = client.get(f"/api/v1/bom/{top_bom.id}/flatten", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert_discloses_nothing(response)


def test_where_used_refuses_another_companys_bom_with_a_flat_404(client: TestClient, db_session: Session):
    top_bom, *_ = build_foreign_two_level_structure(db_session)
    user_b = make_user(db_session, company_id=COMPANY_B)

    response = client.get(f"/api/v1/bom/{top_bom.id}/where-used", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert_discloses_nothing(response)


# ===========================================================================
# The recursive walk cannot cross tenants even through a same-numbered part
# ===========================================================================


def test_explode_never_descends_into_a_foreign_sub_bom(client: TestClient, db_session: Session):
    """Company B's BOM contains B's copy of a part; only COMPANY A has a BOM for a part
    that B's line (corruptly) points at. The walk must not pick up A's sub-BOM."""
    build_foreign_two_level_structure(db_session)
    user_b = make_user(db_session, company_id=COMPANY_B)
    assembly_b = make_part(db_session, company_id=COMPANY_B)
    bom_b = make_bom(db_session, part_id=assembly_b.id, company_id=COMPANY_B)
    # Mis-parented line: B's BOM row pointing at A's sub-assembly part (constructible —
    # SQLite doesn't enforce FKs and no FK carries company_id). The scoped walk must not
    # resolve A's sub-BOM for it.
    foreign_sub = db_session.query(Part).filter(Part.part_number == FOREIGN_SUB_NUMBER).first()
    add_item(db_session, bom_id=bom_b.id, component_part_id=foreign_sub.id, company_id=COMPANY_B)

    response = client.get(f"/api/v1/bom/{bom_b.id}/explode", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_200_OK, response.text
    # The walk must not have descended into Company A's sub-BOM: no leaf, no children.
    assert FOREIGN_LEAF_NUMBER not in response.text
    payload = response.json()
    for item in payload["items"]:
        assert item["children"] == [], f"walk crossed the tenant boundary: {item}"


def test_where_used_only_reports_the_callers_own_parents(client: TestClient, db_session: Session):
    """A mis-parented Company A line naming B's part must not appear in B's where-used."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    part_b = make_part(db_session, company_id=COMPANY_B)
    bom_b = make_bom(db_session, part_id=part_b.id, company_id=COMPANY_B)
    parent_b = make_part(db_session, company_id=COMPANY_B)
    parent_bom_b = make_bom(db_session, part_id=parent_b.id, company_id=COMPANY_B)
    add_item(db_session, bom_id=parent_bom_b.id, component_part_id=part_b.id, company_id=COMPANY_B)
    # Company A also has a BOM line naming B's part id (corrupt state).
    parent_a = make_part(db_session, company_id=COMPANY_A, part_number=FOREIGN_ASSY_NUMBER, name="A parent")
    parent_bom_a = make_bom(db_session, part_id=parent_a.id, company_id=COMPANY_A)
    add_item(db_session, bom_id=parent_bom_a.id, component_part_id=part_b.id, company_id=COMPANY_A)

    response = client.get(f"/api/v1/bom/{bom_b.id}/where-used", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert FOREIGN_ASSY_NUMBER not in response.text
    parents = {row["parent_part_id"] for row in response.json()["used_in"]}
    assert parent_b.id in parents
    assert parent_a.id not in parents


# ===========================================================================
# Positive controls: same-tenant behaviour still works end to end
# ===========================================================================


def test_explode_and_flatten_still_walk_the_callers_own_multilevel_bom(client: TestClient, db_session: Session):
    user_a = make_user(db_session, company_id=COMPANY_A)
    top_bom, assy, sub, leaf = build_foreign_two_level_structure(db_session)

    exploded = client.get(f"/api/v1/bom/{top_bom.id}/explode", headers=headers_for(user_a))
    assert exploded.status_code == status.HTTP_200_OK, exploded.text
    payload = exploded.json()
    assert payload["part_number"] == FOREIGN_ASSY_NUMBER
    assert payload["total_levels"] == 2
    assert FOREIGN_LEAF_NUMBER in exploded.text  # the level-2 leaf was reached

    flattened = client.get(f"/api/v1/bom/{top_bom.id}/flatten", headers=headers_for(user_a))
    assert flattened.status_code == status.HTTP_200_OK, flattened.text
    flat = flattened.json()
    assert flat["total_items"] == 2
    assert {row["part_number"] for row in flat["items"]} == {FOREIGN_SUB_NUMBER, FOREIGN_LEAF_NUMBER}


def test_where_used_still_reports_same_tenant_parents(client: TestClient, db_session: Session):
    user_a = make_user(db_session, company_id=COMPANY_A)
    top_bom, assy, sub, leaf = build_foreign_two_level_structure(db_session)
    sub_bom = db_session.query(BOM).filter(BOM.part_id == sub.id).first()

    response = client.get(f"/api/v1/bom/{sub_bom.id}/where-used", headers=headers_for(user_a))
    assert response.status_code == status.HTTP_200_OK, response.text
    rows = response.json()["used_in"]
    assert any(row["parent_part_id"] == assy.id for row in rows), rows
