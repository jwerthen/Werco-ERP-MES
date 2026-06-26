"""POST /laser-nests/extract -- single-PDF auto-extract endpoint.

Stateless extract endpoint that feeds the manual-modal auto-fill. The underlying
extraction service is mocked at the endpoint's import site, so no real PDF
parsing or Anthropic call happens (offline by contract). Covers the happy-path
field mapping (``confidence`` from ``extraction_confidence``), the non-PDF 400,
RBAC (operator 403 / write-roles allowed), and the unauthenticated 401.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.api.endpoints.laser_nests as laser_nests_endpoint
from app.core.security import create_access_token
from app.models.company import Company
from app.models.user import User, UserRole

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True))
        db.commit()


def make_user(db: Session, *, role: UserRole, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"extract-{n}@co{company_id}.test",
        employee_id=f"EXTR-{n:05d}",
        first_name="Extract",
        last_name=f"Co{company_id}",
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=True,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


@pytest.fixture
def mock_extraction(monkeypatch):
    """Stub the extraction service the endpoint imports.

    Returns the captured-args holder so a test can assert what the endpoint
    passed through (file_name, company_id). The default return mirrors a clean
    AI extraction with an overall ``extraction_confidence`` of "high".
    """
    captured = {}

    def _fake_extract(pdf_path, file_name, company_id=None):
        captured["pdf_path"] = pdf_path
        captured["file_name"] = file_name
        captured["company_id"] = company_id
        return {
            "cnc_number": "05749",
            "material": "A36",
            "thickness": "0.25in",
            "sheet_size": "72.5x120",
            "planned_runs": 3,
            "confidence": {"cnc_number": "high"},
            "extraction_confidence": "high",
            "source": "ai",
            "warning": None,
            "_extraction_metadata": {"model": "claude-stub"},
        }

    monkeypatch.setattr(laser_nests_endpoint, "extract_nest_fields_from_pdf", _fake_extract)
    return captured


def _post_extract(
    client: TestClient, headers: dict, *, name="05749.pdf", mime="application/pdf", content=b"%PDF-1.4\n"
):
    return client.post(
        "/api/v1/laser-nests/extract",
        headers=headers,
        files={"file": (name, content, mime)},
    )


class TestExtractHappyPath:
    def test_returns_mapped_fields(self, client, db_session, mock_extraction):
        headers = headers_for(make_user(db_session, role=UserRole.ADMIN))
        resp = _post_extract(client, headers)

        assert resp.status_code == status.HTTP_200_OK, resp.text
        body = resp.json()
        assert body["cnc_number"] == "05749"
        assert body["material"] == "A36"
        assert body["thickness"] == "0.25in"
        assert body["sheet_size"] == "72.5x120"
        assert body["planned_runs"] == 3
        # confidence is mapped from the service's extraction_confidence key.
        assert body["confidence"] == "high"
        assert body["source"] == "ai"
        assert body["warning"] is None

    def test_passes_filename_and_company_to_service(self, client, db_session, mock_extraction):
        user = make_user(db_session, role=UserRole.MANAGER)
        resp = _post_extract(client, headers_for(user), name="NEST-001.pdf")

        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert mock_extraction["file_name"] == "NEST-001.pdf"
        # company_id flows through for tenant-scoped AI-usage telemetry.
        assert mock_extraction["company_id"] == user.company_id

    def test_warning_surfaced_on_degraded_extraction(self, client, db_session, monkeypatch):
        """When the service degrades (filename-only result with a warning), the
        endpoint must surface source=="filename" and the warning verbatim."""
        monkeypatch.setattr(
            laser_nests_endpoint,
            "extract_nest_fields_from_pdf",
            lambda pdf_path, file_name, company_id=None: {
                "cnc_number": "05749",
                "material": None,
                "thickness": None,
                "sheet_size": None,
                "planned_runs": None,
                "extraction_confidence": "low",
                "source": "filename",
                "warning": "API key not configured",
            },
        )
        headers = headers_for(make_user(db_session, role=UserRole.ADMIN))
        resp = _post_extract(client, headers)

        assert resp.status_code == status.HTTP_200_OK, resp.text
        body = resp.json()
        assert body["source"] == "filename"
        assert body["confidence"] == "low"
        assert body["warning"] == "API key not configured"


class TestExtractValidation:
    def test_non_pdf_rejected_400(self, client, db_session, mock_extraction):
        headers = headers_for(make_user(db_session, role=UserRole.ADMIN))
        resp = _post_extract(client, headers, name="notes.txt", mime="text/plain")

        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert "PDF" in resp.json()["detail"]
        # The extraction service must NOT be called for a rejected upload.
        assert mock_extraction == {}


class TestExtractRBAC:
    @pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.VIEWER])
    def test_non_write_roles_forbidden(self, client, db_session, mock_extraction, role):
        headers = headers_for(make_user(db_session, role=role))
        resp = _post_extract(client, headers)
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    @pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])
    def test_write_roles_allowed(self, client, db_session, mock_extraction, role):
        headers = headers_for(make_user(db_session, role=role))
        resp = _post_extract(client, headers)
        assert resp.status_code == status.HTTP_200_OK, resp.text

    def test_unauthenticated_401(self, client, db_session, mock_extraction):
        resp = client.post(
            "/api/v1/laser-nests/extract",
            files={"file": ("05749.pdf", b"%PDF-1.4\n", "application/pdf")},
        )
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED
