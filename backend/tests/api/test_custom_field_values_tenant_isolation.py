"""Two-company coverage for the custom-field VALUE routes (``/api/v1/custom-fields/values/*``).

Before this fix the four value routes took no company at all: the definition was found by
``field_key`` across every tenant, the value row by (definition, entity_type, entity_id)
alone, and the target entity was never checked -- so any signed-in user could read,
overwrite or physically delete another tenant's value by guessing an id, and the create
leg built a ``CustomFieldValue`` with no ``company_id`` (NOT NULL -> 500). Nothing was
audited.

Pinned here:

* happy path in one company: create / read / overwrite / delete, each write audited with
  the active company and the old/new value;
* every route against another company's entity is a 404 with NO value-row delta and NO
  audit delta -- including when the caller's own company defines the same ``field_key``;
* a same-key definition in another company is never used or touched;
* reads accept a soft-deleted entity, writes refuse it (404);
* ``ENTITY_MODELS`` maps every ``EntityType`` so no type can skip the existence check.
"""

from datetime import datetime

import pytest
import sqlalchemy as sa
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.bom import BOM
from app.models.company import Company
from app.models.custom_field import CustomFieldDefinition, CustomFieldValue, EntityType, FieldType
from app.models.customer import Customer
from app.models.inventory import InventoryItem
from app.models.part import Part
from app.models.purchasing import Vendor
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.services.custom_field_values import ENTITY_MODELS, RESOURCE_TYPE

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
FIELD_KEY = "heat_treat_spec"

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True))
        db.commit()


def _make_user(db: Session, *, company_id: int, role: UserRole = UserRole.MANAGER) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"cfv-{n}@co{company_id}.test",
        employee_id=f"CFV-{n:05d}",
        first_name="Field",
        last_name=f"C{company_id}",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",  # tokens are minted directly; never used for login
        role=role,
        is_active=True,
        is_superuser=False,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _headers(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _part(db: Session, *, company_id: int, is_deleted: bool = False) -> Part:
    _ensure_company(db, company_id)
    part = Part(
        part_number=f"CFV-P-{_next()}",
        name="Bracket",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        is_deleted=is_deleted,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def _definition(
    db: Session,
    *,
    company_id: int,
    field_key: str = FIELD_KEY,
    field_type: FieldType = FieldType.TEXT,
    entity_type: EntityType = EntityType.PART,
) -> CustomFieldDefinition:
    _ensure_company(db, company_id)
    field = CustomFieldDefinition(
        field_key=field_key,
        display_name=field_key.replace("_", " ").title(),
        entity_type=entity_type,
        field_type=field_type,
        is_active=True,
        company_id=company_id,
    )
    db.add(field)
    db.commit()
    db.refresh(field)
    return field


def _value(db: Session, *, company_id: int, field: CustomFieldDefinition, part: Part, text: str) -> CustomFieldValue:
    row = CustomFieldValue(
        field_definition_id=field.id,
        entity_type=EntityType.PART,
        entity_id=part.id,
        value_text=text,
        company_id=company_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _counts(db: Session) -> tuple:
    db.expire_all()
    return db.query(CustomFieldValue).count(), db.query(AuditLog).count()


def _value_audits(db: Session, action: str):
    db.rollback()  # only COMMITTED audit rows survive this
    db.expire_all()
    return (
        db.query(AuditLog)
        .filter(AuditLog.resource_type == RESOURCE_TYPE, AuditLog.action == action)
        .order_by(AuditLog.sequence_number)
        .all()
    )


def test_entity_models_cover_every_entity_type():
    assert set(ENTITY_MODELS) == set(EntityType)
    for model in ENTITY_MODELS.values():
        assert hasattr(model, "company_id"), f"{model.__name__} is not a tenant table"


class TestCustomFieldValuesSameCompany:
    def test_create_read_overwrite_delete_are_scoped_and_audited(self, client: TestClient, db_session: Session):
        user = _make_user(db_session, company_id=COMPANY_A)
        part = _part(db_session, company_id=COMPANY_A)
        field = _definition(db_session, company_id=COMPANY_A)
        headers = _headers(user)
        url = f"/api/v1/custom-fields/values/part/{part.id}"

        created = client.post(url, headers=headers, json={"field_key": FIELD_KEY, "value": "AMS 2759"})
        assert created.status_code == status.HTTP_200_OK, created.text
        assert created.json() == {"message": "Value created", "field_key": FIELD_KEY}

        row = db_session.query(CustomFieldValue).one()
        assert row.company_id == COMPANY_A
        assert row.field_definition_id == field.id

        read = client.get(url, headers=headers)
        assert read.status_code == status.HTTP_200_OK
        assert read.json()["fields"] == {FIELD_KEY: "AMS 2759"}

        updated = client.post(url, headers=headers, json={"field_key": FIELD_KEY, "value": "AMS 2759/1"})
        assert updated.status_code == status.HTTP_200_OK
        assert updated.json()["message"] == "Value updated"

        deleted = client.delete(f"{url}/{FIELD_KEY}", headers=headers)
        assert deleted.status_code == status.HTTP_200_OK, deleted.text
        assert _counts(db_session)[0] == 0

        creates = _value_audits(db_session, "CREATE")
        updates = _value_audits(db_session, "UPDATE")
        deletes = _value_audits(db_session, "DELETE")
        assert [len(creates), len(updates), len(deletes)] == [1, 1, 1]
        for audit_row in (*creates, *updates, *deletes):
            assert audit_row.company_id == COMPANY_A
            assert audit_row.user_id == user.id
            assert audit_row.resource_identifier == f"part:{part.id}:{FIELD_KEY}"
        assert creates[0].new_values["value_text"] == "AMS 2759"
        assert updates[0].old_values["value_text"] == "AMS 2759"
        assert updates[0].new_values["value_text"] == "AMS 2759/1"
        assert deletes[0].old_values["value_text"] == "AMS 2759/1"

    def test_bulk_writes_are_scoped_and_audited(self, client: TestClient, db_session: Session):
        user = _make_user(db_session, company_id=COMPANY_A)
        part = _part(db_session, company_id=COMPANY_A)
        _definition(db_session, company_id=COMPANY_A)
        _definition(db_session, company_id=COMPANY_A, field_key="batch_size", field_type=FieldType.NUMBER)

        resp = client.post(
            "/api/v1/custom-fields/values/bulk",
            headers=_headers(user),
            json={
                "entity_type": "part",
                "entity_id": part.id,
                "values": {FIELD_KEY: "AMS 2770", "batch_size": "not-a-number", "unknown_key": 1},
            },
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        by_key = {r["field_key"]: r["status"] for r in resp.json()["results"]}
        assert by_key == {FIELD_KEY: "success", "batch_size": "error", "unknown_key": "error"}

        rows = db_session.query(CustomFieldValue).all()
        assert [(r.company_id, r.value_text) for r in rows] == [(COMPANY_A, "AMS 2770")]
        assert len(_value_audits(db_session, "CREATE")) == 1

    def test_a_value_the_field_type_cannot_hold_is_400(self, client: TestClient, db_session: Session):
        user = _make_user(db_session, company_id=COMPANY_A)
        part = _part(db_session, company_id=COMPANY_A)
        _definition(db_session, company_id=COMPANY_A, field_key="batch_size", field_type=FieldType.NUMBER)

        before = _counts(db_session)
        resp = client.post(
            f"/api/v1/custom-fields/values/part/{part.id}",
            headers=_headers(user),
            json={"field_key": "batch_size", "value": "twelve"},
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert _counts(db_session) == before

    def test_soft_deleted_entity_reads_but_refuses_writes(self, client: TestClient, db_session: Session):
        user = _make_user(db_session, company_id=COMPANY_A)
        part = _part(db_session, company_id=COMPANY_A)
        field = _definition(db_session, company_id=COMPANY_A)
        _value(db_session, company_id=COMPANY_A, field=field, part=part, text="historic")
        part.is_deleted = True
        db_session.commit()
        headers = _headers(user)
        url = f"/api/v1/custom-fields/values/part/{part.id}"

        read = client.get(url, headers=headers)
        assert read.status_code == status.HTTP_200_OK
        assert read.json()["fields"] == {FIELD_KEY: "historic"}

        before = _counts(db_session)
        assert (
            client.post(url, headers=headers, json={"field_key": FIELD_KEY, "value": "x"}).status_code
            == status.HTTP_404_NOT_FOUND
        )
        assert client.delete(f"{url}/{FIELD_KEY}", headers=headers).status_code == status.HTTP_404_NOT_FOUND
        assert _counts(db_session) == before

    def test_unknown_entity_id_is_404(self, client: TestClient, db_session: Session):
        user = _make_user(db_session, company_id=COMPANY_A)
        _definition(db_session, company_id=COMPANY_A)
        before = _counts(db_session)
        resp = client.post(
            "/api/v1/custom-fields/values/part/999999",
            headers=_headers(user),
            json={"field_key": FIELD_KEY, "value": "x"},
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND
        assert _counts(db_session) == before


class TestCustomFieldValuesWrongTenant:
    """User of company B against company A's part. B defines the SAME field_key, so the
    old by-key lookup would have resolved a definition; only the entity check stops it."""

    @pytest.fixture
    def scenario(self, db_session: Session):
        part_a = _part(db_session, company_id=COMPANY_A)
        field_a = _definition(db_session, company_id=COMPANY_A)
        value_a = _value(db_session, company_id=COMPANY_A, field=field_a, part=part_a, text="A-SECRET")
        user_b = _make_user(db_session, company_id=COMPANY_B)
        field_b = _definition(db_session, company_id=COMPANY_B)
        return {"part_a": part_a, "value_a_id": value_a.id, "field_b": field_b, "headers_b": _headers(user_b)}

    def _assert_a_untouched(self, db: Session, value_a_id: int) -> None:
        db.expire_all()
        row = db.query(CustomFieldValue).filter(CustomFieldValue.id == value_a_id).one()
        assert row.value_text == "A-SECRET"
        assert row.company_id == COMPANY_A

    def test_get_is_404(self, client: TestClient, db_session: Session, scenario):
        before = _counts(db_session)
        resp = client.get(f"/api/v1/custom-fields/values/part/{scenario['part_a'].id}", headers=scenario["headers_b"])
        assert resp.status_code == status.HTTP_404_NOT_FOUND
        assert "A-SECRET" not in resp.text
        assert _counts(db_session) == before

    def test_set_is_404(self, client: TestClient, db_session: Session, scenario):
        before = _counts(db_session)
        resp = client.post(
            f"/api/v1/custom-fields/values/part/{scenario['part_a'].id}",
            headers=scenario["headers_b"],
            json={"field_key": FIELD_KEY, "value": "OVERWRITTEN"},
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND
        assert _counts(db_session) == before
        self._assert_a_untouched(db_session, scenario["value_a_id"])

    def test_bulk_is_404(self, client: TestClient, db_session: Session, scenario):
        before = _counts(db_session)
        resp = client.post(
            "/api/v1/custom-fields/values/bulk",
            headers=scenario["headers_b"],
            json={"entity_type": "part", "entity_id": scenario["part_a"].id, "values": {FIELD_KEY: "OVERWRITTEN"}},
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND
        assert _counts(db_session) == before
        self._assert_a_untouched(db_session, scenario["value_a_id"])

    def test_delete_is_404(self, client: TestClient, db_session: Session, scenario):
        before = _counts(db_session)
        resp = client.delete(
            f"/api/v1/custom-fields/values/part/{scenario['part_a'].id}/{FIELD_KEY}", headers=scenario["headers_b"]
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND
        assert _counts(db_session) == before
        self._assert_a_untouched(db_session, scenario["value_a_id"])

    def test_own_write_uses_own_definition_and_never_touches_the_other_tenants(
        self, client: TestClient, db_session: Session, scenario
    ):
        user_b = _make_user(db_session, company_id=COMPANY_B)
        part_b = _part(db_session, company_id=COMPANY_B)
        resp = client.post(
            f"/api/v1/custom-fields/values/part/{part_b.id}",
            headers=_headers(user_b),
            json={"field_key": FIELD_KEY, "value": "B-VALUE"},
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text

        db_session.expire_all()
        row = db_session.query(CustomFieldValue).filter(CustomFieldValue.entity_id == part_b.id).one()
        assert row.company_id == COMPANY_B
        assert row.field_definition_id == scenario["field_b"].id
        self._assert_a_untouched(db_session, scenario["value_a_id"])

        read = client.get(f"/api/v1/custom-fields/values/part/{part_b.id}", headers=_headers(user_b))
        assert read.json()["fields"] == {FIELD_KEY: "B-VALUE"}

    def test_a_value_whose_definition_is_another_tenants_is_never_read(self, client: TestClient, db_session: Session):
        """A stray row (company B's id on company A's definition) is invisible to both."""
        user_a = _make_user(db_session, company_id=COMPANY_A)
        part_a = _part(db_session, company_id=COMPANY_A)
        field_b = _definition(db_session, company_id=COMPANY_B)
        db_session.add(
            CustomFieldValue(
                field_definition_id=field_b.id,
                entity_type=EntityType.PART,
                entity_id=part_a.id,
                value_text="CROSSED",
                company_id=COMPANY_A,
            )
        )
        db_session.commit()

        read = client.get(f"/api/v1/custom-fields/values/part/{part_a.id}", headers=_headers(user_a))
        assert read.status_code == status.HTTP_200_OK
        assert read.json()["fields"] == {}


# ---------------------------------------------------------------------------
# Gaps closed by the test pass (each names the mutation it was checked against)
# ---------------------------------------------------------------------------

# An id no other mapped table holds in a fresh per-test database (dependency rows get
# low auto ids), so a mis-mapped EntityType cannot be rescued by an id collision.
TARGET_ID = 9001


def _entity_row(db: Session, entity_type: EntityType, *, company_id: int, row_id: int) -> None:
    """One row of ``entity_type``'s real table, with an explicit id."""
    _ensure_company(db, company_id)
    if entity_type is EntityType.PART:
        row = Part(
            id=row_id,
            part_number=f"CFV-T-{row_id}",
            name="Target",
            part_type="manufactured",
            unit_of_measure="each",
            company_id=company_id,
        )
    elif entity_type is EntityType.CUSTOMER:
        row = Customer(id=row_id, name=f"CFV Customer {row_id}", code=f"CFVC{row_id}", company_id=company_id)
    elif entity_type is EntityType.SUPPLIER:
        row = Vendor(id=row_id, name=f"CFV Vendor {row_id}", code=f"CFVV{row_id}", company_id=company_id)
    elif entity_type is EntityType.WORK_CENTER:
        row = WorkCenter(
            id=row_id, code=f"CFVW{row_id}", name="Target WC", work_center_type="fabrication", company_id=company_id
        )
    else:
        dependency = _part(db, company_id=company_id)
        if entity_type is EntityType.WORK_ORDER:
            row = WorkOrder(
                id=row_id,
                work_order_number=f"CFV-WO-{row_id}",
                part_id=dependency.id,
                quantity_ordered=1,
                status=WorkOrderStatus.DRAFT,
                company_id=company_id,
            )
        elif entity_type is EntityType.INVENTORY:
            row = InventoryItem(id=row_id, part_id=dependency.id, location="A-01", company_id=company_id)
        elif entity_type is EntityType.BOM:
            row = BOM(id=row_id, part_id=dependency.id, company_id=company_id)
        else:  # pragma: no cover - test_entity_models_cover_every_entity_type keeps this unreachable
            raise AssertionError(f"no fixture for {entity_type}")
    db.add(row)
    db.commit()


@pytest.mark.parametrize("entity_type", list(EntityType), ids=lambda e: e.value)
def test_every_entity_type_resolves_its_own_table_and_refuses_another_tenant(
    client: TestClient, db_session: Session, entity_type: EntityType
):
    """``ENTITY_MODELS`` is the whole existence check, so exercise every entry over HTTP.

    The original wrong-tenant class only ever targets ``part``. Checked: mapping
    ``supplier`` to ``Customer`` (or ``inventory`` to ``Part``) fails company A's own write
    here, because the explicit ``TARGET_ID`` exists only in the right table.
    """
    _entity_row(db_session, entity_type, company_id=COMPANY_A, row_id=TARGET_ID)
    _definition(db_session, company_id=COMPANY_A, entity_type=entity_type)
    _definition(db_session, company_id=COMPANY_B, entity_type=entity_type)  # B defines the same key
    headers_a = _headers(_make_user(db_session, company_id=COMPANY_A))
    headers_b = _headers(_make_user(db_session, company_id=COMPANY_B))
    url = f"/api/v1/custom-fields/values/{entity_type.value}/{TARGET_ID}"

    before = _counts(db_session)
    assert client.get(url, headers=headers_b).status_code == status.HTTP_404_NOT_FOUND
    assert (
        client.post(url, headers=headers_b, json={"field_key": FIELD_KEY, "value": "B"}).status_code
        == status.HTTP_404_NOT_FOUND
    )
    bulk = client.post(
        "/api/v1/custom-fields/values/bulk",
        headers=headers_b,
        json={"entity_type": entity_type.value, "entity_id": TARGET_ID, "values": {FIELD_KEY: "B"}},
    )
    assert bulk.status_code == status.HTTP_404_NOT_FOUND
    assert client.delete(f"{url}/{FIELD_KEY}", headers=headers_b).status_code == status.HTTP_404_NOT_FOUND
    assert _counts(db_session) == before

    own = client.post(url, headers=headers_a, json={"field_key": FIELD_KEY, "value": "A-OWN"})
    assert own.status_code == status.HTTP_200_OK, own.text
    assert client.get(url, headers=headers_a).json()["fields"] == {FIELD_KEY: "A-OWN"}
    row = db_session.query(CustomFieldValue).one()
    assert (row.company_id, row.entity_type, row.entity_id) == (COMPANY_A, entity_type, TARGET_ID)


class TestCustomFieldValuesGapCoverage:
    def test_another_tenants_definition_is_never_used(self, client: TestClient, db_session: Session):
        """Only company A defines the key; company B writes to its OWN live part.

        In the wrong-tenant class B also defines the key, so a definition lookup that lost
        its tenant scope still found a definition. Checked: replacing ``tenant_query`` with
        ``db.query`` in ``find_definition`` makes B's write create a row on A's definition and
        fails this test.
        """
        _definition(db_session, company_id=COMPANY_A)
        user_b = _make_user(db_session, company_id=COMPANY_B)
        part_b = _part(db_session, company_id=COMPANY_B)
        headers_b = _headers(user_b)
        url = f"/api/v1/custom-fields/values/part/{part_b.id}"

        before = _counts(db_session)
        single = client.post(url, headers=headers_b, json={"field_key": FIELD_KEY, "value": "x"})
        assert single.status_code == status.HTTP_404_NOT_FOUND
        assert single.json()["detail"] == f"Field '{FIELD_KEY}' not found for part"

        bulk = client.post(
            "/api/v1/custom-fields/values/bulk",
            headers=headers_b,
            json={"entity_type": "part", "entity_id": part_b.id, "values": {FIELD_KEY: "x"}},
        )
        assert bulk.status_code == status.HTTP_200_OK
        assert bulk.json()["results"] == [{"field_key": FIELD_KEY, "status": "error", "message": "Field not found"}]

        deleted = client.delete(f"{url}/{FIELD_KEY}", headers=headers_b)
        assert deleted.status_code == status.HTTP_404_NOT_FOUND
        assert deleted.json()["detail"] == "Field not found"
        assert _counts(db_session) == before

    def test_delete_checks_the_entity_before_the_value(self, client: TestClient, db_session: Session):
        """A stray value row of company B's own on company A's part id is still a 404 for B.

        The wrong-tenant ``test_delete_is_404`` is stopped by the tenant-scoped definition and
        value lookups alone. Checked: deleting ``require_entity`` from the DELETE route leaves
        that test green, but this one fails (B deletes the row through A's entity id).
        """
        part_a = _part(db_session, company_id=COMPANY_A)
        field_b = _definition(db_session, company_id=COMPANY_B)
        stray = CustomFieldValue(
            field_definition_id=field_b.id,
            entity_type=EntityType.PART,
            entity_id=part_a.id,
            value_text="STRAY",
            company_id=COMPANY_B,
        )
        db_session.add(stray)
        db_session.commit()
        stray_id = stray.id
        headers_b = _headers(_make_user(db_session, company_id=COMPANY_B))

        before = _counts(db_session)
        url = f"/api/v1/custom-fields/values/part/{part_a.id}"
        assert client.delete(f"{url}/{FIELD_KEY}", headers=headers_b).status_code == status.HTTP_404_NOT_FOUND
        assert client.get(url, headers=headers_b).status_code == status.HTTP_404_NOT_FOUND
        assert _counts(db_session) == before
        assert db_session.query(CustomFieldValue).filter(CustomFieldValue.id == stray_id).one().value_text == "STRAY"

    def test_deactivated_field_value_is_hidden_unwritable_but_still_deletable(
        self, client: TestClient, db_session: Session
    ):
        """Checked: passing ``active_only=True`` to the DELETE route's lookup fails this test."""
        user = _make_user(db_session, company_id=COMPANY_A)
        part = _part(db_session, company_id=COMPANY_A)
        field = _definition(db_session, company_id=COMPANY_A)
        _value(db_session, company_id=COMPANY_A, field=field, part=part, text="retired")
        field.is_active = False
        db_session.commit()
        headers = _headers(user)
        url = f"/api/v1/custom-fields/values/part/{part.id}"

        assert client.get(url, headers=headers).json()["fields"] == {}
        before = _counts(db_session)
        write = client.post(url, headers=headers, json={"field_key": FIELD_KEY, "value": "new"})
        assert write.status_code == status.HTTP_404_NOT_FOUND
        assert _counts(db_session) == before

        deleted = client.delete(f"{url}/{FIELD_KEY}", headers=headers)
        assert deleted.status_code == status.HTTP_200_OK, deleted.text
        assert _counts(db_session)[0] == 0
        audits = _value_audits(db_session, "DELETE")
        assert [(a.company_id, a.old_values["value_text"]) for a in audits] == [(COMPANY_A, "retired")]

    def test_bulk_overwrite_is_audited_as_an_update(self, client: TestClient, db_session: Session):
        user = _make_user(db_session, company_id=COMPANY_A)
        part = _part(db_session, company_id=COMPANY_A)
        field = _definition(db_session, company_id=COMPANY_A)
        _value(db_session, company_id=COMPANY_A, field=field, part=part, text="first")

        resp = client.post(
            "/api/v1/custom-fields/values/bulk",
            headers=_headers(user),
            json={"entity_type": "part", "entity_id": part.id, "values": {FIELD_KEY: "second"}},
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        db_session.expire_all()
        assert [r.value_text for r in db_session.query(CustomFieldValue).all()] == ["second"]
        updates = _value_audits(db_session, "UPDATE")
        assert [(u.old_values["value_text"], u.new_values["value_text"]) for u in updates] == [("first", "second")]
        assert _value_audits(db_session, "CREATE") == []


# ---------------------------------------------------------------------------
# Review follow-ups: non-string dates, definition audit, no per-row lazy load
# ---------------------------------------------------------------------------

NON_STRING_DATES = [5, True, {"year": 2026}, [2026, 11, 5]]


class TestDateValuesMustBeStrings:
    """A DATE / DATETIME value arrives over JSON; only an ISO-8601 string can be one.

    Checked: with ``parse_value`` passing a non-string straight into ``value_date`` (the old
    ``else`` branch) the create is a 500 at ``upsert_value``'s flush, the overwrite a 500 from
    ``PendingRollbackError`` after the audit call's autoflush swallowed the failure, and the
    bulk request dies whole instead of reporting one key's error.
    """

    @pytest.mark.parametrize("raw", NON_STRING_DATES)
    def test_create_with_a_non_string_date_is_400(self, client: TestClient, db_session: Session, raw):
        user = _make_user(db_session, company_id=COMPANY_A)
        part = _part(db_session, company_id=COMPANY_A)
        _definition(db_session, company_id=COMPANY_A, field_key="cert_date", field_type=FieldType.DATE)

        before = _counts(db_session)
        resp = client.post(
            f"/api/v1/custom-fields/values/part/{part.id}",
            headers=_headers(user),
            json={"field_key": "cert_date", "value": raw},
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        assert resp.json()["detail"] == "Invalid value for 'cert_date' (date)"
        assert _counts(db_session) == before

    @pytest.mark.parametrize("raw", NON_STRING_DATES)
    def test_overwrite_with_a_non_string_datetime_is_400_and_keeps_the_value(
        self, client: TestClient, db_session: Session, raw
    ):
        user = _make_user(db_session, company_id=COMPANY_A)
        part = _part(db_session, company_id=COMPANY_A)
        field = _definition(db_session, company_id=COMPANY_A, field_key="shipped_at", field_type=FieldType.DATETIME)
        original = datetime(2026, 11, 1, 14, 30)
        db_session.add(
            CustomFieldValue(
                field_definition_id=field.id,
                entity_type=EntityType.PART,
                entity_id=part.id,
                value_date=original,
                company_id=COMPANY_A,
            )
        )
        db_session.commit()

        before = _counts(db_session)
        resp = client.post(
            f"/api/v1/custom-fields/values/part/{part.id}",
            headers=_headers(user),
            json={"field_key": "shipped_at", "value": raw},
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        assert _counts(db_session) == before
        assert db_session.query(CustomFieldValue).one().value_date == original

    def test_bulk_reports_a_non_string_date_per_key_and_applies_the_rest(self, client: TestClient, db_session: Session):
        user = _make_user(db_session, company_id=COMPANY_A)
        part = _part(db_session, company_id=COMPANY_A)
        _definition(db_session, company_id=COMPANY_A)
        _definition(db_session, company_id=COMPANY_A, field_key="cert_date", field_type=FieldType.DATE)

        resp = client.post(
            "/api/v1/custom-fields/values/bulk",
            headers=_headers(user),
            json={
                "entity_type": "part",
                "entity_id": part.id,
                "values": {FIELD_KEY: "AMS 2770", "cert_date": 20261105},
            },
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        by_key = {r["field_key"]: r["status"] for r in resp.json()["results"]}
        assert by_key == {FIELD_KEY: "success", "cert_date": "error"}
        db_session.expire_all()
        assert [(r.value_text, r.value_date) for r in db_session.query(CustomFieldValue).all()] == [("AMS 2770", None)]
        assert len(_value_audits(db_session, "CREATE")) == 1

    def test_an_iso_string_still_round_trips(self, client: TestClient, db_session: Session):
        user = _make_user(db_session, company_id=COMPANY_A)
        part = _part(db_session, company_id=COMPANY_A)
        _definition(db_session, company_id=COMPANY_A, field_key="cert_date", field_type=FieldType.DATE)
        url = f"/api/v1/custom-fields/values/part/{part.id}"

        resp = client.post(url, headers=_headers(user), json={"field_key": "cert_date", "value": "2026-11-05"})
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert client.get(url, headers=_headers(user)).json()["fields"]["cert_date"].startswith("2026-11-05")


DEFINITION_RESOURCE_TYPE = "custom_field_definition"


def _definition_audits(db: Session, action: str) -> list:
    db.rollback()  # only COMMITTED audit rows survive this
    db.expire_all()
    return (
        db.query(AuditLog)
        .filter(AuditLog.resource_type == DEFINITION_RESOURCE_TYPE, AuditLog.action == action)
        .order_by(AuditLog.sequence_number)
        .all()
    )


class TestDefinitionWritesAreAudited:
    """Deactivating a definition hides every value under it; editing one changes how they read.

    Checked: removing any one of the three ``audit.log_*`` calls fails the matching assertion.
    """

    def test_create_update_and_deactivate_each_write_a_committed_row(self, client: TestClient, db_session: Session):
        manager = _make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
        admin = _make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)

        created = client.post(
            "/api/v1/custom-fields/definitions",
            headers=_headers(manager),
            json={
                "field_key": "coating",
                "display_name": "Coating",
                "entity_type": "part",
                "field_type": "select",
                "options": ["Zinc", "Black oxide"],
            },
        )
        assert created.status_code == status.HTTP_200_OK, created.text
        field_id = created.json()["id"]

        creates = _definition_audits(db_session, "CREATE")
        assert [(a.resource_id, a.resource_identifier, a.company_id, a.user_id) for a in creates] == [
            (field_id, "part:coating", COMPANY_A, manager.id)
        ]
        assert creates[0].new_values["options"] == ["Zinc", "Black oxide"]
        assert creates[0].new_values["company_id"] == COMPANY_A

        updated = client.put(
            f"/api/v1/custom-fields/definitions/{field_id}",
            headers=_headers(manager),
            json={"options": ["Zinc", "Black oxide", "Passivate"], "display_name": "Coating"},
        )
        assert updated.status_code == status.HTTP_200_OK, updated.text
        updates = _definition_audits(db_session, "UPDATE")
        assert len(updates) == 1
        assert updates[0].user_id == manager.id
        # display_name was supplied unchanged; only the real change is in ``changes``.
        assert set(updates[0].extra_data["changes"]) == {"options"}
        assert updates[0].old_values["options"] == ["Zinc", "Black oxide"]
        assert updates[0].new_values["options"] == ["Zinc", "Black oxide", "Passivate"]

        deactivated = client.delete(f"/api/v1/custom-fields/definitions/{field_id}", headers=_headers(admin))
        assert deactivated.status_code == status.HTTP_200_OK, deactivated.text
        again = client.delete(f"/api/v1/custom-fields/definitions/{field_id}", headers=_headers(admin))
        assert again.status_code == status.HTTP_200_OK, again.text

        changes = _definition_audits(db_session, "STATUS_CHANGE")
        # The repeat deactivation changed nothing and recorded nothing.
        assert [(a.user_id, a.old_values, a.new_values) for a in changes] == [
            (admin.id, {"status": "active"}, {"status": "inactive"})
        ]
        assert (
            db_session.query(CustomFieldDefinition).filter(CustomFieldDefinition.id == field_id).one().is_active
            is False
        )

    def test_wrong_tenant_definition_writes_are_404_with_no_audit(self, client: TestClient, db_session: Session):
        admin_a = _make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)
        theirs = _definition(db_session, company_id=COMPANY_B)

        before = _counts(db_session)
        put = client.put(
            f"/api/v1/custom-fields/definitions/{theirs.id}", headers=_headers(admin_a), json={"display_name": "Mine"}
        )
        assert put.status_code == status.HTTP_404_NOT_FOUND
        delete = client.delete(f"/api/v1/custom-fields/definitions/{theirs.id}", headers=_headers(admin_a))
        assert delete.status_code == status.HTTP_404_NOT_FOUND
        assert _counts(db_session) == before
        row = db_session.query(CustomFieldDefinition).filter(CustomFieldDefinition.id == theirs.id).one()
        assert (row.display_name, row.is_active) == ("Heat Treat Spec", True)


def test_entity_read_loads_definitions_from_the_join_not_per_row(client: TestClient, db_session: Session):
    """``entity_values`` joins the definition already; ``contains_eager`` makes the GET use it.

    Checked: without ``contains_eager`` this GET lazy-loads each value row's definition with its
    own ``custom_field_definitions`` SELECT, and the assertion below fails.
    """
    user = _make_user(db_session, company_id=COMPANY_A)
    part = _part(db_session, company_id=COMPANY_A)
    for key in ("finish", "coating", "heat_lot"):
        field = _definition(db_session, company_id=COMPANY_A, field_key=key)
        _value(db_session, company_id=COMPANY_A, field=field, part=part, text=key.upper())
    headers = _headers(user)
    part_id = part.id
    db_session.expunge_all()  # nothing left in the identity map to satisfy a lazy load silently

    statements: list = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()))

    engine = db_session.get_bind()
    sa.event.listen(engine, "before_cursor_execute", _record)
    try:
        resp = client.get(f"/api/v1/custom-fields/values/part/{part_id}", headers=headers)
    finally:
        sa.event.remove(engine, "before_cursor_execute", _record)

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["fields"] == {"finish": "FINISH", "coating": "COATING", "heat_lot": "HEAT_LOT"}
    lazy_loads = [s for s in statements if "FROM custom_field_definitions WHERE" in s]
    assert lazy_loads == []
