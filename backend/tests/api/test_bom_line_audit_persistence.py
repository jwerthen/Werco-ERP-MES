"""BOM-line writes are audited, tenant-safe, and warn when the part is armed for backflush.

Three endpoints in ``app.api.endpoints.bom`` took NO ``AuditService`` at all, so creating,
editing or deleting a line on a BOM -- a controlled document the shop builds parts from --
left no record of any kind. The import paths in the same file always logged. This suite
locks the gap closed and pins the three adjacent defects fixed alongside it.

What is proved, and why each one needs its own test
--------------------------------------------------

1. **The audit rows COMMIT** (§1). Not "are written" -- committed. See the guard below.
2. **``work_center_id`` is tenant-checked on EVERY write path that accepts it** (§2). The
   component part and the parent part were already scoped; ``work_center_id`` rode in
   unchecked, so a caller in company B could point a BOM line at company A's machine.
   There are FOUR BOM-line write paths (``grep "BOMItem("``): ``add_bom_item``,
   ``update_bom_item``, ``create_bom``'s inline ``items``, and the two importers -- which
   never set the field, so they have nothing to check. A first pass here covered only the
   first two and left ``create_bom`` open, which is why each door gets its own test.
3. **``has_bom`` respects soft delete** (§3). ``BOM`` carries ``SoftDeleteMixin`` and the
   probe ignored it, so a deleted BOM made the response claim a component is an assembly
   -- which is what drives the expand affordance in the BOM tree.
4. **The armed-part warning WARNS and does not REFUSE** (§4). ``Part.backflush_components``
   gates whether BOM demand leaves stock at completion, and its opt-in gate is a ONE-TIME
   check at the instant of the flip (docs/MATERIAL_CONSUMPTION_PLAN.md -> "Exposing the
   flag"). Nothing on the edit path knew a part was armed. The signal added here is a
   response field plus an audit annotation; ``test_editing_an_armed_parts_bom_still_succeeds``
   is the guard against someone later "upgrading" it to a 409, which the plan
   deliberately declined to build.

The guard: committed, not merely flushed
----------------------------------------
``_committed_audit_rows`` calls ``db.rollback()`` BEFORE querying. The ``client`` fixture
shares ONE never-closed session with the endpoint, so a flushed-but-uncommitted audit row
is fully visible to a plain ``db.query(AuditLog)`` -- a naive assertion passes against
audit-after-``db.commit()`` code, which is precisely the bug the ordering exists to
prevent (``AuditService.log()`` only flushes; a call after the commit lands in a fresh
transaction that ``get_db`` teardown rolls back). A committed row survives the rollback; a
flushed one does not. Same technique as the rest of the ``test_*_audit_persistence.py``
family.

``AuditLog`` rows are NEVER inserted directly here (tamper-evident hash chain) -- they are
produced by the endpoints and only read back.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.bom import BOM, BOMItem
from app.models.company import Company
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"

# Module-level counter so every fixture row gets a globally unique natural key even
# across tests sharing a worker DB under -n auto.
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


# ---------------------------------------------------------------------------
# Fixtures (local, like every sibling suite in this feature)
# ---------------------------------------------------------------------------


def _ensure_company(db: Session, company_id: int = COMPANY_A) -> Company:
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
        email=f"bomaudit-{n}@co{company_id}.test",
        employee_id=f"BOMAUD-{n:05d}",
        first_name="Bom",
        last_name="Audit",
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
    uom: str = "each",
    part_type: str = "manufactured",
    company_id: int = COMPANY_A,
) -> Part:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"BA-P-{n}",
        name=f"Part {n}",
        description="bom-line-audit fixture part",
        part_type=part_type,
        unit_of_measure=uom,
        standard_cost=5.0,
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


def make_work_center(db: Session, *, company_id: int = COMPANY_A) -> WorkCenter:
    _ensure_company(db, company_id)
    n = _next()
    wc = WorkCenter(
        code=f"BA-WC-{n}",
        name=f"Work Center {n}",
        work_center_type="fabrication",
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def raw_line(
    db: Session,
    bom: BOM,
    component: Part,
    *,
    item_number: int = 10,
    quantity: float = 1.0,
    item_type: str = "buy",
    unit_of_measure: str = "each",
    company_id: int = COMPANY_A,
) -> BOMItem:
    """A BOM line written at the MODEL layer, bypassing the write paths under test."""
    item = BOMItem(
        bom_id=bom.id,
        component_part_id=component.id,
        item_number=item_number,
        quantity=quantity,
        item_type=item_type,
        line_type="component",
        unit_of_measure=unit_of_measure,
        company_id=company_id,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def add_item(client: TestClient, user: User, bom_id: int, component: Part, **overrides):
    body = {
        "component_part_id": component.id,
        "item_number": overrides.pop("item_number", 10),
        "quantity": overrides.pop("quantity", 2),
        "item_type": overrides.pop("item_type", "buy"),
    }
    body.update(overrides)
    return client.post(f"/api/v1/bom/{bom_id}/items", headers=headers_for(user), json=body)


def arm(client: TestClient, user: User, part_id: int):
    """Arm a part for automatic backflush through the REAL gate (no direct column write)."""
    return client.put(
        f"/api/v1/parts/{part_id}",
        headers=headers_for(user),
        json={"version": 0, "backflush_components": True},
    )


def _committed_audit_rows(db: Session, *, resource_id: int, action: str = None) -> list:
    """``bom_line`` AuditLog rows that actually COMMITTED, not merely flushed.

    The rollback is the whole point -- see the module docstring.
    """
    db.rollback()
    db.expire_all()
    query = db.query(AuditLog).filter(AuditLog.resource_type == "bom_line", AuditLog.resource_id == resource_id)
    if action:
        query = query.filter(AuditLog.action == action)
    return query.order_by(AuditLog.sequence_number.desc()).all()


# ===========================================================================
# §1 -- every BOM-line verb writes a COMMITTED audit row
# ===========================================================================


def test_add_bom_item_writes_a_committed_create_row(client: TestClient, db_session: Session):
    """Would FAIL against the old no-audit code AND against audit-after-commit code:
    both leave zero rows surviving the rollback."""
    user = make_user(db_session)
    assembly = make_part(db_session)
    component = make_part(db_session, part_type="raw_material")
    bom = make_bom(db_session, assembly)

    response = add_item(client, user, bom.id, component, quantity=4)
    assert response.status_code == status.HTTP_200_OK, response.text
    item_id = response.json()["id"]

    rows = _committed_audit_rows(db_session, resource_id=item_id, action="CREATE")

    assert len(rows) == 1, "exactly one committed CREATE row for the new BOM line"
    row = rows[0]
    assert row.company_id == COMPANY_A, "the row is tenant-tagged"
    assert row.user_id == user.id, "the row names who did it"
    # The identifier is the human handle an auditor reads by: assembly, line, component.
    assert assembly.part_number in row.resource_identifier
    assert component.part_number in row.resource_identifier
    assert "line 10" in row.resource_identifier


def test_update_bom_item_writes_a_committed_update_row_with_the_diff(client: TestClient, db_session: Session):
    user = make_user(db_session)
    assembly = make_part(db_session)
    component = make_part(db_session, part_type="raw_material")
    bom = make_bom(db_session, assembly)
    line = raw_line(db_session, bom, component, quantity=1.0)

    response = client.put(
        f"/api/v1/bom/items/{line.id}",
        headers=headers_for(user),
        json={"quantity": 7.0},
    )
    assert response.status_code == status.HTTP_200_OK, response.text

    rows = _committed_audit_rows(db_session, resource_id=line.id, action="UPDATE")

    assert len(rows) == 1, "exactly one committed UPDATE row"
    changes = (rows[0].extra_data or {}).get("changes") or {}
    assert "quantity" in changes, f"the diff must name the field that moved, got {changes}"


def test_an_idempotent_update_writes_no_row(client: TestClient, db_session: Session):
    """``log_update`` self-suppresses on an empty diff. A PUT that restates the current
    values changed nothing, so putting a row on the tamper-evident chain would be noise --
    the same behaviour ``update_work_center`` relies on."""
    user = make_user(db_session)
    assembly = make_part(db_session)
    component = make_part(db_session, part_type="raw_material")
    bom = make_bom(db_session, assembly)
    line = raw_line(db_session, bom, component, quantity=3.0)

    response = client.put(
        f"/api/v1/bom/items/{line.id}",
        headers=headers_for(user),
        json={"quantity": 3.0},
    )
    assert response.status_code == status.HTTP_200_OK, response.text

    assert _committed_audit_rows(db_session, resource_id=line.id, action="UPDATE") == []


def test_delete_bom_item_writes_a_committed_delete_row_carrying_the_pre_image(client: TestClient, db_session: Session):
    """The delete is PHYSICAL (``BOMItem`` has no ``SoftDeleteMixin``), so the audit row is
    the ONLY surviving record of the line. It must therefore carry the full pre-image and
    say the delete was hard -- otherwise the row is gone with nothing to reconstruct it
    from."""
    user = make_user(db_session)
    assembly = make_part(db_session)
    component = make_part(db_session, part_type="raw_material")
    bom = make_bom(db_session, assembly)
    line = raw_line(db_session, bom, component, quantity=9.0, item_number=40)
    line_id = line.id

    response = client.delete(f"/api/v1/bom/items/{line_id}", headers=headers_for(user))
    assert response.status_code == status.HTTP_200_OK, response.text

    rows = _committed_audit_rows(db_session, resource_id=line_id, action="DELETE")

    assert len(rows) == 1, "exactly one committed DELETE row"
    row = rows[0]
    assert (row.extra_data or {}).get("soft_delete") is False, "the row must state the delete was physical"
    old = row.old_values or {}
    assert float(old.get("quantity")) == 9.0, f"the pre-image must survive the row, got {old}"
    assert old.get("component_part_id") == component.id
    # And the row really is gone -- this is an audited hard delete, not a tombstone.
    assert db_session.get(BOMItem, line_id) is None


# ===========================================================================
# §2 -- work_center_id is tenant-checked on BOTH write paths
# ===========================================================================


def test_add_bom_item_refuses_a_foreign_work_center(client: TestClient, db_session: Session):
    """404, not 403, so a foreign id cannot be probed -- matching the component check
    immediately above it in the same handler."""
    user = make_user(db_session)
    assembly = make_part(db_session)
    component = make_part(db_session, part_type="raw_material")
    bom = make_bom(db_session, assembly)
    foreign_wc = make_work_center(db_session, company_id=COMPANY_B)

    response = add_item(client, user, bom.id, component, work_center_id=foreign_wc.id)

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert "Work center not found" in response.json()["detail"]
    db_session.rollback()
    assert db_session.query(BOMItem).filter(BOMItem.bom_id == bom.id).count() == 0, "a refusal writes no line"


def test_add_bom_item_accepts_an_own_work_center(client: TestClient, db_session: Session):
    """The negative control: the guard must not have broken the legitimate case."""
    user = make_user(db_session)
    assembly = make_part(db_session)
    component = make_part(db_session, part_type="raw_material")
    bom = make_bom(db_session, assembly)
    own_wc = make_work_center(db_session)

    response = add_item(client, user, bom.id, component, work_center_id=own_wc.id)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["work_center_id"] == own_wc.id


def test_create_bom_with_inline_items_refuses_a_foreign_work_center(client: TestClient, db_session: Session):
    """The THIRD door, and the one a first pass at this missed.

    ``POST /bom/`` accepts its lines inline and splats ``BOMItemCreate.model_dump()``
    straight into ``BOMItem(...)``, so guarding only the two ``/bom/items`` endpoints left
    this one wide open -- same field, same harm, different URL.
    """
    user = make_user(db_session)
    assembly = make_part(db_session)
    component = make_part(db_session, part_type="raw_material")
    foreign_wc = make_work_center(db_session, company_id=COMPANY_B)

    response = client.post(
        "/api/v1/bom/",
        headers=headers_for(user),
        json={
            "part_id": assembly.id,
            "revision": "A",
            "items": [
                {
                    "component_part_id": component.id,
                    "item_number": 10,
                    "quantity": 1,
                    "item_type": "buy",
                    "work_center_id": foreign_wc.id,
                }
            ],
        },
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert "Work center not found" in response.json()["detail"]
    db_session.rollback()
    assert (
        db_session.query(BOMItem).filter(BOMItem.work_center_id == foreign_wc.id).count() == 0
    ), "a refusal must not store the foreign work-center id"


def test_create_bom_with_inline_items_accepts_an_own_work_center(client: TestClient, db_session: Session):
    """Negative control for the guard above."""
    user = make_user(db_session)
    assembly = make_part(db_session)
    component = make_part(db_session, part_type="raw_material")
    own_wc = make_work_center(db_session)

    response = client.post(
        "/api/v1/bom/",
        headers=headers_for(user),
        json={
            "part_id": assembly.id,
            "revision": "A",
            "items": [
                {
                    "component_part_id": component.id,
                    "item_number": 10,
                    "quantity": 1,
                    "item_type": "buy",
                    "work_center_id": own_wc.id,
                }
            ],
        },
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["items"][0]["work_center_id"] == own_wc.id


def test_update_bom_item_refuses_a_foreign_work_center(client: TestClient, db_session: Session):
    """The update path is settable too, so guarding only the create path would leave the
    hole wide open through a second door."""
    user = make_user(db_session)
    assembly = make_part(db_session)
    component = make_part(db_session, part_type="raw_material")
    bom = make_bom(db_session, assembly)
    line = raw_line(db_session, bom, component)
    foreign_wc = make_work_center(db_session, company_id=COMPANY_B)

    response = client.put(
        f"/api/v1/bom/items/{line.id}",
        headers=headers_for(user),
        json={"work_center_id": foreign_wc.id},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    db_session.rollback()
    db_session.expire_all()
    assert db_session.get(BOMItem, line.id).work_center_id is None, "a refusal leaves the row untouched"


def test_update_bom_item_still_allows_clearing_the_work_center(client: TestClient, db_session: Session):
    """An explicit null DETACHES the reference and must not be validated -- there is no id
    to own."""
    user = make_user(db_session)
    assembly = make_part(db_session)
    component = make_part(db_session, part_type="raw_material")
    bom = make_bom(db_session, assembly)
    own_wc = make_work_center(db_session)
    line = raw_line(db_session, bom, component)
    line.work_center_id = own_wc.id
    db_session.commit()

    response = client.put(
        f"/api/v1/bom/items/{line.id}",
        headers=headers_for(user),
        json={"work_center_id": None},
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    db_session.rollback()
    db_session.expire_all()
    assert db_session.get(BOMItem, line.id).work_center_id is None


# ===========================================================================
# §3 -- has_bom respects soft delete (and tenancy)
# ===========================================================================


def test_a_soft_deleted_bom_does_not_make_a_component_look_like_an_assembly(client: TestClient, db_session: Session):
    """``has_bom`` drives the expand/drill-down affordance in the BOM tree. A soft-deleted
    BOM used to satisfy the probe, offering a drill-down into a structure the shop
    deleted."""
    user = make_user(db_session)
    assembly = make_part(db_session)
    sub_assembly = make_part(db_session)
    make_bom(db_session, sub_assembly, is_deleted=True)  # the sub-assembly's BOM is deleted
    bom = make_bom(db_session, assembly)

    response = add_item(client, user, bom.id, sub_assembly)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["component_part"]["has_bom"] is False, "a deleted BOM is not a BOM"

    # And the same answer on the read path the BOM tree actually renders from.
    read = client.get(f"/api/v1/bom/{bom.id}", headers=headers_for(user))
    assert read.status_code == status.HTTP_200_OK, read.text
    assert read.json()["items"][0]["component_part"]["has_bom"] is False


def test_a_live_bom_still_marks_a_component_as_an_assembly(client: TestClient, db_session: Session):
    """Negative control for the predicate above."""
    user = make_user(db_session)
    assembly = make_part(db_session)
    sub_assembly = make_part(db_session)
    make_bom(db_session, sub_assembly)
    bom = make_bom(db_session, assembly)

    response = add_item(client, user, bom.id, sub_assembly)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["component_part"]["has_bom"] is True


# ===========================================================================
# §4 -- the backflush-armed warning: WARN, never REFUSE
# ===========================================================================


def _armed_assembly(client: TestClient, db_session: Session, user: User):
    """An assembly armed for automatic backflush, through the real opt-in gate."""
    assembly = make_part(db_session, part_type="manufactured")
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    bom = make_bom(db_session, assembly)
    first = add_item(client, user, bom.id, sheet, item_number=10, quantity=2)
    assert first.status_code == status.HTTP_200_OK, first.text
    armed = arm(client, user, assembly.id)
    assert armed.status_code == status.HTTP_200_OK, armed.text
    return assembly, bom, sheet


def test_adding_a_line_to_an_armed_parts_bom_warns_and_annotates_the_audit_row(client: TestClient, db_session: Session):
    user = make_user(db_session)
    assembly, bom, _ = _armed_assembly(client, db_session, user)
    another = make_part(db_session, uom="pounds", part_type="raw_material")

    response = add_item(client, user, bom.id, another, item_number=20, quantity=3)

    assert response.status_code == status.HTTP_200_OK, response.text
    warning = response.json()["backflush_armed_warning"]
    assert warning, "an armed part must produce a warning"
    assert assembly.part_number in warning
    assert "backflush-readiness" in warning, "the warning must say where to re-check"

    rows = _committed_audit_rows(db_session, resource_id=response.json()["id"], action="CREATE")
    assert len(rows) == 1
    assert (rows[0].extra_data or {}).get("backflush_armed_parts") == [assembly.part_number], (
        "the audit row carries the armed part so the arming verdict and this edit are " "correlatable on one chain"
    )


def test_editing_an_armed_parts_bom_still_succeeds(client: TestClient, db_session: Session):
    """THE contract of this feature, and the guard against it being 'upgraded' to a 409.

    The plan is explicit that the opt-in gate protects the instant of the flip and that the
    completion-time refusal is the NET behind it, *not* a second gate. A 409 here would
    also block its own remedy: the documented way to fix a blocking unit mismatch is
    ``PUT /bom/items/{id}``, which refusing on an armed part would make impossible without
    first disarming.
    """
    user = make_user(db_session)
    assembly, bom, sheet = _armed_assembly(client, db_session, user)
    line_id = db_session.query(BOMItem.id).filter(BOMItem.bom_id == bom.id).order_by(BOMItem.id.asc()).scalar()

    updated = client.put(f"/api/v1/bom/items/{line_id}", headers=headers_for(user), json={"quantity": 11.0})
    assert updated.status_code == status.HTTP_200_OK, "the write must SUCCEED -- this warns, it does not refuse"
    assert updated.json()["backflush_armed_warning"], "and it must say so"

    deleted = client.delete(f"/api/v1/bom/items/{line_id}", headers=headers_for(user))
    assert deleted.status_code == status.HTTP_200_OK, deleted.text
    assert deleted.json()["backflush_armed_warning"], "delete carries the warning on its own response shape"

    db_session.rollback()
    db_session.expire_all()
    assert db_session.get(Part, assembly.id).backflush_components is True, "the flag is untouched by an edit"


def test_an_unarmed_part_produces_no_warning_and_no_audit_annotation(client: TestClient, db_session: Session):
    """The signal has to be quiet by default: an empty key on every BOM-line row would be
    noise on a chain that is read by hand."""
    user = make_user(db_session)
    assembly = make_part(db_session)
    component = make_part(db_session, part_type="raw_material")
    bom = make_bom(db_session, assembly)

    response = add_item(client, user, bom.id, component)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["backflush_armed_warning"] is None

    rows = _committed_audit_rows(db_session, resource_id=response.json()["id"], action="CREATE")
    assert len(rows) == 1
    assert "backflush_armed_parts" not in (rows[0].extra_data or {})


def test_an_armed_ancestor_reached_through_a_phantom_line_is_named(client: TestClient, db_session: Session):
    """A phantom is a planning fiction that is never stocked: the backflush EXPLODES
    THROUGH it, so a line on the phantom's own BOM really does state the armed ancestor's
    demand."""
    user = make_user(db_session)
    top = make_part(db_session, part_type="manufactured")
    sub = make_part(db_session, part_type="manufactured")
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")

    sub_bom = make_bom(db_session, sub)
    assert add_item(client, user, sub_bom.id, sheet, item_number=10, quantity=2).status_code == 200

    top_bom = make_bom(db_session, top)
    assert add_item(client, user, top_bom.id, sub, item_number=10, item_type="phantom").status_code == 200

    assert arm(client, user, top.id).status_code == status.HTTP_200_OK

    # Now edit the SUB assembly's BOM -- the armed part is the grandparent.
    another = make_part(db_session, uom="pounds", part_type="raw_material")
    response = add_item(client, user, sub_bom.id, another, item_number=20)

    assert response.status_code == status.HTTP_200_OK, response.text
    warning = response.json()["backflush_armed_warning"]
    assert warning and top.part_number in warning, f"the phantom ancestor must be named, got {warning!r}"


def test_an_armed_ancestor_above_a_make_line_IS_named(client: TestClient, db_session: Session):
    """A ``make`` line is NOT a wall, and an earlier version of this walk got it backwards.

    The tempting rationale -- ``_explode_backflush_bom`` issues a ``make`` sub-assembly as a
    stocked unit and never issues its children, so an armed grandparent cannot be affected
    -- holds for the BOM DEMAND leg and is false for the leg beside it. The explosion still
    walks a ``make`` subtree in exclude-only mode, and every line it passes there lands in
    ``excluded_part_ids``; a routing operation naming a part in that set raises
    ``routing_component_excluded_by_bom`` at **BACKFLUSH_BLOCKING**.

    So an edit down here can newly BLOCK the armed ancestor's routing demand -- or, on a
    delete, newly UN-block it and let material issue that was being refused. Either way the
    armed part's behaviour changed and the editor must be told. (The structural
    ``bom_depth_exceeded`` diagnostic is a second such path: it fires before the
    ``consumed`` check, so deepening a ``make`` subtree past the level cap refuses the
    ancestor's whole leg.)

    This test replaced one that asserted the opposite and locked the false negative in.
    """
    user = make_user(db_session)
    top = make_part(db_session, part_type="manufactured")
    sub = make_part(db_session, part_type="manufactured")
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")

    sub_bom = make_bom(db_session, sub)
    assert add_item(client, user, sub_bom.id, sheet, item_number=10, quantity=2).status_code == 200

    top_bom = make_bom(db_session, top)
    assert add_item(client, user, top_bom.id, sub, item_number=10, item_type="make").status_code == 200

    assert arm(client, user, top.id).status_code == status.HTTP_200_OK

    another = make_part(db_session, uom="pounds", part_type="raw_material")
    response = add_item(client, user, sub_bom.id, another, item_number=20)

    assert response.status_code == status.HTTP_200_OK, response.text
    warning = response.json()["backflush_armed_warning"]
    assert warning and top.part_number in warning, (
        f"an armed ancestor above a make line MUST be named -- its routing leg can flip "
        f"between blocked and unblocked on this edit. Got {warning!r}"
    )


def test_nothing_below_a_buy_line_reaches_an_armed_ancestor(client: TestClient, db_session: Session):
    """``buy`` IS the wall, and it is the only one -- so this is the test that keeps the
    widened walk from becoming unbounded.

    ``_explode_backflush_bom`` gates its child-BOM lookup on
    ``if item_type != BOMItemType.BUY.value``, so under a ``buy`` line nothing below is read
    at all: no demand, no ``excluded_part_ids`` entry, no diagnostic. An edit down there
    genuinely cannot reach the ancestor, and claiming otherwise would make the warning cry
    wolf on every raw-material BOM in the shop.
    """
    user = make_user(db_session)
    top = make_part(db_session, part_type="manufactured")
    sub = make_part(db_session, part_type="manufactured")
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")

    sub_bom = make_bom(db_session, sub)
    assert add_item(client, user, sub_bom.id, sheet, item_number=10, quantity=2).status_code == 200

    top_bom = make_bom(db_session, top)
    assert add_item(client, user, top_bom.id, sub, item_number=10, item_type="buy").status_code == 200

    assert arm(client, user, top.id).status_code == status.HTTP_200_OK

    another = make_part(db_session, uom="pounds", part_type="raw_material")
    response = add_item(client, user, sub_bom.id, another, item_number=20)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["backflush_armed_warning"] is None, "a buy line is the wall; nothing below it is read"


def test_a_foreign_companys_armed_part_never_contributes(client: TestClient, db_session: Session):
    """Tenant scoping in the walk itself. The phantom parent row lives in company B and
    points at company A's part (SQLite does not enforce FKs, which is the only reason this
    state is constructible here); company A's edit must not learn it exists."""
    user = make_user(db_session)
    sub = make_part(db_session, part_type="manufactured")
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    sub_bom = make_bom(db_session, sub)
    assert add_item(client, user, sub_bom.id, sheet, item_number=10, quantity=2).status_code == 200

    foreign_top = make_part(db_session, part_type="manufactured", company_id=COMPANY_B)
    foreign_top.backflush_components = True
    foreign_bom = make_bom(db_session, foreign_top, company_id=COMPANY_B)
    db_session.add(
        BOMItem(
            bom_id=foreign_bom.id,
            component_part_id=sub.id,  # points across the tenant boundary
            item_number=10,
            quantity=1.0,
            item_type="phantom",
            line_type="component",
            unit_of_measure="each",
            company_id=COMPANY_B,
        )
    )
    db_session.commit()

    another = make_part(db_session, uom="pounds", part_type="raw_material")
    response = add_item(client, user, sub_bom.id, another, item_number=20)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["backflush_armed_warning"] is None, "a foreign armed part is not this tenant's business"


def test_a_soft_deleted_phantom_parent_bom_does_not_contribute(client: TestClient, db_session: Session):
    """The ascent filters ``BOM.is_deleted`` for the same reason the ``has_bom`` probe now
    does: a deleted BOM states no demand."""
    user = make_user(db_session)
    top = make_part(db_session, part_type="manufactured")
    top.backflush_components = True
    sub = make_part(db_session, part_type="manufactured")
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")

    sub_bom = make_bom(db_session, sub)
    raw_line(db_session, sub_bom, sheet, unit_of_measure="sheets", quantity=2)

    deleted_top_bom = make_bom(db_session, top, is_deleted=True)
    raw_line(db_session, deleted_top_bom, sub, item_type="phantom", item_number=10)
    db_session.commit()

    response = add_item(client, user, sub_bom.id, make_part(db_session, uom="pounds"), item_number=20)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["backflush_armed_warning"] is None


def test_a_cyclic_phantom_chain_terminates(client: TestClient, db_session: Session):
    """The visited set bounds repetition of a PART, not depth. A cycle in the ancestor
    graph must not spin -- this raises inside a WRITE handler if the walk is unbounded."""
    user = make_user(db_session)
    a = make_part(db_session, part_type="manufactured")
    b = make_part(db_session, part_type="manufactured")
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")

    bom_a = make_bom(db_session, a)
    bom_b = make_bom(db_session, b)
    # A's BOM phantom-contains B, and B's BOM phantom-contains A: a cycle going up.
    raw_line(db_session, bom_a, b, item_type="phantom", item_number=10)
    raw_line(db_session, bom_b, a, item_type="phantom", item_number=10)
    raw_line(db_session, bom_a, sheet, unit_of_measure="sheets", item_number=20)
    db_session.commit()

    response = add_item(client, user, bom_a.id, make_part(db_session, uom="pounds"), item_number=30)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["backflush_armed_warning"] is None, "nothing is armed, and the walk terminated"


def test_a_cyclic_make_chain_terminates(client: TestClient, db_session: Session):
    """Same guard, on the newly-followed edge. Widening the ascent to ``make`` widened what
    can form a cycle in the ancestor graph, so the visited set + ``_MAX_BOM_LEVELS`` cap has
    to hold there too -- an unbounded walk here raises inside a WRITE handler."""
    user = make_user(db_session)
    a = make_part(db_session, part_type="manufactured")
    b = make_part(db_session, part_type="manufactured")
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")

    bom_a = make_bom(db_session, a)
    bom_b = make_bom(db_session, b)
    raw_line(db_session, bom_a, b, item_type="make", item_number=10)
    raw_line(db_session, bom_b, a, item_type="make", item_number=10)
    raw_line(db_session, bom_a, sheet, unit_of_measure="sheets", item_number=20)
    db_session.commit()

    response = add_item(client, user, bom_a.id, make_part(db_session, uom="pounds"), item_number=30)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["backflush_armed_warning"] is None, "nothing is armed, and the walk terminated"


# ===========================================================================
# §5 -- the cost of the walk, because it runs in the Batch Add loop
# ===========================================================================


def _count_walk_queries(db_session: Session, bom, company_id: int = COMPANY_A) -> int:
    """SQL statements issued by one ``armed_parts_affected_by_bom`` call."""
    from sqlalchemy import event

    from app.services.completion_inventory_service import armed_parts_affected_by_bom

    # Warm the BOM's attributes BEFORE the listener goes on. ``db.commit()`` expires every
    # loaded object, so the walk's first read of ``bom.part_id`` would otherwise emit a
    # lazy refresh SELECT and be counted as walk cost -- an artifact of the fixture, not of
    # the walk (the endpoint hands it a freshly-loaded BOM).
    _ = bom.part_id

    statements: list = []
    bind = db_session.get_bind()

    def _before(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(bind, "before_cursor_execute", _before)
    try:
        armed_parts_affected_by_bom(db_session, bom, company_id=company_id)
    finally:
        event.remove(bind, "before_cursor_execute", _before)
    return len(statements)


def test_the_walk_costs_two_queries_on_an_ordinary_batch_add_line(client: TestClient, db_session: Session):
    """THE cost claim in the PR body, measured rather than asserted in prose.

    PartBOMTab's Batch Add issues one request per line (~40 for a real assembly), each of
    which now runs this walk. On the shape Batch Add is actually in -- lines being added to
    a top-level assembly's own BOM -- the walk must cost TWO indexed queries: one PK read of
    the BOM's own part, then one parent probe that comes back empty and ends the ascent. No
    recursive walk on an ordinary line.
    """
    assembly = make_part(db_session, part_type="manufactured")
    bom = make_bom(db_session, assembly)

    assert _count_walk_queries(db_session, bom) == 2


def test_each_further_ancestor_level_costs_exactly_one_more_query(client: TestClient, db_session: Session):
    """The other half of the claim: cost scales with ancestor LEVELS, not ancestors, and
    the widened (``make``-following) ascent did not turn into a per-row walk."""
    user = make_user(db_session)
    top = make_part(db_session, part_type="manufactured")
    mid = make_part(db_session, part_type="manufactured")
    low = make_part(db_session, part_type="manufactured")

    low_bom = make_bom(db_session, low)
    mid_bom = make_bom(db_session, mid)
    top_bom = make_bom(db_session, top)
    raw_line(db_session, mid_bom, low, item_type="make", item_number=10)
    raw_line(db_session, top_bom, mid, item_type="make", item_number=10)
    db_session.commit()

    # Nothing is armed, so the ascent runs to the top of the chain and stops on an empty
    # frontier: own-part read + one probe per level (low->mid, mid->top, top->nothing).
    assert _count_walk_queries(db_session, low_bom) == 4
    assert _count_walk_queries(db_session, mid_bom) == 3
    assert _count_walk_queries(db_session, top_bom) == 2

    # Arming the immediate parent short-circuits the ascent at the first armed level.
    db_session.get(Part, mid.id).backflush_components = True
    db_session.commit()
    assert _count_walk_queries(db_session, low_bom) == 2, "stop at the first armed level"
    assert user  # fixture used for symmetry with the suite's other tests
