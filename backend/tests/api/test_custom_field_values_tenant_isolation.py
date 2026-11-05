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

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.custom_field import CustomFieldDefinition, CustomFieldValue, EntityType, FieldType
from app.models.part import Part
from app.models.user import User, UserRole
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
    db: Session, *, company_id: int, field_key: str = FIELD_KEY, field_type: FieldType = FieldType.TEXT
) -> CustomFieldDefinition:
    _ensure_company(db, company_id)
    field = CustomFieldDefinition(
        field_key=field_key,
        display_name=field_key.replace("_", " ").title(),
        entity_type=EntityType.PART,
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
