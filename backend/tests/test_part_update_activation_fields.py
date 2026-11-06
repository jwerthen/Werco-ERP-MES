"""``PartUpdate`` carries neither ``is_active`` nor ``status`` -- the guard.

``POST /parts/{id}/deactivate`` and ``/activate`` are meant to be the ONLY non-delete
writers of ``parts.is_active`` / ``parts.status`` (reasoned, audited, Admin/Manager, and
refusing a soft-deleted part). Their docstrings and CLAUDE.md said so, but ``PartUpdate``
still declared both fields, so the blind ``setattr`` loops behind ``PUT /parts/{id}`` and
``PUT /materials/{id}`` -- Supervisor tier, lookups that deliberately do not filter
``is_deleted`` -- would write them. ``is_active`` doubles as the soft-delete mask, so that
was a way to un-retire a part, or clear a delete mask, through an ordinary form save
(invariant 3's ``Vendor`` trap).

Pinned two ways: the schema property itself, and the behavior of both doors when a client
sends the keys anyway (dropped -- the row is untouched and the audit diff never names them).
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.part import Part
from app.models.user import User, UserRole
from app.schemas.part import PartUpdate

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

FIXTURE_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _user(db: Session, role: UserRole = UserRole.SUPERVISOR) -> User:
    n = _next()
    user = User(
        email=f"part-update-{n}@werco.test",
        employee_id=f"PUPD-{n:05d}",
        first_name="Part",
        last_name="Editor",
        hashed_password=FIXTURE_PASSWORD_HASH,
        role=role,
        is_active=True,
        company_id=1,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _headers(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _part(db: Session, *, part_type: str, is_active: bool, status_value: str) -> Part:
    n = _next()
    part = Part(
        part_number=f"PUPD-{n:05d}",
        name="Activation guard part",
        part_type=part_type,
        unit_of_measure="each",
        is_active=is_active,
        status=status_value,
        company_id=1,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def test_part_update_schema_has_no_activation_fields():
    assert "is_active" not in PartUpdate.model_fields
    assert "status" not in PartUpdate.model_fields
    # And sending them anyway parses to nothing settable.
    parsed = PartUpdate.model_validate({"version": 0, "is_active": False, "status": "obsolete"})
    assert parsed.model_dump(exclude_unset=True) == {"version": 0}


@pytest.mark.parametrize(
    "url_prefix, part_type",
    [("/api/v1/parts", "manufactured"), ("/api/v1/materials", "raw_material")],
)
@pytest.mark.parametrize(
    "start_active, start_status, sent",
    [
        (True, "active", {"is_active": False, "status": "obsolete"}),  # a stealth "deactivate"
        (False, "obsolete", {"is_active": True, "status": "active"}),  # a stealth "activate"
    ],
)
def test_put_cannot_write_is_active_or_status(
    client: TestClient, db_session: Session, url_prefix, part_type, start_active, start_status, sent
):
    supervisor = _user(db_session)
    part = _part(db_session, part_type=part_type, is_active=start_active, status_value=start_status)

    response = client.put(
        f"{url_prefix}/{part.id}",
        headers=_headers(supervisor),
        json={"version": 0, "name": "Renamed by an ordinary save", **sent},
    )
    assert response.status_code == status.HTTP_200_OK, response.text

    db_session.rollback()
    db_session.expire_all()
    reloaded = db_session.get(Part, part.id)
    assert reloaded.name == "Renamed by an ordinary save", "the legitimate edit still lands"
    assert reloaded.is_active is start_active
    assert reloaded.status == start_status

    rows = db_session.query(AuditLog).filter(AuditLog.resource_type == "part", AuditLog.resource_id == part.id).all()
    assert rows, "the edit itself is still audited"
    for row in rows:
        changes = (row.extra_data or {}).get("changes", {})
        assert "is_active" not in changes and "status" not in changes


def test_put_cannot_clear_a_soft_delete_mask(client: TestClient, db_session: Session):
    """The security half: a soft-deleted part stays editable on this door (deliberately --
    see ``update_part``), so ``is_active`` here would have been a delete-mask eraser."""
    supervisor = _user(db_session)
    part = _part(db_session, part_type="manufactured", is_active=False, status_value="obsolete")
    part.soft_delete(supervisor.id)
    db_session.commit()

    response = client.put(
        f"/api/v1/parts/{part.id}",
        headers=_headers(supervisor),
        json={"version": 0, "is_active": True, "status": "active"},
    )
    assert response.status_code == status.HTTP_200_OK, response.text

    db_session.rollback()
    db_session.expire_all()
    reloaded = db_session.get(Part, part.id)
    assert reloaded.is_deleted is True
    assert reloaded.is_active is False
    assert reloaded.status == "obsolete"
