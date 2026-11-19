"""Purchase-order quality flow-down (AS9100D 8.4.3, migration 128).

A purchase order prints a supplier quality requirements block: the company's flow-down profile
(heading, document reference, standard paragraph) plus the clauses the PO selected from the
company's catalog. The rules pinned here:

* **Profile.** ``GET /purchasing/flowdown-profile`` answers a default unsaved shape when the
  company has no row. ``PUT`` is Admin / Manager / Quality, a version compare-and-swap (409 on a
  stale version, nothing changed), creates the row on the first save, writes a REQUIRED audit row
  (lost -> 503 with nothing saved), and a save that changes nothing writes nothing.
* **Clause catalog.** Create / update / retire / reactivate, a duplicate code is 409 ignoring
  case, the code is immutable, there is no delete, ``include_inactive`` adds retired clauses.
* **Tenant isolation.** Another company's clause is a 404, its catalog never reaches this
  company's list, a PO's selection, or a PO's printed block (live or frozen).
* **The PO selection.** ``quality_clause_codes`` on create / update: unknown or retired codes
  are 422 naming them, duplicates dropped in first-seen order, NULL kept when omitted, a change
  on a non-DRAFT PO is 409, an unchanged re-send on a non-DRAFT PO is accepted. "Unchanged" is
  set equality: the same codes in another order change nothing; NULL and a list always differ.
  ``POResponse`` reports the raw selection, ``flowdown_frozen`` and, once issued, the frozen
  ``flowdown_snapshot`` (null on a draft, even one carrying a stale snapshot from an earlier issue).
* **The freeze.** A DRAFT prints the LIVE block. Every way out of DRAFT -- the status ``PUT``, the
  send verb, an accepted document delivery, and a PO inserted already issued (PO upload, the
  open-PO import, the reviewed-import command) -- freezes the block onto the PO, after which no
  catalog or profile edit changes its print data. A disabled profile freezes ``{"enabled":
  false}``. An issued PO with a NULL snapshot (issued before 128) prints no block and is never
  back-filled. Moving back to DRAFT prints live again and re-freezes on the next issue. The
  status-change audit row of a PO leaving DRAFT through Send, an accepted delivery or the
  reviewed-import command carries the frozen block's SHA-256 (``flowdown_snapshot_sha256``).
* **The server PDF** carries the heading, ``Reference: ...``, the body paragraphs and one line
  per clause, with every value escaped (``<REF>`` appears literally, nothing raises).

Every clause code and text here is invented.
"""

import hashlib
import json
import re
from io import BytesIO
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.part import Part
from app.models.po_flowdown import DEFAULT_FLOWDOWN_HEADING, POFlowdownProfile, POQualityClause
from app.models.purchasing import POStatus, PurchaseOrder, Vendor
from app.models.user import User, UserRole
from app.services import po_flowdown_service as flowdown
from app.services.audit_service import AuditService

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

BASE = "/api/v1/purchasing"
PROFILE_URL = f"{BASE}/flowdown-profile"
CLAUSES_URL = f"{BASE}/quality-clauses"
PO_URL = f"{BASE}/purchase-orders"
FIXTURE_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens are minted directly; never used to log in

COMPANY_A = 1  # seeded by the db_session fixture
COMPANY_B = 2

PROFILE = {
    "heading": "Supplier Quality Terms (test)",
    "document_reference": "SQR-TEST Rev B",
    "body": "First test paragraph.\n\nSecond test paragraph.",
}

# Print order is sort_order, then code: QX2 / QX3 (10), then QX1 (20). QX9 is retired, so it never
# prints even though it is a default.
CLAUSES = [
    {
        "code": "QX1",
        "title": "Alpha requirement",
        "body": "Alpha clause text.",
        "default_selected": True,
        "sort_order": 20,
    },
    {
        "code": "QX2",
        "title": "Bravo requirement",
        "body": "Bravo clause text.",
        "default_selected": False,
        "sort_order": 10,
    },
    {"code": "QX3", "title": "Charlie requirement", "body": None, "default_selected": True, "sort_order": 10},
    {
        "code": "QX9",
        "title": "Retired requirement",
        "body": "Retired clause text.",
        "default_selected": True,
        "sort_order": 0,
        "is_active": False,
    },
]
CLAUSE_BY_CODE = {clause["code"]: clause for clause in CLAUSES}
# The active default-selected clauses in print order: what a PO with NULL codes prints.
DEFAULT_PRINT_ORDER = ("QX3", "QX1")

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


# ── fixtures-as-functions ────────────────────────────────────────────────────


def _ensure_company(db: Session, company_id: int) -> None:
    if db.get(Company, company_id) is None:
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True))
        db.commit()


def _user(db: Session, role: UserRole = UserRole.ADMIN, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"flowdown-{n}@co{company_id}.test",
        employee_id=f"FD-{n:05d}",
        first_name=role.value.title(),
        last_name=f"C{company_id}",
        hashed_password=FIXTURE_PASSWORD_HASH,
        role=role,
        is_active=True,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _headers(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _vendor(db: Session, company_id: int = COMPANY_A) -> Vendor:
    _ensure_company(db, company_id)
    n = _next()
    vendor = Vendor(
        code=f"VFD-{n:05d}",
        name=f"Flowdown Test Vendor {n}",
        email=f"vendor{n}@example.com",
        is_active=True,
        company_id=company_id,
    )
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return vendor


def _part(db: Session, company_id: int = COMPANY_A) -> Part:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"PFD-{n:05d}",
        name=f"Flowdown test part {n}",
        part_type="purchased",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def _seed_flowdown(
    db: Session,
    company_id: int = COMPANY_A,
    *,
    enabled: bool = True,
    profile: dict = PROFILE,
    clauses: list = CLAUSES,
) -> dict:
    """The company's profile and catalog, straight into the tables. Returns clauses by code."""
    _ensure_company(db, company_id)
    if profile is not None:
        db.add(POFlowdownProfile(company_id=company_id, enabled=enabled, version=1, **profile))
    rows = {}
    for values in clauses:
        clause = POQualityClause(company_id=company_id, **{"is_active": True, **values})
        db.add(clause)
        rows[values["code"]] = clause
    db.commit()
    return rows


def _block(*codes: str, **overrides) -> dict:
    """The block a PO prints for ``codes`` (already in print order) under PROFILE / CLAUSES."""
    titles = overrides.pop("titles", {})
    block = {
        "enabled": True,
        "heading": PROFILE["heading"],
        "document_reference": PROFILE["document_reference"],
        "body": PROFILE["body"],
        "clauses": [
            {
                "code": code,
                "title": titles.get(code, CLAUSE_BY_CODE[code]["title"]),
                "body": CLAUSE_BY_CODE[code]["body"],
            }
            for code in codes
        ],
    }
    block.update(overrides)
    return block


def _create_po(client: TestClient, headers: dict, vendor: Vendor, part: Part, **extra):
    payload = {
        "vendor_id": vendor.id,
        "lines": [{"part_id": part.id, "quantity_ordered": "2", "unit_price": "3.50"}],
        **extra,
    }
    return client.post(PO_URL, headers=headers, json=payload)


def _draft_po(client: TestClient, headers: dict, vendor: Vendor, part: Part, **extra) -> dict:
    response = _create_po(client, headers, vendor, part, **extra)
    assert response.status_code == 200, response.text
    return response.json()


def _print_block(client: TestClient, headers: dict, po_id: int):
    response = client.get(f"/api/v1/print/purchase-orders/{po_id}/print-data", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["quality_flowdown"]


def _set_status(client: TestClient, headers: dict, po_id: int, status: str):
    return client.put(f"{PO_URL}/{po_id}", headers=headers, json={"version": 0, "status": status})


def _send(client: TestClient, headers: dict, po_id: int) -> None:
    response = client.post(f"{PO_URL}/{po_id}/send", headers=headers)
    assert response.status_code == 200, response.text


def _stored(db: Session, po_id: int) -> PurchaseOrder:
    db.expire_all()
    return db.get(PurchaseOrder, po_id)


def _audit_rows(db: Session, resource_type: str, resource_id=None):
    db.expire_all()
    query = db.query(AuditLog).filter(AuditLog.resource_type == resource_type)
    if resource_id is not None:
        query = query.filter(AuditLog.resource_id == resource_id)
    return query.order_by(AuditLog.sequence_number).all()


def _rewrite_catalog_and_profile(db: Session, company_id: int = COMPANY_A) -> None:
    """Every edit Quality could make after a PO was issued: retitle, rewrite, retire, add, disable."""
    db.expire_all()
    for clause in db.query(POQualityClause).filter(POQualityClause.company_id == company_id).all():
        clause.title = f"{clause.title} (rewritten)"
        clause.body = "Rewritten after issue."
        clause.is_active = not clause.is_active
    db.add(
        POQualityClause(
            company_id=company_id, code="QX7", title="Added after issue", default_selected=True, sort_order=1
        )
    )
    profile = db.query(POFlowdownProfile).filter(POFlowdownProfile.company_id == company_id).first()
    if profile is not None:
        profile.heading = "Rewritten heading"
        profile.document_reference = "SQR-TEST Rev Z"
        profile.body = "Rewritten body."
        profile.enabled = not profile.enabled
    db.commit()


def _sha256(block: dict) -> str:
    """The digest of a frozen block, computed independently: canonical (sorted, compact) JSON."""
    return hashlib.sha256(json.dumps(block, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _issue_audit_rows(db: Session, po_id: int):
    return [row for row in _audit_rows(db, "purchase_order", po_id) if row.action == "STATUS_CHANGE"]


def _assert_frozen_and_immune(client: TestClient, db: Session, headers: dict, po_id: int, expected: dict) -> None:
    """The PO froze ``expected``, prints it, and keeps printing it after the catalog is rewritten."""
    row = _stored(db, po_id)
    assert row.status != POStatus.DRAFT
    assert row.flowdown_snapshot == expected
    assert _print_block(client, headers, po_id) == expected
    _rewrite_catalog_and_profile(db)
    assert _stored(db, po_id).flowdown_snapshot == expected
    assert _print_block(client, headers, po_id) == expected


@pytest.fixture
def fake_smtp(monkeypatch):
    """Document delivery without a mail server: the transport is an in-memory fake."""
    from app.services import document_delivery_service as delivery_module

    transport = AsyncMock()
    monkeypatch.setattr(delivery_module, "dispatch_document_email", transport)
    monkeypatch.setattr(delivery_module, "smtp_configured", lambda: True)
    return transport


# ── Profile ──────────────────────────────────────────────────────────────────


class TestFlowdownProfile:
    def test_get_answers_the_default_unsaved_shape_when_there_is_no_row(self, client: TestClient, db_session):
        operator = _user(db_session, UserRole.OPERATOR)

        response = client.get(PROFILE_URL, headers=_headers(operator))

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["id"] is None
        assert body["enabled"] is False
        assert body["heading"] == DEFAULT_FLOWDOWN_HEADING
        assert body["document_reference"] is None
        assert body["body"] is None
        assert body["version"] == 0
        assert db_session.query(POFlowdownProfile).count() == 0, "a read must never create the row"

    @pytest.mark.parametrize("role", list(UserRole), ids=lambda role: role.value)
    def test_every_role_can_read_it(self, client: TestClient, db_session, role):
        user = _user(db_session, role)
        assert client.get(PROFILE_URL, headers=_headers(user)).status_code == 200

    @pytest.mark.parametrize(
        "role, allowed",
        [
            (UserRole.ADMIN, True),
            (UserRole.MANAGER, True),
            (UserRole.QUALITY, True),
            (UserRole.SUPERVISOR, False),
            (UserRole.OPERATOR, False),
            (UserRole.SHIPPING, False),
            (UserRole.VIEWER, False),
        ],
        ids=lambda value: value.value if isinstance(value, UserRole) else str(value),
    )
    def test_only_admin_manager_and_quality_can_save_it(self, client: TestClient, db_session, role, allowed):
        user = _user(db_session, role)

        response = client.put(PROFILE_URL, headers=_headers(user), json={"enabled": True, **PROFILE, "version": 0})

        if allowed:
            assert response.status_code == 200, response.text
            assert db_session.query(POFlowdownProfile).filter_by(company_id=COMPANY_A).count() == 1
        else:
            assert response.status_code == 403, response.text
            assert db_session.query(POFlowdownProfile).count() == 0
            assert _audit_rows(db_session, flowdown.PROFILE_RESOURCE_TYPE) == []

    def test_first_save_creates_the_row_and_its_audit_record(self, client: TestClient, db_session):
        admin = _user(db_session)

        response = client.put(PROFILE_URL, headers=_headers(admin), json={"enabled": True, **PROFILE, "version": 0})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["id"] is not None
        assert body["version"] == 1
        assert body["enabled"] is True
        assert {key: body[key] for key in PROFILE} == PROFILE
        assert body["updated_by"] == admin.id
        row = db_session.query(POFlowdownProfile).filter_by(company_id=COMPANY_A).one()
        assert row.id == body["id"] and row.version == 1
        (audit,) = _audit_rows(db_session, flowdown.PROFILE_RESOURCE_TYPE)
        assert audit.action == "CREATE"
        assert audit.resource_id == row.id
        assert audit.company_id == COMPANY_A
        assert audit.user_id == admin.id
        assert audit.new_values["enabled"] is True
        assert audit.new_values["document_reference"] == PROFILE["document_reference"]

    def test_a_save_bumps_the_version_trims_text_and_audits_the_diff(self, client: TestClient, db_session):
        admin = _user(db_session)
        headers = _headers(admin)
        assert (
            client.put(PROFILE_URL, headers=headers, json={"enabled": True, **PROFILE, "version": 0}).status_code == 200
        )

        response = client.put(
            PROFILE_URL,
            headers=headers,
            json={
                "enabled": False,
                "heading": "  Revised heading  ",
                "document_reference": "   ",
                "body": "",
                "version": 1,
            },
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["version"] == 2
        assert body["enabled"] is False
        assert body["heading"] == "Revised heading"
        assert body["document_reference"] is None  # blank is stored as null
        assert body["body"] is None
        create, update = _audit_rows(db_session, flowdown.PROFILE_RESOURCE_TYPE)
        assert (create.action, update.action) == ("CREATE", "UPDATE")
        changes = update.extra_data["changes"]
        assert {"enabled", "heading", "document_reference", "body", "version"} <= set(changes)

    def test_a_stale_version_is_409_and_changes_nothing(self, client: TestClient, db_session):
        headers = _headers(_user(db_session))
        assert (
            client.put(PROFILE_URL, headers=headers, json={"enabled": True, **PROFILE, "version": 0}).status_code == 200
        )
        audit_before = len(_audit_rows(db_session, flowdown.PROFILE_RESOURCE_TYPE))

        stale = client.put(PROFILE_URL, headers=headers, json={"enabled": False, "heading": "Lost edit", "version": 0})

        assert stale.status_code == 409, stale.text
        assert stale.json()["detail"] == flowdown.PROFILE_STALE_DETAIL
        db_session.expire_all()
        row = db_session.query(POFlowdownProfile).filter_by(company_id=COMPANY_A).one()
        assert (row.enabled, row.heading, row.version) == (True, PROFILE["heading"], 1)
        assert len(_audit_rows(db_session, flowdown.PROFILE_RESOURCE_TYPE)) == audit_before

    def test_a_nonzero_version_before_the_first_save_is_409_and_creates_nothing(self, client: TestClient, db_session):
        headers = _headers(_user(db_session))

        response = client.put(PROFILE_URL, headers=headers, json={"enabled": True, **PROFILE, "version": 3})

        assert response.status_code == 409, response.text
        assert db_session.query(POFlowdownProfile).count() == 0
        assert _audit_rows(db_session, flowdown.PROFILE_RESOURCE_TYPE) == []

    def test_a_save_that_changes_nothing_writes_nothing(self, client: TestClient, db_session):
        headers = _headers(_user(db_session))
        first = client.put(PROFILE_URL, headers=headers, json={"enabled": True, **PROFILE, "version": 0}).json()

        again = client.put(PROFILE_URL, headers=headers, json={"enabled": True, **PROFILE, "version": 1})

        assert again.status_code == 200, again.text
        assert again.json()["version"] == 1, "a no-op save must not bump the version"
        assert again.json()["updated_at"] == first["updated_at"]
        assert len(_audit_rows(db_session, flowdown.PROFILE_RESOURCE_TYPE)) == 1

    def test_first_save_with_the_audit_record_lost_is_503_and_creates_no_row(
        self, client: TestClient, db_session, monkeypatch
    ):
        headers = _headers(_user(db_session))
        monkeypatch.setattr(AuditService, "log", lambda self, *args, **kwargs: None)

        response = client.put(PROFILE_URL, headers=headers, json={"enabled": True, **PROFILE, "version": 0})

        assert response.status_code == 503, response.text
        assert response.json()["detail"] == flowdown.PROFILE_AUDIT_UNAVAILABLE
        db_session.expire_all()
        assert db_session.query(POFlowdownProfile).count() == 0

    def test_an_update_with_the_audit_record_lost_is_503_and_leaves_the_row_alone(
        self, client: TestClient, db_session, monkeypatch
    ):
        headers = _headers(_user(db_session))
        assert (
            client.put(PROFILE_URL, headers=headers, json={"enabled": True, **PROFILE, "version": 0}).status_code == 200
        )
        monkeypatch.setattr(AuditService, "log", lambda self, *args, **kwargs: None)

        response = client.put(PROFILE_URL, headers=headers, json={"enabled": False, "heading": "Lost", "version": 1})

        assert response.status_code == 503, response.text
        assert response.json()["detail"] == flowdown.PROFILE_AUDIT_UNAVAILABLE
        monkeypatch.undo()
        stored = client.get(PROFILE_URL, headers=headers).json()
        assert (stored["enabled"], stored["heading"], stored["version"]) == (True, PROFILE["heading"], 1)


# ── Clause catalog ───────────────────────────────────────────────────────────


class TestClauseCatalog:
    def test_create_returns_the_clause_and_audits_it(self, client: TestClient, db_session):
        admin = _user(db_session)

        response = client.post(
            CLAUSES_URL,
            headers=_headers(admin),
            json={
                "code": "  QX1 ",
                "title": " Alpha requirement ",
                "body": "Alpha clause text.",
                "default_selected": True,
            },
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["code"] == "QX1"
        assert body["title"] == "Alpha requirement"
        assert body["body"] == "Alpha clause text."
        assert body["default_selected"] is True
        assert body["is_active"] is True
        assert body["sort_order"] == 0
        assert body["created_by"] == admin.id
        row = db_session.query(POQualityClause).filter_by(company_id=COMPANY_A, code="QX1").one()
        (audit,) = _audit_rows(db_session, flowdown.CLAUSE_RESOURCE_TYPE)
        assert (audit.action, audit.resource_id, audit.resource_identifier) == ("CREATE", row.id, "QX1")

    @pytest.mark.parametrize("duplicate", ["QX1", "qx1", "Qx1", "  qx1  "])
    def test_a_duplicate_code_is_409_ignoring_case(self, client: TestClient, db_session, duplicate):
        headers = _headers(_user(db_session))
        assert client.post(CLAUSES_URL, headers=headers, json={"code": "QX1", "title": "Alpha"}).status_code == 200

        response = client.post(CLAUSES_URL, headers=headers, json={"code": duplicate, "title": "Copy"})

        assert response.status_code == 409, response.text
        assert "already exists" in response.json()["detail"]
        assert db_session.query(POQualityClause).count() == 1
        assert len(_audit_rows(db_session, flowdown.CLAUSE_RESOURCE_TYPE)) == 1

    def test_update_changes_only_the_fields_sent_and_audits_them(self, client: TestClient, db_session):
        headers = _headers(_user(db_session))
        created = client.post(
            CLAUSES_URL, headers=headers, json={"code": "QX1", "title": "Alpha", "body": "Old text", "sort_order": 5}
        ).json()

        # Echoing the stored code back is accepted.
        response = client.put(
            f"{CLAUSES_URL}/{created['id']}",
            headers=headers,
            json={"code": "QX1", "title": "Alpha revised", "default_selected": True},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["code"], body["title"], body["default_selected"]) == ("QX1", "Alpha revised", True)
        assert (body["body"], body["sort_order"], body["is_active"]) == ("Old text", 5, True)
        _, update = _audit_rows(db_session, flowdown.CLAUSE_RESOURCE_TYPE)
        assert update.action == "UPDATE"
        assert set(update.extra_data["changes"]) == {"title", "default_selected"}

    def test_an_update_that_changes_nothing_writes_no_audit_record(self, client: TestClient, db_session):
        headers = _headers(_user(db_session))
        created = client.post(CLAUSES_URL, headers=headers, json={"code": "QX1", "title": "Alpha"}).json()

        response = client.put(f"{CLAUSES_URL}/{created['id']}", headers=headers, json={"title": "Alpha"})

        assert response.status_code == 200, response.text
        assert len(_audit_rows(db_session, flowdown.CLAUSE_RESOURCE_TYPE)) == 1

    def test_the_code_is_immutable(self, client: TestClient, db_session):
        headers = _headers(_user(db_session))
        created = client.post(CLAUSES_URL, headers=headers, json={"code": "QX1", "title": "Alpha"}).json()

        response = client.put(f"{CLAUSES_URL}/{created['id']}", headers=headers, json={"code": "QX2", "title": "Moved"})

        assert response.status_code == 409, response.text
        assert response.json()["detail"] == flowdown.CLAUSE_CODE_IMMUTABLE_DETAIL
        db_session.expire_all()
        row = db_session.get(POQualityClause, created["id"])
        assert (row.code, row.title) == ("QX1", "Alpha")
        assert len(_audit_rows(db_session, flowdown.CLAUSE_RESOURCE_TYPE)) == 1

    def test_an_explicit_null_on_a_required_field_is_422(self, client: TestClient, db_session):
        headers = _headers(_user(db_session))
        created = client.post(CLAUSES_URL, headers=headers, json={"code": "QX1", "title": "Alpha"}).json()

        response = client.put(f"{CLAUSES_URL}/{created['id']}", headers=headers, json={"title": None})

        assert response.status_code == 422, response.text

    def test_there_is_no_delete(self, client: TestClient, db_session):
        headers = _headers(_user(db_session))
        created = client.post(CLAUSES_URL, headers=headers, json={"code": "QX1", "title": "Alpha"}).json()

        response = client.delete(f"{CLAUSES_URL}/{created['id']}", headers=headers)

        assert response.status_code == 405, response.text
        assert db_session.query(POQualityClause).count() == 1

    def test_retire_hides_it_from_the_default_list_and_reactivate_brings_it_back(self, client: TestClient, db_session):
        headers = _headers(_user(db_session))
        _seed_flowdown(db_session)
        qx1 = db_session.query(POQualityClause).filter_by(code="QX1").one()

        def codes(include_inactive=None):
            params = {} if include_inactive is None else {"include_inactive": include_inactive}
            response = client.get(CLAUSES_URL, headers=headers, params=params)
            assert response.status_code == 200, response.text
            return [clause["code"] for clause in response.json()]

        # Print order: sort_order, then code. QX9 is retired.
        assert codes() == ["QX2", "QX3", "QX1"]
        assert codes(include_inactive=True) == ["QX9", "QX2", "QX3", "QX1"]

        retired = client.put(f"{CLAUSES_URL}/{qx1.id}", headers=headers, json={"is_active": False})
        assert retired.status_code == 200, retired.text
        assert retired.json()["is_active"] is False
        assert codes() == ["QX2", "QX3"]
        assert codes(include_inactive=True) == ["QX9", "QX2", "QX3", "QX1"]

        reactivated = client.put(f"{CLAUSES_URL}/{qx1.id}", headers=headers, json={"is_active": True})
        assert reactivated.status_code == 200, reactivated.text
        assert codes() == ["QX2", "QX3", "QX1"]
        assert db_session.query(POQualityClause).count() == len(CLAUSES), "retire never deletes"

    @pytest.mark.parametrize(
        "role, allowed",
        [
            (UserRole.ADMIN, True),
            (UserRole.MANAGER, True),
            (UserRole.QUALITY, True),
            (UserRole.SUPERVISOR, False),
            (UserRole.OPERATOR, False),
            (UserRole.VIEWER, False),
        ],
        ids=lambda value: value.value if isinstance(value, UserRole) else str(value),
    )
    def test_writes_are_admin_manager_and_quality_only(self, client: TestClient, db_session, role, allowed):
        _seed_flowdown(db_session)
        qx1 = db_session.query(POQualityClause).filter_by(code="QX1").one()
        headers = _headers(_user(db_session, role))

        created = client.post(CLAUSES_URL, headers=headers, json={"code": "QX5", "title": "Echo"})
        updated = client.put(f"{CLAUSES_URL}/{qx1.id}", headers=headers, json={"title": "Alpha by role"})
        listed = client.get(CLAUSES_URL, headers=headers)

        assert listed.status_code == 200, "every role reads the catalog"
        if allowed:
            assert (created.status_code, updated.status_code) == (200, 200), (created.text, updated.text)
        else:
            assert (created.status_code, updated.status_code) == (403, 403), (created.text, updated.text)
            db_session.expire_all()
            assert db_session.query(POQualityClause).filter_by(code="QX5").count() == 0
            assert db_session.get(POQualityClause, qx1.id).title == "Alpha requirement"
            assert _audit_rows(db_session, flowdown.CLAUSE_RESOURCE_TYPE) == []

    def test_create_with_the_audit_record_lost_is_503_and_saves_nothing(
        self, client: TestClient, db_session, monkeypatch
    ):
        headers = _headers(_user(db_session))
        monkeypatch.setattr(AuditService, "log", lambda self, *args, **kwargs: None)

        response = client.post(CLAUSES_URL, headers=headers, json={"code": "QX5", "title": "Lost"})

        assert response.status_code == 503, response.text
        assert response.json()["detail"] == flowdown.CLAUSE_AUDIT_UNAVAILABLE
        db_session.expire_all()
        assert db_session.query(POQualityClause).count() == 0

    def test_update_with_the_audit_record_lost_is_503_and_changes_nothing(
        self, client: TestClient, db_session, monkeypatch
    ):
        headers = _headers(_user(db_session))
        _seed_flowdown(db_session)
        qx1 = db_session.query(POQualityClause).filter_by(code="QX1").one()
        monkeypatch.setattr(AuditService, "log", lambda self, *args, **kwargs: None)

        response = client.put(f"{CLAUSES_URL}/{qx1.id}", headers=headers, json={"title": "Lost", "is_active": False})

        assert response.status_code == 503, response.text
        db_session.expire_all()
        row = db_session.get(POQualityClause, qx1.id)
        assert (row.title, row.is_active) == ("Alpha requirement", True)


# ── Tenant isolation ─────────────────────────────────────────────────────────


class TestTenantIsolation:
    def test_another_companys_clause_is_404_and_untouched(self, client: TestClient, db_session):
        _seed_flowdown(db_session, COMPANY_B)
        foreign = db_session.query(POQualityClause).filter_by(company_id=COMPANY_B, code="QX1").one()
        headers = _headers(_user(db_session, UserRole.ADMIN, COMPANY_A))

        response = client.put(f"{CLAUSES_URL}/{foreign.id}", headers=headers, json={"title": "Hijacked", "code": "QX1"})

        assert response.status_code == 404, response.text
        db_session.expire_all()
        assert db_session.get(POQualityClause, foreign.id).title == "Alpha requirement"
        assert _audit_rows(db_session, flowdown.CLAUSE_RESOURCE_TYPE) == []

    def test_the_catalog_list_is_this_companys_only(self, client: TestClient, db_session):
        _seed_flowdown(db_session, COMPANY_B, clauses=[{"code": "QB7", "title": "Company B only", "sort_order": 0}])
        _seed_flowdown(
            db_session, COMPANY_A, profile=None, clauses=[{"code": "QX1", "title": "Alpha", "sort_order": 0}]
        )
        headers = _headers(_user(db_session, UserRole.OPERATOR, COMPANY_A))

        for params in ({}, {"include_inactive": True}):
            response = client.get(CLAUSES_URL, headers=headers, params=params)
            assert response.status_code == 200, response.text
            assert [clause["code"] for clause in response.json()] == ["QX1"]

    def test_the_same_code_may_exist_in_two_companies(self, client: TestClient, db_session):
        _seed_flowdown(db_session, COMPANY_B)
        headers = _headers(_user(db_session, UserRole.ADMIN, COMPANY_A))

        response = client.post(CLAUSES_URL, headers=headers, json={"code": "qx1", "title": "Company A's own"})

        assert response.status_code == 200, response.text

    def test_the_profile_is_this_companys_only(self, client: TestClient, db_session):
        _seed_flowdown(db_session, COMPANY_B, clauses=[])
        db_session.query(POFlowdownProfile).filter_by(company_id=COMPANY_B).update({"version": 4})
        db_session.commit()
        headers = _headers(_user(db_session, UserRole.ADMIN, COMPANY_A))

        read = client.get(PROFILE_URL, headers=headers).json()
        assert (read["id"], read["enabled"], read["version"]) == (None, False, 0)

        # Company B's version is not company A's: A's first save is version 0 and makes A's own row.
        saved = client.put(
            PROFILE_URL, headers=headers, json={"enabled": True, "heading": "Company A terms", "version": 0}
        )
        assert saved.status_code == 200, saved.text
        db_session.expire_all()
        b_row = db_session.query(POFlowdownProfile).filter_by(company_id=COMPANY_B).one()
        assert (b_row.heading, b_row.version) == (PROFILE["heading"], 4)
        assert db_session.query(POFlowdownProfile).filter_by(company_id=COMPANY_A).one().heading == "Company A terms"

    def test_another_companys_code_is_unknown_on_a_po(self, client: TestClient, db_session):
        _seed_flowdown(db_session, COMPANY_B, clauses=[{"code": "QB7", "title": "Company B only"}])
        _seed_flowdown(db_session, COMPANY_A)
        headers = _headers(_user(db_session, UserRole.ADMIN, COMPANY_A))

        response = _create_po(client, headers, _vendor(db_session), _part(db_session), quality_clause_codes=["QB7"])

        assert response.status_code == 422, response.text
        assert "QB7" in response.json()["detail"]

    def test_another_companys_catalog_never_reaches_a_po_block(self, client: TestClient, db_session):
        # Company B: same codes, different words, and a default clause company A does not have.
        _seed_flowdown(
            db_session,
            COMPANY_B,
            profile={"heading": "Company B heading", "document_reference": "B-DOC", "body": "Company B body"},
            clauses=[
                {"code": "QX1", "title": "Company B wording", "default_selected": True},
                {"code": "QB7", "title": "Company B only", "default_selected": True},
            ],
        )
        _seed_flowdown(db_session, COMPANY_A)
        headers = _headers(_user(db_session, UserRole.ADMIN, COMPANY_A))
        vendor, part = _vendor(db_session), _part(db_session)

        draft = _draft_po(client, headers, vendor, part)
        assert _print_block(client, headers, draft["id"]) == _block(*DEFAULT_PRINT_ORDER)
        _send(client, headers, draft["id"])
        assert _stored(db_session, draft["id"]).flowdown_snapshot == _block(*DEFAULT_PRINT_ORDER)

    def test_a_company_with_no_profile_prints_and_freezes_nothing_while_another_has_one(
        self, client: TestClient, db_session
    ):
        _seed_flowdown(db_session, COMPANY_B)
        headers = _headers(_user(db_session, UserRole.ADMIN, COMPANY_A))

        draft = _draft_po(client, headers, _vendor(db_session), _part(db_session))
        assert _print_block(client, headers, draft["id"]) is None
        _send(client, headers, draft["id"])
        assert _stored(db_session, draft["id"]).flowdown_snapshot == {"enabled": False}


# ── The PO's clause selection ────────────────────────────────────────────────


class TestPurchaseOrderClauseSelection:
    @pytest.fixture
    def setup(self, client: TestClient, db_session):
        _seed_flowdown(db_session)
        admin = _user(db_session)
        return _headers(admin), _vendor(db_session), _part(db_session)

    def test_omitted_codes_store_null_and_print_the_defaults(self, client: TestClient, db_session, setup):
        headers, vendor, part = setup

        po = _draft_po(client, headers, vendor, part)

        assert po["quality_clause_codes"] is None
        assert po["flowdown_frozen"] is False
        assert _stored(db_session, po["id"]).quality_clause_codes is None
        assert _stored(db_session, po["id"]).flowdown_snapshot is None, "a draft never carries a snapshot"
        assert _print_block(client, headers, po["id"]) == _block(*DEFAULT_PRINT_ORDER)

    def test_codes_are_deduplicated_in_first_seen_order_and_print_in_catalog_order(
        self, client: TestClient, db_session, setup
    ):
        headers, vendor, part = setup

        po = _draft_po(client, headers, vendor, part, quality_clause_codes=["QX1", " QX2", "QX1", "QX2 "])

        assert po["quality_clause_codes"] == ["QX1", "QX2"]
        assert _stored(db_session, po["id"]).quality_clause_codes == ["QX1", "QX2"]
        # Printed in the catalog's order (sort_order 10 before 20), not the selection's.
        assert _print_block(client, headers, po["id"]) == _block("QX2", "QX1")

    def test_an_empty_list_is_kept_and_prints_the_block_with_no_clauses(self, client: TestClient, db_session, setup):
        headers, vendor, part = setup

        po = _draft_po(client, headers, vendor, part, quality_clause_codes=[])

        assert po["quality_clause_codes"] == []
        assert _print_block(client, headers, po["id"]) == _block()

    def test_unknown_and_retired_codes_are_422_naming_them_and_create_nothing(
        self, client: TestClient, db_session, setup
    ):
        headers, vendor, part = setup
        before = db_session.query(PurchaseOrder).count()

        response = _create_po(client, headers, vendor, part, quality_clause_codes=["QX1", "NOPE-1", "QX9"])

        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert "NOPE-1" in detail and "QX9" in detail
        assert "QX1," not in detail and not detail.endswith("QX1")
        assert db_session.query(PurchaseOrder).count() == before

    def test_a_draft_can_change_its_codes_and_the_po_update_audit_records_it(
        self, client: TestClient, db_session, setup
    ):
        headers, vendor, part = setup
        po = _draft_po(client, headers, vendor, part)

        response = client.put(
            f"{PO_URL}/{po['id']}", headers=headers, json={"version": 0, "quality_clause_codes": ["QX2", "QX2", "QX3"]}
        )

        assert response.status_code == 200, response.text
        assert response.json()["quality_clause_codes"] == ["QX2", "QX3"]
        assert _print_block(client, headers, po["id"]) == _block("QX2", "QX3")
        updates = [row for row in _audit_rows(db_session, "purchase_order", po["id"]) if row.action == "UPDATE"]
        assert any("quality_clause_codes" in (row.extra_data or {}).get("changes", {}) for row in updates)

    def test_an_unknown_code_on_update_is_422_and_changes_nothing(self, client: TestClient, db_session, setup):
        headers, vendor, part = setup
        po = _draft_po(client, headers, vendor, part, quality_clause_codes=["QX1"])

        response = client.put(
            f"{PO_URL}/{po['id']}",
            headers=headers,
            json={"version": 0, "notes": "should not land", "quality_clause_codes": ["QX2", "NOPE-2"]},
        )

        assert response.status_code == 422, response.text
        assert "NOPE-2" in response.json()["detail"]
        row = _stored(db_session, po["id"])
        assert row.quality_clause_codes == ["QX1"]
        assert row.notes is None

    def test_an_update_that_omits_the_codes_keeps_them(self, client: TestClient, db_session, setup):
        headers, vendor, part = setup
        po = _draft_po(client, headers, vendor, part, quality_clause_codes=["QX2"])

        response = client.put(f"{PO_URL}/{po['id']}", headers=headers, json={"version": 0, "notes": "unrelated edit"})

        assert response.status_code == 200, response.text
        assert response.json()["quality_clause_codes"] == ["QX2"]

    def test_echoing_a_since_retired_code_back_is_not_a_change(self, client: TestClient, db_session, setup):
        headers, vendor, part = setup
        po = _draft_po(client, headers, vendor, part, quality_clause_codes=["QX1"])
        db_session.query(POQualityClause).filter_by(code="QX1").update({"is_active": False})
        db_session.commit()

        response = client.put(
            f"{PO_URL}/{po['id']}", headers=headers, json={"version": 0, "notes": "x", "quality_clause_codes": ["QX1"]}
        )

        assert response.status_code == 200, response.text
        assert response.json()["quality_clause_codes"] == ["QX1"]

    def test_a_change_on_an_issued_po_is_409_and_changes_nothing(self, client: TestClient, db_session, setup):
        headers, vendor, part = setup
        po = _draft_po(client, headers, vendor, part, quality_clause_codes=["QX1"])
        _send(client, headers, po["id"])

        response = client.put(
            f"{PO_URL}/{po['id']}", headers=headers, json={"version": 0, "notes": "x", "quality_clause_codes": ["QX2"]}
        )

        assert response.status_code == 409, response.text
        assert response.json()["detail"] == flowdown.flowdown_frozen_detail(po["po_number"])
        row = _stored(db_session, po["id"])
        assert row.quality_clause_codes == ["QX1"]
        assert row.notes is None
        assert row.flowdown_snapshot == _block("QX1")

    @pytest.mark.parametrize("stored, resent", [(["QX1", "QX2"], ["QX1", "QX2"]), (None, None)])
    def test_an_unchanged_resend_on_an_issued_po_is_accepted(
        self, client: TestClient, db_session, setup, stored, resent
    ):
        headers, vendor, part = setup
        extra = {} if stored is None else {"quality_clause_codes": stored}
        po = _draft_po(client, headers, vendor, part, **extra)
        _send(client, headers, po["id"])

        response = client.put(
            f"{PO_URL}/{po['id']}",
            headers=headers,
            json={"version": 0, "notes": "after issue", "quality_clause_codes": resent},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["quality_clause_codes"] == stored
        assert body["flowdown_frozen"] is True
        assert body["notes"] == "after issue"

    def test_get_reports_the_raw_selection_and_whether_it_is_frozen(self, client: TestClient, db_session, setup):
        headers, vendor, part = setup
        po = _draft_po(client, headers, vendor, part, quality_clause_codes=["QX3"])

        before = client.get(f"{PO_URL}/{po['id']}", headers=headers).json()
        _send(client, headers, po["id"])
        after = client.get(f"{PO_URL}/{po['id']}", headers=headers).json()

        assert (before["quality_clause_codes"], before["flowdown_frozen"]) == (["QX3"], False)
        assert before["flowdown_snapshot"] is None
        assert (after["quality_clause_codes"], after["flowdown_frozen"]) == (["QX3"], True)
        assert after["flowdown_snapshot"] == _block("QX3")

    def test_the_frozen_snapshot_can_differ_from_the_stored_codes(self, client: TestClient, db_session, setup):
        """A clause retired before issue is dropped at the freeze; the response says what was sent."""
        headers, vendor, part = setup
        po = _draft_po(client, headers, vendor, part, quality_clause_codes=["QX1", "QX2"])
        db_session.query(POQualityClause).filter_by(code="QX2").update({"is_active": False})
        db_session.commit()

        _send(client, headers, po["id"])

        body = client.get(f"{PO_URL}/{po['id']}", headers=headers).json()
        assert body["quality_clause_codes"] == ["QX1", "QX2"]
        assert body["flowdown_snapshot"] == _block("QX1")

    def test_the_same_codes_in_another_order_are_not_a_change(self, client: TestClient, db_session, setup):
        headers, vendor, part = setup
        issued = _draft_po(client, headers, vendor, part, quality_clause_codes=["QX1", "QX2"])
        draft = _draft_po(client, headers, vendor, part, quality_clause_codes=["QX1", "QX2"])
        _send(client, headers, issued["id"])

        for po in (issued, draft):
            response = client.put(
                f"{PO_URL}/{po['id']}",
                headers=headers,
                json={"version": 0, "quality_clause_codes": ["QX2", " QX1", "QX2"]},
            )
            assert response.status_code == 200, response.text
            assert response.json()["quality_clause_codes"] == ["QX1", "QX2"], "the stored list is left as it was"

    @pytest.mark.parametrize("stored, resent", [(None, []), ([], None)])
    def test_null_and_an_empty_list_are_different_selections(
        self, client: TestClient, db_session, setup, stored, resent
    ):
        headers, vendor, part = setup
        extra = {} if stored is None else {"quality_clause_codes": stored}
        po = _draft_po(client, headers, vendor, part, **extra)
        _send(client, headers, po["id"])

        response = client.put(
            f"{PO_URL}/{po['id']}", headers=headers, json={"version": 0, "quality_clause_codes": resent}
        )

        assert response.status_code == 409, response.text
        assert _stored(db_session, po["id"]).quality_clause_codes == stored


# ── The freeze ───────────────────────────────────────────────────────────────


class TestFreezeOnIssue:
    @pytest.fixture
    def setup(self, client: TestClient, db_session):
        _seed_flowdown(db_session)
        return _headers(_user(db_session)), _vendor(db_session), _part(db_session)

    def test_a_draft_prints_the_live_block(self, client: TestClient, db_session, setup):
        headers, vendor, part = setup
        po = _draft_po(client, headers, vendor, part)
        assert _print_block(client, headers, po["id"]) == _block(*DEFAULT_PRINT_ORDER)

        qx1 = db_session.query(POQualityClause).filter_by(code="QX1").one()
        assert client.put(f"{CLAUSES_URL}/{qx1.id}", headers=headers, json={"title": "Alpha live"}).status_code == 200
        assert _print_block(client, headers, po["id"]) == _block(*DEFAULT_PRINT_ORDER, titles={"QX1": "Alpha live"})

        assert client.put(PROFILE_URL, headers=headers, json={"enabled": False, "version": 1}).status_code == 200
        assert _print_block(client, headers, po["id"]) is None, "a disabled profile prints no block on a draft"
        assert _stored(db_session, po["id"]).flowdown_snapshot is None

    @pytest.mark.parametrize("status", ["pending_approval", "approved", "cancelled"])
    def test_the_status_put_out_of_draft_freezes(self, client: TestClient, db_session, setup, status):
        headers, vendor, part = setup
        po = _draft_po(client, headers, vendor, part, quality_clause_codes=["QX1", "QX2"])

        response = _set_status(client, headers, po["id"], status)

        assert response.status_code == 200, response.text
        assert response.json()["flowdown_frozen"] is True
        _assert_frozen_and_immune(client, db_session, headers, po["id"], _block("QX2", "QX1"))

    def test_the_send_verb_freezes(self, client: TestClient, db_session, setup):
        headers, vendor, part = setup
        po = _draft_po(client, headers, vendor, part)

        _send(client, headers, po["id"])

        (issue,) = _issue_audit_rows(db_session, po["id"])
        assert issue.extra_data["flowdown_snapshot_sha256"] == _sha256(_block(*DEFAULT_PRINT_ORDER))
        _assert_frozen_and_immune(client, db_session, headers, po["id"], _block(*DEFAULT_PRINT_ORDER))

    def test_a_disabled_block_is_digested_too(self, client: TestClient, db_session):
        _seed_flowdown(db_session, enabled=False)
        headers = _headers(_user(db_session))
        po = _draft_po(client, headers, _vendor(db_session), _part(db_session))

        _send(client, headers, po["id"])

        (issue,) = _issue_audit_rows(db_session, po["id"])
        assert issue.extra_data["flowdown_snapshot_sha256"] == _sha256({"enabled": False})
        assert issue.extra_data["flowdown_snapshot_sha256"] == flowdown.flowdown_snapshot_sha256({"enabled": False})

    def test_an_accepted_document_delivery_freezes(self, client: TestClient, db_session, setup, fake_smtp):
        headers, vendor, part = setup
        po = _draft_po(client, headers, vendor, part, quality_clause_codes=["QX2"])

        preview = client.post(
            "/api/v1/document-deliveries/preview",
            headers=headers,
            json={"entity_type": "purchase_order", "entity_id": po["id"]},
        )
        assert preview.status_code == 200, preview.text
        review = preview.json()
        attachment = client.get(f"/api/v1/document-deliveries/{review['id']}/attachment", headers=headers).content
        assert PROFILE["heading"] in _pdf_text(attachment), "the emailed draft PDF carries the live block"
        sent = client.post(
            f"/api/v1/document-deliveries/{review['id']}/send",
            headers=headers,
            json={
                "expected_version": review["version"],
                "request_key": "flowdown-delivery-01",
                "recipient": review["recipient"],
                "subject": review["subject"],
                "body": review["body"],
            },
        )

        assert sent.status_code == 200, sent.text
        assert fake_smtp.await_count == 1
        assert _stored(db_session, po["id"]).status == POStatus.SENT
        issue = [row for row in _issue_audit_rows(db_session, po["id"]) if (row.extra_data or {}).get("delivery_id")]
        assert len(issue) == 1
        assert issue[0].extra_data["flowdown_snapshot_sha256"] == _sha256(_block("QX2"))
        _assert_frozen_and_immune(client, db_session, headers, po["id"], _block("QX2"))

    def test_a_po_uploaded_straight_to_sent_freezes_at_birth(self, client: TestClient, db_session, setup):
        headers, vendor, part = setup

        response = client.post(
            "/api/v1/po-upload/create-from-upload",
            headers=headers,
            json={
                "po_number": "PO-FD-UPLOAD-1",
                "vendor_id": vendor.id,
                "line_items": [
                    {"part_id": part.id, "part_number": part.part_number, "quantity_ordered": 2, "unit_price": 3}
                ],
                "pdf_path": "",
            },
        )

        assert response.status_code == 200, response.text
        po_id = response.json()["po_id"]
        _assert_frozen_and_immune(client, db_session, headers, po_id, _block(*DEFAULT_PRINT_ORDER))

    def test_an_open_po_import_freezes_at_birth(self, client: TestClient, db_session, setup):
        headers, vendor, part = setup
        csv = f"po_number,vendor_code,part_number,quantity,unit_price\nPO-FD-IMPORT-1,{vendor.code},{part.part_number},5,2.00\n"

        response = client.post(
            f"{PO_URL}/import",
            headers=headers,
            files={"file": ("import.csv", BytesIO(csv.encode("utf-8")), "text/csv")},
        )

        assert response.status_code == 200, response.text
        assert response.json()["errors"] == []
        po = db_session.query(PurchaseOrder).filter_by(po_number="PO-FD-IMPORT-1", company_id=COMPANY_A).one()
        _assert_frozen_and_immune(client, db_session, headers, po.id, _block(*DEFAULT_PRINT_ORDER))

    def test_the_ready_for_receiving_command_freezes(self, client: TestClient, db_session, setup):
        from app.schemas.purchasing import POCreate
        from app.services.erp_draft_commands import create_purchase_order_command

        headers, vendor, part = setup
        admin = _user(db_session)
        po_in = POCreate(
            vendor_id=vendor.id,
            lines=[{"part_id": part.id, "quantity_ordered": "1", "unit_price": "1"}],
            quality_clause_codes=["QX3"],
        )

        po = create_purchase_order_command(
            db_session, po_in, admin, COMPANY_A, AuditService(db_session, admin), ready_for_receiving=True
        )
        db_session.commit()

        assert po.status == POStatus.SENT
        (issue,) = _issue_audit_rows(db_session, po.id)
        assert issue.extra_data["flowdown_snapshot_sha256"] == _sha256(_block("QX3"))
        _assert_frozen_and_immune(client, db_session, headers, po.id, _block("QX3"))

    @pytest.mark.parametrize("status", [POStatus.SENT, "sent", POStatus.APPROVED])
    def test_an_orm_insert_already_issued_freezes(self, client: TestClient, db_session, setup, status):
        headers, vendor, _ = setup
        po = PurchaseOrder(po_number=f"PO-FD-ORM-{_next()}", vendor_id=vendor.id, status=status, company_id=COMPANY_A)
        db_session.add(po)
        db_session.commit()

        _assert_frozen_and_immune(client, db_session, headers, po.id, _block(*DEFAULT_PRINT_ORDER))

    def test_a_draft_insert_writes_no_snapshot(self, db_session, setup):
        _, vendor, _ = setup
        po = PurchaseOrder(po_number=f"PO-FD-ORM-{_next()}", vendor_id=vendor.id, company_id=COMPANY_A)
        db_session.add(po)
        db_session.commit()

        assert _stored(db_session, po.id).flowdown_snapshot is None

    def test_a_po_whose_attributes_were_expired_still_freezes_with_its_stored_codes(self, db_session, setup):
        """The listener's no-ORM-load branch: neither the old status nor the codes are loaded."""
        _, vendor, _ = setup
        po = PurchaseOrder(
            po_number=f"PO-FD-ORM-{_next()}", vendor_id=vendor.id, company_id=COMPANY_A, quality_clause_codes=["QX2"]
        )
        db_session.add(po)
        db_session.commit()
        db_session.expire(po)

        po.status = POStatus.APPROVED
        db_session.commit()

        assert _stored(db_session, po.id).flowdown_snapshot == _block("QX2")

    def test_the_first_issue_freezes_and_a_later_transition_does_not_refreeze(
        self, client: TestClient, db_session, setup
    ):
        headers, vendor, part = setup
        po = _draft_po(client, headers, vendor, part)
        assert _set_status(client, headers, po["id"], "approved").status_code == 200
        frozen = _block(*DEFAULT_PRINT_ORDER)
        assert _stored(db_session, po["id"]).flowdown_snapshot == frozen

        _rewrite_catalog_and_profile(db_session)
        _send(client, headers, po["id"])  # APPROVED -> SENT: still frozen at approval
        assert _stored(db_session, po["id"]).flowdown_snapshot == frozen
        (send_row,) = _issue_audit_rows(db_session, po["id"])
        assert "flowdown_snapshot_sha256" not in (send_row.extra_data or {}), "only leaving DRAFT freezes"
        assert _set_status(client, headers, po["id"], "cancelled").status_code == 200  # SENT -> CANCELLED
        assert _stored(db_session, po["id"]).flowdown_snapshot == frozen
        assert _print_block(client, headers, po["id"]) == frozen

    def test_catalog_and_profile_edits_through_the_api_never_change_an_issued_po(
        self, client: TestClient, db_session, setup
    ):
        headers, vendor, part = setup
        issued = _draft_po(client, headers, vendor, part, quality_clause_codes=["QX1", "QX3"])
        draft = _draft_po(client, headers, vendor, part, quality_clause_codes=["QX1", "QX3"])
        _send(client, headers, issued["id"])
        frozen = _print_block(client, headers, issued["id"])
        assert frozen == _block("QX3", "QX1")

        qx1 = db_session.query(POQualityClause).filter_by(code="QX1").one()
        qx3 = db_session.query(POQualityClause).filter_by(code="QX3").one()
        assert client.put(f"{CLAUSES_URL}/{qx1.id}", headers=headers, json={"title": "Alpha v2"}).status_code == 200
        assert client.put(f"{CLAUSES_URL}/{qx3.id}", headers=headers, json={"is_active": False}).status_code == 200
        profile = client.put(
            PROFILE_URL, headers=headers, json={"enabled": True, "heading": "New heading", "version": 1}
        )
        assert profile.status_code == 200, profile.text

        assert _print_block(client, headers, issued["id"]) == frozen
        # The draft beside it follows every edit.
        assert _print_block(client, headers, draft["id"]) == {
            "enabled": True,
            "heading": "New heading",
            "document_reference": None,
            "body": None,
            "clauses": [{"code": "QX1", "title": "Alpha v2", "body": CLAUSE_BY_CODE["QX1"]["body"]}],
        }

    def test_a_disabled_profile_freezes_enabled_false(self, client: TestClient, db_session):
        _seed_flowdown(db_session, enabled=False)
        headers = _headers(_user(db_session))
        po = _draft_po(client, headers, _vendor(db_session), _part(db_session))
        assert _print_block(client, headers, po["id"]) is None

        _send(client, headers, po["id"])

        assert _stored(db_session, po["id"]).flowdown_snapshot == {"enabled": False}
        # Turning the block on afterwards does not reach a PO issued while it was off.
        assert (
            client.put(PROFILE_URL, headers=headers, json={"enabled": True, **PROFILE, "version": 1}).status_code == 200
        )
        assert _print_block(client, headers, po["id"]) is None
        assert _stored(db_session, po["id"]).flowdown_snapshot == {"enabled": False}

    def test_no_profile_at_all_freezes_enabled_false(self, client: TestClient, db_session):
        _seed_flowdown(db_session, profile=None)
        headers = _headers(_user(db_session))
        po = _draft_po(client, headers, _vendor(db_session), _part(db_session))

        _send(client, headers, po["id"])

        assert _stored(db_session, po["id"]).flowdown_snapshot == {"enabled": False}
        assert _print_block(client, headers, po["id"]) is None

    def test_an_issued_po_with_no_snapshot_prints_no_block_and_is_never_backfilled(
        self, client: TestClient, db_session, setup
    ):
        """A PO issued before migration 128: Core insert, so no mapper listener runs."""
        headers, vendor, _ = setup
        result = db_session.execute(
            PurchaseOrder.__table__.insert().values(
                company_id=COMPANY_A, po_number="PO-FD-LEGACY-1", vendor_id=vendor.id, status=POStatus.SENT
            )
        )
        db_session.commit()
        po_id = result.inserted_primary_key[0]
        assert _stored(db_session, po_id).flowdown_snapshot is None

        assert _print_block(client, headers, po_id) is None
        record = client.get(f"{PO_URL}/{po_id}", headers=headers).json()
        assert (record["quality_clause_codes"], record["flowdown_frozen"]) == (None, True)
        change = client.put(f"{PO_URL}/{po_id}", headers=headers, json={"version": 0, "quality_clause_codes": ["QX1"]})
        assert change.status_code == 409, change.text
        # A later non-draft transition does not back-fill it either.
        assert _set_status(client, headers, po_id, "cancelled").status_code == 200
        assert _stored(db_session, po_id).flowdown_snapshot is None
        assert _print_block(client, headers, po_id) is None

    def test_back_to_draft_prints_live_and_refreezes_on_the_next_issue(self, client: TestClient, db_session, setup):
        headers, vendor, part = setup
        po = _draft_po(client, headers, vendor, part, quality_clause_codes=["QX1"])
        assert _set_status(client, headers, po["id"], "approved").status_code == 200
        assert _stored(db_session, po["id"]).flowdown_snapshot == _block("QX1")

        qx1 = db_session.query(POQualityClause).filter_by(code="QX1").one()
        assert client.put(f"{CLAUSES_URL}/{qx1.id}", headers=headers, json={"title": "Alpha v2"}).status_code == 200
        assert _print_block(client, headers, po["id"]) == _block("QX1"), "still frozen while approved"

        back = _set_status(client, headers, po["id"], "draft")
        assert back.status_code == 200, back.text
        assert back.json()["flowdown_frozen"] is False
        assert back.json()["flowdown_snapshot"] is None, "a draft prints live; its old snapshot is stale"
        assert _stored(db_session, po["id"]).flowdown_snapshot == _block("QX1"), "kept until the next issue"
        assert _print_block(client, headers, po["id"]) == _block("QX1", titles={"QX1": "Alpha v2"})
        # Editable again while it is a draft.
        edit = client.put(
            f"{PO_URL}/{po['id']}", headers=headers, json={"version": 0, "quality_clause_codes": ["QX1", "QX2"]}
        )
        assert edit.status_code == 200, edit.text

        _send(client, headers, po["id"])

        refrozen = _block("QX2", "QX1", titles={"QX1": "Alpha v2"})
        assert _stored(db_session, po["id"]).flowdown_snapshot == refrozen
        assert _print_block(client, headers, po["id"]) == refrozen
        send_row = _issue_audit_rows(db_session, po["id"])[-1]
        assert send_row.extra_data["flowdown_snapshot_sha256"] == _sha256(refrozen)


# ── The server PDF ───────────────────────────────────────────────────────────


def _pdf_text(pdf_bytes: bytes) -> str:
    """Every page's text with layout whitespace collapsed (see tests/test_pdf_text_escaping.py)."""
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(pdf_bytes))
    return re.sub(r"\s+", " ", " ".join(page.extract_text() or "" for page in reader.pages))


def _print_data(**overrides) -> dict:
    data = {
        "po_number": "PO-FD-PDF-1",
        "printed_at": "11/19/2026",
        "vendor_name": "Test Vendor & Sons",
        "vendor_address": "1 Test Way",
        "vendor_contact": None,
        "vendor_phone": None,
        "vendor_email": None,
        "buyer_name": "Test Buyer",
        "buyer_email": None,
        "order_date": "11/19/2026",
        "required_date": None,
        "expected_date": None,
        "ship_to": None,
        "shipping_method": None,
        "subtotal": "$7.00",
        "tax": "$0.00",
        "shipping": "$0.00",
        "total": "$7.00",
        "notes": "Closing test notes.",
        "lines": [
            {
                "line_number": 1,
                "part_number": "PFD-1",
                "part_name": "Test part",
                "quantity_ordered": "2",
                "quantity_received": "0",
                "unit_price": "$3.50",
                "line_total": "$7.00",
                "required_date": None,
            }
        ],
    }
    data.update(overrides)
    return data


HOSTILE_BLOCK = {
    "enabled": True,
    "heading": "Supplier Terms <REF> & Co",
    "document_reference": "SQR<1> Rev A&B",
    "body": "Check OD<ID before press.\n\nSecond <b>paragraph</b> & more.",
    "clauses": [
        {"code": "QX1", "title": "Cert <TYP>", "body": "R&D <script>alert(1)</script> text"},
        {"code": "QX2", "title": "No body clause", "body": None},
    ],
}


class TestServerPdf:
    def test_the_pdf_carries_the_block_literally_and_before_the_notes(self):
        from app.services.document_pdf_service import build_purchase_order_document

        text = _pdf_text(build_purchase_order_document(_print_data(quality_flowdown=HOSTILE_BLOCK)))

        assert "Supplier Terms <REF> & Co" in text
        assert "Reference: SQR<1> Rev A&B" in text
        assert "Check OD<ID before press." in text
        assert "Second <b>paragraph</b> & more." in text
        assert "QX1 — Cert <TYP>: R&D <script>alert(1)</script> text" in text
        assert "QX2 — No body clause" in text
        assert text.index("Supplier Terms <REF> & Co") < text.index("Closing test notes.")

    def test_no_block_prints_nothing(self):
        from app.services.document_pdf_service import build_purchase_order_document

        for flowdown_value in (None, {}):
            text = _pdf_text(build_purchase_order_document(_print_data(quality_flowdown=flowdown_value)))
            assert "Reference:" not in text
            assert "Supplier Quality" not in text
        assert "Reference:" not in _pdf_text(build_purchase_order_document(_print_data()))

    def test_the_story_is_empty_without_a_block(self):
        from app.services.document_pdf_service import purchase_order_flowdown_story

        assert purchase_order_flowdown_story(None, None, None) == []

    def test_the_print_data_route_feeds_the_pdf_end_to_end(self, client: TestClient, db_session):
        headers = _headers(_user(db_session))
        profile = {key: HOSTILE_BLOCK[key] for key in ("heading", "document_reference", "body")}
        assert (
            client.put(PROFILE_URL, headers=headers, json={"enabled": True, **profile, "version": 0}).status_code == 200
        )
        clause = client.post(
            CLAUSES_URL, headers=headers, json={**HOSTILE_BLOCK["clauses"][0], "default_selected": True}
        )
        assert clause.status_code == 200, clause.text
        po = _draft_po(client, headers, _vendor(db_session), _part(db_session))

        data = client.get(f"/api/v1/print/purchase-orders/{po['id']}/print-data", headers=headers).json()
        # Stored verbatim: nothing was stripped on the way in.
        assert data["quality_flowdown"] == {**HOSTILE_BLOCK, "clauses": HOSTILE_BLOCK["clauses"][:1]}

        from app.services.document_pdf_service import build_purchase_order_document

        text = _pdf_text(build_purchase_order_document(data))
        assert "Supplier Terms <REF> & Co" in text
        assert "QX1 — Cert <TYP>: R&D <script>alert(1)</script> text" in text
