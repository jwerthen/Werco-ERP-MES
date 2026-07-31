"""``pdf_escape`` and the two PDF builders that depend on it.

reportlab's ``Paragraph`` parses a mini-HTML dialect, so every f-string hole in
``quote_pdf_service`` and ``coc_pdf_service`` is an injection sink. This is the
*only* markup-interpreting sink in the backend, and escaping here is what makes
it safe to store request bodies verbatim rather than stripping markup at ingest
(see ``tests/test_frontend_no_raw_html_render_guard.py`` for the whole argument).

Two distinct failure modes are covered, because only one of them is obvious:

1. **Hard failure.** ``Paragraph("Check OD<ID before press", style)`` raises
   ``ValueError: paraparser: syntax error: parse ended with 1 unclosed tags``.
   Unescaped, a part note or a customer name is an HTTP 500 on a quote or CoC
   download. ``test_unescaped_paragraph_still_raises`` pins that the hazard is
   real and not a historical note.
2. **Silent alteration.** ``"Acme <b>Corp</b>"`` renders *bolded* with the tags
   eaten, and ``"<REF>"`` disappears entirely. On a Certificate of Conformance —
   a compliance artifact asserting what was shipped — a value that renders as
   something other than what is stored is a records-integrity defect.

The builder tests extract text back out of the produced PDF with ``pypdf`` and
assert the hostile value appears **literally**, which is the only assertion that
distinguishes "escaped" from "stripped".
"""

import re

import pytest

from app.services.coc_pdf_service import build_certificate_of_conformance_pdf
from app.services.pdf_text import pdf_escape
from app.services.quote_pdf_service import build_customer_quote_pdf

pytestmark = pytest.mark.unit

# Values chosen to cover each hazard class: intentional-looking markup, an
# unterminated "<" (the ValueError case), a bare ampersand, a script tag, and
# real ASME Y14.5 drawing notation that the old ingest sanitizer destroyed.
HOSTILE_VALUES = [
    "Acme <b>Corp</b>",
    "Check OD<ID before press",
    "R&D Group",
    "<script>alert(1)</script>",
    "Dim is 2.500 <REF> per print",
]


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Return the concatenated text of every page, with layout whitespace collapsed.

    reportlab breaks lines wherever the frame ends, so an extracted string carries
    newlines and doubled spaces that have nothing to do with the input. Collapsing
    runs of whitespace keeps the assertions about *content* rather than wrapping.
    """
    from io import BytesIO

    from pypdf import PdfReader

    reader = PdfReader(BytesIO(pdf_bytes))
    return re.sub(r"\s+", " ", " ".join(page.extract_text() or "" for page in reader.pages))


# ---------------------------------------------------------------------------
# The helper itself.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Acme <b>Corp</b>", "Acme &lt;b&gt;Corp&lt;/b&gt;"),
        ("Check OD<ID before press", "Check OD&lt;ID before press"),
        ("R&D Group", "R&amp;D Group"),
        ("<script>alert(1)</script>", "&lt;script&gt;alert(1)&lt;/script&gt;"),
        ("Dim is 2.500 <REF> per print", "Dim is 2.500 &lt;REF&gt; per print"),
        ("plain text", "plain text"),
        ("", ""),
    ],
)
def test_pdf_escape_escapes_the_three_markup_characters(raw: str, expected: str):
    assert pdf_escape(raw) == expected


def test_pdf_escape_does_not_double_escape_ampersands():
    """The ordering bug a hand-rolled ``.replace()`` chain walks into.

    Escaping ``<`` before ``&`` turns ``a & b`` into ``a &amp;amp; b``, which
    renders the entity text literally in the PDF. ``xml.sax.saxutils.escape``
    handles ``&`` first; this pins that we still delegate to something that does.
    """
    assert pdf_escape("a & b") == "a &amp; b"
    assert pdf_escape("<a & b>") == "&lt;a &amp; b&gt;"
    # An input that already looks like an entity is data, not markup: it must be
    # escaped again so it renders as the characters the operator typed.
    assert pdf_escape("&amp;") == "&amp;amp;"


def test_pdf_escape_leaves_quotes_alone():
    """Values land in element content, never in an attribute, so quotes are not
    special — and ``&quot;`` noise on a customer-facing quote would be a bug."""
    assert pdf_escape('12" stock, Bob\'s cell') == '12" stock, Bob\'s cell'


def test_pdf_escape_maps_none_to_empty_string():
    """Both builders take many Optional fields; a literal "None" on a Certificate
    of Conformance is worse than a blank."""
    assert pdf_escape(None) == ""


def test_pdf_escape_coerces_non_strings():
    assert pdf_escape(42) == "42"
    assert pdf_escape(2.5) == "2.5"


# ---------------------------------------------------------------------------
# The hazard is real: proof that the unescaped form actually breaks.
# ---------------------------------------------------------------------------


def test_unescaped_paragraph_still_raises():
    """``Paragraph`` with an unterminated ``<`` raises — this is a live 500, not lore.

    If a future reportlab release stops raising here, this test fails and the
    urgency of ``pdf_escape`` should be re-evaluated (the silent-alteration half
    below would still stand).
    """
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph

    style = getSampleStyleSheet()["Normal"]

    with pytest.raises(ValueError, match="paraparser"):
        Paragraph("Check OD<ID before press", style)

    # ...and the escaped form of the same value constructs fine.
    assert Paragraph(pdf_escape("Check OD<ID before press"), style) is not None


# ---------------------------------------------------------------------------
# End to end through both builders.
# ---------------------------------------------------------------------------


def build_quote_with(value: str) -> bytes:
    return build_customer_quote_pdf(
        quote_number=f"Q-1001 {value}",
        revision=value,
        customer_name=value,
        customer_contact=value,
        customer_email="ops@example.com",
        rfq_reference=value,
        quote_date="2026-07-30",
        valid_until="2026-08-30",
        lead_time_label=value,
        total_amount=1234.5,
        line_summaries=[{"part_display": value, "qty": 2, "material": value, "thickness": "0.125", "finish": value}],
        assumptions=[{"field": value, "assumption": value}],
        exclusions=[value],
    )


def build_coc_with(value: str) -> bytes:
    return build_certificate_of_conformance_pdf(
        coc_number=f"COC-2001 {value}",
        customer_name=value,
        customer_po=value,
        work_order_number=value,
        part_number=value,
        part_name=value,
        revision=value,
        quantity=25,
        lot_number=value,
        serial_numbers=[value],
        ship_date="2026-07-30",
        conformance_statement=None,
        issued_by_name=value,
        issued_at="2026-07-30T12:00:00Z",
    )


@pytest.mark.parametrize("value", HOSTILE_VALUES)
def test_quote_pdf_renders_hostile_values_literally(value: str):
    """No exception, and the value survives into the page text verbatim."""
    text = extract_pdf_text(build_quote_with(value))

    assert text.startswith("%") is False  # sanity: we got text, not raw bytes
    assert value in text, f"{value!r} did not survive into the quote PDF text; got: {text[:400]!r}"


@pytest.mark.parametrize("value", HOSTILE_VALUES)
def test_coc_pdf_renders_hostile_values_literally(value: str):
    text = extract_pdf_text(build_coc_with(value))

    assert value in text, f"{value!r} did not survive into the CoC PDF text; got: {text[:400]!r}"


def test_builders_emit_valid_pdfs():
    """Cheap structural check alongside the text assertions."""
    for pdf in (build_quote_with("R&D <REF>"), build_coc_with("R&D <REF>")):
        assert pdf.startswith(b"%PDF-"), pdf[:20]
        assert b"%%EOF" in pdf[-2048:]


def test_our_own_bold_markup_still_renders_as_markup():
    """The escaping must neutralize *values*, not the templates' own ``<b>`` tags.

    A blanket escape of the whole f-string would print a literal ``<b>`` on every
    label. Extracted text contains neither the tag nor its entity form, which is
    what "reportlab consumed it as markup" looks like.
    """
    text = extract_pdf_text(build_coc_with("Acme Corp"))

    assert "Customer:" in text
    assert "<b>" not in text
    assert "&lt;b&gt;" not in text


def test_table_cells_are_not_markup_and_need_no_escaping():
    """Pins the sink-audit finding that reportlab ``Table`` cells take plain strings.

    The quote's line table interpolates ``part_display`` / ``material`` / ``finish``
    into ``Table`` rows, not ``Paragraph``s. If a future reportlab (or a switch to
    wrapping cells in ``Paragraph``) started parsing them, the value below would
    raise or come back altered — and the fix would be to route those through
    ``pdf_escape`` too.
    """
    pdf = build_customer_quote_pdf(
        quote_number="Q-1002",
        revision="A",
        customer_name="Acme",
        customer_contact=None,
        customer_email=None,
        rfq_reference=None,
        quote_date="2026-07-30",
        valid_until=None,
        lead_time_label=None,
        total_amount=10.0,
        line_summaries=[
            {
                "part_display": "Check OD<ID",
                "qty": 1,
                "material": "R&D alloy",
                "thickness": "0.125",
                "finish": "<none>",
            }
        ],
        assumptions=[],
        exclusions=[],
    )

    text = extract_pdf_text(pdf)
    assert "Check OD<ID" in text
    assert "R&D alloy" in text
