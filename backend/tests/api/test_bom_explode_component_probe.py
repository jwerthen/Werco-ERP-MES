"""``explode_bom_recursive`` resolves each component's BOM ONCE, not twice.

The explosion already fetches ``component_bom`` for every line -- it needs the row to
recurse into -- and then handed the response builder a ``Part``, which probed
``parts_with_active_bom`` for the *same* part id with the *same* four filters
(``part_id`` / ``company_id`` / ``is_active`` / ``is_deleted``). A 40-line assembly paid 40
redundant round trips per level, and a deep explosion multiplies that by the level count.

Two things have to hold at once, and only one of them is about speed:

1. **The count drops.** ``_count_bom_selects`` counts the ``FROM boms`` statements the
   explosion emits. This is the assertion that fails against the pre-fix code.
2. **``has_bom`` still means exactly what it meant.** Substituting one query for another is
   only safe if they are the same query, so §2 pins the flag against all four states a
   component can be in -- active BOM, soft-deleted BOM, deactivated BOM, no BOM at all --
   through the real endpoint. A speed fix that quietly flipped the drill-down affordance on
   a deleted structure would be a records defect, not an optimisation.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.bom import BOM, BOMItem
from app.models.company import Company
from app.models.part import Part
from app.models.user import User, UserRole

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


# ---------------------------------------------------------------------------
# Fixtures (local, like every sibling BOM suite)
# ---------------------------------------------------------------------------


def _ensure_company(db: Session, company_id: int = COMPANY_A) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True)
        db.add(company)
        db.commit()
    return company


def make_user(db: Session, *, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"bomexp-{n}@co{company_id}.test",
        employee_id=f"BOMEXP-{n:05d}",
        first_name="Bom",
        last_name="Explode",
        hashed_password=TEST_PASSWORD_HASH,  # tokens are minted directly; never used for login
        role=UserRole.ADMIN,
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
        part_number=f"BX-P-{n}",
        name=f"Part {n}",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        is_deleted=False,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_bom(
    db: Session,
    part: Part,
    *,
    is_active: bool = True,
    is_deleted: bool = False,
    company_id: int = COMPANY_A,
) -> BOM:
    bom = BOM(
        part_id=part.id,
        revision="A",
        status="draft",
        is_active=is_active,
        is_deleted=is_deleted,
        company_id=company_id,
    )
    db.add(bom)
    db.commit()
    db.refresh(bom)
    return bom


def add_line(db: Session, bom: BOM, component: Part, *, item_number: int, company_id: int = COMPANY_A) -> BOMItem:
    item = BOMItem(
        bom_id=bom.id,
        component_part_id=component.id,
        item_number=item_number,
        quantity=1.0,
        item_type="make",
        line_type="component",
        unit_of_measure="each",
        company_id=company_id,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


class _BomSelectCounter:
    """Counts statements that read the ``boms`` table, for the duration of a ``with``."""

    def __init__(self, session: Session):
        self._session = session
        self.count = 0

    def _on_execute(self, conn, clauseelement, multiparams, params, execution_options):
        sql = str(clauseelement).lower()
        if sql.startswith("select") and "from boms" in sql:
            self.count += 1

    def __enter__(self) -> "_BomSelectCounter":
        event.listen(self._session.get_bind(), "before_execute", self._on_execute)
        return self

    def __exit__(self, *exc) -> None:
        event.remove(self._session.get_bind(), "before_execute", self._on_execute)


def _flat_component_map(payload: dict) -> dict:
    """``{part_number: has_bom}`` over every item in the exploded tree."""
    out: dict = {}

    def walk(items):
        for item in items:
            component = item.get("component_part")
            if component:
                out[component["part_number"]] = component["has_bom"]
            walk(item.get("children") or [])

    walk(payload["items"])
    return out


# ---------------------------------------------------------------------------
# 1. The redundant probe is gone
# ---------------------------------------------------------------------------


def test_explode_reads_each_components_bom_once(client: TestClient, db_session: Session):
    """A 5-line flat assembly emits ONE ``boms`` read per line, plus two fixed ones.

    The two fixed reads are the endpoint's own 404 lookup and the recursion's
    ``joinedload`` of the same header; the five are ``component_bom``, one per line. Pre-fix
    there were TEN: ``component_bom`` for the recursion plus an identical
    ``parts_with_active_bom`` probe for ``has_bom``, per line.

    ``bom_id`` is resolved BEFORE the counter is armed -- the fixture commits expire the
    instance, so touching ``bom.id`` inside the block would charge the endpoint for the
    test's own primary-key reload. The bound is an upper limit rather than an exact figure
    so an unrelated future read does not fail this for the wrong reason, but it is tight
    enough that reinstating the second probe (12 reads) breaks it.
    """
    user = make_user(db_session)
    assembly = make_part(db_session)
    bom = make_bom(db_session, assembly)
    for i in range(5):
        add_line(db_session, bom, make_part(db_session), item_number=(i + 1) * 10)
    bom_id = bom.id

    with _BomSelectCounter(db_session) as counter:
        response = client.get(f"/api/v1/bom/{bom_id}/explode", headers=headers_for(user))
        assert response.status_code == status.HTTP_200_OK, response.text

    assert len(response.json()["items"]) == 5
    # 2 fixed + 1 per line = 7. The pre-fix code emitted 2 + 2*5 = 12.
    assert counter.count <= 7, f"explode emitted {counter.count} boms reads for a 5-line BOM"


def test_explode_probe_count_scales_with_lines_not_with_lines_squared(client: TestClient, db_session: Session):
    """Doubling the line count must add one read per added line, not two.

    Counting one shape in isolation can be satisfied by an off-by-one; comparing two sizes
    pins the SLOPE, which is the actual claim ("one query per line, not two").
    """
    user = make_user(db_session)
    counts = {}
    for line_count in (2, 6):
        assembly = make_part(db_session)
        bom = make_bom(db_session, assembly)
        for i in range(line_count):
            add_line(db_session, bom, make_part(db_session), item_number=(i + 1) * 10)
        bom_id = bom.id  # resolved before the counter is armed; see the test above
        with _BomSelectCounter(db_session) as counter:
            response = client.get(f"/api/v1/bom/{bom_id}/explode", headers=headers_for(user))
            assert response.status_code == status.HTTP_200_OK, response.text
        counts[line_count] = counter.count

    added_reads = counts[6] - counts[2]
    assert added_reads == 4, f"4 extra lines cost {added_reads} extra boms reads (expected 4, pre-fix 8)"


# ---------------------------------------------------------------------------
# 2. has_bom semantics are unchanged
# ---------------------------------------------------------------------------


def test_explode_has_bom_matches_the_probe_it_replaced(client: TestClient, db_session: Session):
    """All four states a component can be in, through the real endpoint.

    ``has_bom`` drives the expand/drill-down affordance in the BOM tree, so each of these
    is a user-visible claim about the document: only an ACTIVE, NON-DELETED BOM in this
    company makes a component an assembly.
    """
    user = make_user(db_session)
    assembly = make_part(db_session)
    bom = make_bom(db_session, assembly)

    with_bom = make_part(db_session)
    sub_bom = make_bom(db_session, with_bom)
    add_line(db_session, sub_bom, make_part(db_session), item_number=10)

    with_deleted_bom = make_part(db_session)
    make_bom(db_session, with_deleted_bom, is_deleted=True)

    with_inactive_bom = make_part(db_session)
    make_bom(db_session, with_inactive_bom, is_active=False)

    without_bom = make_part(db_session)

    for i, component in enumerate((with_bom, with_deleted_bom, with_inactive_bom, without_bom)):
        add_line(db_session, bom, component, item_number=(i + 1) * 10)

    response = client.get(f"/api/v1/bom/{bom.id}/explode", headers=headers_for(user))
    assert response.status_code == status.HTTP_200_OK, response.text
    flags = _flat_component_map(response.json())

    assert flags[with_bom.part_number] is True
    assert flags[with_deleted_bom.part_number] is False, "a soft-deleted BOM must not make a component an assembly"
    assert flags[with_inactive_bom.part_number] is False
    assert flags[without_bom.part_number] is False


def test_explode_still_descends_into_the_sub_bom_it_reports(client: TestClient, db_session: Session):
    """``has_bom`` and the recursion now read the SAME row, so they cannot disagree.

    That is the substitution's real payoff and its real risk: if the two ever came apart,
    the tree would offer a drill-down that returns nothing (or hide one that would). Here
    the reported flag and the returned children are asserted together.
    """
    user = make_user(db_session)
    assembly = make_part(db_session)
    bom = make_bom(db_session, assembly)

    sub_assembly = make_part(db_session)
    sub_bom = make_bom(db_session, sub_assembly)
    leaf = make_part(db_session)
    add_line(db_session, sub_bom, leaf, item_number=10)
    add_line(db_session, bom, sub_assembly, item_number=10)

    response = client.get(f"/api/v1/bom/{bom.id}/explode", headers=headers_for(user))
    assert response.status_code == status.HTTP_200_OK, response.text
    payload = response.json()

    top = payload["items"][0]
    assert top["component_part"]["has_bom"] is True
    assert [child["component_part"]["part_number"] for child in top["children"]] == [leaf.part_number]
    assert top["children"][0]["component_part"]["has_bom"] is False
