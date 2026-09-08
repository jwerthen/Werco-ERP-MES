"""Focused development tests; independent API/concurrency review has separate coverage."""

import json
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.db.database import atomic_transaction
from app.models.audit_log import AuditLog
from app.models.quote_nesting_draft import QuoteNestingDraft, QuoteNestingRevision
from app.services.audit_service import AuditService, AuditWriteError
from app.services.quote_nesting_drafts import get_revision, list_drafts, parse_estimate, save_revision


def project():
    return {
        "version": 4,
        "units": "in",
        "currency": "USD",
        "name": "Test inputs",
        "activeGroupId": "g",
        "groups": [
            {
                "id": "g",
                "quote": {
                    "version": 3,
                    "units": "in",
                    "currency": "USD",
                    "name": "Carbon sheet",
                    "material": "Carbon steel",
                    "thickness": 0.125,
                    "margin": 0.375,
                    "gap": 0.125,
                    "objective": "area",
                    "spacingMode": "auto",
                    "options": [{"id": "s", "width": 96, "height": 48, "enabled": True, "price": None}],
                    "parts": [
                        {
                            "id": "p",
                            "name": "Circle",
                            "quantity": 1,
                            "rotate": True,
                            "color": 0,
                            "loops": [{"type": "circle", "cx": 1, "cy": 1, "r": 1}],
                        }
                    ],
                },
            }
        ],
    }


def _save(db, user, data=None, **kwargs):
    with atomic_transaction(db):
        return save_revision(
            db,
            user,
            1,
            AuditService(db, user),
            content=json.dumps(data or project()).encode(),
            request_key=kwargs.pop("request_key", str(uuid4())),
            expected_company_id=1,
            **kwargs,
        )


def test_create_replay_append_and_historical_conflict(db_session, admin_user):
    key = str(uuid4())
    first = _save(db_session, admin_user, request_key=key)
    assert _save(db_session, admin_user, request_key=key) == first
    assert db_session.query(QuoteNestingRevision).count() == 1
    data = project()
    data["name"] = "Updated input name"
    second = _save(db_session, admin_user, data, draft_id=first["draft_id"], expected_version=1)
    assert second["draft_version"] == 2
    assert get_revision(db_session, 1, first["draft_id"], 1)["name"] == "Test inputs"
    with pytest.raises(HTTPException) as error:
        _save(db_session, admin_user, draft_id=first["draft_id"], expected_version=1)
    assert error.value.status_code == 409
    assert db_session.query(QuoteNestingRevision).count() == 2
    assert db_session.query(AuditLog).filter(AuditLog.resource_type == "quote_nesting_revision").count() == 2


def test_strict_audit_failure_rolls_back_header_and_revision(db_session, admin_user, monkeypatch):
    monkeypatch.setattr(AuditService, "log", lambda *args, **kwargs: None)
    with pytest.raises(AuditWriteError):
        _save(db_session, admin_user)
    assert db_session.query(QuoteNestingDraft).count() == 0
    assert db_session.query(QuoteNestingRevision).count() == 0


def test_company_pin_refuses_even_unbound_project(db_session, admin_user):
    with pytest.raises(HTTPException) as error:
        with atomic_transaction(db_session):
            save_revision(
                db_session,
                admin_user,
                1,
                AuditService(db_session, admin_user),
                content=json.dumps(project()).encode(),
                request_key=str(uuid4()),
                expected_company_id=2,
            )
    assert error.value.status_code == 409
    assert db_session.query(QuoteNestingDraft).count() == 0


def test_lists_do_not_include_geometry_or_create_audit(db_session, admin_user):
    first = _save(db_session, admin_user)
    before = db_session.query(AuditLog).count()
    result = list_drafts(db_session, 1, page=1, per_page=20)
    assert result["items"][0]["draft_id"] == first["draft_id"]
    assert "estimate" not in result["items"][0]
    assert db_session.query(AuditLog).count() == before


@pytest.mark.parametrize("content", [b'{"version":4,"version":5}', b'{"groups":NaN}', b"[" * 10000 + b"]" * 10000])
def test_malformed_json_is_bounded_and_actionable(content):
    with pytest.raises(HTTPException) as error:
        parse_estimate(content)
    assert error.value.status_code == 422


def test_outputs_cannot_be_disguised_as_saved_inputs():
    data = project()
    data["approved"] = True
    with pytest.raises(HTTPException):
        parse_estimate(json.dumps(data).encode())


def test_real_multipart_route(client, admin_headers):
    response = client.post(
        "/api/v1/quote-nesting/drafts",
        headers=admin_headers,
        files={"estimate": ("estimate.json", json.dumps(project()), "application/json")},
        data={"request_key": str(uuid4()), "expected_company_id": "1"},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["schema_version"] == 1
    assert result["estimate"] == project()
    assert result["status"] == "DRAFT"
    assert result["review_issues"][0]["code"] == "unapproved_client_snapshot"
    listing = client.get("/api/v1/quote-nesting/drafts", headers=admin_headers)
    assert listing.status_code == 200, listing.text
    assert listing.json()["schema_version"] == 1
