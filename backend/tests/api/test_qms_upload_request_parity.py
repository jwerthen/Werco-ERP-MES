"""``POST /qms-standards/{id}/upload-pdf`` behaves the same across the #9-0 prompt move.

This test fakes only the Anthropic client (``llm_client.get_anthropic_client``)
and the telemetry session, which both the pre-move inline code and the new
``qms_extraction_service`` reach through the real ``run_llm_task``. So the same
file runs unchanged against both versions of the route, and it passes on both.
Passing on the pre-move code proves the legacy prompt copy in
``tests/services/test_qms_extraction_service.py`` is faithful. Passing on the
new code proves the move sent the same bytes.

It pins, end to end:
- the exact request (model, ``max_tokens``, the single user message, no
  ``system``, and no ``with_options`` timeout or retry override), byte for byte,
  for a standard with a version and one without (``version=None``);
- the response, the clause rows and the one ``qms_standard`` audit row;
- the HTTP mapping of every extraction failure (invalid JSON, a JSON object
  instead of an array, egress off, not configured, an SDK error).
"""

import io
import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy.orm import Session

import app.services.llm_client as llm_client
from app.models.audit_log import AuditLog
from app.models.qms_standard import QMSClause, QMSStandard
from app.models.user import User
from app.services.llm_client import LLMNotConfiguredError
from app.services.llm_model_router import LLMTaskContext, select_anthropic_model

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

_MODEL_ENV_VARS = (
    "ANTHROPIC_QMS_MODEL",
    "ANTHROPIC_MODEL_SELECTION",
    "ANTHROPIC_FAST_MODEL",
    "ANTHROPIC_DEFAULT_MODEL",
    "ANTHROPIC_REASONING_MODEL",
)

# Braces in the body and in the standard name: a template move that re-parsed
# substituted values (str.format on the document text) would raise or eat them.
_PDF_BODY = (
    "AS9100D Quality Manual {draft} rev {0}\n"
    "4.1 Understanding the organization and its context\n"
    "8.5.2 Identification and traceability {{lot}} applies to every shipment"
)

_CLAUSES = [
    {"clause_number": "4.1", "title": "Understanding the organization", "description": "Determine issues."},
    {"clause_number": "8.5.2", "title": "Identification and traceability", "description": "Identify outputs."},
    # Skipped by the handler (no title), exactly as before.
    {"clause_number": "9.9", "title": "", "description": "dropped"},
]


# ---------------------------------------------------------------------------
# The pre-#9-0 inline f-string, verbatim (re-indented only). Do not edit.
# ---------------------------------------------------------------------------
def _legacy_inline_prompt(standard: Any, pdf_text: str) -> str:
    clause_schema = """[
  {
    "clause_number": "string - e.g. '4.1', '8.5.2'",
    "title": "string - clause title",
    "description": "string - full clause text or summary of requirements"
  }
]"""

    prompt = f"""You are a QMS standards expert. Extract ALL clauses and sub-clauses from this quality management document.

The document is: {standard.name} {standard.version or ''}

Rules:
1. Extract EVERY numbered clause and sub-clause (e.g., 4.1, 4.2, 5.1.1, 8.5.2)
2. Include the clause number, title, and the full requirement text as description
3. Maintain the hierarchical numbering exactly as shown in the document
4. Include ALL levels of sub-clauses
5. For the description, include the actual requirement text — not just a summary
6. Return a JSON array of objects, nothing else

Schema:
{clause_schema}

Document text:
---
{pdf_text[:100000]}
---

Return ONLY a valid JSON array. No markdown, no explanations."""
    return prompt


def _handler_pdf_text(content: bytes) -> str:
    """The handler's own pypdf read (unchanged by #9-0), so the expected prompt
    is built from the same text the route sends."""
    reader = PdfReader(io.BytesIO(content))
    pages_text = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            pages_text.append(text)
    return "\n\n".join(pages_text)


def _text_pdf_bytes(body: str) -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter, invariant=1)
    y = 720
    for line in body.split("\n"):
        c.drawString(72, y, line)
        y -= 16
    c.showPage()
    c.save()
    return buf.getvalue()


def _wire(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, text: str):
        self.content = [SimpleNamespace(type="text", text=text)]
        self.usage = SimpleNamespace(
            input_tokens=900, output_tokens=250, cache_creation_input_tokens=0, cache_read_input_tokens=0
        )


class _FakeClient:
    def __init__(self, reply: str = "[]", error: Optional[BaseException] = None):
        self.reply = reply
        self.error = error
        self.calls: List[Dict[str, Any]] = []
        self.with_options_calls: List[Dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return _FakeResponse(self.reply)

    def with_options(self, **kwargs):
        self.with_options_calls.append(kwargs)
        return self


class _RecordingSession:
    def __init__(self):
        self.added: List[Any] = []

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _default_model_routing(monkeypatch):
    for name in _MODEL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def telemetry(monkeypatch) -> _RecordingSession:
    session = _RecordingSession()
    monkeypatch.setattr(llm_client, "_usage_session_factory", lambda: session)
    return session


def _install_client(monkeypatch, **kwargs) -> _FakeClient:
    client = _FakeClient(**kwargs)
    monkeypatch.setattr(llm_client, "get_anthropic_client", lambda: client)
    return client


def _make_standard(db: Session, *, name: str, version: Optional[str]) -> QMSStandard:
    standard = QMSStandard(company_id=1, name=name, version=version, standard_body="SAE", is_active=True)
    db.add(standard)
    db.commit()
    db.refresh(standard)
    return standard


def _upload(client: TestClient, headers: dict, standard_id: int, pdf: bytes):
    return client.post(
        f"/api/v1/qms-standards/{standard_id}/upload-pdf",
        headers=headers,
        files={"file": ("manual.pdf", io.BytesIO(pdf), "application/pdf")},
    )


def _extraction_audit_rows(db: Session, standard_id: int) -> List[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.resource_type == "qms_standard",
            AuditLog.resource_id == standard_id,
            AuditLog.description.like("Extracted % clauses from PDF"),
        )
        .all()
    )


# ---------------------------------------------------------------------------
# The request and the success path
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,version",
    [
        pytest.param("AS9100D {draft}", "2016", id="with-version"),
        pytest.param("AS9100D {draft}", None, id="version-None"),
    ],
)
def test_upload_sends_the_pre_move_request_byte_for_byte(
    client: TestClient,
    db_session: Session,
    admin_user: User,
    admin_headers: dict,
    monkeypatch,
    telemetry,
    name,
    version,
):
    fake = _install_client(monkeypatch, reply="```json\n" + json.dumps(_CLAUSES) + "\n```")
    standard = _make_standard(db_session, name=name, version=version)
    pdf = _text_pdf_bytes(_PDF_BODY)
    pdf_text = _handler_pdf_text(pdf)
    assert "{draft}" in pdf_text and "{{lot}}" in pdf_text  # the braces survive pypdf

    resp = _upload(client, admin_headers, standard.id, pdf)

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert len(fake.calls) == 1
    expected = {
        "model": select_anthropic_model(
            LLMTaskContext(task="qms_clause_extraction", input_chars=len(pdf_text), max_output_tokens=16000)
        ).model,
        "max_tokens": 16000,
        "messages": [
            {
                "role": "user",
                "content": _legacy_inline_prompt(SimpleNamespace(name=name, version=version), pdf_text),
            }
        ],
    }
    assert _wire(fake.calls[0]) == _wire(expected)
    assert list(fake.calls[0]) == ["model", "max_tokens", "messages"]
    assert fake.calls[0]["model"] == "claude-sonnet-4-6"
    assert fake.with_options_calls == []

    # Telemetry labels and tenant.
    assert [(r.company_id, r.task, r.feature, r.prompt_version, r.success) for r in telemetry.added] == [
        (1, "qms_clause_extraction", "qms_standards", "1.0.0", True)
    ]

    # Response, rows and audit: two clauses kept, the title-less one skipped.
    body = resp.json()
    assert [(c["clause_number"], c["title"], c["sort_order"]) for c in body] == [
        ("4.1", "Understanding the organization", 0),
        ("8.5.2", "Identification and traceability", 1),
    ]
    db_session.expire_all()
    rows = db_session.query(QMSClause).filter(QMSClause.standard_id == standard.id).all()
    assert sorted((r.clause_number, r.company_id) for r in rows) == [("4.1", 1), ("8.5.2", 1)]
    audit_rows = _extraction_audit_rows(db_session, standard.id)
    assert [a.description for a in audit_rows] == ["Extracted 2 clauses from PDF"]
    assert audit_rows[0].user_id == admin_user.id


# ---------------------------------------------------------------------------
# The failure mapping
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "client_kwargs,expected_status,expected_detail",
    [
        pytest.param(
            {"reply": "Sure! The clauses are 4.1 and 8.5.2."},
            500,
            "AI extraction returned invalid data. Try again or use manual entry.",
            id="invalid-json",
        ),
        pytest.param(
            {"reply": '{"clauses": []}'},
            500,
            "AI extraction failed: Expected a JSON array of clauses",
            id="json-object-not-array",
        ),
        pytest.param(
            {"error": RuntimeError("upstream exploded")},
            500,
            "AI extraction failed: upstream exploded",
            id="sdk-error",
        ),
    ],
)
def test_extraction_failures_map_to_the_same_responses(
    client: TestClient,
    db_session: Session,
    admin_headers: dict,
    monkeypatch,
    telemetry,
    client_kwargs,
    expected_status,
    expected_detail,
):
    _install_client(monkeypatch, **client_kwargs)
    standard = _make_standard(db_session, name="AS9100D", version="2016")

    resp = _upload(client, admin_headers, standard.id, _text_pdf_bytes(_PDF_BODY))

    assert resp.status_code == expected_status, resp.text
    assert resp.json()["detail"] == expected_detail
    db_session.expire_all()
    assert db_session.query(QMSClause).filter(QMSClause.standard_id == standard.id).count() == 0
    assert _extraction_audit_rows(db_session, standard.id) == []


def test_egress_off_is_403_and_nothing_is_sent(
    client: TestClient, db_session: Session, admin_headers: dict, monkeypatch, telemetry
):
    fake = _install_client(monkeypatch)
    monkeypatch.setattr(llm_client, "_ai_egress_allowed", lambda company_id=None: False)
    standard = _make_standard(db_session, name="AS9100D", version="2016")

    resp = _upload(client, admin_headers, standard.id, _text_pdf_bytes(_PDF_BODY))

    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    assert resp.json()["detail"] == "AI extraction is disabled for your company (allow_ai_egress is off)."
    assert fake.calls == []
    assert telemetry.added == []


@pytest.mark.parametrize(
    "reason,expected_detail",
    [
        pytest.param("api_key", "AI extraction not configured (ANTHROPIC_API_KEY missing)", id="no-key"),
        pytest.param("library", "AI extraction library not available", id="no-sdk"),
    ],
)
def test_not_configured_is_500_with_the_same_detail(
    client: TestClient, db_session: Session, admin_headers: dict, monkeypatch, telemetry, reason, expected_detail
):
    def _not_configured():
        raise LLMNotConfiguredError(reason)

    monkeypatch.setattr(llm_client, "get_anthropic_client", _not_configured)
    standard = _make_standard(db_session, name="AS9100D", version="2016")

    resp = _upload(client, admin_headers, standard.id, _text_pdf_bytes(_PDF_BODY))

    assert resp.status_code == 500, resp.text
    assert resp.json()["detail"] == expected_detail
