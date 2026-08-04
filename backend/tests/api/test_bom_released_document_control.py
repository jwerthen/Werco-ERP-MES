"""A released BOM is a controlled document: nobody can forge its approval, and nobody can
edit its configuration in place.

Two defects, one file, because they are the same failure of document control.

**The escalation (§1).** ``BOMUpdate.status`` was an unvalidated ``Optional[str]`` that
``PUT /bom/{id}`` blind-``setattr``'d, behind ``require_role([ADMIN, MANAGER, SUPERVISOR])``
-- one tier WIDER than the ``release_bom`` / ``unrelease_bom`` / ``delete_bom`` verbs it
shadowed (Admin + Manager). Three reachable consequences:

* a SUPERVISOR ``PUT {"status": "released"}`` bypassed BOTH the Admin/Manager gate AND
  ``release_bom``'s "Cannot release BOM with no items" precondition, producing a RELEASED
  controlled document with ``approved_by`` / ``approved_at`` NULL -- **an approved document
  with no approver**;
* ``{"status": "draft"}`` un-released without clearing the approver -- a draft stamped with
  a stale one;
* any garbage string stuck (``"RELEASED"``, ``"obsolete"``, junk), making the frontend's
  ``BOMStatus`` union a lie.

``BOM.status == "released"`` is load-bearing: it drives go-live readiness (``setup.py``) and
work-order release readiness. docs/RBAC_PERMISSIONS.md documents Release as Admin/Manager
only; the code did not enforce it.

The field is now ABSENT from the schema, so the failure mode is Pydantic's default
``extra="ignore"``: the request still answers 200 (a legacy client is not broken) but the
status is untouched. §1 therefore never asserts on a status code alone -- **every test
asserts the STORED row**, which is the only assertion that distinguishes "ignored" from
"applied". ``test_no_route_can_produce_a_released_bom_with_no_approver`` is the invariant
itself, swept over every door.

**The freeze (§2).** No BOM verb checked ``bom.status``, so every line of a released BOM
could be added to, retyped or deleted in place, and the header's revision / BOM type /
effectivity re-written -- silently changing what the shop builds from an approved document
without any re-approval. The precedent is ``routing.py``: ``delete_operation`` refuses on a
released routing, and ``update_operation`` carves out a field-scoped lane. BOM takes the
outright refusal for lines (it has an ``unrelease`` verb routing does not, so
unrelease -> edit -> re-release strands nobody and leaves three chain rows instead of one
silent mutation) and a one-field carve-out on the header (``description`` is metadata ABOUT
the document, not the configuration).

Every released BOM in this file is released through the REAL ``POST /bom/{id}/release``
verb, never by writing the column -- a fixture that writes ``status="released"`` directly
would prove nothing about a guard that exists to stop exactly that.
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
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


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
        email=f"bomdoc-{n}@co{company_id}.test",
        employee_id=f"BOMDOC-{n:05d}",
        first_name="Doc",
        last_name="Control",
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


def make_part(db: Session, *, part_type: str = "manufactured", company_id: int = COMPANY_A) -> Part:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"BD-P-{n}",
        name=f"Part {n}",
        description="released-document-control fixture part",
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


def make_bom(db: Session, part: Part, *, status_value: str = "draft", company_id: int = COMPANY_A) -> BOM:
    bom = BOM(
        part_id=part.id,
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


def raw_line(db: Session, bom: BOM, component: Part, *, item_number: int = 10, company_id: int = COMPANY_A) -> BOMItem:
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


def reload_bom(db: Session, bom_id: int) -> BOM:
    """Read the STORED row, fresh. ``db_session`` is shared with the app, so the identity
    map has to be expired or a stale in-memory copy could pass while the committed row
    holds the forged value."""
    db.rollback()
    db.expire_all()
    return db.get(BOM, bom_id)


def released_bom(client: TestClient, db: Session, approver: User = None) -> tuple:
    """A genuinely released BOM via the REAL verb. Returns ``(bom_id, assembly, component,
    line_id)``."""
    approver = approver or make_user(db, role=UserRole.MANAGER)
    assembly = make_part(db)
    component = make_part(db, part_type="raw_material")
    bom = make_bom(db, assembly)
    line = raw_line(db, bom, component)
    response = client.post(f"/api/v1/bom/{bom.id}/release", headers=headers_for(approver))
    assert response.status_code == status.HTTP_200_OK, response.text
    return bom.id, assembly, component, line.id


# ===========================================================================
# §1 -- the escalation: PUT /bom/{id} cannot write status, from any role
# ===========================================================================


def test_a_supervisor_cannot_release_a_bom_through_the_update_endpoint(client: TestClient, db_session: Session):
    """THE ESCALATION. WOULD FAIL AGAINST OLD CODE: ``BOMUpdate.status`` was
    ``Optional[str]`` and ``update_bom`` blind-``setattr``'d it, so this exact request
    flipped a draft to ``released`` from a role the release verb refuses -- and, with no
    line on the BOM, from a state the release verb refuses too.

    The assertion is on the STORED row, not the status code: with the field removed the
    request is ignored rather than rejected, and only the row distinguishes the two."""
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    assembly = make_part(db_session)
    bom = make_bom(db_session, assembly)  # deliberately EMPTY -- release_bom refuses these

    response = client.put(f"/api/v1/bom/{bom.id}", headers=headers_for(supervisor), json={"status": "released"})

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["status"] == "draft", "the response must echo the TRUE status"
    stored = reload_bom(db_session, bom.id)
    assert stored.status == "draft", "a supervisor forged a release through the update endpoint"
    assert stored.approved_by is None
    assert stored.approved_at is None


def test_the_supervisors_release_verb_is_still_refused_403(client: TestClient, db_session: Session):
    """The gate the escalation went around. Without this, a future 'fix' that merely
    re-validates the string could still leave the wrong role holding the verb."""
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    assembly = make_part(db_session)
    component = make_part(db_session, part_type="raw_material")
    bom = make_bom(db_session, assembly)
    raw_line(db_session, bom, component)

    response = client.post(f"/api/v1/bom/{bom.id}/release", headers=headers_for(supervisor))

    assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
    assert reload_bom(db_session, bom.id).status == "draft"


def test_even_an_admin_cannot_write_status_through_the_update_endpoint(client: TestClient, db_session: Session):
    """The hole was not only a role mismatch. An ADMIN going through ``PUT`` bypassed the
    "no items" precondition and the approval stamp just as completely -- and produced the
    same approver-less released document. WOULD FAIL AGAINST OLD CODE: admin ``PUT
    {"status": "released"}`` set it."""
    admin = make_user(db_session, role=UserRole.ADMIN)
    bom = make_bom(db_session, make_part(db_session))

    response = client.put(f"/api/v1/bom/{bom.id}", headers=headers_for(admin), json={"status": "released"})

    assert response.status_code == status.HTTP_200_OK, response.text
    assert reload_bom(db_session, bom.id).status == "draft"


def test_the_update_endpoint_cannot_un_release_a_bom_either(client: TestClient, db_session: Session):
    """WOULD FAIL AGAINST OLD CODE: ``PUT {"status": "draft"}`` on a released BOM set the
    status to draft WITHOUT clearing ``approved_by``/``approved_at`` -- a draft stamped with
    a stale approver, and the approval evidence left dangling on a document that is no
    longer approved. ``unrelease_bom`` is the only door, and it records what it clears."""
    approver = make_user(db_session, role=UserRole.MANAGER)
    bom_id, _assembly, _component, _line_id = released_bom(client, db_session, approver)
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)

    response = client.put(f"/api/v1/bom/{bom_id}", headers=headers_for(supervisor), json={"status": "draft"})

    assert response.status_code == status.HTTP_200_OK, response.text
    stored = reload_bom(db_session, bom_id)
    assert stored.status == "released", "the update endpoint un-released a controlled document"
    assert stored.approved_by == approver.id, "the approval evidence is intact"


@pytest.mark.parametrize("junk", ["RELEASED", "obsolete", "Released", "', DROP", "approved", ""])
def test_an_arbitrary_status_string_never_reaches_the_column(client: TestClient, db_session: Session, junk: str):
    """WOULD FAIL AGAINST OLD CODE: the field was an unvalidated ``Optional[str]`` and the
    handler ``setattr``'d whatever arrived, so every one of these stuck -- making the
    frontend's ``BOMStatus`` union a lie and, for ``"obsolete"``, silently writing a
    terminal state with no verb behind it.

    Note the guard is now structural rather than a validator: the field cannot be parsed at
    all, so the assertion is that the column is untouched (a 422 would be an equally
    acceptable outcome, but a 200-with-the-junk-applied is the defect)."""
    admin = make_user(db_session, role=UserRole.ADMIN)
    bom = make_bom(db_session, make_part(db_session))

    response = client.put(f"/api/v1/bom/{bom.id}", headers=headers_for(admin), json={"status": junk})

    assert response.status_code in (status.HTTP_200_OK, status.HTTP_422_UNPROCESSABLE_ENTITY), response.text
    assert reload_bom(db_session, bom.id).status == "draft", f"the junk status {junk!r} reached the column"


def test_a_legitimate_field_still_updates_when_status_rides_along(client: TestClient, db_session: Session):
    """The negative control the removal must not break: an old client that PUTs the whole
    record back, ``status`` included, still gets its real edit applied. Without this, a
    schema change that 422'd on the extra key would look identical to a passing suite."""
    admin = make_user(db_session, role=UserRole.ADMIN)
    bom = make_bom(db_session, make_part(db_session))

    response = client.put(
        f"/api/v1/bom/{bom.id}",
        headers=headers_for(admin),
        json={"status": "released", "description": "still editable"},
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    stored = reload_bom(db_session, bom.id)
    assert stored.description == "still editable", "the legitimate half of the payload must still apply"
    assert stored.status == "draft"


def test_no_route_can_produce_a_released_bom_with_no_approver(client: TestClient, db_session: Session):
    """THE INVARIANT ITSELF, swept over every door that can write a BOM.

    An approved controlled document with a NULL approver is the state the escalation
    produced, and it is what AS9100D approval evidence exists to prevent. This drives every
    write verb in ``bom.py`` from the widest role that can reach each one, then asserts the
    property over EVERY ``boms`` row in the tenant -- so a future endpoint that sets the
    column somewhere else fails here even if nobody writes a test for it.

    WOULD FAIL AGAINST OLD CODE: the ``PUT`` step alone produced exactly that row."""
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    manager = make_user(db_session, role=UserRole.MANAGER)
    assembly = make_part(db_session)
    component = make_part(db_session, part_type="raw_material")

    created = client.post(
        "/api/v1/bom/",
        headers=headers_for(supervisor),
        json={
            "part_id": assembly.id,
            "revision": "A",
            "items": [{"component_part_id": component.id, "item_number": 10, "quantity": 1, "item_type": "buy"}],
        },
    )
    assert created.status_code == status.HTTP_200_OK, created.text
    bom_id = created.json()["id"]

    # Every shape of the forged-release attempt, from the widest role that can reach the verb.
    for payload in ({"status": "released"}, {"status": "RELEASED"}, {"status": "released", "revision": "B"}):
        assert (
            client.put(f"/api/v1/bom/{bom_id}", headers=headers_for(supervisor), json=payload).status_code
            == status.HTTP_200_OK
        )

    # The only legitimate door, which stamps the approver.
    assert client.post(f"/api/v1/bom/{bom_id}/release", headers=headers_for(manager)).status_code == 200

    db_session.rollback()
    db_session.expire_all()
    released = db_session.query(BOM).filter(BOM.company_id == COMPANY_A, BOM.status == "released").all()
    assert released, "positive control: the legitimate release really did happen"
    for row in released:
        assert row.approved_by is not None, f"BOM {row.id} is released with NO approver"
        assert row.approved_at is not None, f"BOM {row.id} is released with no approval timestamp"


# ===========================================================================
# §2 -- a released BOM's LINES are frozen
# ===========================================================================


def test_adding_a_line_to_a_released_bom_is_refused(client: TestClient, db_session: Session):
    """WOULD FAIL AGAINST OLD CODE: no BOM verb checked ``bom.status``, so a line could be
    appended to an approved document and the shop would build to it with no re-approval."""
    bom_id, _assembly, _component, _line_id = released_bom(client, db_session)
    editor = make_user(db_session, role=UserRole.SUPERVISOR)
    extra = make_part(db_session, part_type="raw_material")

    response = client.post(
        f"/api/v1/bom/{bom_id}/items",
        headers=headers_for(editor),
        json={"component_part_id": extra.id, "item_number": 20, "quantity": 5, "item_type": "buy"},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
    assert "Released BOM" in response.json()["detail"]
    assert "unrelease" in response.json()["detail"].lower()
    db_session.rollback()
    db_session.expire_all()
    assert db_session.query(BOMItem).filter(BOMItem.bom_id == bom_id).count() == 1, "a refusal writes no line"


def test_editing_a_line_on_a_released_bom_is_refused(client: TestClient, db_session: Session):
    """WOULD FAIL AGAINST OLD CODE: ``update_bom_item`` never looked at the parent's status,
    so the quantity the shop builds to could be changed under an approved document."""
    bom_id, _assembly, _component, line_id = released_bom(client, db_session)
    editor = make_user(db_session, role=UserRole.SUPERVISOR)

    response = client.put(f"/api/v1/bom/items/{line_id}", headers=headers_for(editor), json={"quantity": 99.0})

    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
    assert "Released BOM" in response.json()["detail"]
    db_session.rollback()
    db_session.expire_all()
    assert float(db_session.get(BOMItem, line_id).quantity) == 1.0, "a refusal leaves the row untouched"


def test_deleting_a_line_from_a_released_bom_is_refused(client: TestClient, db_session: Session):
    """WOULD FAIL AGAINST OLD CODE: ``delete_bom_item`` physically deletes, so on a released
    BOM this silently removed a component from an approved configuration.

    The editor is a MANAGER, not a supervisor: ``delete_bom_item`` is the one line verb
    gated to Admin/Manager, so a supervisor would be refused 403 by ``require_role`` before
    the status gate is ever reached and the test would prove nothing about the freeze."""
    bom_id, _assembly, _component, line_id = released_bom(client, db_session)
    editor = make_user(db_session, role=UserRole.MANAGER)

    response = client.delete(f"/api/v1/bom/items/{line_id}", headers=headers_for(editor))

    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
    assert "Released BOM" in response.json()["detail"]
    db_session.rollback()
    db_session.expire_all()
    assert db_session.get(BOMItem, line_id) is not None, "a refusal deletes nothing"


def test_the_line_freeze_is_an_allowlist_so_a_junk_status_lands_frozen(client: TestClient, db_session: Session):
    """The guard is ``!= "draft"``, not ``== "released"``, ON PURPOSE: rows written by the
    old unvalidated ``BOMUpdate.status`` may carry junk, and those must land on the SAFE
    side rather than being accidentally editable. WOULD FAIL AGAINST OLD CODE (no gate at
    all) and against a ``== "released"`` implementation."""
    bom = make_bom(db_session, make_part(db_session), status_value="obsolete")
    component = make_part(db_session, part_type="raw_material")
    editor = make_user(db_session, role=UserRole.SUPERVISOR)

    response = client.post(
        f"/api/v1/bom/{bom.id}/items",
        headers=headers_for(editor),
        json={"component_part_id": component.id, "item_number": 10, "quantity": 1, "item_type": "buy"},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
    assert "obsolete" in response.json()["detail"], response.json()


def test_a_foreign_bom_id_is_still_a_404_not_a_released_400(client: TestClient, db_session: Session):
    """Ordering guard: the parent 404 must come BEFORE the status gate, or the shape of the
    refusal tells a caller in another tenant whether a given BOM id exists and is
    released."""
    other = make_user(db_session, role=UserRole.SUPERVISOR, company_id=2)
    bom_id, _assembly, _component, _line_id = released_bom(client, db_session)
    component = make_part(db_session, part_type="raw_material", company_id=2)

    response = client.post(
        f"/api/v1/bom/{bom_id}/items",
        headers=headers_for(other),
        json={"component_part_id": component.id, "item_number": 20, "quantity": 1, "item_type": "buy"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert "Released" not in response.text


def test_unreleasing_reopens_the_lines_for_editing(client: TestClient, db_session: Session):
    """The carve-out that makes the outright line freeze acceptable: BOM has an
    ``unrelease`` verb, so the workflow is unrelease -> edit -> re-release. Without this the
    freeze would be a dead end rather than a control, and the whole design justification
    would be false."""
    manager = make_user(db_session, role=UserRole.MANAGER)
    bom_id, _assembly, _component, line_id = released_bom(client, db_session, manager)

    assert client.post(f"/api/v1/bom/{bom_id}/unrelease", headers=headers_for(manager)).status_code == 200

    edited = client.put(f"/api/v1/bom/items/{line_id}", headers=headers_for(manager), json={"quantity": 4.0})
    assert edited.status_code == status.HTTP_200_OK, edited.text

    rereleased = client.post(f"/api/v1/bom/{bom_id}/release", headers=headers_for(manager))
    assert rereleased.status_code == status.HTTP_200_OK, rereleased.text
    stored = reload_bom(db_session, bom_id)
    assert stored.status == "released"
    assert stored.approved_by == manager.id, "the re-release re-stamps a real approver"


def test_a_draft_bom_still_accepts_every_line_edit(client: TestClient, db_session: Session):
    """The positive control. Every refusal above also passes against a handler that refuses
    everything, so the ordinary draft workflow has to be pinned in the same file.

    Two roles on purpose: add/update are Admin/Manager/Supervisor, while ``delete_bom_item``
    is Admin/Manager. Driving the delete leg with the supervisor would fail 403 on the ROLE
    gate and say nothing about the status gate."""
    editor = make_user(db_session, role=UserRole.SUPERVISOR)
    deleter = make_user(db_session, role=UserRole.MANAGER)
    assembly = make_part(db_session)
    component = make_part(db_session, part_type="raw_material")
    bom = make_bom(db_session, assembly)
    head = headers_for(editor)

    added = client.post(
        f"/api/v1/bom/{bom.id}/items",
        headers=head,
        json={"component_part_id": component.id, "item_number": 10, "quantity": 2, "item_type": "buy"},
    )
    assert added.status_code == status.HTTP_200_OK, added.text
    line_id = added.json()["id"]

    updated = client.put(f"/api/v1/bom/items/{line_id}", headers=head, json={"quantity": 6.0})
    assert updated.status_code == status.HTTP_200_OK, updated.text
    assert float(updated.json()["quantity"]) == 6.0

    deleted = client.delete(f"/api/v1/bom/items/{line_id}", headers=headers_for(deleter))
    assert deleted.status_code == status.HTTP_200_OK, deleted.text


# ===========================================================================
# §2b -- a released BOM's HEADER is frozen except for the carve-out
# ===========================================================================


@pytest.mark.parametrize(
    "payload,field",
    [
        ({"revision": "B"}, "revision"),
        ({"bom_type": "phantom"}, "bom_type"),
        ({"effective_date": "2030-01-01T00:00:00Z"}, "effective_date"),
    ],
)
def test_a_released_boms_identity_fields_are_frozen(client: TestClient, db_session: Session, payload, field):
    """WOULD FAIL AGAINST OLD CODE: ``update_bom`` never checked the status, so the revision
    (the approved document's IDENTITY), the BOM type (how it explodes into demand) and the
    effectivity (AS9100D: when the approved configuration took effect) could all be
    re-written in place on an approved document."""
    manager = make_user(db_session, role=UserRole.MANAGER)
    bom_id, _assembly, _component, _line_id = released_bom(client, db_session, manager)
    before = getattr(reload_bom(db_session, bom_id), field)

    response = client.put(f"/api/v1/bom/{bom_id}", headers=headers_for(manager), json=payload)

    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
    detail = response.json()["detail"]
    assert "Released BOM" in detail and "description" in detail, detail
    assert getattr(reload_bom(db_session, bom_id), field) == before, "a refusal leaves the row untouched"


def test_a_released_boms_description_is_the_one_editable_field(client: TestClient, db_session: Session):
    """THE CARVE-OUT. A description is metadata ABOUT the document, not the configuration,
    so freezing it would be pointless friction -- and this is the assertion that stops the
    guard being 'quietly widened' into a blanket refusal later.

    It deliberately does NOT re-stamp the approval (a divergence from
    ``routing.update_operation``, whose carved-out fields ARE released production content):
    overwriting the record of who approved the configuration in order to record a typo fix
    would be a worse outcome than the typo."""
    manager = make_user(db_session, role=UserRole.MANAGER)
    bom_id, _assembly, _component, _line_id = released_bom(client, db_session, manager)
    approved_at_before = reload_bom(db_session, bom_id).approved_at

    response = client.put(
        f"/api/v1/bom/{bom_id}",
        headers=headers_for(manager),
        json={"description": "corrected typo in the title block"},
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    stored = reload_bom(db_session, bom_id)
    assert stored.description == "corrected typo in the title block"
    assert stored.status == "released"
    assert stored.approved_by == manager.id, "the approval must NOT be re-stamped by a metadata edit"
    assert stored.approved_at == approved_at_before


def test_a_mixed_payload_on_a_released_bom_is_refused_whole(client: TestClient, db_session: Session):
    """The gate runs BEFORE the first ``setattr``, so a payload that pairs the allowed field
    with a frozen one applies NEITHER. An implementation that checked per-field while
    assigning would leave the description written and the request 400 -- a half-applied
    write on a controlled document."""
    manager = make_user(db_session, role=UserRole.MANAGER)
    bom_id, _assembly, _component, _line_id = released_bom(client, db_session, manager)

    response = client.put(
        f"/api/v1/bom/{bom_id}",
        headers=headers_for(manager),
        json={"description": "should not persist", "revision": "C"},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
    stored = reload_bom(db_session, bom_id)
    assert stored.revision == "A"
    assert stored.description != "should not persist", "the refusal must not have half-applied"


def test_a_no_op_put_on_a_released_bom_is_not_refused(client: TestClient, db_session: Session):
    """``changed_fields`` is 'present AND different' (``update_operation`` semantics), so a
    form-shaped client that PUTs the released record back unchanged is not refused for
    changing nothing. A naive 'any frozen field present -> 400' gate would break every such
    client."""
    manager = make_user(db_session, role=UserRole.MANAGER)
    bom_id, _assembly, _component, _line_id = released_bom(client, db_session, manager)

    response = client.put(
        f"/api/v1/bom/{bom_id}",
        headers=headers_for(manager),
        json={"revision": "A", "bom_type": "standard"},
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert reload_bom(db_session, bom_id).revision == "A"


# ---------------------------------------------------------------------------
# §2c -- the no-op detection the freeze rests on has to survive a ROUND TRIP
# ---------------------------------------------------------------------------
# ``test_a_no_op_put_on_a_released_bom_is_not_refused`` above only round-trips strings, and
# strings compare fine. ``effective_date`` did not: ``BOM.effective_date`` is a NAIVE
# ``DateTime`` column while ``BOMResponse`` is a ``UTCModel`` that serves it with a trailing
# ``Z``, so the value a client reads back parsed to an AWARE datetime -- and ``naive !=
# aware`` is ``True``. Every no-op PUT therefore counted the effectivity as changed. These
# tests take the value from the API's OWN response rather than hand-writing a literal,
# because the defect only exists on the round trip.


def _released_bom_with_effectivity(client: TestClient, db: Session, manager: User) -> int:
    """A released BOM whose ``effective_date`` was set through the API while it was a draft
    (the only door -- the field is frozen once released)."""
    assembly = make_part(db)
    component = make_part(db, part_type="raw_material")
    bom = make_bom(db, assembly)
    raw_line(db, bom, component)

    dated = client.put(
        f"/api/v1/bom/{bom.id}",
        headers=headers_for(manager),
        json={"effective_date": "2030-01-01T12:00:00Z"},
    )
    assert dated.status_code == status.HTTP_200_OK, dated.text
    released = client.post(f"/api/v1/bom/{bom.id}/release", headers=headers_for(manager))
    assert released.status_code == status.HTTP_200_OK, released.text
    return bom.id


def test_putting_back_the_served_effective_date_is_not_an_edit(client: TestClient, db_session: Session):
    """WOULD FAIL AGAINST PRE-FIX CODE (400, not 200). The client sends back EXACTLY what
    ``GET /bom/{id}`` served it and changes nothing, and the freeze refused it -- so the
    ordinary "open the form, hit save" path was unusable on every released BOM that had an
    effectivity, and the only way to correct a typo in the description was to unrelease the
    document."""
    manager = make_user(db_session, role=UserRole.MANAGER)
    bom_id = _released_bom_with_effectivity(client, db_session, manager)

    served = client.get(f"/api/v1/bom/{bom_id}", headers=headers_for(manager))
    assert served.status_code == status.HTTP_200_OK, served.text
    served_effective_date = served.json()["effective_date"]
    assert served_effective_date.endswith("Z"), f"precondition: UTCModel serves a Z -- got {served_effective_date}"
    before = reload_bom(db_session, bom_id).effective_date

    response = client.put(
        f"/api/v1/bom/{bom_id}",
        headers=headers_for(manager),
        json={
            "revision": served.json()["revision"],
            "bom_type": served.json()["bom_type"],
            "effective_date": served_effective_date,
        },
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert reload_bom(db_session, bom_id).effective_date == before


def test_a_round_tripped_effective_date_writes_no_phantom_audit_row(client: TestClient, db_session: Session):
    """PASSES AGAINST PRE-FIX CODE TOO, and is here deliberately.

    The obvious corollary of the defect -- ``changed_fields`` non-empty means
    ``log_update`` is called, so a no-op PUT should have written an UPDATE row claiming the
    AS9100D effectivity of an approved configuration changed -- did NOT happen, and it is
    worth knowing why before someone "simplifies" the reason away. TWO independent layers
    caught it: ``bom.py::_audit_values`` runs both halves of the diff through
    ``to_utc_iso`` (so the naive old value and the aware new one serialize identically),
    and ``AuditService.log_update`` returns ``None`` on an empty diff.

    This test pins the OUTCOME, so it survives whichever layer is touched: the schema
    normalization above is now the first line, and if it and either of those two are
    dropped together the chain starts carrying false effectivity changes."""
    from app.models.audit_log import AuditLog

    manager = make_user(db_session, role=UserRole.MANAGER)
    assembly = make_part(db_session)
    bom = make_bom(db_session, assembly)
    assert (
        client.put(
            f"/api/v1/bom/{bom.id}",
            headers=headers_for(manager),
            json={"effective_date": "2030-01-01T12:00:00Z"},
        ).status_code
        == status.HTTP_200_OK
    )

    served = client.get(f"/api/v1/bom/{bom.id}", headers=headers_for(manager)).json()

    db_session.rollback()
    db_session.expire_all()
    rows_before = (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == "bom", AuditLog.resource_id == bom.id, AuditLog.action == "UPDATE")
        .count()
    )

    response = client.put(
        f"/api/v1/bom/{bom.id}",
        headers=headers_for(manager),
        json={"effective_date": served["effective_date"], "revision": served["revision"]},
    )
    assert response.status_code == status.HTTP_200_OK, response.text

    db_session.rollback()
    db_session.expire_all()
    rows_after = (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == "bom", AuditLog.resource_id == bom.id, AuditLog.action == "UPDATE")
        .count()
    )
    assert rows_after == rows_before, "a PUT that changed nothing put a phantom effectivity change on the chain"


def test_an_effective_date_with_an_offset_is_stored_as_the_utc_instant(client: TestClient, db_session: Session):
    """WOULD FAIL AGAINST PRE-FIX CODE (stored 07:00, not 12:00). The aware datetime was
    ``setattr``'d onto a naive column, so the offset was dropped rather than applied and the
    stored effectivity was the caller's local wall clock -- an approved configuration
    silently re-dated by the size of the offset. Store UTC, serve UTC, display Central."""
    from datetime import datetime as _datetime

    manager = make_user(db_session, role=UserRole.MANAGER)
    bom = make_bom(db_session, make_part(db_session))

    response = client.put(
        f"/api/v1/bom/{bom.id}",
        headers=headers_for(manager),
        json={"effective_date": "2030-01-01T07:00:00-05:00"},
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    stored = reload_bom(db_session, bom.id).effective_date
    assert stored.tzinfo is None, "the column is naive; a stored aware value would re-break the comparison"
    assert stored == _datetime(2030, 1, 1, 12, 0, 0)
    assert response.json()["effective_date"].startswith("2030-01-01T12:00:00")


def test_a_genuine_effective_date_change_is_still_frozen(client: TestClient, db_session: Session):
    """The counterweight. Normalizing both sides must make the no-op compare EQUAL without
    making a real change compare equal too -- otherwise the fix would have quietly deleted
    the effectivity half of the freeze."""
    manager = make_user(db_session, role=UserRole.MANAGER)
    bom_id = _released_bom_with_effectivity(client, db_session, manager)
    before = reload_bom(db_session, bom_id).effective_date

    response = client.put(
        f"/api/v1/bom/{bom_id}",
        headers=headers_for(manager),
        json={"effective_date": "2031-06-01T12:00:00Z"},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
    assert "Released BOM" in response.json()["detail"]
    assert reload_bom(db_session, bom_id).effective_date == before


def test_a_bom_with_a_junk_status_is_fully_locked_at_the_header(client: TestClient, db_session: Session):
    """Not even the ``description`` carve-out applies outside ``draft``/``released`` -- the
    carve-out is scoped to ``released``, and anything else is unknown state that no verb
    produced."""
    admin = make_user(db_session, role=UserRole.ADMIN)
    bom = make_bom(db_session, make_part(db_session), status_value="obsolete")

    response = client.put(f"/api/v1/bom/{bom.id}", headers=headers_for(admin), json={"description": "nope"})

    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
    assert "obsolete" in response.json()["detail"], response.json()
    assert reload_bom(db_session, bom.id).description != "nope"


def test_only_draft_boms_can_be_deleted(client: TestClient, db_session: Session):
    """Pre-existing guard, pinned because ``delete_bom`` was rewritten around it: the
    released refusal has to survive the soft-delete conversion."""
    manager = make_user(db_session, role=UserRole.MANAGER)
    bom_id, _assembly, _component, _line_id = released_bom(client, db_session, manager)

    response = client.delete(f"/api/v1/bom/{bom_id}", headers=headers_for(manager))

    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
    assert "Only draft BOMs can be deleted" in response.json()["detail"]
    assert reload_bom(db_session, bom_id).is_deleted is False


# ---------------------------------------------------------------------------
# The de-corruption door (review finding F3)
# ---------------------------------------------------------------------------
# The three tests above pin the freeze on a junk status. Taken alone that freeze STRANDS the
# part: every verb refuses, ``BOM.part_id`` is UNIQUE so no replacement BOM can be created,
# and the document is readable and permanently useless. ``unrelease`` is the one escape
# hatch -- it refuses only an already-``draft`` BOM and withdraws anything else BACK to
# draft. These would FAIL against a ``!= "released"`` implementation of that guard.


def test_unrelease_normalises_a_junk_status_back_to_draft(client: TestClient, db_session: Session):
    manager = make_user(db_session, role=UserRole.MANAGER)
    bom = make_bom(db_session, make_part(db_session), status_value="obsolete")

    response = client.post(f"/api/v1/bom/{bom.id}/unrelease", headers=headers_for(manager))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert reload_bom(db_session, bom.id).status == "draft"


def test_unrelease_records_the_actual_prior_status_not_a_hardcoded_released(client: TestClient, db_session: Session):
    """The audit row is evidence, so it must say what really happened. A hardcoded
    ``old_status="released"`` would make a normalisation indistinguishable from an ordinary
    withdrawal of an approved document."""
    from app.models.audit_log import AuditLog

    manager = make_user(db_session, role=UserRole.MANAGER)
    bom = make_bom(db_session, make_part(db_session), status_value="obsolete")

    assert client.post(f"/api/v1/bom/{bom.id}/unrelease", headers=headers_for(manager)).status_code == 200

    db_session.rollback()  # only COMMITTED rows count -- a flushed row would still be visible
    row = (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == "bom", AuditLog.resource_id == bom.id)
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert row is not None, "the normalisation must be on the chain"
    assert "obsolete" in str(row.old_values), row.old_values


def test_a_normalised_bom_is_manageable_again(client: TestClient, db_session: Session):
    """The whole point of the escape hatch: after the withdrawal every verb that refused the
    junk-status row works, so the part is no longer stranded."""
    manager = make_user(db_session, role=UserRole.MANAGER)
    bom = make_bom(db_session, make_part(db_session), status_value="RELEASED")

    assert client.post(f"/api/v1/bom/{bom.id}/unrelease", headers=headers_for(manager)).status_code == 200

    edited = client.put(f"/api/v1/bom/{bom.id}", headers=headers_for(manager), json={"description": "recovered"})
    assert edited.status_code == status.HTTP_200_OK, edited.text

    deleted = client.delete(f"/api/v1/bom/{bom.id}", headers=headers_for(manager))
    assert deleted.status_code == status.HTTP_200_OK, deleted.text
    assert reload_bom(db_session, bom.id).is_deleted is True


def test_unrelease_still_refuses_a_draft(client: TestClient, db_session: Session):
    """The widening must not become "unrelease anything": a draft is already withdrawn, and
    the pinned message stays exactly what it was."""
    manager = make_user(db_session, role=UserRole.MANAGER)
    bom = make_bom(db_session, make_part(db_session), status_value="draft")

    response = client.post(f"/api/v1/bom/{bom.id}/unrelease", headers=headers_for(manager))

    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
    assert response.json()["detail"] == "BOM is not released"


def test_unrelease_is_still_admin_manager_only(client: TestClient, db_session: Session):
    """The de-corruption door must not be a wider door: a SUPERVISOR reaching a junk-status
    BOM through it would re-open the privilege gap the removed ``status`` field created."""
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    bom = make_bom(db_session, make_part(db_session), status_value="obsolete")

    response = client.post(f"/api/v1/bom/{bom.id}/unrelease", headers=headers_for(supervisor))

    assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
    assert reload_bom(db_session, bom.id).status == "obsolete"
