"""A BOM line may only ever name a component part of the SAME company (invariant #1).

Three lookups in ``api/endpoints/bom.py`` resolved a ``Part`` with no company filter, and
this file is one test per lookup plus the positive control that keeps them honest:

1. ``create_bom`` — the inline-items loop (``POST /bom/``).
2. ``add_bom_item`` — the component lookup (``POST /bom/{id}/items``).
3. ``add_bom_item`` — the ``parent_part`` lookup behind ``customer_name`` inheritance.

**Why it was not merely a disclosure.** Company B could POST a BOM line naming a
``component_part_id`` owned by Company A. Three distinct harms fell out of the one missing
filter, and each gets its own assertion below because each fails differently:

* **READ** — ``build_bom_item_response`` echoed A's ``part_number`` and ``name`` straight
  back to B. On this system a part number IS the customer's design identity, so the leak is
  the finding even when nothing is written.
* **WRITE onto B's row** — since the unit-of-measure change, ``_resolve_line_uom`` stamps
  the resolved component's stocking unit onto the new ``bom_items`` row, so an unscoped
  component put A's ``sheets`` on B's line: A's master data silently steering B's backflush.
* **WRITE onto A's row** — ``component.customer_name = parent_part.customer_name`` mutates
  the COMPONENT. With A's part resolved as the component, that is a cross-tenant write into
  another company's master data, committed and audited to nobody. This is the half that
  cannot be undone by fixing a response shape, so ``test_..._foreign_part_row_is_unchanged``
  asserts the stored row, not the response.

**The positive control is not decoration.** Every refusal test here also passes against an
endpoint that refuses *everything*, so §3 pins the same-company behaviour — the line is
created, the component's unit is inherited, and same-company ``customer_name`` inheritance
(a real feature, not collateral of the bug) still happens.

Traps that shaped the fixtures
------------------------------
* **The third lookup needs a MIS-PARENTED BOM to be reachable at all.** ``bom`` is already
  tenant-scoped, so ``bom.part_id`` normally names this company's own part. The unscoped
  ``parent_part`` query only crosses a tenant boundary when a BOM row points at a foreign
  part — constructible here at the model layer (SQLite does not enforce foreign keys, and
  no FK in the schema would catch it on Postgres either, because ``company_id`` is not part
  of the reference). That is exactly the corrupt state the filter has to survive, and with
  it the endpoint must inherit NOTHING rather than copy a foreign customer's name.
* **The refusals must not leak through the error string either.** ``create_bom`` answers
  400 naming only the id the caller already sent; ``add_bom_item`` answers a flat 404
  ("Component part not found") so a foreign id cannot be probed by comparing messages
  against a genuinely-absent id. Both are asserted on the whole response body, not on the
  parsed ``detail``, so a future response shape that adds the part number anywhere fails.
* **Read the foreign row FRESH.** ``db_session`` is shared with the app, so the assertion
  expires the identity map first — otherwise a stale in-memory copy could pass while the
  committed row holds the leaked value.
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

# Sentinels. Distinctive enough that a substring search over a whole response body is a
# real leak check rather than a coincidence, and shaped like the data that actually matters
# on this system: a customer's part number and the customer's own name.
FOREIGN_PART_NUMBER = "ACME-SECRET-77821"
FOREIGN_PART_NAME = "Classified Bracket Assembly"
FOREIGN_CUSTOMER = "ACME AEROSPACE (COMPANY A CUSTOMER)"

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
        email=f"bom-iso-{n}@co{company_id}.test",
        employee_id=f"BOMISO-{n:05d}",
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


def make_part(
    db: Session,
    *,
    company_id: int,
    part_number: str = None,
    name: str = None,
    uom: str = "each",
    part_type: str = "manufactured",
    customer_name: str = None,
) -> Part:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=part_number or f"ISO-P-{n}",
        name=name or f"Isolation part {n}",
        description="tenant-isolation fixture part",
        part_type=part_type,
        unit_of_measure=uom,
        standard_cost=5.0,
        is_active=True,
        customer_name=customer_name,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_bom(db: Session, *, part_id: int, company_id: int) -> BOM:
    """A BOM row. ``part_id`` is passed explicitly so a MIS-PARENTED BOM is constructible."""
    bom = BOM(part_id=part_id, revision="A", status="draft", is_active=True, company_id=company_id)
    db.add(bom)
    db.commit()
    db.refresh(bom)
    return bom


def foreign_part(db: Session) -> Part:
    """Company A's part: the one Company B must never be able to name, read or write."""
    return make_part(
        db,
        company_id=COMPANY_A,
        part_number=FOREIGN_PART_NUMBER,
        name=FOREIGN_PART_NAME,
        uom="sheets",
        part_type="raw_material",
        customer_name=None,
    )


def assert_discloses_nothing(response) -> None:
    """Neither identifier may appear ANYWHERE in the body — detail, echo or field."""
    body = response.text
    assert FOREIGN_PART_NUMBER not in body, f"leaked the foreign part number: {body}"
    assert FOREIGN_PART_NAME not in body, f"leaked the foreign part name: {body}"


def reload_part(db: Session, part_id: int) -> Part:
    """The row as COMMITTED, not as the shared session last remembered it."""
    db.expire_all()
    return db.get(Part, part_id)


# ===========================================================================
# 1. POST /bom/ — the inline-items loop
# ===========================================================================


def test_create_bom_refuses_an_inline_item_naming_another_companys_part(client: TestClient, db_session: Session):
    """Company B builds a BOM whose one line names Company A's part. It must be refused,
    and the refusal must not tell B what that part is called."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    assembly_b = make_part(db_session, company_id=COMPANY_B)
    stolen = foreign_part(db_session)

    response = client.post(
        "/api/v1/bom/",
        headers=headers_for(user_b),
        json={
            "part_id": assembly_b.id,
            "revision": "A",
            "items": [{"component_part_id": stolen.id, "item_number": 10, "quantity": 3, "item_type": "buy"}],
        },
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
    assert_discloses_nothing(response)
    assert db_session.query(BOMItem).filter(BOMItem.component_part_id == stolen.id).count() == 0


def test_create_bom_does_not_write_the_foreign_parts_unit_onto_the_new_line(client: TestClient, db_session: Session):
    """The write-onto-B's-row half of the same lookup.

    ``_resolve_line_uom`` stamps the resolved component's stocking unit on the line, so an
    unscoped component let Company A's ``sheets`` decide how Company B's backflush issues
    material. No line may survive the refusal at all — asserted on the whole company, so a
    partially-committed loop (line 1 legitimate, line 2 foreign) is caught too.
    """
    user_b = make_user(db_session, company_id=COMPANY_B)
    assembly_b = make_part(db_session, company_id=COMPANY_B)
    own_component = make_part(db_session, company_id=COMPANY_B, uom="pounds", part_type="raw_material")
    stolen = foreign_part(db_session)

    response = client.post(
        "/api/v1/bom/",
        headers=headers_for(user_b),
        json={
            "part_id": assembly_b.id,
            "revision": "A",
            "items": [
                {"component_part_id": own_component.id, "item_number": 10, "quantity": 1, "item_type": "buy"},
                {"component_part_id": stolen.id, "item_number": 20, "quantity": 1, "item_type": "buy"},
            ],
        },
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
    assert_discloses_nothing(response)
    lines = db_session.query(BOMItem).filter(BOMItem.company_id == COMPANY_B).all()
    assert lines == [], "a refused create must leave no line behind, not even the legitimate one"
    assert "sheets" not in response.text, "the foreign part's stocking unit must not surface either"


# ===========================================================================
# 2. POST /bom/{id}/items — the component lookup, and the write onto A's row
# ===========================================================================


def test_add_bom_item_refuses_another_companys_part(client: TestClient, db_session: Session):
    """404 rather than 403: a foreign id must be indistinguishable from a missing one, or
    the status code itself becomes an existence oracle."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    assembly_b = make_part(db_session, company_id=COMPANY_B)
    bom_b = make_bom(db_session, part_id=assembly_b.id, company_id=COMPANY_B)
    stolen = foreign_part(db_session)

    response = client.post(
        f"/api/v1/bom/{bom_b.id}/items",
        headers=headers_for(user_b),
        json={"component_part_id": stolen.id, "item_number": 10, "quantity": 2, "item_type": "buy"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert_discloses_nothing(response)
    assert db_session.query(BOMItem).filter(BOMItem.component_part_id == stolen.id).count() == 0


def test_add_bom_item_refuses_a_foreign_id_exactly_like_an_absent_one(client: TestClient, db_session: Session):
    """The two refusals must be byte-identical. A different status or a different sentence
    would let Company B enumerate which ids exist in other tenants."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    assembly_b = make_part(db_session, company_id=COMPANY_B)
    bom_b = make_bom(db_session, part_id=assembly_b.id, company_id=COMPANY_B)
    stolen = foreign_part(db_session)

    def add(component_part_id: int):
        return client.post(
            f"/api/v1/bom/{bom_b.id}/items",
            headers=headers_for(user_b),
            json={"component_part_id": component_part_id, "item_number": 10, "quantity": 2, "item_type": "buy"},
        )

    foreign = add(stolen.id)
    absent = add(stolen.id + 900_000)

    assert (foreign.status_code, foreign.json()) == (absent.status_code, absent.json())


def test_the_foreign_part_row_is_unchanged_after_a_refused_add(client: TestClient, db_session: Session):
    """THE cross-tenant WRITE half, and the reason this file exists.

    ``component.customer_name = parent_part.customer_name`` mutates the resolved COMPONENT.
    With Company A's part resolved as that component, Company B's request wrote A's row —
    stamping B's customer name onto A's master data, committed, with no audit trail A could
    ever see. The fixture therefore gives B's assembly a customer name (so the inheritance
    branch is live and would fire) and leaves A's part without one (so it is eligible).
    Asserted against the STORED row, because a correct-looking response proves nothing here.
    """
    user_b = make_user(db_session, company_id=COMPANY_B)
    assembly_b = make_part(db_session, company_id=COMPANY_B, customer_name="COMPANY B CUSTOMER")
    bom_b = make_bom(db_session, part_id=assembly_b.id, company_id=COMPANY_B)
    stolen = foreign_part(db_session)
    before = (stolen.customer_name, stolen.unit_of_measure, stolen.name, stolen.part_number, stolen.company_id)
    assert before[0] is None, "fixture must leave the foreign part eligible for inheritance"

    response = client.post(
        f"/api/v1/bom/{bom_b.id}/items",
        headers=headers_for(user_b),
        json={"component_part_id": stolen.id, "item_number": 10, "quantity": 2, "item_type": "buy"},
    )

    # The stored row is asserted BEFORE the status code on purpose: the write is the finding,
    # and a status assertion firing first would mask it behind "expected 404, got 200".
    after = reload_part(db_session, stolen.id)
    assert after.customer_name is None, "Company B wrote its customer name onto Company A's part"
    assert (
        after.customer_name,
        after.unit_of_measure,
        after.name,
        after.part_number,
        after.company_id,
    ) == before, "a refused request must leave the foreign row byte-identical"
    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text


def test_add_item_does_not_inherit_a_customer_name_from_a_foreign_parent_part(client: TestClient, db_session: Session):
    """The THIRD lookup: ``parent_part``, reachable only through a mis-parented BOM.

    Company B's BOM row points at Company A's part (see the module note — nothing in the
    schema prevents it). Unscoped, ``parent_part`` resolved A's part and copied A's customer
    name onto B's component: Company A's customer identity landing in Company B's master
    data through a request neither company made. Scoped, the parent simply does not resolve
    and NOTHING is inherited — the corrupt row is survived, not propagated.
    """
    user_b = make_user(db_session, company_id=COMPANY_B)
    foreign_assembly = make_part(
        db_session,
        company_id=COMPANY_A,
        part_number=FOREIGN_PART_NUMBER,
        name=FOREIGN_PART_NAME,
        customer_name=FOREIGN_CUSTOMER,
    )
    mis_parented = make_bom(db_session, part_id=foreign_assembly.id, company_id=COMPANY_B)
    component_b = make_part(db_session, company_id=COMPANY_B, uom="pounds", part_type="raw_material")
    assert component_b.customer_name is None

    response = client.post(
        f"/api/v1/bom/{mis_parented.id}/items",
        headers=headers_for(user_b),
        json={"component_part_id": component_b.id, "item_number": 10, "quantity": 2, "item_type": "buy"},
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert FOREIGN_CUSTOMER not in response.text, "leaked Company A's customer name in the response"
    assert_discloses_nothing(response)
    assert reload_part(db_session, component_b.id).customer_name is None, "inherited a foreign customer name"


# ===========================================================================
# 3. THE POSITIVE CONTROL — without this, an endpoint that refuses EVERYTHING passes §1–§2
# ===========================================================================


def test_same_company_component_still_creates_a_bom_with_its_line(client: TestClient, db_session: Session):
    """``POST /bom/`` unchanged for the legitimate case: the line is created and it still
    inherits the component part's unit."""
    user_b = make_user(db_session, company_id=COMPANY_B)
    assembly_b = make_part(db_session, company_id=COMPANY_B)
    sheet_b = make_part(db_session, company_id=COMPANY_B, uom="sheets", part_type="raw_material")
    foreign_part(db_session)  # present the whole time, and irrelevant

    response = client.post(
        "/api/v1/bom/",
        headers=headers_for(user_b),
        json={
            "part_id": assembly_b.id,
            "revision": "A",
            "items": [{"component_part_id": sheet_b.id, "item_number": 10, "quantity": 3, "item_type": "buy"}],
        },
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["component_part_id"] == sheet_b.id
    assert items[0]["unit_of_measure"] == "sheets"


def test_same_company_component_still_adds_and_still_inherits_the_customer_name(
    client: TestClient, db_session: Session
):
    """``POST /bom/{id}/items`` unchanged for the legitimate case.

    The ``customer_name`` inheritance is a real feature — the scoping fix must narrow WHICH
    parts it can touch, not switch it off. Same company: it still fires, on both the line
    and the component row.
    """
    user_b = make_user(db_session, company_id=COMPANY_B)
    assembly_b = make_part(db_session, company_id=COMPANY_B, customer_name="COMPANY B CUSTOMER")
    bom_b = make_bom(db_session, part_id=assembly_b.id, company_id=COMPANY_B)
    sheet_b = make_part(db_session, company_id=COMPANY_B, uom="sheets", part_type="raw_material")
    foreign_part(db_session)

    response = client.post(
        f"/api/v1/bom/{bom_b.id}/items",
        headers=headers_for(user_b),
        json={"component_part_id": sheet_b.id, "item_number": 10, "quantity": 2, "item_type": "buy"},
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["component_part_id"] == sheet_b.id
    assert body["unit_of_measure"] == "sheets"
    assert reload_part(db_session, sheet_b.id).customer_name == "COMPANY B CUSTOMER"


def test_each_company_only_ever_sees_its_own_bom_line(client: TestClient, db_session: Session):
    """The end-to-end shape of the invariant: two companies, the same workflow, no overlap.

    Company A's part number must not appear in anything Company B can read back — including
    the BOM read that ``create_bom`` returns through.
    """
    user_a = make_user(db_session, company_id=COMPANY_A)
    user_b = make_user(db_session, company_id=COMPANY_B)
    assembly_a = make_part(db_session, company_id=COMPANY_A)
    stolen = foreign_part(db_session)
    bom_a = make_bom(db_session, part_id=assembly_a.id, company_id=COMPANY_A)

    added_a = client.post(
        f"/api/v1/bom/{bom_a.id}/items",
        headers=headers_for(user_a),
        json={"component_part_id": stolen.id, "item_number": 10, "quantity": 1, "item_type": "buy"},
    )
    assert added_a.status_code == status.HTTP_200_OK, added_a.text
    assert FOREIGN_PART_NUMBER in added_a.text, "its OWN part is exactly what Company A should see"

    denied_b = client.get(f"/api/v1/bom/{bom_a.id}", headers=headers_for(user_b))
    assert denied_b.status_code == status.HTTP_404_NOT_FOUND, denied_b.text
    assert_discloses_nothing(denied_b)
