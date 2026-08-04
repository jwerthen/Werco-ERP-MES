"""A BOM line NAMING a foreign component must never RENDER it (invariant #1, read side).

The write side of this hole was closed by PR #161: all four line-write paths now resolve
``component_part_id`` scoped to the active company, and
``test_bom_component_tenant_isolation.py`` pins that. **This file is the other half**, and it
is the half that survived two consecutive security passes over ``bom.py``.

The defect
----------
``BOMItem.component_part`` is a plain relationship joined on ``component_part_id`` alone
(``models/bom.py``) — it carries **no** ``company_id`` predicate. Every read path in
``bom.py`` walked it to build the response, so on a mis-parented line (Company B's BOM row
whose ``component_part_id`` names a Company A part) the endpoints materialised A's ``Part``
and echoed its ``part_number``, ``name`` and ``revision`` straight back to B. On this system
a part number IS the customer's design identity, so the render is the finding — no write
required.

``get_component_part_info`` made it worse in a second, quieter way: it computed its
``has_bom`` flag as ``parts_with_active_bom(db, [part.id], part.company_id)`` — the
**component's own** company, read off an object the caller does not own. So B's response
also reported whether *Company A* holds an active BOM for A's part. That is why
``company_id`` is now a required argument rather than something derived from the data.

Why the write-side fix is not enough to make this moot
-----------------------------------------------------
Scoping only the writes would leave the reads correct-by-luck. They render rows written
before #161 landed, rows a future write door might introduce, and rows left behind by a
residual foreign key. Scoping the LOOKUP means the foreign object is never materialised at
all, rather than materialised and then carefully not printed.

How these tests are built
-------------------------
The mis-parented row is seeded at the MODEL layer, which is the only way to reach this state
now that the write paths refuse it — and it is a genuinely reachable state, not a contrivance
(SQLite does not enforce foreign keys, and on Postgres no FK carries ``company_id`` either,
because it is not part of the reference). This is the same technique
``test_bom_multilevel_tenant_isolation.py:183`` already uses for the recursive walk; that
test asserts the walk does not DESCEND into a foreign sub-BOM, but it never asserted that the
mis-parented component itself is not RENDERED — which is exactly the gap here.

Every leak assertion runs over the **whole response body**, not a parsed field, so a future
response shape that surfaces the foreign identifier anywhere still fails. And every path gets
a same-tenant positive control, because "render nothing, ever" would otherwise pass the lot.
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

# Distinctive enough that a substring search over a whole response body is a real leak check
# rather than a coincidence, and shaped like the data that actually matters here: a
# customer's part number, the part's name, and the customer's own name.
FOREIGN_PART_NUMBER = "ACME-READLEAK-55107"
FOREIGN_PART_NAME = "Classified Gearbox Housing"
FOREIGN_CUSTOMER = "ACME AEROSPACE (COMPANY A CUSTOMER)"

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


# ---------------------------------------------------------------------------
# Fixtures (local, like every sibling BOM suite)
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
        email=f"bom-read-iso-{n}@co{company_id}.test",
        employee_id=f"BOMRD-{n:05d}",
        first_name="Read",
        last_name="Isolation",
        hashed_password=TEST_PASSWORD_HASH,  # tokens are minted directly; never used for login
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


def make_part(
    db: Session,
    *,
    company_id: int,
    part_number: str = None,
    name: str = None,
    part_type: str = "manufactured",
    customer_name: str = None,
) -> Part:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=part_number or f"RD-P-{n}",
        name=name or f"Read isolation part {n}",
        description="read-isolation fixture part",
        part_type=part_type,
        unit_of_measure="each",
        standard_cost=5.0,
        is_active=True,
        is_deleted=False,
        customer_name=customer_name,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_bom(db: Session, *, part_id: int, company_id: int, status_value: str = "draft") -> BOM:
    bom = BOM(
        part_id=part_id,
        revision="A",
        status=status_value,
        is_active=True,
        is_deleted=False,
        company_id=company_id,
    )
    db.add(bom)
    db.commit()
    db.refresh(bom)
    return bom


def add_line(db: Session, *, bom_id: int, component_part_id: int, company_id: int, item_number: int = 10) -> BOMItem:
    """A BOM line written at the MODEL layer.

    This is the ONLY way to seed the mis-parented row under test: since PR #161 every write
    door refuses a foreign ``component_part_id``, which is the point — the read paths must
    survive state the write paths can no longer create.
    """
    item = BOMItem(
        bom_id=bom_id,
        component_part_id=component_part_id,
        item_number=item_number,
        quantity=1.0,
        item_type="buy",
        line_type="component",
        unit_of_measure="each",
        company_id=company_id,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def foreign_part(db: Session) -> Part:
    """Company A's part: the one Company B must never be able to read back."""
    return make_part(
        db,
        company_id=COMPANY_A,
        part_number=FOREIGN_PART_NUMBER,
        name=FOREIGN_PART_NAME,
        part_type="raw_material",
        customer_name=FOREIGN_CUSTOMER,
    )


def mis_parented_bom(db: Session) -> tuple:
    """Company B's BOM carrying one line that names COMPANY A's part.

    Returns ``(user_b, bom_b, line, foreign)``.
    """
    user_b = make_user(db, company_id=COMPANY_B)
    foreign = foreign_part(db)
    assembly_b = make_part(db, company_id=COMPANY_B)
    bom_b = make_bom(db, part_id=assembly_b.id, company_id=COMPANY_B)
    line = add_line(db, bom_id=bom_b.id, component_part_id=foreign.id, company_id=COMPANY_B)
    return user_b, bom_b, line, foreign


def assert_discloses_nothing(response) -> None:
    """None of the foreign identifiers may appear ANYWHERE in the body."""
    body = response.text
    assert FOREIGN_PART_NUMBER not in body, f"leaked the foreign part number: {body}"
    assert FOREIGN_PART_NAME not in body, f"leaked the foreign part name: {body}"
    assert FOREIGN_CUSTOMER not in body, f"leaked the foreign customer name: {body}"


# ===========================================================================
# §1 -- every read path that RENDERS a component
# ===========================================================================
# Each of these is a SEPARATE call site in bom.py with a genuinely different resolution
# shape, which is why they are separate tests rather than one parametrised sweep:
#   GET /bom/{id}      -> build_bom_item_response, BATCHED map (components_by_id)
#   GET /bom/          -> list_boms' own inline builder, BATCHED map
#   GET /bom/{id}/explode  -> component_part_info, per-level batched map (``has_bom`` comes
#                             from the BOM row the recursion already fetched)
#   GET /bom/{id}/flatten  -> the exploded tree, re-flattened
#   PUT /bom/items/{id}    -> build_bom_item_response, UNBATCHED self-resolving branch
# A fix applied to only some of them passes the others.


def test_get_bom_never_renders_a_foreign_component(client: TestClient, db_session: Session):
    """WOULD FAIL AGAINST OLD CODE: ``get_bom`` ran
    ``joinedload(BOMItem.component_part)`` and handed the materialised foreign ``Part`` to
    ``build_bom_item_response``, which rendered its number, name and revision into
    ``items[].component_part``."""
    user_b, bom_b, line, _foreign = mis_parented_bom(db_session)

    response = client.get(f"/api/v1/bom/{bom_b.id}", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert_discloses_nothing(response)
    items = response.json()["items"]
    assert len(items) == 1, "the line itself is still listed -- it is B's row"
    assert items[0]["id"] == line.id
    assert items[0]["component_part"] is None, "an unresolvable component renders as null"


def test_list_boms_never_renders_a_foreign_component(client: TestClient, db_session: Session):
    """WOULD FAIL AGAINST OLD CODE: ``list_boms`` used
    ``selectinload(BOM.items).selectinload(BOMItem.component_part)`` and read
    ``item.component_part`` directly in its own inline response builder — a second,
    independent copy of the leak that a fix to ``build_bom_item_response`` alone would miss.

    ``list_boms`` swallows a per-item exception with ``except Exception: pass``, so the
    assertion that the line is still PRESENT is load-bearing: a crash there would look
    identical to a clean refusal."""
    user_b, bom_b, line, _foreign = mis_parented_bom(db_session)

    response = client.get("/api/v1/bom/", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert_discloses_nothing(response)
    listed = [row for row in response.json() if row["id"] == bom_b.id]
    assert len(listed) == 1, "B's own BOM is still listed"
    assert [item["id"] for item in listed[0]["items"]] == [line.id], "the line was not silently dropped"
    assert listed[0]["items"][0]["component_part"] is None


def test_explode_never_renders_a_foreign_component(client: TestClient, db_session: Session):
    """WOULD FAIL AGAINST OLD CODE: ``explode_bom_recursive`` called
    ``get_component_part_info(item.component_part, db)`` — and that helper then probed
    ``has_bom`` against ``part.company_id``, i.e. COMPANY A's BOMs, so the response also
    reported a fact about A's data.

    The sibling ``test_bom_multilevel_tenant_isolation.py::test_explode_never_descends_into_
    a_foreign_sub_bom`` asserts the walk does not DESCEND; this asserts the mis-parented
    component is not RENDERED, which that test never checked."""
    user_b, bom_b, _line, _foreign = mis_parented_bom(db_session)

    response = client.get(f"/api/v1/bom/{bom_b.id}/explode", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert_discloses_nothing(response)
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["component_part"] is None


def test_flatten_never_renders_a_foreign_component(client: TestClient, db_session: Session):
    """WOULD FAIL AGAINST OLD CODE: ``flatten_bom`` re-flattens the exploded tree and copies
    ``component_part.part_number`` / ``.name`` into every flat row, so the leak reached the
    reporting/MRP surface too."""
    user_b, bom_b, _line, _foreign = mis_parented_bom(db_session)

    response = client.get(f"/api/v1/bom/{bom_b.id}/flatten", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert_discloses_nothing(response)
    rows = response.json()["items"]
    assert len(rows) == 1
    assert rows[0]["part_number"] == "", "an unresolvable component flattens to a blank, not a foreign number"
    assert rows[0]["part_name"] == ""


def test_updating_a_mis_parented_line_never_echoes_the_foreign_component(client: TestClient, db_session: Session):
    """The UNBATCHED branch of ``build_bom_item_response``.

    ``update_bom_item`` passes no ``components_by_id``, so the builder does its own scoped
    lookup — a different code path from the four reads above, and the one a batched-map-only
    fix would leave open. WOULD FAIL AGAINST OLD CODE on two counts: the handler carried
    ``joinedload(BOMItem.component_part)`` AND the response builder walked the relationship.

    The update itself must still SUCCEED — B's line is B's row, and refusing to edit it would
    strand the only person who can correct the corruption."""
    user_b, _bom_b, line, _foreign = mis_parented_bom(db_session)

    response = client.put(f"/api/v1/bom/items/{line.id}", headers=headers_for(user_b), json={"quantity": 7.0})

    assert response.status_code == status.HTTP_200_OK, response.text
    assert_discloses_nothing(response)
    assert response.json()["component_part"] is None
    assert float(response.json()["quantity"]) == 7.0, "B can still edit B's own line"


def test_the_foreign_part_row_is_untouched_by_every_read(client: TestClient, db_session: Session):
    """A read must stay a read. Sweeping all five surfaces in one test is the cheap guard
    that none of them writes to the part it declined to render (the ``customer_name``
    inheritance in ``add_bom_item`` is a live example of a component-mutating BOM path, so
    this is not hypothetical)."""
    user_b, bom_b, line, foreign = mis_parented_bom(db_session)
    head = headers_for(user_b)
    foreign_id = foreign.id

    client.get(f"/api/v1/bom/{bom_b.id}", headers=head)
    client.get("/api/v1/bom/", headers=head)
    client.get(f"/api/v1/bom/{bom_b.id}/explode", headers=head)
    client.get(f"/api/v1/bom/{bom_b.id}/flatten", headers=head)
    client.put(f"/api/v1/bom/items/{line.id}", headers=head, json={"quantity": 3.0})

    db_session.rollback()
    db_session.expire_all()
    stored = db_session.get(Part, foreign_id)
    assert stored.part_number == FOREIGN_PART_NUMBER
    assert stored.name == FOREIGN_PART_NAME
    assert stored.customer_name == FOREIGN_CUSTOMER
    assert stored.company_id == COMPANY_A


# ===========================================================================
# §2 -- positive controls
# ===========================================================================
# Every assertion in §1 also passes against a build_bom_item_response that returns None
# unconditionally. These pin that a legitimate, same-company component is still fully
# rendered on each of the same five surfaces.


def test_a_same_company_component_is_still_fully_rendered_on_every_read(client: TestClient, db_session: Session):
    """THE POSITIVE CONTROL for §1. Without it, deleting the component render entirely would
    turn this whole file green."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    assembly = make_part(db_session, company_id=COMPANY_B)
    component = make_part(db_session, company_id=COMPANY_B, part_type="raw_material")
    bom = make_bom(db_session, part_id=assembly.id, company_id=COMPANY_B)
    line = add_line(db_session, bom_id=bom.id, component_part_id=component.id, company_id=COMPANY_B)
    head = headers_for(user_b)

    fetched = client.get(f"/api/v1/bom/{bom.id}", headers=head)
    assert fetched.status_code == status.HTTP_200_OK, fetched.text
    assert fetched.json()["items"][0]["component_part"]["part_number"] == component.part_number

    listed = client.get("/api/v1/bom/", headers=head)
    assert listed.status_code == status.HTTP_200_OK, listed.text
    row = next(r for r in listed.json() if r["id"] == bom.id)
    assert row["items"][0]["component_part"]["part_number"] == component.part_number

    exploded = client.get(f"/api/v1/bom/{bom.id}/explode", headers=head)
    assert exploded.status_code == status.HTTP_200_OK, exploded.text
    assert exploded.json()["items"][0]["component_part"]["part_number"] == component.part_number

    flattened = client.get(f"/api/v1/bom/{bom.id}/flatten", headers=head)
    assert flattened.status_code == status.HTTP_200_OK, flattened.text
    assert flattened.json()["items"][0]["part_number"] == component.part_number

    updated = client.put(f"/api/v1/bom/items/{line.id}", headers=head, json={"quantity": 2.0})
    assert updated.status_code == status.HTTP_200_OK, updated.text
    assert updated.json()["component_part"]["part_number"] == component.part_number


def test_the_has_bom_flag_is_computed_against_the_callers_company(client: TestClient, db_session: Session):
    """``get_component_part_info`` took ``company_id`` as a required argument precisely so it
    could stop deriving the scope from ``part.company_id`` — data the caller does not own.

    This pins the flag still works for the legitimate case (a same-company component that has
    its own BOM reports ``has_bom: true``), which is what makes the signature change safe
    rather than merely defensive. A sub-assembly with its own BOM is how the UI knows a line
    is explodable."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    top = make_part(db_session, company_id=COMPANY_B)
    sub = make_part(db_session, company_id=COMPANY_B)
    leaf = make_part(db_session, company_id=COMPANY_B, part_type="raw_material")
    sub_bom = make_bom(db_session, part_id=sub.id, company_id=COMPANY_B)
    add_line(db_session, bom_id=sub_bom.id, component_part_id=leaf.id, company_id=COMPANY_B)
    top_bom = make_bom(db_session, part_id=top.id, company_id=COMPANY_B)
    add_line(db_session, bom_id=top_bom.id, component_part_id=sub.id, company_id=COMPANY_B)

    response = client.get(f"/api/v1/bom/{top_bom.id}", headers=headers_for(user_b))

    assert response.status_code == status.HTTP_200_OK, response.text
    component = response.json()["items"][0]["component_part"]
    assert component["id"] == sub.id
    assert component["has_bom"] is True, "a same-company sub-assembly with its own BOM is explodable"
