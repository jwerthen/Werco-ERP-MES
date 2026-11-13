"""P2.4 -- quotes always carry the drawing revision.

Werco's 2026-10-01 quote went out with the Rev column blank on every line. Pinned here:

* **The gate holds on EVERY door to the customer** -- ``POST /quotes/{id}/send``, ``PUT``
  to ``sent`` / ``accepted`` from draft or pending, the document-delivery preview AND send,
  and ``POST /quotes/{id}/generate-pdf`` -- with the exact 409 naming the lines, and each
  refusal writes nothing (no status move, no audit row, no delivery row, no SMTP call).
  Part-less lines are included, or the gate would never fire on the hand-off quote.
* An idempotent re-send of an already-SENT quote is unaffected.
* ``line_revisions`` on ``PUT`` is a content edit: normalized, audited per line, refused on
  an issued quote and for a line that is not on this quote, tenant-isolated.
* The response and print data expose ``part_revision`` and ``part_current_revision``.
* The customer PDF prints the Rev column, "Quote Rev", Valid Until (quote date + 30 days
  when unset) and the Terms block with the FOB point from the Company record.
"""

import itertools
from datetime import date, timedelta
from io import BytesIO
from unittest.mock import AsyncMock

import pytest
from pypdf import PdfReader
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.document_delivery import DocumentDelivery
from app.models.part import Part
from app.models.quote import Quote, QuoteLine, QuoteStatus
from app.models.user import User, UserRole
from app.services import document_delivery_service as delivery_module
from app.services.document_pdf_service import company_city_state, quote_terms
from app.services.quote_send_gate import (
    MISSING_REVISION_DETAIL,
    default_quote_line_revision,
    normalize_line_revision,
    quote_send_blockers,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens minted directly; never used for login
_seq = itertools.count(1)


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    transport = AsyncMock()
    monkeypatch.setattr(delivery_module, "dispatch_document_email", transport)
    monkeypatch.setattr(delivery_module, "smtp_configured", lambda: True)
    return transport


# --------------------------------------------------------------------------- builders


def _company(db: Session, company_id: int = 1, **fields) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if company is None:
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"rev-co-{company_id}", is_active=True)
        db.add(company)
    for key, value in fields.items():
        setattr(company, key, value)
    db.commit()
    return company


def _user(db: Session, role: UserRole = UserRole.ADMIN, company_id: int = 1) -> User:
    _company(db, company_id)
    n = next(_seq)
    user = User(
        email=f"quote-rev-{n}@co{company_id}.test",
        employee_id=f"QREV-{n:05d}",
        first_name="Quote",
        last_name=f"Rev{n}",
        hashed_password=PASSWORD_HASH,
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


def _part(db: Session, company_id: int = 1, revision: str = "C") -> Part:
    _company(db, company_id)
    n = next(_seq)
    part = Part(
        part_number=f"QREV-P-{company_id}-{n:04d}",
        name=f"Quoted part {n}",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        revision=revision,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def _quote(db: Session, revisions, *, status=QuoteStatus.DRAFT, company_id=1, part=None, **fields) -> Quote:
    """A quote whose line N carries ``revisions[N-1]`` (``None`` = blank). Line 1 names ``part``."""
    _company(db, company_id)
    n = next(_seq)
    quote = Quote(
        company_id=company_id,
        quote_number=f"QREV-{company_id}-{n:05d}",
        customer_name="Acme & Sons",
        customer_email="buyer@example.com",
        status=status,
        subtotal=10.0 * len(revisions),
        total=10.0 * len(revisions),
        **fields,
    )
    quote.lines = [
        QuoteLine(
            company_id=company_id,
            line_number=index,
            part_id=part.id if (part is not None and index == 1) else None,
            part_revision=revision,
            description=f"Line {index}",
            quantity=1,
            unit_price=10,
            line_total=10,
        )
        for index, revision in enumerate(revisions, 1)
    ]
    db.add(quote)
    db.commit()
    db.refresh(quote)
    return quote


def _state(db: Session, quote_id: int):
    """Status, approval, line revisions, audit rows and delivery rows -- for "nothing written"."""
    db.rollback()
    db.expire_all()
    quote = db.get(Quote, quote_id)
    return (
        quote.status,
        quote.approved_by,
        quote.updated_at,
        sorted((line.id, line.part_revision) for line in quote.lines),
        db.query(AuditLog).filter(AuditLog.resource_type.in_(["quote", "document_delivery"])).count(),
        db.query(DocumentDelivery).count(),
    )


def _quote_rows(db: Session, quote_id: int):
    db.rollback()
    db.expire_all()
    return (
        db.query(AuditLog)
        .filter(AuditLog.resource_type == "quote", AuditLog.resource_id == quote_id)
        .order_by(AuditLog.sequence_number)
        .all()
    )


def _pdf_text(content: bytes) -> str:
    return " ".join(" ".join((page.extract_text() or "").split()) for page in PdfReader(BytesIO(content)).pages)


EXPECTED_1_3 = f"{MISSING_REVISION_DETAIL} Missing on line(s): 1, 3."


# ================================================================ every door refuses


class TestEverySendDoorRefuses:
    @pytest.mark.parametrize("start", [QuoteStatus.DRAFT, QuoteStatus.PENDING])
    def test_send(self, client, db_session, start):
        quote = _quote(db_session, [None, "B", "  "], status=start)
        before = _state(db_session, quote.id)
        resp = client.post(f"/api/v1/quotes/{quote.id}/send", headers=_headers(_user(db_session)))
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == EXPECTED_1_3
        assert _state(db_session, quote.id) == before

    @pytest.mark.parametrize("start", [QuoteStatus.DRAFT, QuoteStatus.PENDING])
    @pytest.mark.parametrize("target", ["sent", "accepted"])
    def test_put_status(self, client, db_session, start, target):
        quote = _quote(db_session, [None, "B", None], status=start)
        before = _state(db_session, quote.id)
        resp = client.put(f"/api/v1/quotes/{quote.id}", headers=_headers(_user(db_session)), json={"status": target})
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == EXPECTED_1_3
        assert _state(db_session, quote.id) == before

    def test_generate_pdf(self, client, db_session):
        quote = _quote(db_session, [None, "B", None])
        before = _state(db_session, quote.id)
        resp = client.post(f"/api/v1/quotes/{quote.id}/generate-pdf", headers=_headers(_user(db_session)))
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == EXPECTED_1_3
        assert _state(db_session, quote.id) == before

    def test_delivery_preview(self, client, db_session, fake_smtp):
        quote = _quote(db_session, [None, "B", None])
        before = _state(db_session, quote.id)
        resp = client.post(
            "/api/v1/document-deliveries/preview",
            headers=_headers(_user(db_session)),
            json={"entity_type": "quote", "entity_id": quote.id},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == EXPECTED_1_3
        assert _state(db_session, quote.id) == before
        fake_smtp.assert_not_called()

    def test_delivery_send_of_a_snapshot_prepared_before_the_gate(self, client, db_session, monkeypatch, fake_smtp):
        """A delivery prepared before the gate existed (same source hash) must not reach SMTP."""
        quote = _quote(db_session, [None, "B", None])
        headers = _headers(_user(db_session))
        real_gate = delivery_module.assert_quote_sendable
        monkeypatch.setattr(delivery_module, "assert_quote_sendable", lambda _quote: None)
        prepared = client.post(
            "/api/v1/document-deliveries/preview", headers=headers, json={"entity_type": "quote", "entity_id": quote.id}
        )
        assert prepared.status_code == 200, prepared.text
        monkeypatch.setattr(delivery_module, "assert_quote_sendable", real_gate)
        review = prepared.json()
        before = _state(db_session, quote.id)
        resp = client.post(
            f"/api/v1/document-deliveries/{review['id']}/send",
            headers=headers,
            json={
                "expected_version": review["version"],
                "request_key": "rev-gate-send-0001",
                "recipient": review["recipient"],
                "subject": review["subject"],
                "body": review["body"],
            },
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == EXPECTED_1_3
        assert _state(db_session, quote.id) == before
        db_session.expire_all()
        assert db_session.get(DocumentDelivery, review["id"]).status == "prepared"
        fake_smtp.assert_not_called()

    def test_a_part_less_line_is_not_exempt(self, client, db_session):
        quote = _quote(db_session, [None])  # the Fabrication Quoting hand-off shape: one part-less line
        resp = client.post(f"/api/v1/quotes/{quote.id}/send", headers=_headers(_user(db_session)))
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == f"{MISSING_REVISION_DETAIL} Missing on line(s): 1."


class TestDoorsOpenOnceEveryLineHasARevision:
    def test_send_and_generate_pdf_succeed_with_na_on_a_line_with_no_drawing(self, client, db_session):
        quote = _quote(db_session, ["A", "N/A"])
        headers = _headers(_user(db_session))
        assert client.post(f"/api/v1/quotes/{quote.id}/generate-pdf", headers=headers).status_code == 200
        assert client.post(f"/api/v1/quotes/{quote.id}/send", headers=headers).status_code == 200

    def test_idempotent_resend_of_an_already_sent_quote_is_unaffected(self, client, db_session):
        # A quote that reached SENT before the gate existed: re-marking it sent moves nothing.
        quote = _quote(db_session, [None, None], status=QuoteStatus.SENT)
        resp = client.post(f"/api/v1/quotes/{quote.id}/send", headers=_headers(_user(db_session)))
        assert resp.status_code == 200, resp.text
        assert _quote_rows(db_session, quote.id) == []

    def test_put_to_accepted_from_sent_is_not_a_send_door(self, client, db_session):
        quote = _quote(db_session, [None], status=QuoteStatus.SENT)
        resp = client.put(
            f"/api/v1/quotes/{quote.id}", headers=_headers(_user(db_session)), json={"status": "accepted"}
        )
        assert resp.status_code == 200, resp.text


# ================================================================== line_revisions


class TestLineRevisionEntry:
    def test_entry_is_normalized_audited_per_line_and_resets_a_pending_quote(self, client, db_session):
        quote = _quote(db_session, [None, None, "A"], status=QuoteStatus.PENDING, approved_by=None)
        approver = _user(db_session)
        quote.approved_by = approver.id
        db_session.commit()
        lines = sorted(quote.lines, key=lambda line: line.line_number)
        resp = client.put(
            f"/api/v1/quotes/{quote.id}",
            headers=_headers(_user(db_session)),
            json={
                "line_revisions": [
                    {"line_id": lines[0].id, "part_revision": " b2 "},
                    {"line_id": lines[1].id, "part_revision": "n/a"},
                    {"line_id": lines[2].id, "part_revision": "A"},
                ]
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [line["part_revision"] for line in body["lines"]] == ["B2", "N/A", "A"]
        assert body["status"] == "draft"  # a content edit resets PENDING -> DRAFT ...

        db_session.expire_all()
        reloaded = db_session.get(Quote, quote.id)
        assert reloaded.approved_by is None  # ... and clears the approval stamp

        rows = _quote_rows(db_session, quote.id)
        update = next(row for row in rows if row.action == "UPDATE")
        assert update.extra_data["edited_fields"] == ["line_revisions"]
        assert update.extra_data["line_revision_changes"] == [
            {"line_id": lines[0].id, "line_number": 1, "old": None, "new": "B2"},
            {"line_id": lines[1].id, "line_number": 2, "old": None, "new": "N/A"},
        ]
        assert update.old_values["line_revisions"][str(lines[0].id)] is None
        assert update.new_values["line_revisions"][str(lines[0].id)] == "B2"
        assert any(row.action == "STATUS_CHANGE" for row in rows)

        sent = client.post(f"/api/v1/quotes/{quote.id}/send", headers=_headers(_user(db_session)))
        assert sent.status_code == 200, sent.text

    @pytest.mark.parametrize("start", [QuoteStatus.SENT, QuoteStatus.ACCEPTED])
    def test_refused_on_an_issued_quote(self, client, db_session, start):
        quote = _quote(db_session, [None], status=start)
        before = _state(db_session, quote.id)
        resp = client.put(
            f"/api/v1/quotes/{quote.id}",
            headers=_headers(_user(db_session)),
            json={"line_revisions": [{"line_id": quote.lines[0].id, "part_revision": "A"}]},
        )
        assert resp.status_code == 409, resp.text
        assert _state(db_session, quote.id) == before

    def test_a_line_of_another_quote_or_tenant_is_422(self, client, db_session):
        quote = _quote(db_session, [None])
        other = _quote(db_session, [None])
        foreign = _quote(db_session, [None], company_id=2)
        headers = _headers(_user(db_session))
        before = _state(db_session, quote.id)
        for line_id in (other.lines[0].id, foreign.lines[0].id, 999999):
            resp = client.put(
                f"/api/v1/quotes/{quote.id}",
                headers=headers,
                json={"line_revisions": [{"line_id": line_id, "part_revision": "A"}]},
            )
            assert resp.status_code == 422, resp.text
        assert _state(db_session, quote.id) == before

    def test_the_same_line_twice_is_422(self, client, db_session):
        quote = _quote(db_session, [None])
        line_id = quote.lines[0].id
        resp = client.put(
            f"/api/v1/quotes/{quote.id}",
            headers=_headers(_user(db_session)),
            json={
                "line_revisions": [
                    {"line_id": line_id, "part_revision": "A"},
                    {"line_id": line_id, "part_revision": "B"},
                ]
            },
        )
        assert resp.status_code == 422, resp.text

    @pytest.mark.parametrize("value", ["", "   ", "A-1", "REV A", "X" * 21, "<b>"])
    def test_an_invalid_value_is_422(self, client, db_session, value):
        quote = _quote(db_session, [None])
        resp = client.put(
            f"/api/v1/quotes/{quote.id}",
            headers=_headers(_user(db_session)),
            json={"line_revisions": [{"line_id": quote.lines[0].id, "part_revision": value}]},
        )
        assert resp.status_code == 422, resp.text

    def test_another_tenant_cannot_edit_line_revisions(self, client, db_session):
        quote = _quote(db_session, [None])
        before = _state(db_session, quote.id)
        resp = client.put(
            f"/api/v1/quotes/{quote.id}",
            headers=_headers(_user(db_session, company_id=2)),
            json={"line_revisions": [{"line_id": quote.lines[0].id, "part_revision": "A"}]},
        )
        assert resp.status_code == 404, resp.text
        assert _state(db_session, quote.id) == before

    @pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.OPERATOR])
    def test_read_only_roles_cannot_enter_revisions(self, client, db_session, role):
        quote = _quote(db_session, [None])
        resp = client.put(
            f"/api/v1/quotes/{quote.id}",
            headers=_headers(_user(db_session, role=role)),
            json={"line_revisions": [{"line_id": quote.lines[0].id, "part_revision": "A"}]},
        )
        assert resp.status_code == 403, resp.text


# ============================================================== response / print data


def test_response_and_print_data_expose_line_and_current_part_revision(client, db_session):
    part = _part(db_session, revision="F")
    quote = _quote(db_session, [None, "N/A"], part=part)
    headers = _headers(_user(db_session))
    lines = client.get(f"/api/v1/quotes/{quote.id}", headers=headers).json()["lines"]
    assert [(line["part_revision"], line["part_current_revision"]) for line in lines] == [(None, "F"), ("N/A", None)]
    printed = client.get(f"/api/v1/print/quotes/{quote.id}/print-data", headers=headers)
    assert printed.status_code == 200, printed.text
    assert [(line["part_revision"], line["part_current_revision"]) for line in printed.json()["lines"]] == [
        (None, "F"),
        ("N/A", None),
    ]


def test_default_line_revision_is_the_own_parts_current_revision(db_session):
    own = _part(db_session, revision="d")
    foreign = _part(db_session, company_id=2, revision="Z")
    assert default_quote_line_revision(db_session, 1, own.id) == "D"
    assert default_quote_line_revision(db_session, 1, foreign.id) is None
    assert default_quote_line_revision(db_session, 1, None) is None


@pytest.mark.unit
def test_normalize_and_blockers_units():
    assert normalize_line_revision(" a1 ") == "A1"
    assert normalize_line_revision("n/a") == "N/A"
    for bad in ("", "A.1", "X" * 21):
        with pytest.raises(ValueError):
            normalize_line_revision(bad)

    class _Line:
        def __init__(self, number, revision):
            self.id, self.line_number, self.part_revision = number, number, revision

    class _Quote:
        lines = [_Line(3, None), _Line(1, ""), _Line(2, "B")]

    assert quote_send_blockers(_Quote()) == f"{MISSING_REVISION_DETAIL} Missing on line(s): 1, 3."


# ======================================================================= customer PDF


def test_pdf_prints_rev_column_quote_rev_valid_until_and_terms(client, db_session):
    _company(db_session, address="415 East Houston Street\nBroken Arrow, OK 74012")
    part = _part(db_session, revision="C")
    quote = _quote(db_session, ["C", "N/A"], part=part, revision="2", quote_date=date(2026, 10, 1))
    resp = client.post(f"/api/v1/quotes/{quote.id}/generate-pdf", headers=_headers(_user(db_session)))
    assert resp.status_code == 200, resp.text
    text = _pdf_text(resp.content)
    assert "Part Rev Qty Material Thickness Finish Line Total" in text
    assert f"{part.part_number} - Line 1 C" in text and "Line 2 N/A" in text
    assert "Quote Rev: 2" in text
    assert "Valid Until: 10/31/2026" in text  # quote date + 30 days when the quote carries none
    assert "Terms" in text and "Exclusions" not in text
    for term in (
        "Quote valid for 30 days from the quote date.",
        "FOB Broken Arrow, OK.",
        "All prices are in U.S. dollars (USD).",
        "Sales tax is not included.",
        "Lead time and payment terms are confirmed at time of order.",
        "Subject to drawing/specification review at order entry.",
    ):
        assert term in text, f"term {term!r} missing from the quote PDF"


def test_pdf_keeps_an_explicit_valid_until_and_falls_back_to_fob_origin(client, db_session):
    _company(db_session, address=None)
    quote = _quote(db_session, ["A"], valid_until=date(2026, 12, 15), quote_date=date(2026, 10, 1))
    text = _pdf_text(
        client.post(f"/api/v1/quotes/{quote.id}/generate-pdf", headers=_headers(_user(db_session))).content
    )
    assert "Valid Until: 12/15/2026" in text
    assert "FOB Origin." in text


@pytest.mark.unit
@pytest.mark.parametrize(
    "address,expected",
    [
        ("415 East Houston Street\nBroken Arrow, OK 74012", ("Broken Arrow", "OK")),
        ("415 East Houston Street, Broken Arrow, OK 74012-1234", ("Broken Arrow", "OK")),
        ("1 Main St\nTulsa, ok\nUSA", ("Tulsa", "OK")),
        ("415 East Houston Street, OK 74012", None),  # a street is never taken for the city
        ("PO Box 12", None),
        (None, None),
        ("", None),
    ],
)
def test_company_city_state(address, expected):
    assert company_city_state(address) == expected


@pytest.mark.unit
def test_terms_block_is_the_module_constant_with_the_fob_point():
    assert quote_terms("Broken Arrow, OK 74012")[1] == "FOB Broken Arrow, OK."
    assert quote_terms(None)[1] == "FOB Origin."
    assert len(quote_terms(None)) == 6


def test_delivery_preview_after_entering_revisions_succeeds(client, db_session):
    quote = _quote(db_session, ["A"], valid_until=date.today() + timedelta(days=30))
    resp = client.post(
        "/api/v1/document-deliveries/preview",
        headers=_headers(_user(db_session)),
        json={"entity_type": "quote", "entity_id": quote.id},
    )
    assert resp.status_code == 200, resp.text
