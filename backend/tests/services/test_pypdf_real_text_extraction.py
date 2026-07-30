"""Real-PDF text-extraction coverage for every pypdf consumer in the app.

The suite's other pypdf tests (``tests/services/test_laser_nest_pdf_split.py``,
``tests/api/test_laser_nest_bare_pdf.py``) build their fixtures with
``PdfWriter().add_blank_page()`` — pages with no content stream at all. They pin
page *counting* and *splitting* mechanics, but they never extract a single
character, so nothing in the suite exercised **text extraction** — which is
exactly what feeds the AI extraction pipeline (RFQ / PO / BOM / nest / QMS-clause
uploads) and exactly what the pypdf 6.12–6.14 hardening touched
(``MAX_DECLARED_STREAM_LENGTH`` on length-less streams, inline-image and
text-extraction loop guards, XMP size/element limits, cyclic ``/Pages``
detection).

These tests close that gap. They build a realistic text-bearing multi-page
laser-nest report with **reportlab** and push it through the three real
consumers:

  * ``pdf_service._extract_native_text`` — the shared native extractor behind
    every document-upload path;
  * ``qms_standards.upload_pdf_and_extract_clauses``' inline
    ``PdfReader(io.BytesIO(...))`` + per-page ``extract_text()`` + 50-character
    floor;
  * ``laser_nest_pdf_split_service.split_pdf_segments`` — where the load-bearing
    new assertion is that **each written segment's text still survives**, since
    the segment PDFs are what get handed to AI extraction downstream.

Deliberately formatting-agnostic: the per-page sentinels are whitespace-free
tokens and every assertion is substring- or count-based. pypdf has already
changed extraction whitespace once (6.12.0, layout mode) and may adjust
default-mode spacing again; an exact character count or an exact-spacing match
would be a false regression signal. Page counts, sentinel presence, page markers
and the pass/fail side of the app's own thresholds are the properties worth
pinning — and every assertion in this module was run green against **both** pypdf
6.10.2 (the pre-bump pin) and 6.14.2, so nothing here encodes 6.14-specific
behavior.
"""

import inspect
import io
from pathlib import Path

import pytest
from pypdf import PdfReader
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

from app.api.endpoints import qms_standards
from app.services.laser_nest_pdf_split_service import get_pdf_page_count, split_pdf_segments
from app.services.pdf_service import _extract_native_text, extract_text_from_pdf

pytestmark = pytest.mark.unit

_REPORT_PAGES = 5

# The QMS upload endpoint's inline floor: joined page text shorter than this is
# rejected with 400 ("scanned/image-based or empty").
_QMS_MIN_TEXT_CHARS = 50

# pdf_service.extract_text_from_pdf falls back to OCR when native text is <= this.
_OCR_FALLBACK_THRESHOLD_CHARS = 100


def _sentinel(page: int) -> str:
    """Whitespace-free per-page marker.

    Safe to assert on across pypdf versions, unlike any token containing spaces:
    only the presence of the glyph run is being pinned, never how the extractor
    chose to space it.
    """
    return f"NESTSENTINEL-P{page:03d}-OF-{_REPORT_PAGES:03d}"


def _build_nest_report(path: Path, pages: int = _REPORT_PAGES) -> Path:
    """Write a text-bearing multi-page laser-nest report — the real upload shape.

    Realistic body content (header block + a part table) so the extractor has
    something representative to walk; the *assertions* only ever use
    ``_sentinel``.
    """
    pdf = canvas.Canvas(str(path), pagesize=letter)
    for page in range(1, pages + 1):
        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawString(1 * inch, 10 * inch, _sentinel(page))
        pdf.setFont("Helvetica", 10)
        header = [
            f"Program: NST-{page:03d}-A",
            "Machine: Ermaksan Fiber 6kW",
            "Material: A36 Hot Rolled Steel",
            f"Thickness: 0.{page}25 in",
            f"Sheet Size: 60.00 x 120.00 in     Sheets Required: {page}",
            f"Utilization: {70 + page}.4%     Cut Time: 00:{12 + page}:35",
            "PART NO          QTY   DESCRIPTION",
        ]
        for row, line in enumerate(header):
            pdf.drawString(1 * inch, (9.4 - row * 0.22) * inch, line)
        for row in range(12):
            pdf.drawString(
                1 * inch,
                (7.5 - row * 0.22) * inch,
                f"WRC-{page}{row:03d}-01      {2 + row}   BRACKET ASSY REV {chr(65 + row)}",
            )
        pdf.showPage()
    pdf.save()
    return path


@pytest.fixture(scope="module")
def nest_report(tmp_path_factory) -> Path:
    """The shared 5-page text-bearing report.

    Module-scoped (built once per xdist worker): the reportlab build is the only
    non-trivial cost in this module and every consumer reads the same bytes.
    Nothing here mutates the file.
    """
    return _build_nest_report(tmp_path_factory.mktemp("pypdf-real-text") / "nest-report.pdf")


@pytest.fixture
def corrupt_pdf(tmp_path) -> Path:
    """A file with a PDF header and no usable body — the shape a truncated or
    hostile upload arrives in."""
    path = tmp_path / "corrupt.pdf"
    path.write_bytes(b"%PDF-1.7\nthis is not a pdf body at all\n%%EOF\n")
    return path


# --------------------------------------------------------------------------- #
# pdf_service._extract_native_text — the shared native extractor
# --------------------------------------------------------------------------- #
class TestExtractNativeText:
    def test_returns_page_count_and_text_from_every_page(self, nest_report):
        text, page_count = _extract_native_text(str(nest_report))

        assert page_count == _REPORT_PAGES
        # Every page's text survives — including the LAST one, which is where an
        # off-by-one or an early-exit loop guard would show up first.
        for page in range(1, _REPORT_PAGES + 1):
            assert _sentinel(page) in text, f"page {page} text missing from extraction"

    def test_emits_a_page_marker_per_page(self, nest_report):
        """Downstream AI prompts rely on ``--- Page N ---`` to attribute an
        extracted field back to a source page."""
        text, _ = _extract_native_text(str(nest_report))

        for page in range(1, _REPORT_PAGES + 1):
            assert f"--- Page {page} ---" in text
        assert f"--- Page {_REPORT_PAGES + 1} ---" not in text

    def test_pages_are_emitted_in_document_order(self, nest_report):
        text, _ = _extract_native_text(str(nest_report))

        positions = [text.index(_sentinel(page)) for page in range(1, _REPORT_PAGES + 1)]
        assert positions == sorted(positions)

    def test_text_clears_the_ocr_fallback_threshold(self, nest_report):
        """A native text PDF must not be mistaken for a scan.

        ``extract_text_from_pdf`` only falls back to OCR when native text is
        <= 100 characters, so this is the pass/fail side of a real branch — not
        an assertion about how many characters pypdf produces.
        """
        text, _ = _extract_native_text(str(nest_report))

        assert len(text.strip()) > _OCR_FALLBACK_THRESHOLD_CHARS

    def test_extract_text_from_pdf_takes_the_native_path(self, nest_report):
        """The public entry point resolves a text PDF natively: no OCR, high
        confidence. (OCR deps are not exercised — the native branch returns
        before they are imported.)"""
        result = extract_text_from_pdf(str(nest_report))

        assert result.is_ocr is False
        assert result.confidence == "high"
        assert result.file_type == "pdf"
        assert result.page_count == _REPORT_PAGES
        assert _sentinel(_REPORT_PAGES) in result.text

    def test_corrupt_pdf_degrades_to_empty_rather_than_raising(self, corrupt_pdf):
        """The contract every caller depends on: unreadable input yields
        ``("", 0)``, so an upload path can decide what to do instead of 500ing."""
        assert _extract_native_text(str(corrupt_pdf)) == ("", 0)

    def test_missing_file_degrades_to_empty(self, tmp_path):
        assert _extract_native_text(str(tmp_path / "nope.pdf")) == ("", 0)


# --------------------------------------------------------------------------- #
# qms_standards — inline PdfReader(BytesIO) + the 50-character floor
# --------------------------------------------------------------------------- #
def _qms_extract(content: bytes) -> tuple[list[str], str]:
    """Mirror of the inline extraction in ``upload_pdf_and_extract_clauses``.

    Mirrored rather than driven through the endpoint because the endpoint's own
    path needs a DB row, auth and an AI call *after* the floor check, while the
    property under test is the pypdf half. ``TestQmsStandardTextFloor
    .test_endpoint_pipeline_still_matches_this_mirror`` fails loudly if the
    endpoint's inline code drifts away from this copy.
    """
    reader = PdfReader(io.BytesIO(content))
    pages_text = [text for text in (page.extract_text() for page in reader.pages) if text]
    return pages_text, "\n\n".join(pages_text)


class TestQmsStandardTextFloor:
    def test_reads_every_page_from_an_in_memory_upload(self, nest_report):
        """The endpoint never touches the filesystem — it wraps the uploaded
        bytes in ``io.BytesIO``. Extraction must behave the same as from a path."""
        pages_text, joined = _qms_extract(nest_report.read_bytes())

        assert len(pages_text) == _REPORT_PAGES
        for page in range(1, _REPORT_PAGES + 1):
            assert _sentinel(page) in joined

    def test_a_normal_document_clears_the_fifty_character_floor(self, nest_report):
        """Below 50 characters the endpoint returns 400 "may be scanned/
        image-based or empty". A normal quality-manual page must clear it by a
        wide margin, not squeak past."""
        _, joined = _qms_extract(nest_report.read_bytes())

        assert len(joined.strip()) > _QMS_MIN_TEXT_CHARS * 10

    def test_a_single_page_document_also_clears_the_floor(self, tmp_path):
        """The floor applies to the whole joined document, so the thinnest
        realistic upload (one page) is the one at risk of tripping it."""
        one_pager = _build_nest_report(tmp_path / "one-page.pdf", pages=1)

        pages_text, joined = _qms_extract(one_pager.read_bytes())

        assert len(pages_text) == 1
        assert len(joined.strip()) > _QMS_MIN_TEXT_CHARS

    def test_endpoint_pipeline_still_matches_this_mirror(self):
        """Guard on the mirror above: if the endpoint's inline extraction or its
        floor changes, update ``_qms_extract`` and these tests to match."""
        source = inspect.getsource(qms_standards.upload_pdf_and_extract_clauses)

        assert "PdfReader(io.BytesIO(content))" in source, "endpoint no longer reads the upload from memory"
        assert f"len(pdf_text.strip()) < {_QMS_MIN_TEXT_CHARS}" in source, "endpoint's text floor moved"


# --------------------------------------------------------------------------- #
# laser_nest_pdf_split_service — text must survive the split
# --------------------------------------------------------------------------- #
class TestSplitPreservesText:
    def test_page_count_of_a_text_bearing_report(self, nest_report):
        assert get_pdf_page_count(str(nest_report)) == _REPORT_PAGES

    def test_segments_keep_deterministic_names_and_their_own_text(self, nest_report, tmp_path):
        """The whole point of the split: each segment PDF is what gets fed to AI
        extraction, so it has to carry its own pages' text and nobody else's."""
        dest = tmp_path / "segments"

        names = split_pdf_segments(str(nest_report), [[1], [2, 3], [4, 5]], str(dest))

        assert names == ["nest-p001.pdf", "nest-p002-p003.pdf", "nest-p004-p005.pdf"]
        expected_pages = {
            "nest-p001.pdf": [1],
            "nest-p002-p003.pdf": [2, 3],
            "nest-p004-p005.pdf": [4, 5],
        }
        for name, source_pages in expected_pages.items():
            segment = dest / name
            reader = PdfReader(str(segment))
            assert len(reader.pages) == len(source_pages)

            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            for page in source_pages:
                assert _sentinel(page) in text, f"{name}: lost page {page}'s text through the split"
            for other in set(range(1, _REPORT_PAGES + 1)) - set(source_pages):
                assert _sentinel(other) not in text, f"{name}: carries page {other}, which belongs elsewhere"

            # And the app's own extractor agrees with the raw read.
            app_text, app_count = _extract_native_text(str(segment))
            assert app_count == len(source_pages)
            for page in source_pages:
                assert _sentinel(page) in app_text

    def test_every_page_split_out_alone_keeps_its_text(self, nest_report, tmp_path):
        """Per-page segmentation is the common real shape (one nest per page)."""
        dest = tmp_path / "per-page"

        names = split_pdf_segments(str(nest_report), [[page] for page in range(1, _REPORT_PAGES + 1)], str(dest))

        assert names == [f"nest-p{page:03d}.pdf" for page in range(1, _REPORT_PAGES + 1)]
        for page, name in enumerate(names, start=1):
            text, count = _extract_native_text(str(dest / name))
            assert count == 1
            assert _sentinel(page) in text
