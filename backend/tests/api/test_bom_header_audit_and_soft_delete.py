"""The five BOM *header* verbs write audit rows, and ``DELETE`` is a SOFT delete.

``create_bom`` / ``update_bom`` / ``release_bom`` / ``unrelease_bom`` / ``delete_bom`` took
no ``AuditService`` at all. A controlled document could be created, approved for
production, withdrawn from production and destroyed with **nothing on the tamper-evident
chain** -- while PR #194 had already audited the three BOM *line* verbs, so the record
showed lines being edited on documents that, as far as the chain knew, never existed.

Three of the five were worse than merely unaudited:

* ``release_bom`` is the APPROVAL of an AS9100D controlled document. ``approved_by`` on the
  row is current state, not a record of the transition -- ``unrelease_bom`` then NULLs it.
* ``unrelease_bom`` DESTROYED that approval evidence (``approved_by`` / ``approved_at`` /
  ``effective_date``) with no record anywhere that a named approval had ever existed. §4
  pins the pre-image onto the chain.
* ``delete_bom`` issued a physical ``db.delete(bom)`` plus a bulk ``delete()`` of every
  line -- on a model that carries ``SoftDeleteMixin``. That is a straight invariant-3
  violation, and §3 pins the conversion plus the read paths that have to honour it.

The guard: committed, not merely flushed
----------------------------------------
``_committed_rows`` calls ``db.rollback()`` BEFORE querying. The ``client`` fixture shares
ONE never-closed session with the endpoint, so a flushed-but-uncommitted audit row is fully
visible to a plain ``db.query(AuditLog)`` -- a naive assertion passes against
audit-*after*-``db.commit()`` code, which is exactly the trap ``AuditService.log()``'s
flush-only behaviour sets (a call placed after the commit opens a fresh transaction that
``get_db`` teardown rolls back, silently discarding the row). A committed row survives the
rollback; a flushed one does not. Same technique as ``test_bom_line_audit_persistence.py``.

``AuditLog`` rows are never inserted directly here -- they are produced by the endpoints
and only read back.
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


def make_user(db: Session, *, role: UserRole = UserRole.ADMIN, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"bomhdr-{n}@co{company_id}.test",
        employee_id=f"BOMHDR-{n:05d}",
        first_name="Bom",
        last_name="Header",
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


def make_part(db: Session, *, part_type: str = "manufactured", company_id: int = COMPANY_A) -> Part:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"BH-P-{n}",
        name=f"Part {n}",
        description="bom-header fixture part",
        part_type=part_type,
        unit_of_measure="each",
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
    revision: str = "A",
    status_value: str = "draft",
    is_active: bool = True,
    company_id: int = COMPANY_A,
) -> BOM:
    bom = BOM(
        part_id=part.id,
        revision=revision,
        status=status_value,
        is_active=is_active,
        is_deleted=False,
        company_id=company_id,
    )
    db.add(bom)
    db.commit()
    db.refresh(bom)
    return bom


def raw_line(db: Session, bom: BOM, component: Part, *, item_number: int = 10, company_id: int = COMPANY_A) -> BOMItem:
    """A BOM line written at the MODEL layer, bypassing the write paths under test."""
    item = BOMItem(
        bom_id=bom.id,
        component_part_id=component.id,
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


def released_bom_via_the_real_verb(client: TestClient, db: Session, user: User) -> tuple:
    """A genuinely RELEASED BOM, produced by ``POST /bom/{id}/release`` -- never by writing
    the column. Returns ``(bom, assembly, component)``."""
    assembly = make_part(db)
    component = make_part(db, part_type="raw_material")
    bom = make_bom(db, assembly)
    raw_line(db, bom, component)
    response = client.post(f"/api/v1/bom/{bom.id}/release", headers=headers_for(user))
    assert response.status_code == status.HTTP_200_OK, response.text
    db.expire_all()
    return db.get(BOM, bom.id), assembly, component


def _committed_rows(db: Session, *, resource_id: int, resource_type: str = "bom", action: str = None) -> list:
    """AuditLog rows that actually COMMITTED, not merely flushed. The rollback is the point."""
    db.rollback()
    db.expire_all()
    query = db.query(AuditLog).filter(AuditLog.resource_type == resource_type, AuditLog.resource_id == resource_id)
    if action:
        query = query.filter(AuditLog.action == action)
    return query.order_by(AuditLog.sequence_number.desc()).all()


def assert_tombstoned(db: Session, bom_id: int) -> None:
    """The row SURVIVED and is marked deleted.

    Paired with an invisibility assertion this is what makes the §3 read tests real rather
    than accidentally-satisfied. Invisibility alone is also true of a physical delete -- the
    OLD behaviour -- so a read test that only checks "it's gone from the list" passes against
    ``db.delete(bom)`` and proves nothing about the conversion. Asserting the tombstone
    EXISTS is the half that only a soft delete can satisfy.
    """
    db.rollback()
    db.expire_all()
    stored = db.get(BOM, bom_id)
    assert stored is not None, "invariant 3: the row must survive the delete (this was a hard delete)"
    assert stored.is_deleted is True, "the row survived but was not marked deleted"


def create_bom_request(client: TestClient, user: User, part: Part, items: list = None, **overrides):
    body = {"part_id": part.id, "revision": "A"}
    if items is not None:
        body["items"] = items
    body.update(overrides)
    return client.post("/api/v1/bom/", headers=headers_for(user), json=body)


# ===========================================================================
# §1 -- create_bom writes a COMMITTED header row, and one per inline line
# ===========================================================================


def test_create_bom_writes_a_committed_create_row_for_the_header(client: TestClient, db_session: Session):
    """WOULD FAIL AGAINST OLD CODE: ``create_bom`` took no ``AuditService`` and wrote no row
    at all -- zero rows, committed or otherwise. Also fails against an audit-after-commit
    implementation, because ``_committed_rows`` rolls back first."""
    user = make_user(db_session)
    assembly = make_part(db_session)

    response = create_bom_request(client, user, assembly)
    assert response.status_code == status.HTTP_200_OK, response.text
    bom_id = response.json()["id"]

    rows = _committed_rows(db_session, resource_id=bom_id, action="CREATE")

    assert len(rows) == 1, "exactly one committed CREATE row for the BOM header"
    row = rows[0]
    assert row.resource_type == "bom"
    assert row.company_id == COMPANY_A, "the row is tenant-tagged"
    assert row.user_id == user.id, "the row names who did it"
    # An auditor reads the trail by part number and revision, not by row id.
    assert assembly.part_number in row.resource_identifier
    assert "rev A" in row.resource_identifier


def test_create_bom_with_inline_lines_audits_every_line_too(client: TestClient, db_session: Session):
    """WOULD FAIL AGAINST OLD CODE: ``POST /bom/`` is BOM-line write path 3 of 4 and PR #194
    audited only the other three, so a BOM born with ten lines recorded none of them --
    while a line added a second later recorded one. Recording the edits but not the birth is
    not a record."""
    user = make_user(db_session)
    assembly = make_part(db_session)
    first = make_part(db_session, part_type="raw_material")
    second = make_part(db_session, part_type="raw_material")

    response = create_bom_request(
        client,
        user,
        assembly,
        items=[
            {"component_part_id": first.id, "item_number": 10, "quantity": 2, "item_type": "buy"},
            {"component_part_id": second.id, "item_number": 20, "quantity": 3, "item_type": "buy"},
        ],
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    line_ids = [item["id"] for item in body["items"]]
    assert len(line_ids) == 2

    for line_id in line_ids:
        rows = _committed_rows(db_session, resource_id=line_id, resource_type="bom_line", action="CREATE")
        assert len(rows) == 1, f"exactly one committed bom_line CREATE row for line {line_id}"
        assert assembly.part_number in rows[0].resource_identifier

    identifiers = {
        _committed_rows(db_session, resource_id=line_id, resource_type="bom_line", action="CREATE")[
            0
        ].resource_identifier
        for line_id in line_ids
    }
    assert any(first.part_number in ident for ident in identifiers), "the first component is named on the chain"
    assert any(second.part_number in ident for ident in identifiers), "the second component is named on the chain"


# ===========================================================================
# §2 -- update_bom / release_bom write COMMITTED rows
# ===========================================================================


def test_update_bom_writes_a_committed_update_row_with_the_diff(client: TestClient, db_session: Session):
    """WOULD FAIL AGAINST OLD CODE: ``update_bom`` wrote nothing."""
    user = make_user(db_session)
    assembly = make_part(db_session)
    bom = make_bom(db_session, assembly, revision="A")

    response = client.put(
        f"/api/v1/bom/{bom.id}",
        headers=headers_for(user),
        json={"revision": "B", "description": "second issue"},
    )
    assert response.status_code == status.HTTP_200_OK, response.text

    rows = _committed_rows(db_session, resource_id=bom.id, action="UPDATE")

    assert len(rows) == 1, "exactly one committed UPDATE row"
    row = rows[0]
    changes = (row.extra_data or {}).get("changes") or {}
    assert "revision" in changes, f"the diff must name the field that moved, got {changes}"
    assert "description" in changes
    # Named as the document was BEFORE the edit, so an auditor searching for the prior state
    # finds it; the new revision is in new_values.
    assert "rev A" in row.resource_identifier, row.resource_identifier
    assert (row.new_values or {}).get("revision") == "B"


def test_an_idempotent_bom_update_writes_no_row(client: TestClient, db_session: Session):
    """``log_update`` self-suppresses on an empty diff, and ``changed_fields`` is computed
    with ``update_operation`` semantics (present AND different) so a form-shaped client that
    PUTs the whole record back is not refused for changing nothing."""
    user = make_user(db_session)
    assembly = make_part(db_session)
    bom = make_bom(db_session, assembly, revision="A")

    response = client.put(f"/api/v1/bom/{bom.id}", headers=headers_for(user), json={"revision": "A"})
    assert response.status_code == status.HTTP_200_OK, response.text

    assert _committed_rows(db_session, resource_id=bom.id, action="UPDATE") == []


def test_release_bom_writes_a_committed_status_change_row(client: TestClient, db_session: Session):
    """WOULD FAIL AGAINST OLD CODE: the APPROVAL of a controlled document wrote nothing.
    ``approved_by`` on the row is current state and ``unrelease_bom`` NULLs it, so without
    this row there is no durable record that the approval ever happened."""
    user = make_user(db_session, role=UserRole.MANAGER)
    assembly = make_part(db_session)
    component = make_part(db_session, part_type="raw_material")
    bom = make_bom(db_session, assembly)
    raw_line(db_session, bom, component)

    response = client.post(f"/api/v1/bom/{bom.id}/release", headers=headers_for(user))
    assert response.status_code == status.HTTP_200_OK, response.text

    rows = _committed_rows(db_session, resource_id=bom.id, action="STATUS_CHANGE")

    assert len(rows) == 1, "exactly one committed STATUS_CHANGE row for the release"
    row = rows[0]
    assert (row.old_values or {}).get("status") == "draft"
    assert (row.new_values or {}).get("status") == "released"
    assert (row.extra_data or {}).get("approved_by") == user.id, "the row names the approver"
    assert (row.extra_data or {}).get("line_count") == 1
    assert assembly.part_number in row.resource_identifier


def test_release_refuses_a_bom_that_is_not_a_draft(client: TestClient, db_session: Session):
    """WOULD FAIL AGAINST OLD CODE: only ``status == "released"`` was refused, so anything
    else -- a terminal ``obsolete``, or the junk the old unvalidated ``BOMUpdate.status``
    could write -- fell straight through into ``released``."""
    user = make_user(db_session, role=UserRole.MANAGER)
    assembly = make_part(db_session)
    component = make_part(db_session, part_type="raw_material")
    bom = make_bom(db_session, assembly, status_value="obsolete")
    raw_line(db_session, bom, component)

    response = client.post(f"/api/v1/bom/{bom.id}/release", headers=headers_for(user))

    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
    assert "draft" in response.json()["detail"]
    db_session.rollback()
    db_session.expire_all()
    assert db_session.get(BOM, bom.id).status == "obsolete", "a refusal changes nothing"


# ===========================================================================
# §3 -- delete_bom is a SOFT delete, audited, and every read honours it
# ===========================================================================


def test_delete_bom_soft_deletes_and_writes_a_committed_delete_row(client: TestClient, db_session: Session):
    """WOULD FAIL AGAINST OLD CODE on BOTH halves: the old handler ran ``db.delete(bom)``
    (so ``db.get`` returns None, not a tombstone) and wrote no audit row."""
    user = make_user(db_session, role=UserRole.MANAGER)
    assembly = make_part(db_session)
    component = make_part(db_session, part_type="raw_material")
    bom = make_bom(db_session, assembly)
    raw_line(db_session, bom, component)
    bom_id = bom.id

    response = client.delete(f"/api/v1/bom/{bom_id}", headers=headers_for(user))
    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["can_restore"] is True

    rows = _committed_rows(db_session, resource_id=bom_id, action="DELETE")
    assert len(rows) == 1, "exactly one committed DELETE row"
    row = rows[0]
    assert (row.extra_data or {}).get("soft_delete") is True, "the row must state the delete was soft"
    assert (row.extra_data or {}).get("retained_line_count") == 1
    assert (row.old_values or {}).get("revision") == "A", "the header pre-image is on the row"

    stored = db_session.get(BOM, bom_id)
    assert stored is not None, "invariant 3: the row is a tombstone, not a grave"
    assert stored.is_deleted is True
    assert stored.is_active is False
    assert stored.deleted_by == user.id


def test_delete_bom_retains_its_lines(client: TestClient, db_session: Session):
    """WOULD FAIL AGAINST OLD CODE: the old handler bulk-``delete()``d every ``BOMItem``
    first. ``POST /bom/{id}/restore`` can only mean something if the content survives, and
    invariant 5 says preserve historical records."""
    user = make_user(db_session, role=UserRole.MANAGER)
    assembly = make_part(db_session)
    component = make_part(db_session, part_type="raw_material")
    bom = make_bom(db_session, assembly)
    line = raw_line(db_session, bom, component)
    line_id = line.id

    assert client.delete(f"/api/v1/bom/{bom.id}", headers=headers_for(user)).status_code == status.HTTP_200_OK

    db_session.rollback()
    db_session.expire_all()
    assert db_session.get(BOMItem, line_id) is not None, "the lines are kept, physically intact"
    # ...and no false ``bom_line`` DELETE rows for lines that still exist.
    assert _committed_rows(db_session, resource_id=line_id, resource_type="bom_line", action="DELETE") == []


def test_a_deleted_bom_disappears_from_every_bom_read(client: TestClient, db_session: Session):
    """WOULD FAIL AGAINST OLD CODE: the ``assert_tombstoned`` half fails outright, because
    the old handler physically destroyed the row.

    That pairing is the whole point. Invisibility ALONE is also true of a hard delete, so a
    test asserting only "it's gone from the list" passes against the old code and proves
    nothing; the row must be shown to still EXIST while being invisible. It also fails
    against the realistic regression in the other direction -- a soft-delete conversion that
    forgets a read predicate -- and each read below is a separate call site in ``bom.py``."""
    user = make_user(db_session, role=UserRole.MANAGER)
    assembly = make_part(db_session)
    component = make_part(db_session, part_type="raw_material")
    bom = make_bom(db_session, assembly)
    raw_line(db_session, bom, component)
    bom_id = bom.id
    head = headers_for(user)

    assert client.delete(f"/api/v1/bom/{bom_id}", headers=head).status_code == status.HTTP_200_OK
    assert_tombstoned(db_session, bom_id)

    listed = client.get("/api/v1/bom/", headers=head)
    assert listed.status_code == status.HTTP_200_OK, listed.text
    assert bom_id not in {row["id"] for row in listed.json()}, "list_boms still shows it"

    assert client.get(f"/api/v1/bom/{bom_id}", headers=head).status_code == status.HTTP_404_NOT_FOUND
    assert client.get(f"/api/v1/bom/part/{assembly.id}", headers=head).status_code == status.HTTP_404_NOT_FOUND
    assert client.get(f"/api/v1/bom/{bom_id}/explode", headers=head).status_code == status.HTTP_404_NOT_FOUND
    assert client.get(f"/api/v1/bom/{bom_id}/flatten", headers=head).status_code == status.HTTP_404_NOT_FOUND
    assert client.get(f"/api/v1/bom/{bom_id}/where-used", headers=head).status_code == status.HTTP_404_NOT_FOUND


def test_a_deleted_bom_no_longer_makes_a_part_release_ready(client: TestClient, db_session: Session):
    """The readiness surface, which is what actually lets work hit the floor.

    ``GET /setup/readiness/part/{id}`` resolves the part's BOM through
    ``setup._active_bom_for_part``; blind to ``is_deleted`` it would keep reporting an
    assembly as BOM-ready off a document the shop deleted. This is the check a release
    decision is made on.

    WOULD FAIL AGAINST OLD CODE on the ``assert_tombstoned`` line -- see
    ``test_a_deleted_bom_disappears_from_every_bom_read`` for why the readiness assertion
    alone would not be enough."""
    user = make_user(db_session, role=UserRole.MANAGER)
    assembly = make_part(db_session, part_type="assembly")
    component = make_part(db_session, part_type="raw_material")
    bom = make_bom(db_session, assembly)
    raw_line(db_session, bom, component)
    head = headers_for(user)

    before = client.get(f"/api/v1/setup/readiness/part/{assembly.id}", headers=head)
    assert before.status_code == status.HTTP_200_OK, before.text
    assert before.json()["checks"].get("bom") != "missing", "positive control: the BOM is found before the delete"

    assert client.delete(f"/api/v1/bom/{bom.id}", headers=head).status_code == status.HTTP_200_OK
    assert_tombstoned(db_session, bom.id)

    after = client.get(f"/api/v1/setup/readiness/part/{assembly.id}", headers=head)
    assert after.status_code == status.HTTP_200_OK, after.text
    assert after.json()["checks"].get("bom") == "missing", after.json()
    assert any("No active BOM" in blocker for blocker in after.json()["blockers"]), after.json()


def test_a_deleted_bom_drives_no_work_order_material_requirements(client: TestClient, db_session: Session):
    """``work_orders._get_active_bom`` is THE "which BOM does this part build from" lookup --
    ten-odd call sites incl. release readiness, job costing and the backflush descent.

    ``GET /work-orders/preview-operations/{part_id}`` is the cheapest window onto it.
    ALSO would fail against old code independently of soft delete: the hand-rolled query in
    ``get_material_requirements`` filtered neither ``company_id`` nor ``is_deleted``."""
    user = make_user(db_session, role=UserRole.MANAGER)
    assembly = make_part(db_session, part_type="assembly")
    component = make_part(db_session, part_type="raw_material")
    bom = make_bom(db_session, assembly)
    raw_line(db_session, bom, component)
    head = headers_for(user)

    before = client.get(f"/api/v1/work-orders/preview-operations/{assembly.id}", headers=head)
    assert before.status_code == status.HTTP_200_OK, before.text
    assert before.json()["bom_found"] is True, "positive control"

    assert client.delete(f"/api/v1/bom/{bom.id}", headers=head).status_code == status.HTTP_200_OK
    assert_tombstoned(db_session, bom.id)

    after = client.get(f"/api/v1/work-orders/preview-operations/{assembly.id}", headers=head)
    assert after.status_code == status.HTTP_200_OK, after.text
    assert after.json()["bom_found"] is False, after.json()


def test_a_deleted_bom_is_excluded_from_the_uom_mismatch_worklist(client: TestClient, db_session: Session):
    """A line on a deleted BOM blocks nothing, so listing it sends a human to fix a document
    the shop has deleted. Its lines are RETAINED on purpose, so without the predicate the
    worklist keeps reporting them forever."""
    user = make_user(db_session, role=UserRole.MANAGER)
    assembly = make_part(db_session)
    component = make_part(db_session, part_type="raw_material")
    bom = make_bom(db_session, assembly)
    # A genuine mismatch: the line says "feet", the component part stocks in "each".
    raw_line(db_session, bom, component)
    line = db_session.query(BOMItem).filter(BOMItem.bom_id == bom.id).first()
    line.unit_of_measure = "feet"
    db_session.commit()
    head = headers_for(user)

    before = client.get("/api/v1/bom/uom-mismatches", headers=head)
    assert before.status_code == status.HTTP_200_OK, before.text
    assert line.id in {row["bom_item_id"] for row in before.json()["items"]}, "positive control"

    assert client.delete(f"/api/v1/bom/{bom.id}", headers=head).status_code == status.HTTP_200_OK
    assert_tombstoned(db_session, bom.id)

    after = client.get("/api/v1/bom/uom-mismatches", headers=head)
    assert after.status_code == status.HTTP_200_OK, after.text
    assert line.id not in {row["bom_item_id"] for row in after.json()["items"]}


def test_a_deleted_bom_is_not_reported_as_a_where_used_parent(client: TestClient, db_session: Session):
    """The retained lines of a deleted parent assembly must not keep answering "used in".
    ``where_used`` joins ``BOMItem -> BOM``, so it needs its own predicate."""
    user = make_user(db_session, role=UserRole.MANAGER)
    child = make_part(db_session)
    child_bom = make_bom(db_session, child)
    raw_line(db_session, child_bom, make_part(db_session, part_type="raw_material"))
    parent = make_part(db_session)
    parent_bom = make_bom(db_session, parent)
    raw_line(db_session, parent_bom, child)
    head = headers_for(user)

    before = client.get(f"/api/v1/bom/{child_bom.id}/where-used", headers=head)
    assert before.status_code == status.HTTP_200_OK, before.text
    assert parent.id in {row["parent_part_id"] for row in before.json()["used_in"]}, "positive control"

    assert client.delete(f"/api/v1/bom/{parent_bom.id}", headers=head).status_code == status.HTTP_200_OK
    assert_tombstoned(db_session, parent_bom.id)

    after = client.get(f"/api/v1/bom/{child_bom.id}/where-used", headers=head)
    assert after.status_code == status.HTTP_200_OK, after.text
    assert parent.id not in {row["parent_part_id"] for row in after.json()["used_in"]}


# ===========================================================================
# §3b -- restore is a PREREQUISITE of the soft delete, not garnish
# ===========================================================================


def test_recreating_a_bom_on_a_deleted_parts_slot_is_refused_actionably(client: TestClient, db_session: Session):
    """``BOM.part_id`` is UNIQUE with no soft-delete carve-out, so the tombstone permanently
    occupies the part's only BOM slot.

    WOULD FAIL AGAINST OLD CODE: ``create_bom``'s probe was ``is_active == True`` and
    unscoped, so it could not see the row owning the slot -- the insert passed its own guard
    and died on the constraint with an IntegrityError 500."""
    user = make_user(db_session, role=UserRole.MANAGER)
    assembly = make_part(db_session)
    bom = make_bom(db_session, assembly)
    head = headers_for(user)

    assert client.delete(f"/api/v1/bom/{bom.id}", headers=head).status_code == status.HTTP_200_OK

    response = create_bom_request(client, user, assembly)

    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
    detail = response.json()["detail"]
    assert "restore" in detail.lower(), detail
    assert assembly.part_number in detail


def test_restore_brings_the_bom_and_its_lines_back_and_is_audited(client: TestClient, db_session: Session):
    """WOULD FAIL AGAINST OLD CODE: ``POST /bom/{id}/restore`` did not exist (404)."""
    user = make_user(db_session, role=UserRole.MANAGER)
    assembly = make_part(db_session)
    component = make_part(db_session, part_type="raw_material")
    bom = make_bom(db_session, assembly)
    raw_line(db_session, bom, component)
    bom_id = bom.id
    head = headers_for(user)

    assert client.delete(f"/api/v1/bom/{bom_id}", headers=head).status_code == status.HTTP_200_OK

    response = client.post(f"/api/v1/bom/{bom_id}/restore", headers=head)
    assert response.status_code == status.HTTP_200_OK, response.text

    fetched = client.get(f"/api/v1/bom/{bom_id}", headers=head)
    assert fetched.status_code == status.HTTP_200_OK, fetched.text
    assert len(fetched.json()["items"]) == 1, "restore is meaningless if the content did not survive"
    assert fetched.json()["status"] == "draft", "only drafts can be deleted, so that is what comes back"

    rows = _committed_rows(db_session, resource_id=bom_id, action="RESTORE")
    assert len(rows) == 1, "exactly one committed RESTORE row"
    assert (rows[0].old_values or {}).get("is_deleted") is True
    assert (rows[0].new_values or {}).get("is_deleted") is False


def test_restore_refuses_a_bom_that_is_not_deleted(client: TestClient, db_session: Session):
    user = make_user(db_session, role=UserRole.MANAGER)
    bom = make_bom(db_session, make_part(db_session))

    response = client.post(f"/api/v1/bom/{bom.id}/restore", headers=headers_for(user))

    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
    assert "not deleted" in response.json()["detail"]


def test_a_supervisor_cannot_delete_or_restore_a_bom(client: TestClient, db_session: Session):
    """``restore_bom`` is new surface, so its gate needs pinning: same Admin/Manager tier as
    the delete it undoes, per docs/RBAC_PERMISSIONS.md."""
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    manager = make_user(db_session, role=UserRole.MANAGER)
    bom = make_bom(db_session, make_part(db_session))

    refused = client.delete(f"/api/v1/bom/{bom.id}", headers=headers_for(supervisor))
    assert refused.status_code == status.HTTP_403_FORBIDDEN, refused.text

    assert client.delete(f"/api/v1/bom/{bom.id}", headers=headers_for(manager)).status_code == status.HTTP_200_OK

    refused_restore = client.post(f"/api/v1/bom/{bom.id}/restore", headers=headers_for(supervisor))
    assert refused_restore.status_code == status.HTTP_403_FORBIDDEN, refused_restore.text


# ===========================================================================
# §4 -- unrelease records the approval evidence it destroys
# ===========================================================================


def test_unrelease_records_the_approval_it_destroys(client: TestClient, db_session: Session):
    """WOULD FAIL AGAINST OLD CODE: ``unrelease_bom`` NULLed ``approved_by``/``approved_at``
    and wrote nothing, so the fact that a named approval had ever existed was simply gone.
    The pre-image has to be captured BEFORE the clear -- an implementation that logs after
    it would record ``None``, which this asserts against."""
    approver = make_user(db_session, role=UserRole.MANAGER)
    bom, assembly, _component = released_bom_via_the_real_verb(client, db_session, approver)
    assert bom.approved_by == approver.id, "positive control: the release stamped an approver"
    approved_at = bom.approved_at
    assert approved_at is not None

    withdrawer = make_user(db_session, role=UserRole.ADMIN)
    response = client.post(f"/api/v1/bom/{bom.id}/unrelease", headers=headers_for(withdrawer))
    assert response.status_code == status.HTTP_200_OK, response.text

    rows = _committed_rows(db_session, resource_id=bom.id, action="STATUS_CHANGE")
    # One for the release, one for the unrelease (newest first).
    assert len(rows) == 2, [r.description for r in rows]
    row = rows[0]
    assert (row.old_values or {}).get("status") == "released"
    assert (row.new_values or {}).get("status") == "draft"
    assert row.user_id == withdrawer.id, "the row names who withdrew it"
    extra = row.extra_data or {}
    assert extra.get("cleared_approved_by") == approver.id, f"the destroyed approver must be on the chain: {extra}"
    assert extra.get("cleared_approved_at") is not None, f"the destroyed approval time must be on the chain: {extra}"
    assert extra.get("cleared_effective_date") is not None, f"the destroyed effectivity must be on the chain: {extra}"


def test_unrelease_clears_the_effective_date_too(client: TestClient, db_session: Session):
    """WOULD FAIL AGAINST OLD CODE: only ``approved_by``/``approved_at`` were NULLed, so the
    draft carried the effectivity of the approved configuration it no longer is -- the same
    defect class as a draft stamped with a stale approver."""
    user = make_user(db_session, role=UserRole.MANAGER)
    bom, _assembly, _component = released_bom_via_the_real_verb(client, db_session, user)
    assert bom.effective_date is not None, "positive control: the release stamped effectivity"

    assert client.post(f"/api/v1/bom/{bom.id}/unrelease", headers=headers_for(user)).status_code == 200

    db_session.rollback()
    db_session.expire_all()
    stored = db_session.get(BOM, bom.id)
    assert stored.status == "draft"
    assert stored.approved_by is None
    assert stored.approved_at is None
    assert stored.effective_date is None


def test_unrelease_refuses_a_bom_that_is_not_released_and_writes_nothing(client: TestClient, db_session: Session):
    """The pinned refusal string is unchanged, and a refusal must not put a status-change row
    on the tamper-evident chain."""
    user = make_user(db_session, role=UserRole.MANAGER)
    bom = make_bom(db_session, make_part(db_session))

    response = client.post(f"/api/v1/bom/{bom.id}/unrelease", headers=headers_for(user))

    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
    assert response.json()["detail"] == "BOM is not released"
    assert _committed_rows(db_session, resource_id=bom.id, action="STATUS_CHANGE") == []
