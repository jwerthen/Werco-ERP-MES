"""Partial-read warnings reach the user (roadmap #9-3a; owner decision 2026-11-09).

OCR now reads at most 30 pages within 90 seconds, and the text-layer reader has page, size and
time limits. When either reads only part of a document, the owner wants a VISIBLE warning, not a
log line. Each upload route that feeds extracted text to the AI carries those warnings in its
API response, where the existing UI already renders them:

* PO / quote / invoice upload: ``validation_issues`` entries with ``severity: "warning"`` and
  ``code: "OCR_PARTIAL"`` / ``"TEXT_PARTIAL"`` (the "Issues to Review" panel);
* routing generation: ``warnings`` (the generation result's warning list);
* BOM import preview and one-shot import: ``warnings``; and when no usable text came out at
  all, the 400 detail names the partial read that explains it;
* QMS clause upload has no warning channel (it returns the clause list), so a partial read that
  would shorten what the AI sees is refused with a 400 naming the skip.

The extraction seam is stubbed with a duck-typed result (``SimpleNamespace``) so these tests pin
the ROUTES' handling; ``tests/services/test_pdf_ocr_bounds.py`` and ``test_pdf_text_reader.py``
pin where the warnings come from.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from sqlalchemy.orm import Session

from app.models.part import Part
from app.models.qms_standard import QMSClause, QMSStandard
from app.models.work_center import WorkCenter

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

OCR_CAP_MESSAGE = "Only the first 30 of 120 scanned pages were read."
OCR_PARTIAL = SimpleNamespace(code="OCR_PARTIAL", message=OCR_CAP_MESSAGE)
TEXT_PARTIAL = SimpleNamespace(code="TEXT_PARTIAL", message="Only the first 500 of 640 pages were read.")


def _extraction(text: str, *warnings, is_ocr: bool = True, file_type: str = "pdf"):
    return SimpleNamespace(
        text=text,
        is_ocr=is_ocr,
        page_count=120,
        confidence="medium",
        file_type=file_type,
        warnings=list(warnings),
    )


def _pdf(pages) -> bytes:
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=letter)
    for lines in pages:
        for row, line in enumerate(lines):
            pdf.drawString(72, 720 - row * 18, line)
        pdf.showPage()
    pdf.save()
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# PO / quote / invoice upload
# --------------------------------------------------------------------------- #
_PO_LLM_RESULT = {
    "document_type": "po",
    "po_number": "PO-PARTIAL-1",
    "vendor": {"name": "Acme Metals"},
    "line_items": [{"line_number": 1, "part_number": "AL-1", "description": "Sheet", "qty_ordered": 2}],
    "extraction_confidence": "high",
}


@pytest.fixture
def po_seams(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # the local storage backend writes under ./uploads
    calls = {}

    def fake_llm(text, is_ocr=False, document_type="po", company_id=None):
        calls["text"] = text
        return dict(_PO_LLM_RESULT)

    monkeypatch.setattr("app.api.endpoints.po_upload.extract_po_data_with_llm", fake_llm)
    return calls


def _upload_po(client, headers, path="/api/v1/po-upload/upload-po"):
    return client.post(path, headers=headers, files={"file": ("scan.pdf", b"%PDF-1.4 scan", "application/pdf")})


@pytest.mark.parametrize(
    "path", ["/api/v1/po-upload/upload-po", "/api/v1/po-upload/upload-quote", "/api/v1/po-upload/upload-invoice"]
)
def test_po_upload_surfaces_a_partial_ocr_read_as_a_warning_issue(
    client: TestClient, auth_headers, po_seams, monkeypatch, path
):
    monkeypatch.setattr(
        "app.api.endpoints.po_upload.extract_text_from_document",
        lambda doc_path: _extraction("PURCHASE ORDER PO-PARTIAL-1 " * 10, OCR_PARTIAL),
    )

    response = _upload_po(client, auth_headers, path)

    assert response.status_code == status.HTTP_200_OK, response.text
    issues = response.json()["validation_issues"]
    assert issues[0] == {
        "field": "document_content",
        "severity": "warning",
        "code": "OCR_PARTIAL",
        "message": OCR_CAP_MESSAGE,
    }


def test_po_upload_carries_each_warning_code(client: TestClient, auth_headers, po_seams, monkeypatch):
    monkeypatch.setattr(
        "app.api.endpoints.po_upload.extract_text_from_document",
        lambda doc_path: _extraction("PURCHASE ORDER PO-PARTIAL-1 " * 10, TEXT_PARTIAL, OCR_PARTIAL),
    )

    issues = _upload_po(client, auth_headers).json()["validation_issues"]

    assert [(i.get("code"), i["severity"]) for i in issues[:2]] == [
        ("TEXT_PARTIAL", "warning"),
        ("OCR_PARTIAL", "warning"),
    ]


def test_po_upload_with_too_little_text_still_names_the_partial_read(
    client: TestClient, auth_headers, po_seams, monkeypatch
):
    """The early return (no usable text) used to carry only the generic error."""
    monkeypatch.setattr(
        "app.api.endpoints.po_upload.extract_text_from_document", lambda doc_path: _extraction("", OCR_PARTIAL)
    )

    response = _upload_po(client, auth_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    issues = response.json()["validation_issues"]
    assert issues[0]["severity"] == "error"
    assert issues[1] == {
        "field": "document_content",
        "severity": "warning",
        "code": "OCR_PARTIAL",
        "message": OCR_CAP_MESSAGE,
    }
    assert "text" not in po_seams, "no AI call without usable text"


def test_po_upload_without_a_partial_read_adds_no_issue(client: TestClient, auth_headers, po_seams, monkeypatch):
    monkeypatch.setattr(
        "app.api.endpoints.po_upload.extract_text_from_document",
        lambda doc_path: _extraction("PURCHASE ORDER PO-PARTIAL-1 " * 10, is_ocr=False),
    )

    issues = _upload_po(client, auth_headers).json()["validation_issues"]

    assert all(issue.get("code") not in ("OCR_PARTIAL", "TEXT_PARTIAL") for issue in issues)


def test_po_upload_ai_error_still_names_the_partial_read(client: TestClient, auth_headers, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "app.api.endpoints.po_upload.extract_text_from_document",
        lambda doc_path: _extraction("PURCHASE ORDER PO-PARTIAL-1 " * 10, OCR_PARTIAL),
    )
    monkeypatch.setattr(
        "app.api.endpoints.po_upload.extract_po_data_with_llm",
        lambda text, is_ocr=False, document_type="po", company_id=None: {"_error": "AI extraction failed"},
    )

    response = _upload_po(client, auth_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    issues = response.json()["validation_issues"]
    assert [(issue["severity"], issue.get("code")) for issue in issues] == [("error", None), ("warning", "OCR_PARTIAL")]


# --------------------------------------------------------------------------- #
# Routing generation
# --------------------------------------------------------------------------- #
def test_routing_generation_surfaces_a_partial_ocr_read(
    client: TestClient, admin_headers, db_session: Session, monkeypatch, tmp_path
):
    import app.services.pdf_service as pdf_service
    import app.services.routing_generation_service as generation_service

    monkeypatch.chdir(tmp_path)  # the endpoint writes the upload under ./uploads
    monkeypatch.setattr(
        pdf_service, "extract_text_from_pdf", lambda path: _extraction("DRAWING NOTES REV C " * 10, OCR_PARTIAL)
    )
    monkeypatch.setattr(
        generation_service,
        "generate_draft_routing",
        lambda **kwargs: {
            "part_info": {},
            "operations": [{"sequence": 10, "operation_name": "Cut", "work_center_type": "laser"}],
            "extraction_confidence": "medium",
            "warnings": ["model note"],
            "_extraction_metadata": {
                "model": "m",
                "model_tier": "standard",
                "prompt_version": "1",
                "source_was_ocr": True,
            },
        },
    )
    part = Part(part_number="OCR-PARTIAL-RT-1", name="Bracket", part_type="manufactured", unit_of_measure="each")
    part.company_id = 1
    center = WorkCenter(code="OCR-PARTIAL-WC", name="Laser", work_center_type="laser", is_active=True)
    center.company_id = 1
    db_session.add_all([part, center])
    db_session.commit()

    response = client.post(
        "/api/v1/routing/generate-from-drawing",
        headers=admin_headers,
        data={"part_id": str(part.id)},
        files={"file": ("drawing.pdf", b"%PDF-1.4 scan", "application/pdf")},
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    warnings = response.json()["warnings"]
    assert OCR_CAP_MESSAGE in warnings
    assert "model note" in warnings


# --------------------------------------------------------------------------- #
# BOM import
# --------------------------------------------------------------------------- #
_BOM_LLM_RESULT = {
    "document_type": "bom",
    "assembly": {"part_number": "ASSY-OCR-PARTIAL", "name": "Partial Assembly", "revision": "A"},
    "items": [
        {
            "line_number": 10,
            "part_number": "COMP-OCR-PARTIAL",
            "description": "Bracket",
            "quantity": 2,
            "item_type": "make",
            "line_type": "component",
        }
    ],
    "extraction_confidence": "high",
}


@pytest.fixture
def bom_seams(monkeypatch):
    monkeypatch.setattr("app.api.endpoints.bom.save_uploaded_document", lambda content, filename: "/tmp/fake.pdf")
    monkeypatch.setattr(
        "app.api.endpoints.bom.extract_bom_data_with_llm",
        lambda text, is_ocr=False, company_id=None: dict(_BOM_LLM_RESULT),
    )


def _bom_post(client, headers, path):
    return client.post(path, headers=headers, files={"file": ("bom.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")})


def test_bom_preview_surfaces_a_partial_ocr_read(client: TestClient, auth_headers, bom_seams, monkeypatch):
    monkeypatch.setattr(
        "app.api.endpoints.bom.extract_text_from_document",
        lambda path: _extraction("BILL OF MATERIALS ASSY " * 10, OCR_PARTIAL),
    )

    response = _bom_post(client, auth_headers, "/api/v1/bom/import/preview")

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["warnings"][0] == OCR_CAP_MESSAGE


def test_bom_one_shot_import_surfaces_a_partial_ocr_read(client: TestClient, auth_headers, bom_seams, monkeypatch):
    monkeypatch.setattr(
        "app.api.endpoints.bom.extract_text_from_document",
        lambda path: _extraction("BILL OF MATERIALS ASSY " * 10, OCR_PARTIAL),
    )

    response = _bom_post(client, auth_headers, "/api/v1/bom/import")

    assert response.status_code == status.HTTP_201_CREATED, response.text
    assert OCR_CAP_MESSAGE in response.json()["warnings"]


@pytest.mark.parametrize("path", ["/api/v1/bom/import/preview", "/api/v1/bom/import"])
def test_bom_upload_without_text_names_the_partial_read_in_its_400(
    client: TestClient, auth_headers, bom_seams, monkeypatch, path
):
    monkeypatch.setattr("app.api.endpoints.bom.extract_text_from_document", lambda p: _extraction("", OCR_PARTIAL))

    response = _bom_post(client, auth_headers, path)

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == f"Could not extract text from document. {OCR_CAP_MESSAGE}"


# --------------------------------------------------------------------------- #
# QMS clause upload (real subprocess reader)
# --------------------------------------------------------------------------- #
@pytest.fixture
def qms_standard(db_session: Session) -> QMSStandard:
    standard = QMSStandard(name="Partial Read Manual", version="2015", standard_body="ISO", is_active=True)
    standard.company_id = 1
    db_session.add(standard)
    db_session.commit()
    db_session.refresh(standard)
    return standard


@pytest.fixture
def qms_llm(monkeypatch):
    calls = []

    def fake_run_llm_task(context, **kwargs):
        calls.append({"context": context, **kwargs})
        return SimpleNamespace(
            text='[{"clause_number": "4.1", "title": "Context", "description": "Determine issues."}]',
            model="claude-test",
            model_selection_reason="test",
        )

    monkeypatch.setattr("app.api.endpoints.qms_standards.run_llm_task", fake_run_llm_task)
    return calls


QMS_PAGES = [
    ["4.1 Understanding the organization and its context"],
    ["4.2 Understanding the needs of interested parties"],
    ["4.3 Determining the scope of the quality management system"],
]


def _upload_qms(client, headers, standard_id, content):
    return client.post(
        f"/api/v1/qms-standards/{standard_id}/upload-pdf",
        headers=headers,
        files={"file": ("manual.pdf", content, "application/pdf")},
    )


def test_qms_upload_sends_the_ai_the_same_text_as_before(
    client: TestClient, admin_headers, qms_standard, qms_llm, db_session: Session
):
    from pypdf import PdfReader

    content = _pdf(QMS_PAGES)
    old_text = "\n\n".join(t for t in (p.extract_text() for p in PdfReader(io.BytesIO(content)).pages) if t)

    response = _upload_qms(client, admin_headers, qms_standard.id, content)

    assert response.status_code == status.HTTP_200_OK, response.text
    (call,) = qms_llm
    assert call["context"].input_chars == len(old_text)
    assert f"---\n{old_text}\n---" in call["messages"][0]["content"]


def test_qms_upload_refuses_a_partial_read_that_would_drop_clauses(
    client: TestClient, admin_headers, qms_standard, qms_llm, db_session: Session, monkeypatch
):
    monkeypatch.setattr("app.api.endpoints.qms_standards.QMS_PDF_MAX_PAGES", 2, raising=False)

    response = _upload_qms(client, admin_headers, qms_standard.id, _pdf(QMS_PAGES))

    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
    assert response.json()["detail"] == (
        "Only part of this PDF could be read: Only the first 2 of 3 pages were read. "
        "Split it into smaller PDFs and upload each one."
    )
    assert qms_llm == [], "no AI call on a partial read"
    db_session.expire_all()
    assert db_session.query(QMSClause).filter(QMSClause.standard_id == qms_standard.id).count() == 0


def test_qms_upload_of_an_unreadable_pdf_is_a_plain_400(client: TestClient, admin_headers, qms_standard, qms_llm):
    response = _upload_qms(client, admin_headers, qms_standard.id, b"%PDF-1.7\nnot a pdf body\n%%EOF\n")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "Failed to read PDF: The PDF could not be read. It may be damaged or not a PDF."
    assert qms_llm == []


def _stub_qms_read(monkeypatch, pages, warnings, *, page_count=5, stopped_on_error=False):
    from app.services.pdf_text_reader import PdfReadResult

    monkeypatch.setattr(
        "app.api.endpoints.qms_standards.read_pdf_pages",
        lambda content, limits: PdfReadResult(
            pages=list(pages), page_count=page_count, warnings=list(warnings), stopped_on_error=stopped_on_error
        ),
    )


def test_qms_upload_goes_ahead_when_a_partial_read_covers_the_prompt_window(
    client: TestClient, admin_headers, qms_standard, qms_llm, monkeypatch
):
    """Review F3(b). The AI is sent only the first 100,000 characters, so a partial read that
    already reached them changes nothing it sees: the upload proceeds, with exactly one AI call.
    The joined text here is exactly 100,000 characters (the boundary)."""
    pages = ["A" * 49_999, "", "B" * 49_999]
    _stub_qms_read(monkeypatch, pages, ["Only the first 3 of 5 pages were read."])

    response = _upload_qms(client, admin_headers, qms_standard.id, b"%PDF-1.4 stand-in")

    assert response.status_code == status.HTTP_200_OK, response.text
    (call,) = qms_llm
    expected = "A" * 49_999 + "\n\n" + "B" * 49_999
    assert len(expected) == 100_000
    assert f"---\n{expected}\n---" in call["messages"][0]["content"]


def test_qms_upload_one_character_short_of_the_window_is_still_refused(
    client: TestClient, admin_headers, qms_standard, qms_llm, monkeypatch
):
    _stub_qms_read(monkeypatch, ["A" * 49_999, "B" * 49_998], ["Only the first 2 of 5 pages were read."])

    response = _upload_qms(client, admin_headers, qms_standard.id, b"%PDF-1.4 stand-in")

    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
    assert qms_llm == []


def test_qms_upload_that_broke_part_way_asks_for_a_new_copy_not_a_split(
    client: TestClient, admin_headers, qms_standard, qms_llm, monkeypatch
):
    """Review F7: splitting does not help a damaged page; re-saving the PDF can."""
    warning = "Reading the PDF's text stopped at page 2 of 3 (page 1 read)."
    _stub_qms_read(monkeypatch, ["4.1 Context " * 10], [warning], page_count=3, stopped_on_error=True)

    response = _upload_qms(client, admin_headers, qms_standard.id, b"%PDF-1.4 stand-in")

    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
    assert response.json()["detail"] == (
        f"Only part of this PDF could be read: {warning} "
        "The PDF may be damaged. Save or export it again as a new PDF and upload that copy."
    )
    assert qms_llm == []


def test_qms_prompt_sends_exactly_the_window_the_refusal_rule_measures():
    """Review F7. ``QMS_PROMPT_TEXT_CHARS`` decides when a partial read is refused; the prompt
    f-string slices the text with a literal. They must stay equal, or a partial read could pass
    the refusal check and still hand the AI a shorter text than a whole read would. (The prompt
    text itself is deliberately not edited in this PR.)"""
    import inspect

    from app.api.endpoints.qms_standards import QMS_PROMPT_TEXT_CHARS, upload_pdf_and_extract_clauses

    assert f"pdf_text[:{QMS_PROMPT_TEXT_CHARS}]" in inspect.getsource(upload_pdf_and_extract_clauses)
