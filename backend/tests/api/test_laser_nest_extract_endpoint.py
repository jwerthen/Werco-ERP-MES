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


def _runs_result(planned_runs, *, confidence="high", warning=None) -> dict:
    """An otherwise clean, two-pass extraction result carrying ``planned_runs``."""
    return {
        "cnc_number": "05749",
        "material": "A36",
        "thickness": "0.25in",
        "sheet_size": "72.5x120",
        "planned_runs": planned_runs,
        "confidence": {"cnc_number": "high", "material": "high", "planned_runs": confidence},
        "extraction_confidence": confidence,
        "source": "ai",
        "warning": warning,
        "passes": 2,
    }


def _stub_runs(monkeypatch, planned_runs, *, confidence="high", warning=None):
    """Stub the service with ``_runs_result(planned_runs, ...)``."""
    result = _runs_result(planned_runs, confidence=confidence, warning=warning)
    monkeypatch.setattr(
        laser_nests_endpoint,
        "extract_nest_fields_from_pdf",
        lambda pdf_path, file_name, company_id=None: dict(result),
    )


class TestExtractPlannedRuns:
    """The run count is AI output and never reaches the response raw.

    It used to be handed straight to the response model: "N/A", "3 sheets" or
    2.5 failed its int check as a 500 (which the manual modal swallowed, losing
    every other pre-filled field with it), and an 11-digit misread pre-filled the
    form with a count the manual-create route now refuses. A count that is not a
    clean 1..9,999 is now a real null at low confidence, by the same reader the
    package preview uses.
    """

    @pytest.mark.parametrize("raw", ["N/A", 2.5, "2.5", "sheet 4", 0])
    def test_an_unreadable_count_is_null_and_low(self, client, db_session, monkeypatch, raw):
        _stub_runs(monkeypatch, raw)
        resp = _post_extract(client, headers_for(make_user(db_session, role=UserRole.ADMIN)))

        assert resp.status_code == status.HTTP_200_OK, resp.text
        body = resp.json()
        assert body["planned_runs"] is None
        assert body["confidence"] == "low"
        assert body["warning"] is None
        # Everything else still arrives for the modal to pre-fill.
        assert (body["cnc_number"], body["material"], body["sheet_size"]) == ("05749", "A36", "72.5x120")

    @pytest.mark.parametrize("raw,expected", [("3 sheets", 3), ("x3", 3), (3.0, 3), (9_999, 9_999)])
    def test_a_clean_count_in_any_shape_is_read(self, client, db_session, monkeypatch, raw, expected):
        _stub_runs(monkeypatch, raw)
        resp = _post_extract(client, headers_for(make_user(db_session, role=UserRole.ADMIN)))

        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["planned_runs"] == expected
        assert resp.json()["confidence"] == "high"

    @pytest.mark.parametrize("raw", [10_000, 99_999_999_999])
    def test_a_count_over_the_bound_is_null_low_and_explained(self, client, db_session, monkeypatch, raw):
        _stub_runs(monkeypatch, raw, warning="Verification pass skipped: API error")
        resp = _post_extract(client, headers_for(make_user(db_session, role=UserRole.ADMIN)))

        assert resp.status_code == status.HTTP_200_OK, resp.text
        body = resp.json()
        assert body["planned_runs"] is None
        assert body["confidence"] == "low"
        # The service's own warning is kept, and the bound's is added to it.
        assert body["warning"].startswith(f"Verification pass skipped: API error; Run count '{raw}' looked wrong")


class TestExtractSharesThePreviewRule:
    """ONE not-read rule behind both doors.

    The preview row (``build_parsed_nest_from_extraction``) and this response both take
    their run count, confidence and warning from ``read_extracted_planned_runs`` in
    ``laser_nest_service``. A second copy of that rule in the router could drift from
    the row's, which is the gap these two tests close: the first proves the handler
    delegates, the second that the two doors give the same answer for every shape.
    """

    def test_the_handler_delegates_to_the_service_rule(self, client, db_session, monkeypatch):
        from app.services import laser_nest_service

        real = laser_nest_service.read_extracted_planned_runs
        seen = []

        def spy(result):
            seen.append(result.get("planned_runs"))
            return real(result)

        monkeypatch.setattr(laser_nests_endpoint, "read_extracted_planned_runs", spy)
        _stub_runs(monkeypatch, "N/A")
        resp = _post_extract(client, headers_for(make_user(db_session, role=UserRole.ADMIN)))

        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert seen == ["N/A"]
        assert resp.json()["planned_runs"] is None

    @pytest.mark.parametrize(
        "raw,warning",
        [
            (3, None),
            ("3 sheets", None),
            (9_999, "Verification pass skipped: API error"),
            ("N/A", None),
            (None, "Verification pass skipped: API error"),
            (0, None),
            (2.5, None),
            (10_000, None),
            (99_999_999_999, "Verification pass skipped: API error"),
        ],
    )
    def test_the_response_and_the_preview_row_agree(self, client, db_session, monkeypatch, raw, warning):
        from app.services.laser_nest_service import build_parsed_nest_from_extraction

        _stub_runs(monkeypatch, raw, warning=warning)
        resp = _post_extract(client, headers_for(make_user(db_session, role=UserRole.ADMIN)))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        body = resp.json()
        row = build_parsed_nest_from_extraction(
            _runs_result(raw, warning=warning), abs_path="/tmp/05749.pdf", rel_path="05749.pdf"
        )

        assert body["confidence"] == row.confidence
        assert body["warning"] == row.warning
        if body["planned_runs"] is None:
            # Not read: the row shows the same fact as a floored 1 flagged low.
            assert (row.planned_runs, row.field_confidence["planned_runs"]) == (1, "low")
        else:
            assert body["planned_runs"] == row.planned_runs
            assert row.field_confidence["planned_runs"] == "high"


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
