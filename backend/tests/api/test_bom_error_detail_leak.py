"""The three BOM handlers that used to answer a 500 with a Python traceback.

``get_bom``, ``unrelease_bom`` and ``add_bom_item`` each caught ``Exception`` and put
``traceback.format_exc()`` into the ``HTTPException`` detail. ``main.py``'s
``http_exception_handler`` relays ``exc.detail`` verbatim, so the response body carried
absolute filesystem paths, ORM internals and local frames to any authenticated caller —
and ``GET /bom/{id}`` requires no role at all.

Each handler gets three assertions, because a fix that dropped any one of them would be
worse than the leak:

1. **The body is generic.** Exact-match on the static message, plus a scan for the
   markers a leak would leave behind (the sentinel exception's own text included — the
   deleted code interpolated ``str(e)`` as well as the traceback).
2. **The traceback still reaches the log.** A silent ``except`` is not a fix, it is a
   worse bug: the whole argument for a generic body is that the detail moved to the log
   and Sentry rather than disappearing.
3. **The ``except HTTPException: raise`` arm still runs first.** 404/400 must not be
   swallowed into the new 500 — the failure mode of editing an except block.

``unrelease_bom`` additionally pins its ``db.rollback()``: it is the only one of the
three whose handler rolls back, it does so *before* raising, and nothing else in the
suite would notice it going missing.

The structural companion is ``tests/test_no_traceback_in_error_response_guard.py``,
which fails if any handler reintroduces the shape anywhere under ``app/``.
"""

import logging

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

BOM_LOGGER = "app.api.endpoints.bom"

# Distinctive enough that finding it anywhere in a response body proves the leak is back.
SENTINEL = "sentinel-3f9a2b-underlying-failure-do-not-leak"

# What a leaked traceback looks like on the wire. The exact-match assertion on the
# static detail is the real test; this list says what it is protecting against.
LEAK_MARKERS = (
    "Traceback",
    'File "',
    ".py",
    "site-packages",
    "sqlalchemy",
    SENTINEL,
)

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _boom(*args, **kwargs):
    """Stand-in for whatever blows up inside a handler's ``try``."""
    raise RuntimeError(SENTINEL)


# ---------------------------------------------------------------------------
# Fixtures (local, like every sibling BOM suite)
# ---------------------------------------------------------------------------


def make_user(db: Session, *, role: UserRole = UserRole.ADMIN, company_id: int = COMPANY_A) -> User:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True))
        db.commit()
    n = _next()
    user = User(
        email=f"leak-{n}@co{company_id}.test",
        employee_id=f"LEAK-{n:05d}",
        first_name="Trace",
        last_name="Back",
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


def make_part(db: Session, *, company_id: int = COMPANY_A) -> Part:
    n = _next()
    part = Part(
        part_number=f"LEAK-P-{n}",
        name=f"Part {n}",
        description="traceback-leak fixture part",
        part_type="manufactured",
        unit_of_measure="each",
        standard_cost=5.0,
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_bom(db: Session, part: Part, *, status_value: str = "draft", company_id: int = COMPANY_A) -> BOM:
    bom = BOM(part_id=part.id, revision="A", status=status_value, is_active=True, company_id=company_id)
    db.add(bom)
    db.commit()
    db.refresh(bom)
    return bom


def make_item(db: Session, bom: BOM, component: Part, *, company_id: int = COMPANY_A) -> BOMItem:
    item = BOMItem(
        bom_id=bom.id,
        component_part_id=component.id,
        item_number=10,
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


# ---------------------------------------------------------------------------
# Shared assertions
# ---------------------------------------------------------------------------


def assert_generic_detail(response, expected_detail: str) -> None:
    """The 500 body says only what we chose to say."""
    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR, response.text
    detail = response.json()["detail"]
    assert detail == expected_detail, detail

    body = response.text
    for marker in LEAK_MARKERS:
        assert marker not in body, f"response body leaks {marker!r}:\n{body}"


def assert_traceback_reached_the_log(caplog) -> None:
    """A generic body is only acceptable because the detail moved to the log."""
    records = [r for r in caplog.records if r.name == BOM_LOGGER and r.levelno >= logging.ERROR]
    assert records, f"nothing was logged to {BOM_LOGGER}; the failure would be invisible"
    assert any(r.exc_info for r in records), "the log record carries no exc_info -- use logger.exception()"
    assert "Traceback (most recent call last)" in caplog.text
    assert SENTINEL in caplog.text, "the underlying error text never reached the log"


# ---------------------------------------------------------------------------
# GET /bom/{bom_id}
# ---------------------------------------------------------------------------


def test_get_bom_500_returns_a_generic_detail_and_logs_the_traceback(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch, caplog
):
    user = make_user(db_session)
    part = make_part(db_session)
    component = make_part(db_session)
    bom = make_bom(db_session, part)
    make_item(db_session, bom, component)

    # Fails while building the response, i.e. after the 404 check -- so the generic
    # 500 arm is what answers, not the not-found arm.
    monkeypatch.setattr("app.api.endpoints.bom.build_bom_item_response", _boom)

    with caplog.at_level(logging.ERROR, logger=BOM_LOGGER):
        response = client.get(f"/api/v1/bom/{bom.id}", headers=headers_for(user))

    assert_generic_detail(response, "Error getting BOM")
    assert_traceback_reached_the_log(caplog)


def test_get_bom_still_404s_a_missing_bom(client: TestClient, db_session: Session):
    """The ``except HTTPException: raise`` arm survived the edit."""
    user = make_user(db_session)

    response = client.get("/api/v1/bom/999999", headers=headers_for(user))

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert response.json()["detail"] == "BOM not found"


# ---------------------------------------------------------------------------
# POST /bom/{bom_id}/unrelease
# ---------------------------------------------------------------------------


def test_unrelease_bom_500_returns_a_generic_detail_rolls_back_and_logs(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch, caplog
):
    user = make_user(db_session, role=UserRole.ADMIN)
    part = make_part(db_session)
    bom = make_bom(db_session, part, status_value="released")

    rollbacks = []
    real_rollback = db_session.rollback

    def spy_rollback():
        rollbacks.append(True)
        real_rollback()

    # Patched on the instance the get_db override hands the endpoint, so no other
    # session in the process is affected.
    monkeypatch.setattr(db_session, "rollback", spy_rollback)
    monkeypatch.setattr(db_session, "commit", _boom)

    with caplog.at_level(logging.ERROR, logger=BOM_LOGGER):
        response = client.post(f"/api/v1/bom/{bom.id}/unrelease", headers=headers_for(user))

    assert_generic_detail(response, "Error unreleasing BOM")
    assert_traceback_reached_the_log(caplog)
    assert rollbacks, "db.rollback() was dropped from the handler -- the session stays dirty on failure"


def test_unrelease_bom_still_400s_a_draft_bom(client: TestClient, db_session: Session):
    """The ``except HTTPException: raise`` arm survived the edit."""
    user = make_user(db_session, role=UserRole.ADMIN)
    part = make_part(db_session)
    bom = make_bom(db_session, part, status_value="draft")

    response = client.post(f"/api/v1/bom/{bom.id}/unrelease", headers=headers_for(user))

    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
    assert response.json()["detail"] == "BOM is not released"


# ---------------------------------------------------------------------------
# POST /bom/{bom_id}/items
# ---------------------------------------------------------------------------


def _add_item_body(component: Part) -> dict:
    return {
        "component_part_id": component.id,
        "item_number": 10,
        "quantity": 2,
        "item_type": "buy",
    }


def test_add_bom_item_500_returns_a_generic_detail_and_logs_the_traceback(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch, caplog
):
    user = make_user(db_session, role=UserRole.ADMIN)
    part = make_part(db_session)
    component = make_part(db_session)
    bom = make_bom(db_session, part)

    # Fails after the 404/400 validation arms, before anything is written.
    monkeypatch.setattr("app.api.endpoints.bom.would_create_circular_reference", _boom)

    with caplog.at_level(logging.ERROR, logger=BOM_LOGGER):
        response = client.post(f"/api/v1/bom/{bom.id}/items", headers=headers_for(user), json=_add_item_body(component))

    assert_generic_detail(response, "Error adding BOM item")
    assert_traceback_reached_the_log(caplog)


def test_add_bom_item_still_404s_an_unknown_component(client: TestClient, db_session: Session):
    """The ``except HTTPException: raise`` arm survived the edit."""
    user = make_user(db_session, role=UserRole.ADMIN)
    part = make_part(db_session)
    bom = make_bom(db_session, part)

    response = client.post(
        f"/api/v1/bom/{bom.id}/items",
        headers=headers_for(user),
        json={"component_part_id": 999999, "item_number": 10, "quantity": 2, "item_type": "buy"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert response.json()["detail"] == "Component part not found"
