"""The resource-bounded subprocess PDF text reader (roadmap #9-3a).

``pdf_service.extract_text_from_pdf`` and the QMS clause upload used to run pypdf in-process on
uploaded files, holding the GIL (and, from async routes, the event loop) for as long as a hostile
document wanted. They now read the text layer in ``app.services.pdf_text_reader``'s child
process. Two things are pinned here:

1. **Nothing changes for a normal PDF.** The byte-equality class runs the subprocess reader and
   in-process pypdf over every PDF shape the suite can build (the eval nest fixtures, the
   multi-page nest report, quote and CoC documents, a table PO, blank and empty pages,
   non-ASCII and astral-plane text, an owner-password PDF) and requires identical page text,
   identical ``_extract_native_text`` output and identical QMS joins against a verbatim copy of
   the code it replaced.
2. **Past the limits, the read is bounded and says so**: page/character caps, a wall-clock
   timeout and a CPU kill each keep the pages read so far and add a plain-language warning; a
   document that cannot be opened raises one plain error; and the child's output is validated
   as untrusted.
"""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List

import pytest
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

from app.services import pdf_service, pdf_text_reader
from app.services.coc_pdf_service import build_certificate_of_conformance_pdf
from app.services.pdf_text_reader import (
    PdfReadError,
    PdfReadLimits,
    parse_reader_output,
    read_pdf_pages,
    stream_pages,
)
from app.services.quote_pdf_service import build_customer_quote_pdf
from tests.evals.nest_fixtures import build_digital_nest_pdf, build_scanned_nest_pdf

pytestmark = pytest.mark.unit

GENEROUS = PdfReadLimits(
    max_bytes=64 * 1024 * 1024,
    max_pages=500,
    max_page_chars=100_000,
    max_total_chars=1_000_000,
    cpu_seconds=30,
    wall_seconds=45,
)


def _limits(**overrides) -> PdfReadLimits:
    values = dict(GENEROUS.__dict__)
    values.update(overrides)
    return PdfReadLimits(**values)


# --------------------------------------------------------------------------- #
# Fixture PDFs
# --------------------------------------------------------------------------- #
def _canvas_pdf(pages: List[List[str]]) -> bytes:
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=letter)
    for lines in pages:
        for row, line in enumerate(lines):
            pdf.drawString(1 * inch, (10 - row * 0.25) * inch, line)
        pdf.showPage()
    pdf.save()
    return buf.getvalue()


def _nest_report() -> bytes:
    return _canvas_pdf(
        [
            [f"NESTSENTINEL-P{page:03d}", "Machine: Ermaksan Fiber 6kW", "Material: A36 Hot Rolled Steel"]
            + [f"WRC-{page}{row:03d}-01      {2 + row}   BRACKET ASSY REV {chr(65 + row)}" for row in range(12)]
            for page in range(1, 6)
        ]
    )


def _table_po() -> bytes:
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

    buf = io.BytesIO()
    rows = [["Line", "Part Number", "Description", "Qty", "UOM", "Unit Price", "Total"]] + [
        [str(i), f"AL-5052-{i:03d}", f"Aluminum 5052 Sheet .0{i}", str(i * 5), "EA", f"{i * 1.5:.2f}", f"{i * 7.5:.2f}"]
        for i in range(1, 40)
    ]
    table = Table(rows)
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.25, (0, 0, 0))]))
    SimpleDocTemplate(buf, pagesize=letter).build([table])
    return buf.getvalue()


def _unicode_with_an_empty_middle_page() -> bytes:
    return _canvas_pdf(
        [
            ["Ø 0.250 ±0.005 ° — naïve café Œuvre ß", "Tolerance ≤ 0.1 mm µ", "emoji 😀 survives?"],
            [],
            ["Page three: 漢字 and Ελληνικά"],
        ]
    )


def _blank_pages() -> bytes:
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=612, height=792)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _encrypted(user_password: str) -> bytes:
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(_canvas_pdf([["Owner-locked PO 12345"]]))))
    writer.encrypt(user_password=user_password, owner_password="owner-secret", algorithm="RC4-128")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _quote() -> bytes:
    return build_customer_quote_pdf(
        quote_number="Q-1001",
        revision="B",
        customer_name="Acme <b>Corp</b> & Sons",
        customer_contact="Dana",
        customer_email="ops@example.com",
        rfq_reference="RFQ-9",
        quote_date="2026-07-30",
        valid_until="2026-08-30",
        lead_time_label="3 weeks",
        total_amount=1234.5,
        line_summaries=[{"part_display": "Dim 2.500 <REF>", "qty": 2, "material": "A36", "thickness": "0.125"}],
        assumptions=[{"field": "finish", "assumption": "Powder coat"}],
        exclusions=["Hardware"],
    )


def _coc() -> bytes:
    return build_certificate_of_conformance_pdf(
        coc_number="COC-2001",
        customer_name="Acme",
        customer_po="PO-77",
        work_order_number="WO-1",
        part_number="P-1",
        part_name="Bracket",
        revision="C",
        quantity=25,
        lot_number="LOT-9",
        serial_numbers=["SN-1", "SN-2"],
        ship_date="2026-07-30",
        conformance_statement=None,
        issued_by_name="Q. Inspector",
        issued_at="2026-07-30T12:00:00Z",
    )


CORPUS: Dict[str, Callable[[], bytes]] = {
    "eval-digital-nest": lambda: build_digital_nest_pdf().pdf_bytes,
    "eval-scanned-nest": lambda: build_scanned_nest_pdf().pdf_bytes,
    "nest-report-5-pages": _nest_report,
    "table-po": _table_po,
    "unicode-empty-middle-page": _unicode_with_an_empty_middle_page,
    "blank-pages": _blank_pages,
    "owner-password-only": lambda: _encrypted(""),
    "customer-quote": _quote,
    "certificate-of-conformance": _coc,
}


# The code the reader replaced, verbatim, as the oracle.
def _old_extract_native_text(pdf_path: str):
    try:
        reader = PdfReader(pdf_path)
        page_count = len(reader.pages)
        all_text = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text()
            if text:
                all_text.append(f"--- Page {i+1} ---\n{text}")
        return "\n\n".join(all_text), page_count
    except Exception:
        return "", 0


def _old_qms_text(content: bytes) -> str:
    reader = PdfReader(io.BytesIO(content))
    pages_text = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            pages_text.append(text)
    return "\n\n".join(pages_text)


@pytest.mark.parametrize("name", sorted(CORPUS))
class TestByteEquality:
    def test_page_text_is_identical_to_in_process_pypdf(self, name):
        content = CORPUS[name]()
        in_process = [page.extract_text() or "" for page in PdfReader(io.BytesIO(content)).pages]

        result = read_pdf_pages(content, GENEROUS)

        assert result.page_count == len(in_process)
        assert result.pages == in_process
        assert result.warnings == []

    def test_native_extraction_output_is_unchanged(self, name, tmp_path):
        path = tmp_path / f"{name}.pdf"
        path.write_bytes(CORPUS[name]())

        assert pdf_service._extract_native_text(str(path)) == _old_extract_native_text(str(path))

    def test_qms_join_is_unchanged(self, name):
        content = CORPUS[name]()
        result = read_pdf_pages(content, GENEROUS)

        assert "\n\n".join(text for text in result.pages if text) == _old_qms_text(content)


def test_text_pdfs_still_take_the_native_path_with_identical_text(tmp_path):
    path = tmp_path / "po.pdf"
    path.write_bytes(_table_po())

    result = pdf_service.extract_text_from_pdf(str(path))

    assert result.is_ocr is False and result.confidence == "high"
    assert result.text == _old_extract_native_text(str(path))[0]
    assert result.warnings == []


def test_user_password_pdfs_fail_as_they_did_in_process(tmp_path):
    content = _encrypted("user-secret")
    with pytest.raises(Exception):
        [page.extract_text() for page in PdfReader(io.BytesIO(content)).pages]
    with pytest.raises(PdfReadError):
        read_pdf_pages(content, GENEROUS)
    path = tmp_path / "locked.pdf"
    path.write_bytes(content)
    assert pdf_service._extract_native_text(str(path)) == ("", 0)


# --------------------------------------------------------------------------- #
# Limits
# --------------------------------------------------------------------------- #
THREE_PAGES = [["alpha " * 10], ["bravo " * 10], ["charlie " * 10]]


def test_page_cap_reads_the_first_pages_and_says_so():
    result = read_pdf_pages(_canvas_pdf(THREE_PAGES), _limits(max_pages=2))

    assert result.page_count == 3
    assert len(result.pages) == 2 and "bravo" in result.pages[1]
    assert result.warnings == ["Only the first 2 of 3 pages were read."]


def test_per_page_character_cap_cuts_the_page_and_says_so():
    result = read_pdf_pages(_canvas_pdf(THREE_PAGES), _limits(max_page_chars=12))

    assert [len(page) for page in result.pages] == [12, 12, 12]
    assert result.warnings == [
        f"Page {page} has more text than can be read; only its first 12 characters were read." for page in (1, 2, 3)
    ]


def test_total_character_cap_stops_the_read_and_says_so():
    result = read_pdf_pages(_canvas_pdf(THREE_PAGES), _limits(max_total_chars=80))

    assert sum(len(page) for page in result.pages) == 80
    assert len(result.pages) == 2
    assert result.warnings == ["The PDF's text was cut off after 80 characters (pages 1–2 of 3 read)."]


def test_an_oversize_document_is_refused_before_any_child_runs(monkeypatch):
    def _no_child(*args, **kwargs):
        raise AssertionError("no child process for an oversize document")

    monkeypatch.setattr(pdf_text_reader.subprocess, "run", _no_child)
    with pytest.raises(PdfReadError) as excinfo:
        read_pdf_pages(b"%PDF-" + b"0" * 2048, _limits(max_bytes=1024))
    assert excinfo.value.reason == "too_large"
    assert str(excinfo.value) == "The PDF is larger than 0.000976562 MB, so its text was not read."


def test_a_malformed_pdf_is_one_plain_error():
    with pytest.raises(PdfReadError) as excinfo:
        read_pdf_pages(b"%PDF-1.7\nthis is not a pdf body at all\n%%EOF\n", GENEROUS)
    assert excinfo.value.reason == "unreadable"
    assert str(excinfo.value) == "The PDF could not be read. It may be damaged or not a PDF."


def _stand_in_child(monkeypatch, script: str) -> None:
    monkeypatch.setattr(pdf_text_reader, "_child_command", lambda limits: [sys.executable, "-c", script])


def test_wall_clock_timeout_keeps_the_pages_already_read(monkeypatch):
    _stand_in_child(
        monkeypatch,
        "import sys, time\n"
        "print('{\"page_count\": 3}', flush=True)\n"
        "print('{\"text\": \"first page\", \"cut\": false}', flush=True)\n"
        "time.sleep(60)\n",
    )
    started = time.monotonic()
    result = read_pdf_pages(b"%PDF-1.4", _limits(wall_seconds=1.5))

    assert time.monotonic() - started < 15
    assert result.pages == ["first page"] and result.page_count == 3
    assert result.warnings == ["Reading the PDF's text stopped after 1.5 seconds (page 1 of 3 read)."]


def test_a_timeout_before_the_document_opens_is_a_plain_error(monkeypatch):
    _stand_in_child(monkeypatch, "import time\ntime.sleep(60)\n")

    with pytest.raises(PdfReadError) as excinfo:
        read_pdf_pages(b"%PDF-1.4", _limits(wall_seconds=1))
    assert excinfo.value.reason == "timeout"
    assert str(excinfo.value) == "Reading the PDF took longer than 1 seconds, so its text was not read."


def test_the_child_is_killed_at_its_cpu_limit(monkeypatch):
    """The real ``main()`` applies RLIMIT_CPU: a parser spinning on one page dies after ~1 CPU
    second, long before the 30-second wall clock, and the read reports where it stopped."""
    _stand_in_child(
        monkeypatch,
        "import sys\n"
        "import app.services.pdf_text_reader as reader\n"
        "def spin(content, *, out, **limits):\n"
        "    reader._emit(out, {'page_count': 4})\n"
        "    reader._emit(out, {'text': 'page one', 'cut': False})\n"
        "    while True:\n"
        "        pass\n"
        "reader.stream_pages = spin\n"
        "sys.exit(reader.main(['--max-bytes', '100', '--max-pages', '10', '--max-page-chars', '100',\n"
        "    '--max-total-chars', '1000', '--cpu-seconds', '1', '--memory-bytes', str(512 * 1024 * 1024),\n"
        "    '--max-output-bytes', '100000']))\n",
    )
    started = time.monotonic()
    result = read_pdf_pages(b"%PDF-1.4", _limits(wall_seconds=30))

    assert time.monotonic() - started < 15
    assert result.pages == ["page one"]
    assert result.warnings == ["Reading the PDF's text stopped at page 2 of 4 (page 1 read)."]


def test_a_child_killed_before_the_document_opens_reads_as_too_complex(monkeypatch):
    """A signal death (RLIMIT_CPU, the OOM killer) is a cost limit, not a damaged file."""
    _stand_in_child(monkeypatch, "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n")

    with pytest.raises(PdfReadError) as excinfo:
        read_pdf_pages(b"%PDF-1.4", GENEROUS)
    assert excinfo.value.reason == "too_complex"
    assert str(excinfo.value) == "The PDF needs more processing than can be done safely, so its text was not read."


@pytest.mark.parametrize("error", ["MemoryError", "RecursionError"])
def test_a_resource_error_before_the_document_opens_reads_as_too_complex(error):
    with pytest.raises(PdfReadError) as excinfo:
        parse_reader_output(b'{"error": "%s"}\n' % error.encode(), GENEROUS)
    assert excinfo.value.reason == "too_complex"


@pytest.mark.parametrize(
    "reason, warned",
    [("too_large", True), ("timeout", True), ("too_complex", True), ("unreadable", False), ("unsafe", False)],
)
def test_native_reads_given_up_on_purpose_are_warned(monkeypatch, tmp_path, reason, warned):
    """Size, time and CPU/memory refusals reach the user; a damaged file stays silent (as before)."""

    def _refuse(content, limits):
        raise PdfReadError(f"refused: {reason}", reason=reason)

    monkeypatch.setattr(pdf_service, "read_pdf_pages", _refuse)
    path = tmp_path / "doc.pdf"
    path.write_bytes(b"%PDF-1.4")
    warnings: list = []

    assert pdf_service._extract_native_text(str(path), warnings) == ("", 0)
    assert [(w.code, w.message) for w in warnings] == ([("TEXT_PARTIAL", f"refused: {reason}")] if warned else [])


# --------------------------------------------------------------------------- #
# The child's output is untrusted
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw",
    [
        b'["not", "an", "object"]\n',
        b'{"page_count": -1}\n',
        b'{"page_count": true}\n',
        b'{"text": "before the header"}\n',
        b'{"page_count": 1}\n{"text": "one"}\n{"text": "two"}\n',
        b'{"page_count": 5}\n{"text": "' + b"x" * 101 + b'"}\n',
        b'{"page_count": 1}\n{"text": 7}\n',
        b'{"page_count": 1}\n{"end": "whatever"}\n',
        b'{"page_count": 2}\n{"text": "one"}\n{"end": "complete"}\n',
        b'{"page_count": 1}\n{"surprise": 1}\n',
    ],
)
def test_output_that_breaks_the_protocol_is_unsafe(raw):
    with pytest.raises(PdfReadError) as excinfo:
        parse_reader_output(raw, _limits(max_page_chars=100, max_total_chars=150))
    assert excinfo.value.reason == "unsafe"


def test_output_over_the_total_character_limit_is_unsafe():
    raw = b'{"page_count": 3}\n' + (b'{"text": "' + b"y" * 90 + b'"}\n') * 2
    with pytest.raises(PdfReadError):
        parse_reader_output(raw, _limits(max_page_chars=100, max_total_chars=150))


def test_a_torn_final_line_is_ignored():
    raw = b'{"page_count": 2}\n{"text": "one", "cut": false}\n{"text": "tw'
    result = parse_reader_output(raw, GENEROUS)
    assert result.pages == ["one"]
    assert result.warnings == ["Reading the PDF's text stopped at page 2 of 2 (page 1 read)."]


def test_stream_pages_writes_the_protocol(tmp_path):
    out = io.StringIO()
    stream_pages(_canvas_pdf(THREE_PAGES), max_pages=5, max_page_chars=1000, max_total_chars=10_000, out=out)

    result = parse_reader_output(out.getvalue().encode(), GENEROUS)
    assert result.page_count == 3 and len(result.pages) == 3 and result.warnings == []


def test_the_reader_child_runs_from_any_working_directory(monkeypatch, tmp_path):
    """Callers (and tests) chdir; ``-m app.services.pdf_text_reader`` must still resolve."""
    monkeypatch.chdir(tmp_path)
    result = read_pdf_pages(_canvas_pdf(THREE_PAGES), GENEROUS)
    assert len(result.pages) == 3


def test_native_partial_reads_reach_the_extraction_result(monkeypatch, tmp_path):
    monkeypatch.setattr(pdf_service, "NATIVE_PDF_MAX_PAGES", 2)
    path = tmp_path / "long.pdf"
    path.write_bytes(_canvas_pdf(THREE_PAGES))

    result = pdf_service.extract_text_from_pdf(str(path))

    assert result.is_ocr is False
    assert "--- Page 2 ---" in result.text and "--- Page 3 ---" not in result.text
    assert [(w.code, w.message) for w in result.warnings] == [
        ("TEXT_PARTIAL", "Only the first 2 of 3 pages were read.")
    ]


def test_backend_root_is_where_the_app_package_lives():
    assert (Path(pdf_text_reader._BACKEND_ROOT) / "app" / "services" / "pdf_text_reader.py").is_file()
