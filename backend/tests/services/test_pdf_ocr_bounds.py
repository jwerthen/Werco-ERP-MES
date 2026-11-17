"""OCR bounds in ``pdf_service`` (roadmap #9-3a).

Scanned-PDF OCR used to rasterize EVERY page at 300 dpi into memory at once (``convert_from_path``
with no page range), with no page cap, no timeout on poppler or tesseract, and no limit on how
many documents OCR at once. A 100-page scan needed ~2.5 GB and could take the worker down. These
tests pin the bounds that replaced that:

* pages are rasterized ONE at a time (``first_page == last_page``) and each image is closed;
* at most 30 pages are read (owner decision 2026-11-09), with a visible warning naming the skip;
* every poppler and tesseract call carries a per-page timeout, and a page that times out is
  skipped with a warning instead of sinking the whole document;
* the whole document gets a 90-second budget (owner decision), INCLUDING the wait for an OCR
  slot, checked on a patched clock; when it runs out, one warning names every unread page;
* the page count (pdfinfo) gets 10 seconds, and a timeout there is reported, not dropped;
* at most two documents OCR at once per process;
* a page too large to rasterize at 300 dpi within 25 MP is rendered at a lower dpi.

poppler and tesseract are not installed in CI, so ``pdf2image`` and ``pytesseract`` are replaced
with fakes in ``sys.modules`` (``pdf_service`` imports both inside the function). The fakes accept
both the old call shape (whole document, no timeout) and the new one, so every behavioral test
here runs, and fails on its assertions, against the pre-#9-3a code too.
"""

from __future__ import annotations

import math
import sys
import threading
import time
import types
from contextlib import contextmanager
from typing import Dict, List, Optional, Tuple

import pytest

from app.services import pdf_service

pytestmark = pytest.mark.unit

LETTER = (612.0, 792.0)
ARCH_D = (1728.0, 2592.0)  # 24 x 36 in: 77.8 MP at 300 dpi


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeImage:
    def __init__(self, page: int) -> None:
        self.page = page
        self.closed = False

    def close(self) -> None:
        self.closed = True


class TesseractNotFoundError(OSError):
    pass


class PDFPopplerTimeoutError(Exception):
    """Stand-in for ``pdf2image.exceptions.PDFPopplerTimeoutError``."""


class FakeOcrTools:
    """Stand-ins for ``pdf2image`` and ``pytesseract`` that record every call."""

    def __init__(
        self,
        page_count: int,
        *,
        sizes: Optional[Dict[int, Tuple[float, float]]] = None,
        default_size: Optional[Tuple[float, float]] = LETTER,
        clock: Optional[FakeClock] = None,
        work_s: float = 0.0,
        fail_pages: Tuple[int, ...] = (),
        missing_tesseract: bool = False,
        tesseract_vanishes_at: Optional[int] = None,
        pdfinfo_times_out: bool = False,
    ) -> None:
        self.page_count = page_count
        self.sizes = sizes or {}
        self.default_size = default_size
        self.clock = clock
        self.work_s = work_s
        self.fail_pages = fail_pages
        self.missing_tesseract = missing_tesseract
        self.tesseract_vanishes_at = tesseract_vanishes_at
        self.pdfinfo_times_out = pdfinfo_times_out
        self.pdfinfo_calls: List[dict] = []
        self.convert_calls: List[dict] = []
        self.ocr_calls: List[dict] = []
        self.images: List[FakeImage] = []
        # Optional hooks for the concurrency test.
        self.on_ocr_enter = None

    def _size(self, page: int) -> Optional[Tuple[float, float]]:
        return self.sizes.get(page, self.default_size)

    def pdfinfo_from_path(self, pdf_path, userpw=None, ownerpw=None, poppler_path=None, rawdates=False, **kwargs):
        self.pdfinfo_calls.append(dict(kwargs))
        if self.pdfinfo_times_out:
            if self.clock is not None:
                self.clock.advance(kwargs.get("timeout") or 0)
            raise PDFPopplerTimeoutError("Run poppler poppler timeout.")
        info: Dict[str, object] = {"Pages": self.page_count}
        first = kwargs.get("first_page") or 1
        last = min(kwargs.get("last_page") or 1, self.page_count)
        for page in range(first, last + 1):
            size = self._size(page)
            if size is None:
                continue
            key = "Page size" if first == last else f"Page {page:4d} size"
            info[key] = f"{size[0]:g} x {size[1]:g} pts"
        return info

    def convert_from_path(self, pdf_path, dpi=200, first_page=None, last_page=None, **kwargs):
        self.convert_calls.append(dict(dpi=dpi, first_page=first_page, last_page=last_page, **kwargs))
        first = first_page or 1
        last = min(last_page or self.page_count, self.page_count)
        images = [FakeImage(page) for page in range(first, last + 1)]
        self.images.extend(images)
        return images

    def image_to_string(self, image, lang=None, config="", nice=0, output_type="string", timeout=0):
        now = self.clock.now if self.clock else None
        self.ocr_calls.append(dict(page=image.page, timeout=timeout, at=now))
        if self.missing_tesseract or (
            self.tesseract_vanishes_at is not None and image.page >= self.tesseract_vanishes_at
        ):
            raise TesseractNotFoundError("tesseract is not installed or it's not in your PATH")
        if self.on_ocr_enter is not None:
            self.on_ocr_enter()
        if image.page in self.fail_pages:
            raise RuntimeError("Tesseract process timeout")
        if self.clock and self.work_s:
            if timeout and timeout < self.work_s:
                self.clock.advance(timeout)
                raise RuntimeError("Tesseract process timeout")
            self.clock.advance(self.work_s)
        return f"SCANNED-TEXT-PAGE-{image.page} " * 3

    def install(self, monkeypatch) -> None:
        pdf2image = types.ModuleType("pdf2image")
        pdf2image.convert_from_path = self.convert_from_path
        pdf2image.pdfinfo_from_path = self.pdfinfo_from_path
        pytesseract = types.ModuleType("pytesseract")
        pytesseract.image_to_string = self.image_to_string
        pytesseract.TesseractNotFoundError = TesseractNotFoundError
        exceptions = types.ModuleType("pdf2image.exceptions")
        exceptions.PDFPopplerTimeoutError = PDFPopplerTimeoutError
        pdf2image.exceptions = exceptions
        monkeypatch.setitem(sys.modules, "pdf2image", pdf2image)
        monkeypatch.setitem(sys.modules, "pdf2image.exceptions", exceptions)
        monkeypatch.setitem(sys.modules, "pytesseract", pytesseract)


@pytest.fixture
def scan_path(tmp_path):
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-1.4 image-only stand-in")
    return str(path)


@pytest.fixture(autouse=True)
def _no_text_layer(monkeypatch):
    """Every document here is a scan: the native read finds nothing, so OCR runs."""
    monkeypatch.setattr(pdf_service, "_extract_native_text", lambda pdf_path, warnings=None: ("", 0))


def _messages(result) -> List[str]:
    return [warning.message for warning in getattr(result, "warnings", [])]


def _codes(result) -> List[str]:
    return [warning.code for warning in getattr(result, "warnings", [])]


def test_pages_are_rasterized_one_at_a_time_and_closed(monkeypatch, scan_path):
    tools = FakeOcrTools(page_count=3)
    tools.install(monkeypatch)

    result = pdf_service.extract_text_from_pdf(scan_path)

    assert [(c["first_page"], c["last_page"]) for c in tools.convert_calls] == [(1, 1), (2, 2), (3, 3)]
    assert all(image.closed for image in tools.images), "every rasterized page must be closed after OCR"
    # Same text format as before: one block per page, joined by a blank line.
    expected = "\n\n".join(f"--- Page {p} (OCR) ---\n" + f"SCANNED-TEXT-PAGE-{p} " * 3 for p in (1, 2, 3))
    assert result.text == expected
    assert result.is_ocr is True and result.page_count == 3 and result.confidence == "medium"
    assert _messages(result) == []


def test_page_count_is_read_with_a_bounded_pdfinfo_call(monkeypatch, scan_path):
    tools = FakeOcrTools(page_count=3)
    tools.install(monkeypatch)

    pdf_service.extract_text_from_pdf(scan_path)

    assert len(tools.pdfinfo_calls) == 1
    (call,) = tools.pdfinfo_calls
    assert 0 < call["timeout"] <= 10
    assert call["first_page"] == 1 and call["last_page"] == 30


def test_only_the_first_30_pages_are_read_with_a_visible_warning(monkeypatch, scan_path):
    tools = FakeOcrTools(page_count=120)
    tools.install(monkeypatch)

    result = pdf_service.extract_text_from_pdf(scan_path)

    assert {call["page"] for call in tools.ocr_calls} == set(range(1, 31))
    assert "--- Page 30 (OCR) ---" in result.text
    assert "--- Page 31 (OCR) ---" not in result.text
    assert result.page_count == 120, "page_count still reports the whole document"
    assert _messages(result) == ["Only the first 30 of 120 scanned pages were read."]
    assert _codes(result) == ["OCR_PARTIAL"]


def test_every_poppler_and_tesseract_call_has_a_per_page_timeout(monkeypatch, scan_path):
    tools = FakeOcrTools(page_count=4)
    tools.install(monkeypatch)

    pdf_service.extract_text_from_pdf(scan_path)

    assert tools.convert_calls and all(0 < c.get("timeout", 0) <= 30 for c in tools.convert_calls)
    assert tools.ocr_calls and all(0 < c["timeout"] <= 30 for c in tools.ocr_calls)


def test_a_page_that_times_out_is_skipped_with_a_warning(monkeypatch, scan_path):
    """pytesseract raises RuntimeError on its timeout. One slow page used to discard the whole
    document's OCR text; now only that page is lost, and the user is told which."""
    tools = FakeOcrTools(page_count=3, fail_pages=(2,))
    tools.install(monkeypatch)

    result = pdf_service.extract_text_from_pdf(scan_path)

    assert "--- Page 1 (OCR) ---" in result.text
    assert "--- Page 2 (OCR) ---" not in result.text
    assert "--- Page 3 (OCR) ---" in result.text
    assert _messages(result) == ["Page 2 could not be read and was skipped."]
    assert all(image.closed for image in tools.images)


def test_the_document_stops_at_the_90_second_budget(monkeypatch, scan_path):
    """Each page takes 25 s of (fake) time. Pages 1-3 finish by t=75; page 4 gets only the 15 s
    left, times out, and is skipped; nothing else starts. No call is ever handed a timeout that
    reaches past the budget."""
    clock = FakeClock()
    monkeypatch.setattr(pdf_service, "time", types.SimpleNamespace(monotonic=clock.monotonic), raising=False)
    started = clock.now
    tools = FakeOcrTools(page_count=10, clock=clock, work_s=25.0)
    tools.install(monkeypatch)

    result = pdf_service.extract_text_from_pdf(scan_path)

    assert [call["page"] for call in tools.ocr_calls] == [1, 2, 3, 4]
    for call in tools.ocr_calls:
        assert call["at"] + call["timeout"] <= started + 90 + 1e-6
    assert [p for p in range(1, 11) if f"--- Page {p} (OCR) ---" in result.text] == [1, 2, 3]
    assert _messages(result) == [
        "Text recognition stopped after 90 seconds, so pages 5–10 of 10 were not read.",
        "Page 4 could not be read and was skipped.",
    ]
    assert clock.now - started <= 90 + 1e-6


def _patch_clock(monkeypatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr(pdf_service, "time", types.SimpleNamespace(monotonic=clock.monotonic), raising=False)
    return clock


def _slot_after_waiting(monkeypatch, clock: FakeClock, waited_s: float) -> None:
    """Replace the OCR slot with one that is granted after ``waited_s`` of (fake) queueing."""

    @contextmanager
    def slow_slot(timeout):
        assert timeout == pytest.approx(90)
        clock.advance(waited_s)
        yield True

    monkeypatch.setattr(pdf_service, "ocr_slot", slow_slot)


def test_the_90_second_budget_counts_the_wait_for_an_ocr_slot(monkeypatch, scan_path):
    """Review F3(a); owner decision 2026-11-09: the budget INCLUDES the slot wait. After 80 s in
    the queue only 10 s are left, so no step may be handed more than that."""
    clock = _patch_clock(monkeypatch)
    started = clock.now
    _slot_after_waiting(monkeypatch, clock, 80.0)
    tools = FakeOcrTools(page_count=3, clock=clock, work_s=25.0)
    tools.install(monkeypatch)

    result = pdf_service.extract_text_from_pdf(scan_path)

    assert tools.convert_calls[0]["timeout"] <= 10 + 1e-6
    assert tools.ocr_calls[0]["timeout"] <= 10 + 1e-6
    assert clock.now - started <= 90 + 1e-6
    assert _messages(result) == [
        "Text recognition stopped after 90 seconds, so pages 2–3 of 3 were not read.",
        "Page 1 could not be read and was skipped.",
    ]


def test_a_capped_scan_that_also_runs_out_of_time_gets_one_consistent_warning(monkeypatch, scan_path):
    """Review F4: "Only the first 30 of 120 were read" next to "pages 5-30 of 120 were not read"
    contradicted itself. When the time runs out, one sentence names everything unread."""
    clock = _patch_clock(monkeypatch)
    tools = FakeOcrTools(page_count=120, clock=clock, work_s=25.0)
    tools.install(monkeypatch)

    result = pdf_service.extract_text_from_pdf(scan_path)

    assert _messages(result) == [
        "Text recognition stopped after 90 seconds, so pages 5–120 of 120 were not read.",
        "Page 4 could not be read and was skipped.",
    ]


def test_a_capped_scan_whose_ocr_tool_fails_part_way_gets_one_consistent_warning(monkeypatch, scan_path):
    tools = FakeOcrTools(page_count=120, tesseract_vanishes_at=3)
    tools.install(monkeypatch)

    result = pdf_service.extract_text_from_pdf(scan_path)

    assert "--- Page 2 (OCR) ---" in result.text
    assert _messages(result) == ["Text recognition stopped at page 3 of 120 (pages 1–2 read)."]


def test_a_capped_scan_with_a_skipped_page_names_both(monkeypatch, scan_path):
    tools = FakeOcrTools(page_count=120, fail_pages=(7,))
    tools.install(monkeypatch)

    result = pdf_service.extract_text_from_pdf(scan_path)

    assert _messages(result) == [
        "Only the first 30 of 120 scanned pages were read.",
        "Page 7 could not be read and was skipped.",
    ]


def test_a_pdfinfo_timeout_skips_ocr_with_a_warning(monkeypatch, scan_path):
    """Review F6: pdfinfo hanging past its 10 s used to land in the generic ``except`` and vanish
    without a word, unlike the text-layer reader's timeout."""
    tools = FakeOcrTools(page_count=3, pdfinfo_times_out=True)
    tools.install(monkeypatch)

    result = pdf_service.extract_text_from_pdf(scan_path)

    assert tools.convert_calls == [] and tools.ocr_calls == []
    assert result.text == ""
    assert [(w.code, w.message) for w in result.warnings] == [
        (
            "OCR_PARTIAL",
            "Text recognition was skipped: the scanned document could not be opened within 10 seconds.",
        )
    ]


def test_a_pdfinfo_timeout_cut_short_by_the_budget_names_the_budget(monkeypatch, scan_path):
    clock = _patch_clock(monkeypatch)
    _slot_after_waiting(monkeypatch, clock, 85.0)
    tools = FakeOcrTools(page_count=3, clock=clock, pdfinfo_times_out=True)
    tools.install(monkeypatch)

    result = pdf_service.extract_text_from_pdf(scan_path)

    assert tools.pdfinfo_calls[0]["timeout"] <= 5 + 1e-6
    assert _messages(result) == ["Text recognition was skipped: the 90-second limit ran out before it started."]


def test_at_most_two_documents_ocr_at_once(monkeypatch, tmp_path):
    tools = FakeOcrTools(page_count=1)
    tools.install(monkeypatch)
    lock = threading.Lock()
    release = threading.Event()
    state = {"inside": 0, "peak": 0}

    def enter():
        with lock:
            state["inside"] += 1
            state["peak"] = max(state["peak"], state["inside"])
        try:
            assert release.wait(10), "test never released the OCR fakes"
        finally:
            with lock:
                state["inside"] -= 1

    tools.on_ocr_enter = enter
    results: Dict[int, object] = {}

    def run(index: int) -> None:
        path = tmp_path / f"scan-{index}.pdf"
        path.write_bytes(b"%PDF-1.4")
        results[index] = pdf_service.extract_text_from_pdf(str(path))

    threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(5)]
    for thread in threads:
        thread.start()
    try:
        deadline = time.monotonic() + 5
        while state["inside"] < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.3)  # give a third document every chance to get in
        assert state["inside"] == 2
    finally:
        release.set()
        for thread in threads:
            thread.join(15)
    assert state["peak"] == 2
    assert len(results) == 5 and all("SCANNED-TEXT-PAGE-1" in r.text for r in results.values())


def test_a_large_page_is_rendered_at_a_lower_dpi(monkeypatch, scan_path):
    tools = FakeOcrTools(page_count=2, sizes={1: LETTER, 2: ARCH_D})
    tools.install(monkeypatch)

    pdf_service.extract_text_from_pdf(scan_path)

    letter_call, arch_call = tools.convert_calls
    assert letter_call["dpi"] == 300, "a normal page renders at the same dpi as before (crop box: see below)"
    assert arch_call["dpi"] < 300
    width_px = math.ceil(ARCH_D[0] / 72 * arch_call["dpi"])
    height_px = math.ceil(ARCH_D[1] / 72 * arch_call["dpi"])
    assert width_px * height_px <= 25_000_000
    assert arch_call["dpi"] >= 165, "only as much lower as the 25 MP cap requires"
    # Rendered with the box pdfinfo measured (never larger than it), so the bound holds.
    assert all(call.get("use_cropbox") is True for call in tools.convert_calls)


def test_a_page_with_no_reported_size_is_bounded_by_scale_to(monkeypatch, scan_path):
    tools = FakeOcrTools(page_count=1, default_size=None)
    tools.install(monkeypatch)

    pdf_service.extract_text_from_pdf(scan_path)

    (call,) = tools.convert_calls
    assert call["size"] == 5000  # -scale-to 5000: at most 5000 x 5000 = 25 MP


def test_busy_ocr_slots_skip_the_document_with_a_warning(monkeypatch, scan_path):
    """The 90-second budget includes the wait for an OCR slot."""
    from app.core import parse_limits

    monkeypatch.setattr(pdf_service, "OCR_TOTAL_BUDGET_S", 0.2)
    tools = FakeOcrTools(page_count=1)
    tools.install(monkeypatch)
    held = 0
    try:
        for _ in range(parse_limits.OCR_CONCURRENCY):
            assert parse_limits._ocr_semaphore.acquire(timeout=5)
            held += 1
        result = pdf_service.extract_text_from_pdf(scan_path)
    finally:
        for _ in range(held):
            parse_limits._ocr_semaphore.release()

    assert tools.convert_calls == [] and tools.ocr_calls == []
    assert result.text == ""
    assert _codes(result) == ["OCR_PARTIAL"]
    assert "other scanned documents were being read" in _messages(result)[0]


def test_missing_tesseract_stops_quietly_as_before(monkeypatch, scan_path):
    """An environment without tesseract is not a partial read: no per-page warnings, one attempt."""
    tools = FakeOcrTools(page_count=3, missing_tesseract=True)
    tools.install(monkeypatch)

    result = pdf_service.extract_text_from_pdf(scan_path)

    assert result.text == ""
    assert len(tools.ocr_calls) == 1
    assert "OCR_PARTIAL" not in _codes(result)
