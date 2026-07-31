"""A BOM line's unit of measure now INHERITS the component part's, and the lines that predate it are listable.

Owner decision, 2026-07-27. ``schemas/bom.py`` used to declare
``unit_of_measure: str = "each"`` and the two importers used to call ``_normalize_uom(None)``,
which also returns ``"each"`` — so every BOM line written without an explicit unit stored a
literal ``"each"`` that **nobody chose**. PR 4.5's ``unit_of_measure_mismatch`` diagnostic is
BLOCKING: it refuses ``Part.backflush_components`` at opt-in and refuses that component at
completion. On a sheet-metal shop's real data — components stocked in ``sheets`` / ``pounds``
/ ``feet`` — that unchosen default meant the feature refused nearly every part.

**The diagnostic is right; the default was wrong.** Nothing in this file softens the
severity, and ``test_a_stated_unit_that_contradicts_the_part_is_still_refused`` exists
specifically so a future "fix" that reaches for the severity instead fails here.

What is proved, in the order it matters:

1. **The predicate** (§1). ``models.part.uom_disagrees`` is THE single line-vs-part
   comparison — the blocking diagnostic and ``GET /bom/uom-mismatches`` both call it, so a
   report that listed a different set than the gate refuses is structurally impossible.
   Blank on either side is silent; comparison is exact-label, so ``ea`` is NOT ``each``.
2. **Every write path, one test each** (§2–§3). There are exactly five BOM-line write sites
   in ``app/`` (``grep "BOMItem("``): four creates and one ``setattr`` update, all in
   ``api/endpoints/bom.py``. **A path with no test is a path that regresses**, so each gets
   both halves: an unstated unit inherits, and a STATED unit is honoured verbatim. The
   second half is the one that stops this change from becoming an override — a human who
   types ``each`` against a part stocked in sheets is making a claim, and the gate's job is
   to refuse it, not to silently rewrite it.
3. **The fallbacks** (§4). ``"each"`` survives only as the last resort: a component that
   cannot be resolved at all, or one whose own ``unit_of_measure`` is NULL. Both keep the
   line's unit non-null the way it has always been, and neither can manufacture a mismatch
   (``uom_disagrees`` is silent when the part side is blank).
4. **The remediation report** (§5). Exactly the disagreeing lines, tenant-scoped, and a
   PURE READ — it is the pre-arming worklist, so a report that wrote anything (or that
   showed another company's row) would be worse than no report.
5. **The payoff** (§6). A part whose BOM lines were created through the API without an
   explicit unit is now ARMABLE. Before this change the very same call stored ``"each"``
   and the opt-in was refused with 409 — which the negative control still proves for a
   legacy row, because this series is **correct-forward and does not backfill**.

===========================================================================
Traps that shaped these fixtures
===========================================================================

* **A python-side column default fires on an EXPLICIT ``None``.** ``BOMItem.unit_of_measure``
  and ``Part.unit_of_measure`` both carry ``default=...``, and SQLAlchemy treats an
  attribute set to ``None`` at INSERT as "no value supplied" — so
  ``BOMItem(unit_of_measure=None)`` stores ``"each"``, not NULL. A genuinely NULL unit
  therefore has to be written by a second UPDATE (``make_part`` / ``raw_line`` below do
  exactly that). This is also why ``create_bom`` resolves the unit BEFORE the ``**`` splat
  rather than after: handing the constructor a ``None`` would quietly reinstate ``"each"``.
* **SQLite does not enforce foreign keys**, which is the only reason a BOM line pointing at
  a part id that does not exist is constructible here — the fixture behind the
  unresolvable-component fallback. On Postgres that state arrives via a hard delete.
* **Route order is load-bearing.** ``/bom/uom-mismatches`` is declared BEFORE
  ``@router.get("/{bom_id}")``; below it, the int path parameter swallows the literal and
  the endpoint 422s. ``test_the_report_route_is_not_swallowed_by_the_bom_id_path_param``
  pins that, because the failure is a routing accident nothing else would catch.
"""

import io
from types import SimpleNamespace

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.bom import BOM, BOMItem
from app.models.company import Company
from app.models.inventory import InventoryTransaction
from app.models.operational_event import OperationalEvent
from app.models.part import Part, UnitOfMeasure, uom_disagrees, uom_label
from app.models.user import User, UserRole

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
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
        email=f"uom-{n}@co{company_id}.test",
        employee_id=f"UOM-{n:05d}",
        first_name="Unit",
        last_name="Measure",
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
    uom="each",
    part_type: str = "manufactured",
    company_id: int = COMPANY_A,
    is_deleted: bool = False,
    part_number: str = None,
) -> Part:
    """One part. ``uom=None`` means a genuinely NULL stocking unit.

    The NULL case needs the follow-up UPDATE: ``Part.unit_of_measure`` carries
    ``default=UnitOfMeasure.EACH`` and SQLAlchemy applies a python-side default when the
    attribute is ``None`` at INSERT, so passing ``None`` to the constructor stores
    ``"each"``. See the module docstring's first trap.
    """
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=part_number or f"UOM-P-{n}",
        name=f"Part {n}",
        description="bom-uom fixture part",
        part_type=part_type,
        unit_of_measure=uom if uom is not None else UnitOfMeasure.EACH.value,
        standard_cost=5.0,
        is_active=True,
        is_deleted=is_deleted,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    if uom is None:
        part.unit_of_measure = None
        db.commit()
    db.refresh(part)
    return part


def make_bom(db: Session, part: Part, *, is_active: bool = True, company_id: int = COMPANY_A) -> BOM:
    bom = BOM(part_id=part.id, revision="A", status="draft", is_active=is_active, company_id=company_id)
    db.add(bom)
    db.commit()
    db.refresh(bom)
    return bom


def raw_line(
    db: Session,
    bom: BOM,
    component: Part,
    *,
    unit_of_measure="each",
    item_number: int = 10,
    quantity: float = 1.0,
    item_type: str = "buy",
    line_type: str = "component",
    is_alternate: bool = False,
    is_optional: bool = False,
    component_part_id: int = None,
    company_id: int = COMPANY_A,
) -> BOMItem:
    """A BOM line written at the MODEL layer — i.e. a LEGACY row, bypassing the write paths.

    That is the point: every ``§5``/``§6`` fixture needs rows that predate
    ``_resolve_line_uom`` in order to prove the report finds them and the gate still
    refuses them. ``unit_of_measure=None`` needs the same post-insert UPDATE the part
    fixture does, for the same reason.
    """
    item = BOMItem(
        bom_id=bom.id,
        component_part_id=component_part_id if component_part_id is not None else component.id,
        item_number=item_number,
        quantity=quantity,
        item_type=item_type,
        line_type=line_type,
        unit_of_measure=unit_of_measure if unit_of_measure is not None else UnitOfMeasure.EACH.value,
        is_alternate=is_alternate,
        is_optional=is_optional,
        company_id=company_id,
    )
    db.add(item)
    db.commit()
    if unit_of_measure is None:
        item.unit_of_measure = None
        db.commit()
    db.refresh(item)
    return item


def stored_uom(db: Session, item_id: int):
    """The unit as it actually landed in the row, read fresh — not as a response echoed it."""
    db.expire_all()
    return db.get(BOMItem, item_id).unit_of_measure


def add_item(client: TestClient, user: User, bom_id: int, component: Part, **overrides):
    """``POST /bom/{id}/items`` — write path 4, the one both frontend BOM forms use."""
    body = {
        "component_part_id": component.id,
        "item_number": overrides.pop("item_number", 10),
        "quantity": overrides.pop("quantity", 2),
        "item_type": overrides.pop("item_type", "buy"),
    }
    body.update(overrides)
    return client.post(f"/api/v1/bom/{bom_id}/items", headers=headers_for(user), json=body)


def report(client: TestClient, user: User, **params):
    response = client.get("/api/v1/bom/uom-mismatches", headers=headers_for(user), params=params)
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()


def reported_line_ids(client: TestClient, user: User, **params) -> set:
    return {row["bom_item_id"] for row in report(client, user, **params)["items"]}


def readiness_blockers(client: TestClient, user: User, part_id: int) -> list:
    response = client.get(f"/api/v1/parts/{part_id}/backflush-readiness", headers=headers_for(user))
    assert response.status_code == status.HTTP_200_OK, response.text
    return [d["code"] for d in response.json()["blockers"]]


def arm(client: TestClient, user: User, part_id: int):
    return client.put(
        f"/api/v1/parts/{part_id}",
        headers=headers_for(user),
        json={"version": 0, "backflush_components": True},
    )


def stub_llm_import(monkeypatch: pytest.MonkeyPatch, items: list, assembly_number: str = "ASSY-LLM-UOM") -> None:
    """Stub the three seams behind ``POST /bom/import`` (write path 2) at the endpoint module."""
    monkeypatch.setattr("app.api.endpoints.bom.save_uploaded_document", lambda content, filename: "/tmp/fake.pdf")
    monkeypatch.setattr(
        "app.api.endpoints.bom.extract_text_from_document",
        lambda path: SimpleNamespace(text="BOM document text " * 10, is_ocr=False),
    )
    monkeypatch.setattr(
        "app.api.endpoints.bom.extract_bom_data_with_llm",
        lambda text, is_ocr=False, company_id=None: {
            "document_type": "bom",
            "assembly": {"part_number": assembly_number, "name": "LLM Assembly", "revision": "A"},
            "items": items,
            "extraction_confidence": "high",
        },
    )


def post_llm_import(client: TestClient, user: User):
    return client.post(
        "/api/v1/bom/import",
        headers=headers_for(user),
        files={"file": ("bom.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")},
    )


# ===========================================================================
# 1. THE PREDICATE — one comparison, shared by the gate and the report
# ===========================================================================


def test_uom_label_flattens_enum_enum_value_and_raw_string_to_one_shape():
    """``Part.unit_of_measure`` is a native enum and ``BOMItem.unit_of_measure`` is a
    ``String(20)``, so the two sides of the question arrive in different shapes."""
    assert uom_label(UnitOfMeasure.SHEETS) == "sheets"
    assert uom_label(UnitOfMeasure.SHEETS.value) == "sheets"
    assert uom_label("  SHEETS  ") == "sheets"


def test_uom_label_flattens_a_missing_unit_to_the_empty_string():
    """A MISSING unit is not the same claim as a WRONG one, and the empty string is how
    that difference reaches ``uom_disagrees``."""
    assert uom_label(None) == ""
    assert uom_label("") == ""
    assert uom_label("   ") == ""


def test_uom_disagrees_is_silent_when_either_side_states_nothing():
    """A line that states no unit makes no claim to contradict; a part with no stocking
    unit gives nothing to contradict it. Both are silent — exactly as the blocking
    diagnostic has always been, which is what makes the ``"each"`` fallbacks in §4 safe."""
    assert uom_disagrees(None, UnitOfMeasure.SHEETS) is False
    assert uom_disagrees("", UnitOfMeasure.SHEETS) is False
    assert uom_disagrees("   ", UnitOfMeasure.SHEETS) is False
    assert uom_disagrees("sheets", None) is False
    assert uom_disagrees(None, None) is False


def test_uom_disagrees_compares_exact_labels_and_must_not_learn_synonyms():
    """``ea`` does NOT satisfy ``each`` here, deliberately.

    Teaching this predicate synonyms would make the BLOCKING gate accept lines it
    currently refuses — a softening of a control, not a bug fix. Those rows are surfaced
    by ``GET /bom/uom-mismatches`` so a human normalises the stored value instead.
    """
    assert uom_disagrees("each", UnitOfMeasure.SHEETS) is True
    assert uom_disagrees(" EACH ", UnitOfMeasure.EACH) is False
    assert uom_disagrees("ea", UnitOfMeasure.EACH) is True
    assert uom_disagrees("lbs", UnitOfMeasure.POUNDS) is True


# ===========================================================================
# 2. THE DEFAULT — every write path inherits the component part's unit
# ===========================================================================


def test_path4_add_item_without_a_stated_unit_stores_the_component_unit(client: TestClient, db_session: Session):
    """``POST /bom/{id}/items`` — write path 4, and the one that actually mattered.

    Neither ``pages/BOM.tsx`` nor ``components/parts/PartBOMTab.tsx`` sends
    ``unit_of_measure`` at all, so before this change every hand-added line on a
    sheet-metal component minted the literal ``"each"`` that the blocking diagnostic then
    refused. Both the stored row and the response are asserted: a response that said
    ``sheets`` over a row holding ``each`` would be the worst of the two failures.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    bom = make_bom(db_session, fg)

    response = add_item(client, user, bom.id, sheet)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["unit_of_measure"] == "sheets"
    assert stored_uom(db_session, response.json()["id"]) == "sheets"


def test_path3_create_bom_with_inline_items_inherits_the_component_unit(client: TestClient, db_session: Session):
    """``POST /bom/`` — write path 3, where the resolution has to happen BEFORE the splat.

    ``BOMItem(**item_data.model_dump())`` would hand the constructor an explicit ``None``,
    and a python-side column default fires on exactly that — quietly reinstating ``"each"``
    at INSERT. See the module docstring's first trap.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    bar = make_part(db_session, uom="pounds", part_type="raw_material")

    response = client.post(
        "/api/v1/bom/",
        headers=headers_for(user),
        json={
            "part_id": fg.id,
            "revision": "A",
            "items": [{"component_part_id": bar.id, "item_number": 10, "quantity": 4, "item_type": "buy"}],
        },
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["unit_of_measure"] == "pounds"
    assert stored_uom(db_session, items[0]["id"]) == "pounds"


def test_path1_import_commit_with_a_blank_unit_column_inherits_the_component_unit(
    client: TestClient, db_session: Session
):
    """``POST /bom/import/commit`` — write path 1. The bad default here was
    ``_normalize_uom(None)``, which returns ``"each"`` for a blank/unmapped UOM column."""
    user = make_user(db_session)
    make_part(db_session, uom="sheets", part_type="raw_material", part_number="COMP-SHEET")

    response = client.post(
        "/api/v1/bom/import/commit",
        headers=headers_for(user),
        json={
            "document_type": "bom",
            "assembly": {"part_number": "ASSY-UOM-1", "name": "Imported Assembly", "revision": "A"},
            "items": [
                {
                    "line_number": 10,
                    "part_number": "COMP-SHEET",
                    "description": "Sheet stock",
                    "quantity": 2,
                    "item_type": "buy",
                    "line_type": "component",
                }
            ],
            "create_missing_parts": True,
        },
    )

    assert response.status_code == status.HTTP_201_CREATED, response.text
    line = db_session.query(BOMItem).filter(BOMItem.bom_id == response.json()["bom_id"]).one()
    assert line.unit_of_measure == "sheets"


def test_path2_llm_import_with_a_blank_unit_inherits_the_component_unit(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """``POST /bom/import`` — write path 2, the one-shot upload+commit importer.

    Same rule as path 1, and it is a genuinely separate code path (a second ``BOMItem(``
    construction site), which is exactly why it gets its own test.
    """
    user = make_user(db_session)
    make_part(db_session, uom="feet", part_type="raw_material", part_number="COMP-TUBE")
    stub_llm_import(
        monkeypatch,
        [
            {
                "line_number": 10,
                "part_number": "COMP-TUBE",
                "description": "Square tube",
                "quantity": 3,
                "item_type": "buy",
                "line_type": "component",
            }
        ],
    )

    response = post_llm_import(client, user)

    assert response.status_code == status.HTTP_201_CREATED, response.text
    line = db_session.query(BOMItem).filter(BOMItem.bom_id == response.json()["bom_id"]).one()
    assert line.unit_of_measure == "feet"


def test_path5_update_clearing_the_unit_resolves_to_the_component_unit(client: TestClient, db_session: Session):
    """``PUT /bom/items/{id}`` — write path 5. An explicit ``null`` means "no stated unit",
    which resolves like the four create paths rather than writing a NULL nobody asked for."""
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    line = raw_line(db_session, make_bom(db_session, fg), sheet, unit_of_measure="each")

    response = client.put(
        f"/api/v1/bom/items/{line.id}",
        headers=headers_for(user),
        json={"unit_of_measure": None},
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["unit_of_measure"] == "sheets"
    assert stored_uom(db_session, line.id) == "sheets"


def test_path5_update_that_does_not_mention_the_unit_is_not_a_backfill(client: TestClient, db_session: Session):
    """The other half of write path 5, and a compliance property rather than a nicety.

    This series is **correct-forward**: a legacy line keeps its stored ``"each"`` until a
    human corrects it deliberately. An edit to some unrelated field must not silently
    rewrite the unit — that would be an out-of-band backfill of a value the blocking gate
    reads, hiding rows the remediation report is supposed to surface.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    line = raw_line(db_session, make_bom(db_session, fg), sheet, unit_of_measure="each")

    response = client.put(f"/api/v1/bom/items/{line.id}", headers=headers_for(user), json={"quantity": 7})

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["unit_of_measure"] == "each"
    assert stored_uom(db_session, line.id) == "each", "an unrelated edit must not backfill the unit"
    assert reported_line_ids(client, user) == {line.id}, "and the row must still be on the worklist"


# ===========================================================================
# 3. A STATED UNIT WINS — this resolves an ABSENCE, it does not override a human
# ===========================================================================


def test_path4_add_item_honours_an_explicitly_stated_unit_verbatim(client: TestClient, db_session: Session):
    """A caller who states ``each`` against a part stocked in ``sheets`` is making a CLAIM.

    The change must not second-guess it. The gate's job is to refuse that claim (see §6's
    negative control); rewriting it here would erase the very disagreement the blocking
    diagnostic exists to catch.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    bom = make_bom(db_session, fg)

    response = add_item(client, user, bom.id, sheet, unit_of_measure="each")

    assert response.status_code == status.HTTP_200_OK, response.text
    assert stored_uom(db_session, response.json()["id"]) == "each"
    assert reported_line_ids(client, user) == {response.json()["id"]}


def test_path3_create_bom_honours_an_explicitly_stated_unit_verbatim(client: TestClient, db_session: Session):
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")

    response = client.post(
        "/api/v1/bom/",
        headers=headers_for(user),
        json={
            "part_id": fg.id,
            "revision": "A",
            "items": [
                {
                    "component_part_id": sheet.id,
                    "item_number": 10,
                    "quantity": 1,
                    "item_type": "buy",
                    "unit_of_measure": "each",
                }
            ],
        },
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert stored_uom(db_session, response.json()["items"][0]["id"]) == "each"


def test_path5_update_honours_an_explicitly_stated_unit_verbatim(client: TestClient, db_session: Session):
    """The correcting edit the remediation report exists to drive: a human sets the line to
    the component's real unit, and the row leaves the worklist."""
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    line = raw_line(db_session, make_bom(db_session, fg), sheet, unit_of_measure="each")
    assert reported_line_ids(client, user) == {line.id}

    response = client.put(f"/api/v1/bom/items/{line.id}", headers=headers_for(user), json={"unit_of_measure": "sheets"})

    assert response.status_code == status.HTTP_200_OK, response.text
    assert stored_uom(db_session, line.id) == "sheets"
    assert reported_line_ids(client, user) == set(), "the corrected row leaves the worklist"


def test_path1_import_commit_still_normalises_a_stated_free_text_unit(client: TestClient, db_session: Session):
    """The importer doors keep ``_normalize_uom`` on a STATED value, and the JSON API doors
    deliberately do not.

    Spreadsheet/LLM text arrives as ``lbs`` / ``ea`` and always ran through that mapping;
    normalising the JSON paths instead would silently rewrite a string a client sent on
    purpose. Asserted here so the ``normalize_stated`` split is a decision with a test
    rather than an accident.
    """
    user = make_user(db_session)
    make_part(db_session, uom="pounds", part_type="raw_material", part_number="COMP-BAR")

    response = client.post(
        "/api/v1/bom/import/commit",
        headers=headers_for(user),
        json={
            "document_type": "bom",
            "assembly": {"part_number": "ASSY-UOM-2", "name": "Imported Assembly", "revision": "A"},
            "items": [
                {
                    "line_number": 10,
                    "part_number": "COMP-BAR",
                    "description": "Bar stock",
                    "quantity": 2,
                    "item_type": "buy",
                    "line_type": "component",
                    "unit_of_measure": "lbs",
                }
            ],
            "create_missing_parts": True,
        },
    )

    assert response.status_code == status.HTTP_201_CREATED, response.text
    line = db_session.query(BOMItem).filter(BOMItem.bom_id == response.json()["bom_id"]).one()
    assert line.unit_of_measure == "pounds", "a stated value still wins, normalised on the importer door"
    assert reported_line_ids(client, user) == set(), "and normalising it to the part's unit clears the disagreement"


def test_path2_llm_import_honours_a_stated_unit_that_contradicts_the_part(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """A document that says ``EA`` for a part stocked in sheets keeps saying it.

    ``_normalize_uom("EA")`` is ``"each"``, which contradicts ``sheets`` — so the row is
    stored as extracted AND lands on the remediation worklist. The importer is not the
    place to silently reconcile a document with the master data.
    """
    user = make_user(db_session)
    make_part(db_session, uom="sheets", part_type="raw_material", part_number="COMP-PLATE")
    stub_llm_import(
        monkeypatch,
        [
            {
                "line_number": 10,
                "part_number": "COMP-PLATE",
                "description": "Plate",
                "quantity": 1,
                "item_type": "buy",
                "line_type": "component",
                "unit_of_measure": "EA",
            }
        ],
        assembly_number="ASSY-LLM-UOM-2",
    )

    response = post_llm_import(client, user)

    assert response.status_code == status.HTTP_201_CREATED, response.text
    line = db_session.query(BOMItem).filter(BOMItem.bom_id == response.json()["bom_id"]).one()
    assert line.unit_of_measure == "each"
    assert reported_line_ids(client, user) == {line.id}


# ===========================================================================
# 4. THE FALLBACKS — "each" survives only where there is nothing to inherit
# ===========================================================================


def test_a_component_with_no_stocking_unit_falls_back_to_each_and_raises_nothing(
    client: TestClient, db_session: Session
):
    """``Part.unit_of_measure`` is nullable. A part with no stocking unit gives the line
    nothing to inherit, so the line keeps the column's own ``"each"`` — non-null the way it
    has always been — and ``uom_disagrees`` stays silent on the blank part side, so this
    fallback cannot manufacture the very mismatch the change exists to stop."""
    user = make_user(db_session)
    fg = make_part(db_session)
    unitless = make_part(db_session, uom=None, part_type="raw_material")
    assert db_session.get(Part, unitless.id).unit_of_measure is None, "fixture must really store NULL"
    bom = make_bom(db_session, fg)

    response = add_item(client, user, bom.id, unitless)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert stored_uom(db_session, response.json()["id"]) == "each"
    assert reported_line_ids(client, user) == set(), "a blank part unit is not a disagreement"
    assert "unit_of_measure_mismatch" not in readiness_blockers(client, user, fg.id)


def test_an_unresolvable_component_keeps_the_existing_unit_rather_than_writing_null(
    client: TestClient, db_session: Session
):
    """The last resort: the tenant-scoped component lookup resolves nothing at all.

    Reachable through ``PUT /bom/items/{id}`` when the line's FK points at a part that is
    not there — constructible here only because SQLite does not enforce foreign keys, and
    on Postgres the same state arrives via a hard delete. The requirement is that it
    neither 500s nor stores a NULL, and (since the tenant-scoped resolution fix) that it
    inherits NOTHING: with no in-tenant component to inherit from, the line keeps the
    value it already had instead of manufacturing an ``each`` nobody stated.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    ghost = make_part(db_session, uom="sheets", part_type="raw_material")
    line = raw_line(
        db_session,
        make_bom(db_session, fg),
        ghost,
        unit_of_measure="pounds",
        component_part_id=ghost.id + 900_000,
    )

    response = client.put(f"/api/v1/bom/items/{line.id}", headers=headers_for(user), json={"unit_of_measure": None})

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["unit_of_measure"] == "pounds"
    assert stored_uom(db_session, line.id) == "pounds"


def test_update_never_inherits_a_foreign_components_unit(client: TestClient, db_session: Session):
    """Defense-in-depth (invariant #1): a MIS-PARENTED line whose FK names another
    company's part must not read that row. The old code resolved a cleared unit through
    the unscoped ``component_part`` relationship, so clearing the unit stamped the
    FOREIGN company's stocking unit onto this company's line — foreign master data
    silently steering this company's backflush. Now the scoped lookup misses and the
    line keeps its own value."""
    user = make_user(db_session)
    fg = make_part(db_session)
    foreign_sheet = make_part(db_session, uom="sheets", part_type="raw_material", company_id=COMPANY_B)
    line = raw_line(
        db_session,
        make_bom(db_session, fg),
        foreign_sheet,
        unit_of_measure="pounds",
    )

    response = client.put(f"/api/v1/bom/items/{line.id}", headers=headers_for(user), json={"unit_of_measure": None})

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["unit_of_measure"] == "pounds", "the foreign unit must never be inherited"
    assert stored_uom(db_session, line.id) == "pounds"


def test_a_blank_string_unit_is_treated_as_unstated_not_as_a_claim(client: TestClient, db_session: Session):
    """``"   "`` is an absence, not a unit. It has to resolve like a missing value, or a
    whitespace cell from a spreadsheet would store a claim no human made."""
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    bom = make_bom(db_session, fg)

    response = add_item(client, user, bom.id, sheet, unit_of_measure="   ")

    assert response.status_code == status.HTTP_200_OK, response.text
    assert stored_uom(db_session, response.json()["id"]) == "sheets"


# ===========================================================================
# 5. THE REMEDIATION REPORT — exactly the disagreeing lines, and nothing else
# ===========================================================================


def test_report_returns_exactly_the_disagreeing_lines(client: TestClient, db_session: Session):
    """Four lines, one of them wrong. The three negatives are each a distinct silence rule
    the report has to honour, and a report that returned any of them would send someone to
    "fix" a row the gate never refused."""
    user = make_user(db_session)
    fg = make_part(db_session)
    bom = make_bom(db_session, fg)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    bar = make_part(db_session, uom="pounds", part_type="raw_material")
    unitless = make_part(db_session, uom=None, part_type="raw_material")

    wrong = raw_line(db_session, bom, sheet, unit_of_measure="each", item_number=10)
    raw_line(db_session, bom, bar, unit_of_measure="pounds", item_number=20)  # agrees
    raw_line(db_session, bom, sheet, unit_of_measure=None, item_number=30)  # line states nothing
    raw_line(db_session, bom, unitless, unit_of_measure="each", item_number=40)  # part states nothing

    body = report(client, user)

    assert [row["bom_item_id"] for row in body["items"]] == [wrong.id]
    assert body["total"] == 1 and body["returned"] == 1 and body["truncated"] is False
    row = body["items"][0]
    assert (row["line_unit_of_measure"], row["component_unit_of_measure"]) == ("each", "sheets")
    assert row["part_id"] == fg.id and row["part_number"] == fg.part_number
    assert row["component_part_id"] == sheet.id and row["component_part_number"] == sheet.part_number
    assert row["bom_id"] == bom.id and row["item_number"] == 10
    assert row["blocks_backflush"] is True


def test_report_agrees_with_the_blocking_gate_on_the_same_row(client: TestClient, db_session: Session):
    """The report and the diagnostic share ``uom_disagrees``, and this is the assertion that
    keeps them from drifting: one row, listed here AND refused there.

    A report that listed rows the gate does not refuse would waste a shop's time; one that
    hid rows the gate does refuse would leave a part permanently un-armable with no
    explanation of why.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    line = raw_line(db_session, make_bom(db_session, fg), sheet, unit_of_measure="each", quantity=2)

    assert reported_line_ids(client, user) == {line.id}
    assert "unit_of_measure_mismatch" in readiness_blockers(client, user, fg.id)


def test_report_is_tenant_scoped(client: TestClient, db_session: Session):
    """Company B's mismatched line must NOT appear in Company A's worklist, and vice versa.

    Every one of the four joined tables is scoped, matching how ``_explode_backflush_bom``
    scopes the same walk — a report that ranged wider than the gate would list rows nobody
    can act on.
    """
    user_a = make_user(db_session, company_id=COMPANY_A)
    user_b = make_user(db_session, company_id=COMPANY_B)

    fg_a = make_part(db_session, company_id=COMPANY_A)
    sheet_a = make_part(db_session, uom="sheets", part_type="raw_material", company_id=COMPANY_A)
    line_a = raw_line(
        db_session,
        make_bom(db_session, fg_a, company_id=COMPANY_A),
        sheet_a,
        unit_of_measure="each",
        company_id=COMPANY_A,
    )

    fg_b = make_part(db_session, company_id=COMPANY_B)
    sheet_b = make_part(db_session, uom="sheets", part_type="raw_material", company_id=COMPANY_B)
    line_b = raw_line(
        db_session,
        make_bom(db_session, fg_b, company_id=COMPANY_B),
        sheet_b,
        unit_of_measure="each",
        company_id=COMPANY_B,
    )

    assert reported_line_ids(client, user_a) == {line_a.id}
    assert reported_line_ids(client, user_b) == {line_b.id}


def test_report_writes_nothing(client: TestClient, db_session: Session):
    """PURE READ. It is a poll a human refreshes, with no actor intent and no reason
    recorded, so it must not extend the tamper-evident chain, post a ledger row or emit an
    operational event — the same rule the tie-view read obeys (invariant 6)."""
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    raw_line(db_session, make_bom(db_session, fg), sheet, unit_of_measure="each")

    before = (
        db_session.query(AuditLog).count(),
        db_session.query(InventoryTransaction).count(),
        db_session.query(OperationalEvent).count(),
    )

    assert len(report(client, user)["items"]) == 1
    assert len(report(client, user)["items"]) == 1  # twice — a first-call side effect would still show

    db_session.expire_all()
    assert (
        db_session.query(AuditLog).count(),
        db_session.query(InventoryTransaction).count(),
        db_session.query(OperationalEvent).count(),
    ) == before


def test_report_marks_lines_the_backflush_would_never_issue_as_non_blocking(client: TestClient, db_session: Session):
    """An alternate / optional / reference line raises no diagnostic and refuses no opt-in.

    Listing it without that distinction would send someone to fix a line that blocks
    nothing; dropping it entirely would hide a genuine data error. It is reported with
    ``blocks_backflush=False`` — cosmetic, bottom of the worklist.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    bom = make_bom(db_session, fg)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")

    reference = raw_line(db_session, bom, sheet, unit_of_measure="each", item_number=10, line_type="reference")
    optional = raw_line(db_session, bom, sheet, unit_of_measure="each", item_number=20, is_optional=True)
    blocking = raw_line(db_session, bom, sheet, unit_of_measure="each", item_number=30)

    flags = {row["bom_item_id"]: row["blocks_backflush"] for row in report(client, user)["items"]}

    assert flags == {reference.id: False, optional.id: False, blocking.id: True}


def test_report_discloses_a_soft_deleted_component_rather_than_hiding_the_row(client: TestClient, db_session: Session):
    """The readiness explosion resolves soft-deleted components of this company on purpose,
    so filtering them out here would hide a row that still blocks. It answers "why does
    this line name a part I cannot find"."""
    user = make_user(db_session)
    fg = make_part(db_session)
    gone = make_part(db_session, uom="sheets", part_type="raw_material", is_deleted=True)
    line = raw_line(db_session, make_bom(db_session, fg), gone, unit_of_measure="each")

    rows = report(client, user)["items"]

    assert [row["bom_item_id"] for row in rows] == [line.id]
    assert rows[0]["component_is_deleted"] is True


def test_report_filters_narrow_without_changing_the_verdict(client: TestClient, db_session: Session):
    """``part_id`` / ``bom_id`` / ``component_part_id`` are for working one assembly at a
    time. The UNFILTERED report stays the authoritative pre-arming worklist — the filters
    must subset it, never redefine it."""
    user = make_user(db_session)
    fg_one = make_part(db_session)
    fg_two = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    bar = make_part(db_session, uom="pounds", part_type="raw_material")

    bom_one = make_bom(db_session, fg_one)
    bom_two = make_bom(db_session, fg_two)
    line_one = raw_line(db_session, bom_one, sheet, unit_of_measure="each", item_number=10)
    line_two = raw_line(db_session, bom_two, bar, unit_of_measure="each", item_number=10)

    assert reported_line_ids(client, user) == {line_one.id, line_two.id}
    assert reported_line_ids(client, user, part_id=fg_one.id) == {line_one.id}
    assert reported_line_ids(client, user, bom_id=bom_two.id) == {line_two.id}
    assert reported_line_ids(client, user, component_part_id=sheet.id) == {line_one.id}


def test_report_active_only_defaults_true_and_can_be_widened(client: TestClient, db_session: Session):
    """``active_only`` defaults to the BOMs a backflush actually reads
    (``_get_active_bom`` filters ``is_active``), so an inactive BOM's line is not on the
    pre-arming worklist by default — but it is still findable when someone goes looking."""
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    inactive_line = raw_line(db_session, make_bom(db_session, fg, is_active=False), sheet, unit_of_measure="each")

    assert reported_line_ids(client, user) == set()
    assert reported_line_ids(client, user, active_only=False) == {inactive_line.id}


def test_report_pages_without_losing_the_total(client: TestClient, db_session: Session):
    user = make_user(db_session)
    fg = make_part(db_session)
    bom = make_bom(db_session, fg)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    ids = [raw_line(db_session, bom, sheet, unit_of_measure="each", item_number=n * 10).id for n in range(1, 4)]

    first = report(client, user, skip=0, limit=2)
    second = report(client, user, skip=2, limit=2)

    assert first["total"] == 3 and first["returned"] == 2
    assert second["total"] == 3 and second["returned"] == 1
    assert [row["bom_item_id"] for row in first["items"] + second["items"]] == ids


def test_report_total_counts_every_passing_row_while_items_carry_only_the_page(client: TestClient, db_session: Session):
    """``total`` is the size of the WORKLIST; ``items`` is the size of the PAGE.

    The endpoint now filters candidates to the passing list first and builds response
    models only for the requested slice. That is a shape in which it is very easy to make
    ``total`` accidentally mean "rows on this page" — which would tell a shop mid-
    remediation that it had 3 bad lines when it had 7, i.e. that it was finished when it
    was not. Agreeing lines are present throughout so ``total`` cannot be passing off a
    raw row count either.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    bom = make_bom(db_session, fg)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    bar = make_part(db_session, uom="pounds", part_type="raw_material")

    wrong_ids = [raw_line(db_session, bom, sheet, unit_of_measure="each", item_number=n).id for n in range(1, 8)]
    for n in range(20, 23):
        raw_line(db_session, bom, bar, unit_of_measure="pounds", item_number=n)  # agrees — never counted

    page = report(client, user, skip=0, limit=3)

    assert page["total"] == len(wrong_ids) == 7, "total must count the whole worklist, not the page"
    assert page["returned"] == 3 and len(page["items"]) == 3
    assert [row["bom_item_id"] for row in page["items"]] == wrong_ids[:3]


def test_report_paging_walks_every_row_exactly_once(client: TestClient, db_session: Session):
    """No duplicates, no gaps, stable order, and a total that never moves under the walk.

    Slicing the passing list rather than the built models keeps the page boundaries where
    they were, and this is the assertion that says so: an off-by-one in the new
    ``passing[skip : skip + limit]`` would either repeat a line (a shop "fixes" the same
    row twice) or skip one (a line that still blocks the opt-in never appears on any page,
    and nothing tells anyone it is missing).
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    bom = make_bom(db_session, fg)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    expected = [raw_line(db_session, bom, sheet, unit_of_measure="each", item_number=n).id for n in range(1, 8)]

    walked: list = []
    for skip in range(0, 8, 2):
        page = report(client, user, skip=skip, limit=2)
        assert page["total"] == 7, "the total must not drift as the client pages"
        assert page["returned"] == len(page["items"])
        walked.extend(row["bom_item_id"] for row in page["items"])

    assert walked == expected, "every row exactly once, in the report's own order"
    assert len(set(walked)) == len(walked), "no row may be served on two pages"

    past_the_end = report(client, user, skip=500, limit=2)
    assert past_the_end["items"] == [] and past_the_end["returned"] == 0
    assert past_the_end["total"] == 7, "an empty page still reports the full worklist"


def test_report_total_counts_only_rows_the_python_predicate_passes(client: TestClient, db_session: Session):
    """``uom_disagrees`` is the authority; the SQL predicate is only a narrowing pre-filter.

    ``"each\\t"`` proves the gap is real and one-directional: SQL ``trim`` strips spaces
    only, so the tab-suffixed line reaches Python as a CANDIDATE, while ``uom_label``
    strips all whitespace and finds it identical to the part's ``each``. It must be counted
    in neither ``total`` nor ``items`` — a ``total`` taken off the candidate list instead of
    the passing list would put a row on the worklist that the blocking gate never refuses.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    bom = make_bom(db_session, fg)
    each_part = make_part(db_session, uom="each", part_type="raw_material")
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")

    raw_line(db_session, bom, each_part, unit_of_measure="each\t", item_number=10)  # candidate, not a disagreement
    genuine = raw_line(db_session, bom, sheet, unit_of_measure="each", item_number=20)

    body = report(client, user)

    assert body["total"] == 1 and body["returned"] == 1
    assert [row["bom_item_id"] for row in body["items"]] == [genuine.id]


def test_report_truncation_is_measured_on_candidates_not_on_the_page(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """``truncated`` means "the SCAN stopped early", and it still does.

    It is computed from the candidate fetch (``ceiling + 1``), before either the Python
    filter or the page slice — so building models for one page instead of all of them
    cannot change it. Were it ever re-derived from the page, every paged request would
    claim truncation and the number a shop is working from would silently become a floor.
    The ceiling is monkeypatched rather than met with 5001 fixtures.
    """
    monkeypatch.setattr("app.api.endpoints.bom._UOM_MISMATCH_SCAN_CEILING", 2)
    user = make_user(db_session)
    fg = make_part(db_session)
    bom = make_bom(db_session, fg)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    for n in range(1, 5):
        raw_line(db_session, bom, sheet, unit_of_measure="each", item_number=n)

    body = report(client, user, skip=0, limit=100)

    assert body["truncated"] is True, "the scan stopped at the ceiling and must say so"
    assert body["total"] == 2 and body["returned"] == 2, "a truncated total is a floor: what was scanned"
    assert report(client, user, skip=0, limit=1)["truncated"] is True, "the page size must not change the verdict"


def test_report_is_gated_to_the_roles_that_can_act_on_it(client: TestClient, db_session: Session):
    """It is a remediation worklist. ADMIN / MANAGER / SUPERVISOR are exactly the roles
    that can edit a BOM line or arm the flag; handing it to an operator buys nothing."""
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    raw_line(db_session, make_bom(db_session, fg), sheet, unit_of_measure="each")

    for role in (UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR):
        allowed = client.get("/api/v1/bom/uom-mismatches", headers=headers_for(make_user(db_session, role=role)))
        assert allowed.status_code == status.HTTP_200_OK, f"{role} must be able to read the worklist"

    operator = make_user(db_session, role=UserRole.OPERATOR)
    refused = client.get("/api/v1/bom/uom-mismatches", headers=headers_for(operator))
    assert refused.status_code == status.HTTP_403_FORBIDDEN, refused.text


def test_the_report_route_is_not_swallowed_by_the_bom_id_path_param(client: TestClient, db_session: Session):
    """``GET /bom/{bom_id}`` is a single-segment int path parameter declared in the same
    router. If ``/uom-mismatches`` is ever moved below it, FastAPI matches the parameter
    first and the endpoint 422s on the int conversion — a routing accident with no other
    symptom, so it gets its own assertion."""
    user = make_user(db_session)

    response = client.get("/api/v1/bom/uom-mismatches", headers=headers_for(user))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json() == {"total": 0, "returned": 0, "truncated": False, "items": []}


# ===========================================================================
# 6. THE PAYOFF — the part the shop actually has is now armable
# ===========================================================================


def test_a_part_whose_bom_was_built_without_stating_units_is_now_armable(client: TestClient, db_session: Session):
    """THE point of the change, end to end, through the doors a shop uses.

    A sheet-metal assembly: components stocked in ``sheets`` and ``pounds``, BOM lines
    added through the API exactly as the UI adds them — i.e. with no ``unit_of_measure`` in
    the body at all. Before this change both lines stored the literal ``"each"`` and
    ``PUT /parts/{id}`` refused the opt-in with 409 ``unit_of_measure_mismatch``. Now the
    lines inherit, nothing is on the worklist, and the flag flips.
    """
    user = make_user(db_session)
    fg = make_part(db_session, part_type="manufactured")
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    bar = make_part(db_session, uom="pounds", part_type="raw_material")
    bom = make_bom(db_session, fg)

    first = add_item(client, user, bom.id, sheet, item_number=10, quantity=2)
    second = add_item(client, user, bom.id, bar, item_number=20, quantity=3)
    assert first.status_code == status.HTTP_200_OK, first.text
    assert second.status_code == status.HTTP_200_OK, second.text
    assert stored_uom(db_session, first.json()["id"]) == "sheets"
    assert stored_uom(db_session, second.json()["id"]) == "pounds"

    assert "unit_of_measure_mismatch" not in readiness_blockers(client, user, fg.id)
    assert reported_line_ids(client, user) == set()

    response = arm(client, user, fg.id)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["backflush_components"] is True
    db_session.expire_all()
    assert db_session.get(Part, fg.id).backflush_components is True


def test_a_stated_unit_that_contradicts_the_part_is_still_refused(client: TestClient, db_session: Session):
    """The negative control, and the guard on the severity.

    This is the shape the payoff test used to have — a line holding ``"each"`` against a
    component stocked in ``sheets`` — and it must STILL be refused: as a legacy row it is
    exactly what the correct-forward decision leaves behind, and as a stated value it is a
    claim nothing in the platform can convert. If someone ever "fixes" the feature by
    softening ``BACKFLUSH_BLOCKING``, this test is what fails.
    """
    user = make_user(db_session)
    fg = make_part(db_session, part_type="manufactured")
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    line = raw_line(db_session, make_bom(db_session, fg), sheet, unit_of_measure="each", quantity=2)

    assert "unit_of_measure_mismatch" in readiness_blockers(client, user, fg.id)
    assert reported_line_ids(client, user) == {line.id}

    response = arm(client, user, fg.id)

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    detail = response.json()["detail"]
    assert "sheets" in detail and "each" in detail, "the sentence must name both units or it cannot be acted on"
    db_session.expire_all()
    assert db_session.get(Part, fg.id).backflush_components is False, "a refusal must leave the row untouched"


def test_correcting_the_line_through_the_worklist_unblocks_the_opt_in(client: TestClient, db_session: Session):
    """The full remediation loop the report exists to serve: find the row, correct it,
    arm the part. Proves the report's row identifiers are actionable — the ``bom_item_id``
    it hands back is the one ``PUT /bom/items/{id}`` takes."""
    user = make_user(db_session)
    fg = make_part(db_session, part_type="manufactured")
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    raw_line(db_session, make_bom(db_session, fg), sheet, unit_of_measure="each", quantity=2)

    row = report(client, user, part_id=fg.id)["items"][0]
    assert arm(client, user, fg.id).status_code == status.HTTP_409_CONFLICT

    fix = client.put(
        f"/api/v1/bom/items/{row['bom_item_id']}",
        headers=headers_for(user),
        json={"unit_of_measure": row["component_unit_of_measure"]},
    )
    assert fix.status_code == status.HTTP_200_OK, fix.text

    assert reported_line_ids(client, user) == set()
    assert arm(client, user, fg.id).status_code == status.HTTP_200_OK
