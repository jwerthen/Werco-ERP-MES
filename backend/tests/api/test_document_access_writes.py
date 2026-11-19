"""The writes behind restricted document viewing (AS9100D 7.5.3.1 / 7.5.3.2, migration 131).

What decides a document's ``access_level`` and who may open ``export_controlled`` ones:

* **The upload raise rule** (``document_access_service.resolve_upload_access_level``): a manual
  upload linked to a part whose customer is export-controlled is stored ``export_controlled``
  whatever was asked; a revision is stored at least at its predecessor's level; the server
  never lowers what was asked; the response and the audit row say what was raised and why.
* **``POST /documents/{id}/access-level``**: Admin / Quality, a caller who cannot open the
  document gets the same 404 as a missing one (so lowering an export-controlled document takes
  an export-authorized person), any API token is refused 403, a required audit row with the old
  and new level and the reason (503 and nothing saved without it), 409 on a no-op, and
  ``authorized_user_count``.
* **``PUT /users/{id}/export-authorization``**: Admin only, the platform-principal rule, any API
  token refused 403, ``export_authorized_at`` / ``_by`` stamped on grant and cleared on revoke,
  required audit, 409 on a no-op, and an Admin granting themselves.
* **``GET /documents/access-review``**: what it lists, the unlinked count, who may read it.
* Neither write is an MCP tool, and every new route is tenant-scoped.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.endpoints import documents as documents_router
from app.core.credentials import API_TOKEN_DOCUMENT_ACCESS_LEVEL, API_TOKEN_EXPORT_AUTHORIZATION
from app.main import app
from app.mcp.catalog import DOCUMENT_ACCESS_OPERATIONS, EXCLUDED_OPERATIONS, build_catalog
from app.mcp.convenience import CONVENIENCE_TOOL_NAMES, SHADOWED_OPERATIONS
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.document import Document, DocumentType
from app.models.governance import CustomerContract
from app.models.part import Part
from app.models.user import User, UserRole
from app.services.audit_service import AuditService, AuditWriteError
from app.services.document_access_service import ACCESS_LEVEL_UNCHANGED_DETAIL, CHAIN_NOT_VISIBLE_DETAIL
from app.services.storage_service import LocalStorageBackend
from tests.api.kiosk_test_helpers import bearer, ensure_company, make_user, user_headers

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

API = "/api/v1"
STANDARD = "standard"
RESTRICTED = "restricted"
EXPORT = "export_controlled"
ACCESS_REVIEW_URL = f"{API}/documents/access-review"

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


@pytest.fixture(autouse=True)
def local_files(tmp_path, monkeypatch):
    uploads = tmp_path / "uploads"
    monkeypatch.setattr(documents_router, "UPLOAD_DIR", str(uploads))
    monkeypatch.setattr(documents_router, "get_storage", lambda: LocalStorageBackend())
    return uploads


@pytest.fixture
def people(db_session: Session) -> Dict[str, User]:
    users = {
        "operator": make_user(db_session, role=UserRole.OPERATOR),
        "viewer": make_user(db_session, role=UserRole.VIEWER),
        "shipping": make_user(db_session, role=UserRole.SHIPPING),
        "supervisor": make_user(db_session, role=UserRole.SUPERVISOR),
        "manager": make_user(db_session, role=UserRole.MANAGER),
        "quality": make_user(db_session, role=UserRole.QUALITY),
        "admin": make_user(db_session, role=UserRole.ADMIN),
        "cleared_admin": make_user(db_session, role=UserRole.ADMIN),
        "superuser": make_user(db_session, role=UserRole.ADMIN),
        "platform_admin": make_user(db_session, role=UserRole.PLATFORM_ADMIN),
    }
    users["cleared_admin"].export_authorized = True
    users["superuser"].is_superuser = True
    db_session.commit()
    return users


def _h(people: Dict[str, User], persona: str) -> dict:
    return user_headers(people[persona])


def _document(db: Session, level: str, *, part_id: Optional[int] = None, company_id: int = 1, **extra) -> Document:
    n = _next()
    document = Document(
        document_number=f"ACW-{n:05d}",
        title=f"Access write drawing {n}",
        revision=extra.pop("revision", "A"),
        document_type=extra.pop("document_type", DocumentType.DRAWING),
        status="released",
        access_level=level,
        file_name=f"acw-{n}.pdf",
        mime_type="application/pdf",
        part_id=part_id,
        company_id=company_id,
        **extra,
    )
    db.add(document)
    db.commit()
    db.refresh(document)
    return document


def _part(db: Session, *, customer_name: Optional[str] = None, company_id: int = 1) -> Part:
    ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"ACW-P-{n:05d}",
        name=f"Access part {n}",
        part_type="manufactured",
        customer_name=customer_name,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def _customer(db: Session, name: str, *, company_id: int = 1, control: Optional[str] = "itar") -> Customer:
    """A customer; ``control`` "itar" / "cui" adds a controlling contract, None leaves it cleared."""
    ensure_company(db, company_id)
    customer = Customer(name=name, code=f"C{_next():04d}", company_id=company_id)
    db.add(customer)
    db.flush()
    if control is not None:
        db.add(
            CustomerContract(
                company_id=company_id,
                customer_id=customer.id,
                contract_name=f"{name} program",
                itar_controlled=control == "itar",
                contains_cui=control == "cui",
                default_data_classification="itar_export_controlled" if control == "itar" else "cui",
                active=True,
                expiration_date=datetime.utcnow() + timedelta(days=30),
            )
        )
    db.commit()
    db.refresh(customer)
    return customer


def _upload(client: TestClient, headers: dict, **values):
    data = {"title": "Bracket drawing", "document_type": "drawing", **{k: str(v) for k, v in values.items()}}
    return client.post(
        f"{API}/documents/upload",
        headers=headers,
        data=data,
        files={"file": ("bracket.pdf", b"%PDF-1.4 synthetic", "application/pdf")},
    )


def _audit_rows(db: Session, resource_type: str, resource_id: int, action: Optional[str] = None) -> List[AuditLog]:
    db.expire_all()
    query = db.query(AuditLog).filter(AuditLog.resource_type == resource_type, AuditLog.resource_id == resource_id)
    if action:
        query = query.filter(AuditLog.action == action)
    return query.order_by(AuditLog.id).all()


def _api_token(client: TestClient, people: Dict[str, User], persona: str, **extra) -> dict:
    response = client.post(
        f"{API}/api-tokens/",
        headers=_h(people, "admin"),
        json={"user_id": people[persona].id, "label": f"bot {_next()}", **extra},
    )
    assert response.status_code == 201, response.text
    return bearer(response.json()["token"])


def _fail_required_audit(monkeypatch) -> None:
    def refuse(self, *args, **kwargs):
        raise AuditWriteError("audit store down")

    monkeypatch.setattr(AuditService, "log_required", refuse)


# =========================================================================== #
# The upload raise rule
# =========================================================================== #


class TestUploadRaiseRule:
    @pytest.mark.parametrize("requested", [None, STANDARD, RESTRICTED])
    def test_a_part_with_an_export_controlled_customer_stores_export_controlled(
        self, client, db_session, people, requested
    ):
        _customer(db_session, "Synthetic Defense Co")
        part = _part(db_session, customer_name="Synthetic Defense Co")
        values = {"part_id": part.id}
        if requested:
            values["access_level"] = requested
        response = _upload(client, _h(people, "quality"), **values)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["access_level"] == EXPORT
        assert body["access_level_requested"] == (requested or STANDARD)
        assert body["access_level_raised"] is True
        assert body["access_level_raised_because"] == ["export_controlled_customer"]
        db_session.expire_all()
        assert db_session.get(Document, body["id"]).access_level == EXPORT
        (created,) = _audit_rows(db_session, "document", body["id"], "CREATE")
        assert created.extra_data["access_level_requested"] == (requested or STANDARD)
        assert created.extra_data["access_level_raised_because"] == ["export_controlled_customer"]
        # The uploader is not export-authorized: the echo told them what they stored, and from
        # here on it is the same 404 as a missing document.
        assert client.get(f"{API}/documents/{body['id']}", headers=_h(people, "quality")).status_code == 404

    def test_a_cui_customer_counts_as_export_controlled_too(self, client, db_session, people):
        _customer(db_session, "Synthetic CUI Co", control="cui")
        part = _part(db_session, customer_name="Synthetic CUI Co")
        body = _upload(client, _h(people, "quality"), part_id=part.id).json()
        assert body["access_level"] == EXPORT
        assert body["access_level_raised_because"] == ["export_controlled_customer"]

    @pytest.mark.parametrize(
        "customer_name,control",
        [(None, None), ("Open Commerce Co", None), ("Nobody We Know Inc", "missing")],
        ids=["no_customer", "cleared_customer", "unlinkable_name"],
    )
    def test_only_a_positive_finding_raises(self, client, db_session, people, customer_name, control):
        if control is None and customer_name:
            _customer(db_session, customer_name, control=None)
        part = _part(db_session, customer_name=customer_name)
        body = _upload(client, _h(people, "quality"), part_id=part.id).json()
        assert body["access_level"] == STANDARD
        assert body["access_level_raised"] is False and body["access_level_raised_because"] == []

    def test_an_explicit_export_controlled_request_is_not_a_raise(self, client, db_session, people):
        _customer(db_session, "Synthetic Defense Co")
        part = _part(db_session, customer_name="Synthetic Defense Co")
        body = _upload(client, _h(people, "quality"), part_id=part.id, access_level=EXPORT).json()
        assert body["access_level"] == EXPORT
        assert body["access_level_raised"] is False and body["access_level_raised_because"] == []
        (created,) = _audit_rows(db_session, "document", body["id"], "CREATE")
        assert "access_level_raised_because" not in (created.extra_data or {})
        assert created.new_values["access_level"] == EXPORT

    @pytest.mark.parametrize("requested", [RESTRICTED, EXPORT])
    def test_the_server_never_lowers_what_was_asked(self, client, db_session, people, requested):
        part = _part(db_session)
        body = _upload(client, _h(people, "cleared_admin"), part_id=part.id, access_level=requested).json()
        assert body["access_level"] == requested and body["access_level_raised"] is False

    def test_a_revision_inherits_its_predecessors_level(self, client, db_session, people):
        part = _part(db_session)
        headers = _h(people, "quality")
        rev_a = _upload(client, headers, part_id=part.id, access_level=RESTRICTED).json()
        rev_b = _upload(
            client, headers, part_id=part.id, previous_revision_id=rev_a["id"], revision="B", revision_notes="hole"
        ).json()
        assert rev_b["access_level"] == RESTRICTED
        assert rev_b["access_level_requested"] == STANDARD
        assert rev_b["access_level_raised_because"] == ["predecessor_revision"]
        assert rev_b["access_level_lifted_document_ids"] == []  # the history was already restricted
        # Asking for MORE than the predecessor is honored as asked -- and lifts the history with it.
        rev_c = _upload(
            client,
            _h(people, "cleared_admin"),
            part_id=part.id,
            previous_revision_id=rev_b["id"],
            revision="C",
            revision_notes="material",
            access_level=EXPORT,
        ).json()
        assert rev_c["access_level"] == EXPORT and rev_c["access_level_raised"] is False
        assert sorted(rev_c["access_level_lifted_document_ids"]) == sorted([rev_a["id"], rev_b["id"]])
        # And an export-controlled predecessor carries through a "standard" request.
        rev_d = _upload(
            client,
            _h(people, "cleared_admin"),
            part_id=part.id,
            previous_revision_id=rev_c["id"],
            revision="D",
            revision_notes="finish",
            access_level=STANDARD,
        ).json()
        assert rev_d["access_level"] == EXPORT
        assert rev_d["access_level_raised_because"] == ["predecessor_revision"]

    def test_both_reasons_are_reported_in_order(self, client, db_session, people):
        _customer(db_session, "Synthetic Defense Co")
        part = _part(db_session, customer_name="Synthetic Defense Co")
        previous = _document(db_session, RESTRICTED, part_id=part.id)
        body = _upload(
            client,
            _h(people, "quality"),
            part_id=part.id,
            previous_revision_id=previous.id,
            revision="B",
            revision_notes="x",
        ).json()
        assert body["access_level"] == EXPORT
        assert body["access_level_raised_because"] == ["predecessor_revision", "export_controlled_customer"]
        assert body["access_level_lifted_document_ids"] == [previous.id]
        db_session.expire_all()
        assert db_session.get(Document, previous.id).access_level == EXPORT

    def test_an_unknown_level_is_refused_and_nothing_is_stored(self, client, db_session, people, local_files):
        response = _upload(client, _h(people, "quality"), access_level="confidential")
        assert response.status_code == 422, response.text
        assert db_session.query(Document).count() == 0
        assert not local_files.exists() or not any(local_files.iterdir())

    def test_another_companys_controlled_customer_raises_nothing_here(self, client, db_session, people):
        _customer(db_session, "Synthetic Defense Co", company_id=2)
        part = _part(db_session, customer_name="Synthetic Defense Co")
        assert _upload(client, _h(people, "quality"), part_id=part.id).json()["access_level"] == STANDARD


# =========================================================================== #
# POST /documents/{id}/access-level
# =========================================================================== #


def _change(client, headers, document_id, level, reason="Customer program review"):
    return client.post(
        f"{API}/documents/{document_id}/access-level",
        headers=headers,
        json={"access_level": level, "reason": reason},
    )


class TestAccessLevelVerb:
    @pytest.mark.parametrize("persona", ["admin", "quality", "superuser", "platform_admin"])
    def test_admin_and_quality_change_it_with_a_required_audit_row(self, client, db_session, people, persona):
        document = _document(db_session, STANDARD)
        response = _change(client, _h(people, persona), document.id, RESTRICTED, reason="  Customer flowdown  ")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["access_level"] == RESTRICTED and body["previous_access_level"] == STANDARD
        assert body["id"] == document.id and body["document_number"] == document.document_number
        db_session.expire_all()
        assert db_session.get(Document, document.id).access_level == RESTRICTED
        (row,) = _audit_rows(db_session, "document", document.id, "ACCESS_LEVEL_CHANGE")
        assert row.user_id == people[persona].id
        assert row.old_values == {"access_level": STANDARD}
        assert row.new_values == {"access_level": RESTRICTED}
        assert row.extra_data["reason"] == "Customer flowdown"
        assert row.extra_data["authorized_user_count"] == body["authorized_user_count"]
        # A document with no revisions is a chain of one.
        assert (body["changed_document_ids"], body["chain_size"]) == ([document.id], 1)
        assert row.extra_data["chain_change"] == {
            "trigger": "access_level_change",
            "addressed_document_id": document.id,
            "chain_size": 1,
        }

    @pytest.mark.parametrize("persona", ["manager", "supervisor", "operator", "viewer", "shipping"])
    def test_every_other_role_is_refused(self, client, db_session, people, persona):
        document = _document(db_session, STANDARD)
        response = _change(client, _h(people, persona), document.id, RESTRICTED)
        assert response.status_code == 403, response.text
        db_session.expire_all()
        assert db_session.get(Document, document.id).access_level == STANDARD
        assert _audit_rows(db_session, "document", document.id) == []

    @pytest.mark.parametrize("target", [STANDARD, RESTRICTED, EXPORT])
    def test_a_caller_who_cannot_open_it_gets_404_before_the_no_op_check(self, client, db_session, people, target):
        """Lowering (or even re-stating) an export-controlled document takes an export-authorized person."""
        document = _document(db_session, EXPORT)
        for persona in ("admin", "quality", "superuser", "platform_admin"):
            response = _change(client, _h(people, persona), document.id, target)
            assert (response.status_code, response.json()["detail"]) == (404, "Document not found"), persona
        db_session.expire_all()
        assert db_session.get(Document, document.id).access_level == EXPORT
        assert _audit_rows(db_session, "document", document.id) == []
        lowered = _change(client, _h(people, "cleared_admin"), document.id, STANDARD, reason="Customer cleared it")
        assert lowered.status_code == 200, lowered.text
        assert lowered.json()["previous_access_level"] == EXPORT

    def test_a_missing_document_is_the_same_404(self, client, people):
        response = _change(client, _h(people, "cleared_admin"), 987_654, RESTRICTED)
        assert (response.status_code, response.json()["detail"]) == (404, "Document not found")

    def test_raising_beyond_ones_own_access_is_allowed_and_warned_by_the_count(self, client, db_session, people):
        """Quality may lock a document away from themselves; authorized_user_count says who is left."""
        people["cleared_admin"].export_authorized = False
        db_session.commit()
        document = _document(db_session, STANDARD)
        response = _change(client, _h(people, "quality"), document.id, EXPORT)
        assert response.status_code == 200, response.text
        assert response.json()["access_level"] == EXPORT
        assert response.json()["authorized_user_count"] == 0
        assert client.get(f"{API}/documents/{document.id}", headers=_h(people, "quality")).status_code == 404

    def test_authorized_user_count_counts_active_authorized_users_of_this_company_only(
        self, client, db_session, people
    ):
        inactive = make_user(db_session, role=UserRole.QUALITY, is_active=False)
        inactive.export_authorized = True
        foreign = make_user(db_session, role=UserRole.ADMIN, company_id=2)
        foreign.export_authorized = True
        second = make_user(db_session, role=UserRole.OPERATOR)
        second.export_authorized = True
        db_session.commit()
        document = _document(db_session, STANDARD)
        response = _change(client, _h(people, "quality"), document.id, RESTRICTED)
        assert response.status_code == 200, response.text
        assert response.json()["authorized_user_count"] == 2  # cleared_admin + the operator

    def test_a_no_op_is_409_and_writes_nothing(self, client, db_session, people):
        document = _document(db_session, RESTRICTED)
        response = _change(client, _h(people, "quality"), document.id, RESTRICTED)
        assert (response.status_code, response.json()["detail"]) == (409, ACCESS_LEVEL_UNCHANGED_DETAIL)
        assert _audit_rows(db_session, "document", document.id) == []

    @pytest.mark.parametrize(
        "payload",
        [
            {"access_level": "restricted", "reason": "   "},
            {"access_level": "restricted"},
            {"access_level": "confidential", "reason": "x"},
            {"reason": "x"},
        ],
        ids=["blank_reason", "no_reason", "unknown_level", "no_level"],
    )
    def test_a_reason_and_a_known_level_are_required(self, client, db_session, people, payload):
        document = _document(db_session, STANDARD)
        response = client.post(
            f"{API}/documents/{document.id}/access-level", headers=_h(people, "quality"), json=payload
        )
        assert response.status_code == 422, response.text
        assert _audit_rows(db_session, "document", document.id) == []

    def test_audit_loss_is_503_and_nothing_is_saved(self, client, db_session, people, monkeypatch):
        document = _document(db_session, STANDARD)
        _fail_required_audit(monkeypatch)
        response = _change(client, _h(people, "quality"), document.id, RESTRICTED)
        assert response.status_code == 503, response.text
        db_session.expire_all()
        assert db_session.get(Document, document.id).access_level == STANDARD
        assert _audit_rows(db_session, "document", document.id) == []

    @pytest.mark.parametrize("token", ["full", "read_only", "ai_external"])
    def test_any_api_token_is_refused_403_whatever_its_users_role(self, client, db_session, people, token):
        document = _document(db_session, STANDARD)
        extra = {
            "full": {},
            "read_only": {"scope": "read_only", "ai_external": False},
            "ai_external": {"scope": "read_only"},
        }
        headers = _api_token(client, people, "cleared_admin", **extra[token])
        response = _change(client, headers, document.id, RESTRICTED)
        assert response.status_code == 403, response.text
        if token == "full":
            assert response.json()["detail"] == API_TOKEN_DOCUMENT_ACCESS_LEVEL
        db_session.expire_all()
        assert db_session.get(Document, document.id).access_level == STANDARD
        assert _audit_rows(db_session, "document", document.id) == []

    def test_the_full_token_refusal_comes_before_the_document_is_read(self, client, db_session, people):
        """A token learns nothing about a document: the same 403 for a missing id."""
        headers = _api_token(client, people, "cleared_admin")
        response = _change(client, headers, 987_654, RESTRICTED)
        assert (response.status_code, response.json()["detail"]) == (403, API_TOKEN_DOCUMENT_ACCESS_LEVEL)

    def test_another_companys_document_is_404_and_untouched(self, client, db_session, people):
        ensure_company(db_session, 2)
        foreign = _document(db_session, STANDARD, company_id=2)
        response = _change(client, _h(people, "cleared_admin"), foreign.id, RESTRICTED)
        assert (response.status_code, response.json()["detail"]) == (404, "Document not found")
        db_session.expire_all()
        assert db_session.get(Document, foreign.id).access_level == STANDARD
        # And a company-2 Admin cannot reach a company-1 document.
        foreign_admin = make_user(db_session, role=UserRole.ADMIN, company_id=2)
        foreign_admin.export_authorized = True
        db_session.commit()
        local = _document(db_session, STANDARD)
        assert _change(client, user_headers(foreign_admin), local.id, RESTRICTED).status_code == 404
        db_session.expire_all()
        assert db_session.get(Document, local.id).access_level == STANDARD


# =========================================================================== #
# PUT /users/{id}/export-authorization
# =========================================================================== #


def _authorize(client, headers, user_id, granted=True, reason="ITAR program lead"):
    return client.put(
        f"{API}/users/{user_id}/export-authorization",
        headers=headers,
        json={"export_authorized": granted, "reason": reason},
    )


class TestExportAuthorization:
    def test_grant_stamps_who_and_when_and_revoke_clears_them_both_audited(self, client, db_session, people):
        target = people["quality"]
        admin = people["admin"]
        before = datetime.utcnow() - timedelta(seconds=5)
        granted = _authorize(client, _h(people, "admin"), target.id, reason="  Program lead  ")
        assert granted.status_code == 200, granted.text
        body = granted.json()
        assert body["id"] == target.id and body["export_authorized"] is True
        assert body["export_authorized_by"] == admin.id
        assert body["export_authorized_at"].endswith("Z")
        db_session.expire_all()
        stored = db_session.get(User, target.id)
        assert stored.export_authorized is True and stored.export_authorized_by == admin.id
        assert stored.export_authorized_at >= before
        stamped_at = stored.export_authorized_at
        (grant_row,) = _audit_rows(db_session, "user", target.id, "EXPORT_AUTHORIZATION_GRANT")
        assert grant_row.user_id == admin.id
        assert grant_row.old_values == {"export_authorized": False}
        assert grant_row.new_values == {"export_authorized": True}
        assert grant_row.extra_data["reason"] == "Program lead"

        # The user record reads it back.
        listed = client.get(f"{API}/users/{target.id}", headers=_h(people, "admin")).json()
        assert listed["export_authorized"] is True and listed["export_authorized_by"] == admin.id

        revoked = _authorize(client, _h(people, "cleared_admin"), target.id, granted=False, reason="Left program")
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["export_authorized"] is False
        assert revoked.json()["export_authorized_at"] is None and revoked.json()["export_authorized_by"] is None
        db_session.expire_all()
        stored = db_session.get(User, target.id)
        assert (stored.export_authorized, stored.export_authorized_at, stored.export_authorized_by) == (
            False,
            None,
            None,
        )
        (revoke_row,) = _audit_rows(db_session, "user", target.id, "EXPORT_AUTHORIZATION_REVOKE")
        assert revoke_row.user_id == people["cleared_admin"].id
        assert revoke_row.old_values == {"export_authorized": True}
        assert revoke_row.new_values == {"export_authorized": False}
        assert revoke_row.extra_data["reason"] == "Left program"
        # The revoked grant is kept on the revoke row: who granted it and when.
        assert revoke_row.extra_data["revoked_grant"]["export_authorized_by"] == admin.id
        assert revoke_row.extra_data["revoked_grant"]["export_authorized_at"].startswith(stamped_at.isoformat()[:19])

    def test_a_grant_opens_export_controlled_documents_and_a_revoke_closes_them_at_once(
        self, client, db_session, people
    ):
        document = _document(db_session, EXPORT)
        headers = _h(people, "quality")  # issued before the grant, used after it
        assert client.get(f"{API}/documents/{document.id}", headers=headers).status_code == 404
        assert _authorize(client, _h(people, "admin"), people["quality"].id).status_code == 200
        assert client.get(f"{API}/documents/{document.id}", headers=headers).status_code == 200
        assert _authorize(client, _h(people, "admin"), people["quality"].id, granted=False).status_code == 200
        assert client.get(f"{API}/documents/{document.id}", headers=headers).status_code == 404

    @pytest.mark.parametrize("persona", ["manager", "quality", "supervisor", "operator", "viewer", "shipping"])
    def test_only_an_admin_may_grant(self, client, db_session, people, persona):
        target = people["operator"] if persona != "operator" else people["viewer"]
        response = _authorize(client, _h(people, persona), target.id)
        assert response.status_code == 403, response.text
        db_session.expire_all()
        assert db_session.get(User, target.id).export_authorized is False
        assert _audit_rows(db_session, "user", target.id) == []

    def test_an_admin_may_grant_themselves_and_it_is_attributed_to_them(self, client, db_session, people):
        admin = people["admin"]
        response = _authorize(client, _h(people, "admin"), admin.id, reason="Self-designated program lead")
        assert response.status_code == 200, response.text
        assert response.json()["export_authorized_by"] == admin.id
        (row,) = _audit_rows(db_session, "user", admin.id, "EXPORT_AUTHORIZATION_GRANT")
        assert row.user_id == admin.id and row.extra_data["reason"] == "Self-designated program lead"

    @pytest.mark.parametrize("target_persona", ["superuser", "platform_admin"])
    def test_a_tenant_admin_may_not_manage_a_platform_account(self, client, db_session, people, target_persona):
        target = people[target_persona]
        response = _authorize(client, _h(people, "admin"), target.id)
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == "Only platform administrators can manage platform accounts"
        db_session.expire_all()
        assert db_session.get(User, target.id).export_authorized is False
        assert _audit_rows(db_session, "user", target.id) == []

    @pytest.mark.parametrize("actor", ["superuser", "platform_admin"])
    def test_a_platform_principal_may_manage_platform_and_tenant_accounts(self, client, db_session, people, actor):
        for target in ("superuser", "platform_admin", "operator"):
            if target == actor:
                continue
            response = _authorize(client, _h(people, actor), people[target].id)
            assert response.status_code == 200, (target, response.text)
            assert response.json()["export_authorized_by"] == people[actor].id

    def test_no_op_grant_and_no_op_revoke_are_409_and_write_nothing(self, client, db_session, people):
        cleared = people["cleared_admin"]
        again = _authorize(client, _h(people, "admin"), cleared.id)
        assert (again.status_code, again.json()["detail"]) == (409, "The user is already export-authorized")
        never = _authorize(client, _h(people, "admin"), people["operator"].id, granted=False)
        assert (never.status_code, never.json()["detail"]) == (409, "The user is not export-authorized")
        assert _audit_rows(db_session, "user", cleared.id) == []
        assert _audit_rows(db_session, "user", people["operator"].id) == []

    @pytest.mark.parametrize(
        "payload",
        [{"export_authorized": True, "reason": " "}, {"export_authorized": True}, {"reason": "x"}],
        ids=["blank_reason", "no_reason", "no_flag"],
    )
    def test_a_reason_and_the_flag_are_required(self, client, db_session, people, payload):
        target = people["operator"]
        response = client.put(
            f"{API}/users/{target.id}/export-authorization", headers=_h(people, "admin"), json=payload
        )
        assert response.status_code == 422, response.text
        db_session.expire_all()
        assert db_session.get(User, target.id).export_authorized is False

    @pytest.mark.parametrize("granted", [True, False])
    def test_audit_loss_is_503_and_nothing_is_saved(self, client, db_session, people, monkeypatch, granted):
        target = people["operator"] if granted else people["cleared_admin"]
        _fail_required_audit(monkeypatch)
        response = _authorize(client, _h(people, "admin"), target.id, granted=granted)
        assert response.status_code == 503, response.text
        db_session.expire_all()
        stored = db_session.get(User, target.id)
        assert stored.export_authorized is (not granted)
        if granted:
            assert stored.export_authorized_at is None and stored.export_authorized_by is None
        assert _audit_rows(db_session, "user", target.id) == []

    @pytest.mark.parametrize("token", ["full", "read_only", "ai_external"])
    def test_any_api_token_is_refused_403(self, client, db_session, people, token):
        extra = {
            "full": {},
            "read_only": {"scope": "read_only", "ai_external": False},
            "ai_external": {"scope": "read_only"},
        }
        headers = _api_token(client, people, "admin", **extra[token])
        target = people["operator"]
        response = _authorize(client, headers, target.id)
        assert response.status_code == 403, response.text
        if token == "full":
            assert response.json()["detail"] == API_TOKEN_EXPORT_AUTHORIZATION
        # Even toward a user that does not exist: the token learns nothing.
        missing = _authorize(client, headers, 987_654)
        assert missing.status_code == 403
        db_session.expire_all()
        assert db_session.get(User, target.id).export_authorized is False
        assert _audit_rows(db_session, "user", target.id) == []

    def test_another_companys_user_is_404_and_untouched(self, client, db_session, people):
        foreign = make_user(db_session, role=UserRole.OPERATOR, company_id=2)
        response = _authorize(client, _h(people, "admin"), foreign.id)
        assert (response.status_code, response.json()["detail"]) == (404, "User not found")
        db_session.expire_all()
        assert db_session.get(User, foreign.id).export_authorized is False
        assert _audit_rows(db_session, "user", foreign.id) == []

    def test_the_users_list_reports_the_authorization(self, client, db_session, people):
        rows = {row["id"]: row for row in client.get(f"{API}/users/", headers=_h(people, "admin")).json()}
        assert rows[people["cleared_admin"].id]["export_authorized"] is True
        assert rows[people["operator"].id]["export_authorized"] is False
        assert {"export_authorized_at", "export_authorized_by"} <= set(rows[people["operator"].id])


# =========================================================================== #
# GET /documents/access-review
# =========================================================================== #


class TestAccessReview:
    def _world(self, db: Session):
        itar = _customer(db, "Synthetic Defense Co")
        cui = _customer(db, "Synthetic CUI Co", control="cui")
        _customer(db, "Open Commerce Co", control=None)
        itar_part = _part(db, customer_name="Synthetic Defense Co")
        cui_part = _part(db, customer_name="Synthetic CUI Co")
        open_part = _part(db, customer_name="Open Commerce Co")
        rows = {
            "itar_standard": _document(db, STANDARD, part_id=itar_part.id),
            "itar_restricted": _document(
                db, RESTRICTED, part_id=itar_part.id, document_type=DocumentType.SPECIFICATION
            ),
            "itar_flagged": _document(db, EXPORT, part_id=itar_part.id),
            "cui_standard": _document(db, STANDARD, part_id=cui_part.id),
            "open_standard": _document(db, STANDARD, part_id=open_part.id),
            "unlinked_standard": _document(db, STANDARD),
            "unlinked_restricted": _document(db, RESTRICTED),
            "unlinked_flagged": _document(db, EXPORT),
        }
        return itar, cui, itar_part, cui_part, rows

    @pytest.mark.parametrize("persona", ["quality", "admin"])
    def test_lists_unflagged_documents_of_export_controlled_customers_and_counts_unlinked(
        self, client, db_session, people, persona
    ):
        itar, cui, itar_part, cui_part, docs = self._world(db_session)
        response = client.get(ACCESS_REVIEW_URL, headers=_h(people, persona))
        assert response.status_code == 200, response.text
        body = response.json()
        listed = {row["document_id"]: row for row in body["documents"]}
        assert set(listed) == {docs["itar_standard"].id, docs["itar_restricted"].id, docs["cui_standard"].id}
        assert body["document_count"] == 3
        assert body["unlinked_document_count"] == 2  # export-controlled unlinked ones need no review
        row = listed[docs["itar_restricted"].id]
        assert row == {
            "document_id": docs["itar_restricted"].id,
            "document_number": docs["itar_restricted"].document_number,
            "revision": "A",
            "title": docs["itar_restricted"].title,
            "document_type": "specification",
            "access_level": RESTRICTED,
            "part_id": itar_part.id,
            "part_number": itar_part.part_number,
            "customer_id": itar.id,
            "customer_name": "Synthetic Defense Co",
            "classification": "itar_export_controlled",
        }
        cui_row = listed[docs["cui_standard"].id]
        assert (cui_row["customer_id"], cui_row["classification"]) == (cui.id, "cui")

    def test_it_is_a_pure_read(self, client, db_session, people):
        self._world(db_session)
        before = db_session.query(AuditLog).count()
        levels = {d.id: d.access_level for d in db_session.query(Document).all()}
        assert client.get(ACCESS_REVIEW_URL, headers=_h(people, "quality")).status_code == 200
        db_session.expire_all()
        assert db_session.query(AuditLog).count() == before
        assert {d.id: d.access_level for d in db_session.query(Document).all()} == levels

    def test_an_empty_company_reviews_to_nothing(self, client, people):
        body = client.get(ACCESS_REVIEW_URL, headers=_h(people, "quality")).json()
        assert body == {"documents": [], "document_count": 0, "unlinked_document_count": 0}

    @pytest.mark.parametrize("persona", ["manager", "supervisor", "operator", "viewer", "shipping"])
    def test_only_admin_and_quality_may_read_it(self, client, db_session, people, persona):
        self._world(db_session)
        assert client.get(ACCESS_REVIEW_URL, headers=_h(people, persona)).status_code == 403

    def test_another_companys_documents_are_neither_listed_nor_counted(self, client, db_session, people):
        _customer(db_session, "Foreign Defense Co", company_id=2)
        foreign_part = _part(db_session, customer_name="Foreign Defense Co", company_id=2)
        _document(db_session, STANDARD, part_id=foreign_part.id, company_id=2)
        _document(db_session, STANDARD, company_id=2)
        # A company-1 part naming the same customer is NOT controlled here: no such customer in company 1.
        local_part = _part(db_session, customer_name="Foreign Defense Co")
        _document(db_session, STANDARD, part_id=local_part.id)
        body = client.get(ACCESS_REVIEW_URL, headers=_h(people, "quality")).json()
        assert body == {"documents": [], "document_count": 0, "unlinked_document_count": 0}

    def test_the_review_does_not_shadow_the_document_by_id_route(self, client, db_session, people):
        document = _document(db_session, STANDARD)
        assert client.get(f"{API}/documents/{document.id}", headers=_h(people, "operator")).status_code == 200


# =========================================================================== #
# The access_level list filter (tenant-scoped like the rest of the list)
# =========================================================================== #


def test_the_list_filter_returns_this_companys_visible_documents_at_that_level(client, db_session, people):
    ensure_company(db_session, 2)
    restricted = _document(db_session, RESTRICTED)
    _document(db_session, STANDARD)
    _document(db_session, RESTRICTED, company_id=2)
    response = client.get(f"{API}/documents/", headers=_h(people, "quality"), params={"access_level": RESTRICTED})
    assert [row["id"] for row in response.json()] == [restricted.id]
    assert response.json()[0]["access_level"] == RESTRICTED
    bad = client.get(f"{API}/documents/", headers=_h(people, "quality"), params={"access_level": "secret"})
    assert bad.status_code == 422


# =========================================================================== #
# MCP: neither write is a tool; the reads stay tools
# =========================================================================== #


class TestMcpExclusion:
    @pytest.fixture(scope="class")
    def tools(self):
        return {
            (tool.method, tool.path): tool
            for tool in build_catalog(
                app.openapi(), shadowed=SHADOWED_OPERATIONS, reserved_names=CONVENIENCE_TOOL_NAMES
            )
        }

    def test_the_two_writes_are_excluded(self, tools):
        assert DOCUMENT_ACCESS_OPERATIONS == {
            ("PUT", "/api/v1/users/{user_id}/export-authorization"),
            ("POST", "/api/v1/documents/{document_id}/access-level"),
        }
        assert DOCUMENT_ACCESS_OPERATIONS <= set(EXCLUDED_OPERATIONS)
        for key in DOCUMENT_ACCESS_OPERATIONS:
            assert key not in tools, key

    def test_the_two_writes_are_real_routes(self):
        """An exclusion naming a route that does not exist would exclude nothing."""
        paths = app.openapi()["paths"]
        for method, path in DOCUMENT_ACCESS_OPERATIONS:
            assert method.lower() in paths[path], (method, path)

    def test_the_reads_stay_tools(self, tools):
        for key in (
            ("GET", "/api/v1/documents/access-review"),
            ("GET", "/api/v1/documents/{document_id}"),
            ("GET", "/api/v1/users/{user_id}"),
        ):
            assert key in tools, key


# =========================================================================== #
# A revision history moves together
# =========================================================================== #


def _chain(client, people, persona="quality", *, length=3, part_id=None) -> List[int]:
    """A revision history built through the upload route: A, then B on A, then C on B..."""
    headers = _h(people, persona)
    extra = {"part_id": part_id} if part_id else {}
    ids = [_upload(client, headers, title="Chain drawing", **extra).json()["id"]]
    for label in "BCDEFG"[: length - 1]:
        response = _upload(client, headers, previous_revision_id=ids[-1], revision=label, revision_notes="n", **extra)
        assert response.status_code == 200, response.text
        ids.append(response.json()["id"])
    return ids


def _levels(db: Session, ids: List[int]) -> List[str]:
    db.expire_all()
    return [db.get(Document, document_id).access_level for document_id in ids]


class TestRevisionChain:
    def test_the_verb_changes_every_revision_with_one_audit_row_each(self, client, db_session, people):
        rev_a, rev_b, rev_c = _chain(client, people)
        response = _change(client, _h(people, "quality"), rev_b, RESTRICTED, reason="Program flowdown")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["id"] == rev_b and body["previous_access_level"] == STANDARD
        assert sorted(body["changed_document_ids"]) == [rev_a, rev_b, rev_c] and body["chain_size"] == 3
        assert _levels(db_session, [rev_a, rev_b, rev_c]) == [RESTRICTED] * 3
        for document_id in (rev_a, rev_b, rev_c):
            (row,) = _audit_rows(db_session, "document", document_id, "ACCESS_LEVEL_CHANGE")
            assert row.user_id == people["quality"].id
            assert (row.old_values, row.new_values) == ({"access_level": STANDARD}, {"access_level": RESTRICTED})
            assert row.extra_data["reason"] == "Program flowdown"
            assert row.extra_data["chain_change"] == {
                "trigger": "access_level_change",
                "addressed_document_id": rev_b,
                "chain_size": 3,
            }

    def test_only_the_revisions_that_differ_change(self, client, db_session, people):
        rev_a = _document(db_session, RESTRICTED)
        rev_b = _document(db_session, STANDARD, previous_revision_id=rev_a.id, revision="B")
        response = _change(client, _h(people, "quality"), rev_b.id, RESTRICTED)
        assert response.status_code == 200, response.text
        assert (response.json()["changed_document_ids"], response.json()["chain_size"]) == ([rev_b.id], 2)
        assert _audit_rows(db_session, "document", rev_a.id) == []
        # Now every revision is at that level: a no-op for the whole chain.
        again = _change(client, _h(people, "quality"), rev_a.id, RESTRICTED)
        assert (again.status_code, again.json()["detail"]) == (409, ACCESS_LEVEL_UNCHANGED_DETAIL)

    def test_lowering_needs_a_caller_who_can_open_every_revision(self, client, db_session, people):
        """A legacy mixed history: the caller can open the addressed revision, not all of them."""
        rev_a = _document(db_session, STANDARD)
        rev_b = _document(db_session, EXPORT, previous_revision_id=rev_a.id, revision="B")
        refused = _change(client, _h(people, "quality"), rev_a.id, RESTRICTED)
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"] == CHAIN_NOT_VISIBLE_DETAIL
        assert _levels(db_session, [rev_a.id, rev_b.id]) == [STANDARD, EXPORT]
        assert _audit_rows(db_session, "document", rev_a.id) == []
        assert _audit_rows(db_session, "document", rev_b.id) == []
        # The hidden revision itself is still the plain 404.
        hidden = _change(client, _h(people, "quality"), rev_b.id, RESTRICTED)
        assert (hidden.status_code, hidden.json()["detail"]) == (404, "Document not found")
        # Someone who can open every revision may lower it.
        lowered = _change(client, _h(people, "cleared_admin"), rev_a.id, RESTRICTED)
        assert lowered.status_code == 200, lowered.text
        assert sorted(lowered.json()["changed_document_ids"]) == [rev_a.id, rev_b.id]
        assert _levels(db_session, [rev_a.id, rev_b.id]) == [RESTRICTED, RESTRICTED]

    def test_raising_never_needs_the_hidden_revisions(self, client, db_session, people):
        """Raising locks MORE away; the caller need not open what is already above them."""
        rev_a = _document(db_session, STANDARD)
        rev_b = _document(db_session, EXPORT, previous_revision_id=rev_a.id, revision="B")
        raised = _change(client, _h(people, "quality"), rev_a.id, EXPORT)
        assert raised.status_code == 200, raised.text
        assert raised.json()["changed_document_ids"] == [rev_a.id]
        assert _levels(db_session, [rev_a.id, rev_b.id]) == [EXPORT, EXPORT]

    def test_audit_loss_mid_chain_is_503_and_no_revision_changes(self, client, db_session, people, monkeypatch):
        ids = _chain(client, people)
        _fail_required_audit(monkeypatch)
        response = _change(client, _h(people, "quality"), ids[1], RESTRICTED)
        assert response.status_code == 503, response.text
        assert _levels(db_session, ids) == [STANDARD] * 3
        assert all(_audit_rows(db_session, "document", i, "ACCESS_LEVEL_CHANGE") == [] for i in ids)

    def test_a_raised_revision_lifts_the_lower_revisions_of_its_history(self, client, db_session, people):
        rev_a, rev_b = _chain(client, people, length=2)
        response = _upload(
            client,
            _h(people, "quality"),
            previous_revision_id=rev_b,
            revision="C",
            revision_notes="tighter tolerance",
            title="Chain drawing",
            access_level=RESTRICTED,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["access_level"] == RESTRICTED
        assert sorted(body["access_level_lifted_document_ids"]) == [rev_a, rev_b]
        assert _levels(db_session, [rev_a, rev_b, body["id"]]) == [RESTRICTED] * 3
        for document_id in (rev_a, rev_b):
            (row,) = _audit_rows(db_session, "document", document_id, "ACCESS_LEVEL_CHANGE")
            assert (row.old_values, row.new_values) == ({"access_level": STANDARD}, {"access_level": RESTRICTED})
            assert row.extra_data["chain_change"]["trigger"] == "new_revision"
            assert row.extra_data["chain_change"]["new_document_id"] == body["id"]
            assert row.extra_data["chain_change"]["chain_size"] == 3
        (created,) = _audit_rows(db_session, "document", body["id"], "CREATE")
        assert sorted(created.extra_data["access_level_lifted_document_ids"]) == [rev_a, rev_b]

    def test_a_customer_raise_lifts_a_legacy_history_too(self, client, db_session, people):
        _customer(db_session, "Synthetic Defense Co")
        part = _part(db_session, customer_name="Synthetic Defense Co")
        rev_a = _document(db_session, STANDARD, part_id=part.id)
        rev_b = _document(db_session, STANDARD, part_id=part.id, previous_revision_id=rev_a.id, revision="B")
        body = _upload(
            client,
            _h(people, "cleared_admin"),
            part_id=part.id,
            previous_revision_id=rev_b.id,
            revision="C",
            revision_notes="customer rev",
        ).json()
        assert body["access_level"] == EXPORT
        assert body["access_level_raised_because"] == ["export_controlled_customer"]
        assert sorted(body["access_level_lifted_document_ids"]) == [rev_a.id, rev_b.id]
        assert _levels(db_session, [rev_a.id, rev_b.id]) == [EXPORT, EXPORT]

    def test_a_revision_at_its_historys_level_lifts_nothing(self, client, db_session, people):
        rev_a, rev_b = _chain(client, people, length=2)
        body = _upload(
            client, _h(people, "quality"), previous_revision_id=rev_b, revision="C", revision_notes="n"
        ).json()
        assert body["access_level"] == STANDARD and body["access_level_lifted_document_ids"] == []
        assert _audit_rows(db_session, "document", rev_a, "ACCESS_LEVEL_CHANGE") == []

    def test_a_lift_whose_audit_cannot_be_saved_publishes_nothing(
        self, client, db_session, people, monkeypatch, local_files
    ):
        rev_a, rev_b = _chain(client, people, length=2)
        before = db_session.query(Document).count()
        files_before = sorted(p.name for p in local_files.rglob("*") if p.is_file())
        _fail_required_audit(monkeypatch)
        response = _upload(
            client,
            _h(people, "quality"),
            previous_revision_id=rev_b,
            revision="C",
            revision_notes="n",
            title="Chain drawing",
            access_level=RESTRICTED,
        )
        assert response.status_code == 503, response.text
        db_session.expire_all()
        assert db_session.query(Document).count() == before
        assert _levels(db_session, [rev_a, rev_b]) == [STANDARD, STANDARD]
        assert sorted(p.name for p in local_files.rglob("*") if p.is_file()) == files_before


# =========================================================================== #
# The raise rule on the other writers: work-order-only uploads and Hank intake
# =========================================================================== #


def _work_order_for(db: Session, customer_name: Optional[str]):
    from tests.api.kiosk_test_helpers import make_wo_with_operation, make_work_center

    work_order, _operation = make_wo_with_operation(db, work_center=make_work_center(db))
    work_order.customer_name = customer_name
    db.commit()
    return work_order


class TestWorkOrderUploadRaise:
    def test_a_work_order_whose_customer_is_export_controlled_raises_the_upload(self, client, db_session, people):
        _customer(db_session, "Synthetic Defense Co")
        work_order = _work_order_for(db_session, "Synthetic Defense Co")
        body = _upload(client, _h(people, "quality"), work_order_id=work_order.id).json()
        assert body["work_order_id"] == work_order.id and body["part_id"] is None
        assert body["access_level"] == EXPORT
        assert body["access_level_raised_because"] == ["export_controlled_customer"]

    def test_a_cleared_customers_work_order_raises_nothing(self, client, db_session, people):
        _customer(db_session, "Open Commerce Co", control=None)
        work_order = _work_order_for(db_session, "Open Commerce Co")
        body = _upload(client, _h(people, "quality"), work_order_id=work_order.id).json()
        assert body["access_level"] == STANDARD and body["access_level_raised"] is False


@pytest.fixture
def intake_storage(monkeypatch, db_session):
    """In-memory storage for Hank intake (the shape ``tests/api/test_hank_intake.py`` uses)."""
    from app.services import hank_intake_service as intake

    class MemoryStorage:
        is_remote = True

        def __init__(self):
            self.items = {}

        def save(self, content, *, key):
            self.items[key] = content
            return key

        def delete(self, ref):
            self.items.pop(ref, None)

        def read_bytes(self, ref):
            return self.items[ref]

        def open_stream(self, ref):
            yield self.read_bytes(ref)

    backend = MemoryStorage()
    monkeypatch.setattr(intake, "get_storage", lambda: backend)
    monkeypatch.setattr(intake, "backend_for_ref", lambda ref: backend)
    monkeypatch.setattr(intake, "enqueue_intake", lambda *args: False)
    monkeypatch.setattr("app.api.endpoints.hank_intake.enqueue_intake", lambda *args: False)
    return backend


class TestHankIntakeRaise:
    def _file(self, db_session, user, plan_fields):
        from app.schemas.hank_intake import IntakePlanInput
        from app.services.hank_intake_service import HankIntakeService
        from tests.api import test_hank_intake as intake_tests

        service = HankIntakeService(db_session, user, 1)
        ready = intake_tests.reviewed(
            service,
            plan=IntakePlanInput(title="Filed print", document_type=DocumentType.DRAWING, **plan_fields),
        )
        receipt = service.response_file(
            service.execute(ready.id, intake_tests.command(ready), intake_tests.audit(service))
        )
        service.db.commit()
        db_session.expire_all()
        return receipt.result, db_session.get(Document, receipt.result.document_id)

    @pytest.mark.parametrize("link", ["part_id", "work_order_id"])
    def test_a_filing_linked_to_an_export_controlled_customer_is_filed_export_controlled(
        self, db_session, people, intake_storage, link
    ):
        _customer(db_session, "Synthetic Defense Co")
        if link == "part_id":
            target = _part(db_session, customer_name="Synthetic Defense Co")
        else:
            target = _work_order_for(db_session, "Synthetic Defense Co")
        result, document = self._file(db_session, people["manager"], {link: target.id})
        assert document.access_level == EXPORT
        assert any("export_controlled" in warning for warning in result.warnings), result.warnings
        (created,) = _audit_rows(db_session, "document", document.id, "CREATE")
        assert created.new_values["access_level"] == EXPORT
        assert created.extra_data["access_level_raised_because"] == ["export_controlled_customer"]

    def test_an_uncontrolled_filing_stays_standard(self, db_session, people, intake_storage):
        part = _part(db_session)
        result, document = self._file(db_session, people["manager"], {"part_id": part.id})
        assert document.access_level == STANDARD
        assert not any("export_controlled" in warning for warning in result.warnings)
        (created,) = _audit_rows(db_session, "document", document.id, "CREATE")
        assert "access_level_raised_because" not in (created.extra_data or {})


# =========================================================================== #
# Deactivation retires an export authorization
# =========================================================================== #


class TestDeactivationRevokesExportAuthorization:
    def _assert_retired(self, db_session, user_id: int, actor_id: int) -> None:
        db_session.expire_all()
        user = db_session.get(User, user_id)
        assert (user.is_active, user.export_authorized) == (False, False)
        assert (user.export_authorized_at, user.export_authorized_by) == (None, None)
        (row,) = _audit_rows(db_session, "user", user_id, "EXPORT_AUTHORIZATION_REVOKE")
        assert row.user_id == actor_id
        assert (row.old_values, row.new_values) == ({"export_authorized": True}, {"export_authorized": False})
        assert row.extra_data["reason"] == "user deactivated" and row.extra_data["source"] == "deactivation"
        assert row.extra_data["revoked_grant"]["export_authorized_by"] is not None

    def _granted(self, client, people, persona: str) -> User:
        assert _authorize(client, _h(people, "admin"), people[persona].id).status_code == 200
        return people[persona]

    def test_delete_revokes_it_and_reactivation_restores_nothing(self, client, db_session, people):
        user = self._granted(client, people, "quality")
        document = _document(db_session, EXPORT)
        response = client.delete(f"{API}/users/{user.id}", headers=_h(people, "admin"))
        assert response.status_code == 200, response.text
        assert response.json()["export_authorization_revoked"] is True
        self._assert_retired(db_session, user.id, people["admin"].id)
        reactivated = client.post(f"{API}/users/{user.id}/activate", headers=_h(people, "admin"))
        assert reactivated.status_code == 200, reactivated.text
        db_session.expire_all()
        user = db_session.get(User, user.id)
        assert user.is_active is True and user.export_authorized is False
        assert client.get(f"{API}/documents/{document.id}", headers=_h(people, "quality")).status_code == 404

    def test_put_is_active_false_revokes_it_too(self, client, db_session, people):
        user = self._granted(client, people, "manager")
        response = client.put(f"{API}/users/{user.id}", headers=_h(people, "admin"), json={"is_active": False})
        assert response.status_code == 200, response.text
        assert response.json()["export_authorized"] is False
        self._assert_retired(db_session, user.id, people["admin"].id)
        restored = client.put(f"{API}/users/{user.id}", headers=_h(people, "admin"), json={"is_active": True})
        assert restored.status_code == 200, restored.text
        assert restored.json()["is_active"] is True and restored.json()["export_authorized"] is False

    def test_deactivating_a_user_with_no_authorization_writes_no_revoke_row(self, client, db_session, people):
        target = people["operator"]
        response = client.delete(f"{API}/users/{target.id}", headers=_h(people, "admin"))
        assert response.status_code == 200, response.text
        assert response.json()["export_authorization_revoked"] is False
        assert _audit_rows(db_session, "user", target.id, "EXPORT_AUTHORIZATION_REVOKE") == []

    def test_a_deactivation_whose_revoke_cannot_be_audited_changes_nothing(
        self, client, db_session, people, monkeypatch
    ):
        user = self._granted(client, people, "quality")
        _fail_required_audit(monkeypatch)
        response = client.delete(f"{API}/users/{user.id}", headers=_h(people, "admin"))
        assert response.status_code == 503, response.text
        db_session.expire_all()
        stored = db_session.get(User, user.id)
        assert (stored.is_active, stored.export_authorized) == (True, True)

    def test_the_generic_user_update_cannot_grant_it(self, client, db_session, people):
        """``export_authorized`` is not a field of ``PUT /users/{id}``: the reasoned verb is the only door."""
        target = people["operator"]
        response = client.put(
            f"{API}/users/{target.id}",
            headers=_h(people, "admin"),
            json={"first_name": "Renamed", "export_authorized": True},
        )
        assert response.status_code in (200, 422), response.text
        db_session.expire_all()
        assert db_session.get(User, target.id).export_authorized is False
        assert _audit_rows(db_session, "user", target.id, "EXPORT_AUTHORIZATION_GRANT") == []


# =========================================================================== #
# QMS auto-link persists standard-only document counts
# =========================================================================== #


class TestQmsAutoLinkStoresStandardOnlyCounts:
    def _standard_with_document_clause(self, db_session):
        from app.models.qms_standard import QMSClause, QMSStandard

        standard = QMSStandard(name=f"AS9100D-{_next()}", version="D", is_active=True, company_id=1)
        db_session.add(standard)
        db_session.flush()
        clause = QMSClause(
            standard_id=standard.id,
            company_id=1,
            clause_number="7.5",
            title="Documented information",
            description="Document control of every procedure and work instruction",
        )
        db_session.add(clause)
        db_session.commit()
        for level in (STANDARD, RESTRICTED, EXPORT):
            _document(db_session, level, document_type=DocumentType.PROCEDURE)
        return standard, clause

    def _stored(self, db_session, clause_id):
        from app.models.qms_standard import QMSClauseEvidence

        db_session.expire_all()
        return (
            db_session.query(QMSClauseEvidence)
            .filter(
                QMSClauseEvidence.clause_id == clause_id,
                QMSClauseEvidence.auto_link_query == "document_control",
                QMSClauseEvidence.is_deleted == False,  # noqa: E712
            )
            .one()
        )

    def test_the_stored_count_ignores_who_clicked(self, client, db_session, people):
        standard, clause = self._standard_with_document_clause(db_session)
        # The authorized Admin opens all three...
        live = client.get(
            f"{API}/qms-standards/clauses/{clause.id}/auto-evidence", headers=_h(people, "cleared_admin")
        ).json()
        (block,) = [row for row in live["discovered_evidence"] if row["title"].startswith("Document Control")]
        assert block["total_count"] == 3
        # ...but what auto-link stores counts standard documents only, and says so.
        for persona in ("cleared_admin", "quality"):
            response = client.post(f"{API}/qms-standards/{standard.id}/auto-link", headers=_h(people, persona))
            assert response.status_code == 200, response.text
            stored = self._stored(db_session, clause.id)
            assert stored.live_count == 1, persona
            assert "1 total standard documents" in stored.description
            assert "restricted and export-controlled documents are not counted" in stored.description
