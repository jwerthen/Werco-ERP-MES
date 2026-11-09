"""The process-wide parse cap (roadmap #9-3a): at most two GIL-holding parsers per process.

Today a frozen event loop happens to serialize most document parsing; the off-loop PRs (#9-2a+)
remove that, so this cap lands first. Each test runs six callers at once against a fake that
blocks inside the parser and records how many are inside together. Against the pre-#9-3a code
the in-process ones reach six.
"""

from __future__ import annotations

import subprocess
import threading
import time
from types import SimpleNamespace
from typing import Callable, List

import pytest

pytestmark = pytest.mark.unit

CALLERS = 6
CAP = 2  # PARSE_CONCURRENCY; imported lazily below so the in-process cap tests also run on the old code


class Gate:
    """Counts callers inside a blocked section and the peak reached."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.release = threading.Event()
        self.inside = 0
        self.peak = 0

    def enter(self) -> None:
        with self.lock:
            self.inside += 1
            self.peak = max(self.peak, self.inside)
        try:
            assert self.release.wait(10), "test never released the gate"
        finally:
            with self.lock:
                self.inside -= 1


def _run_concurrently(call: Callable[[int], object], gate: Gate) -> List[object]:
    results: List[object] = [None] * CALLERS
    errors: List[BaseException] = []

    def worker(index: int) -> None:
        try:
            results[index] = call(index)
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(CALLERS)]
    for thread in threads:
        thread.start()
    try:
        deadline = time.monotonic() + 5
        while gate.inside < CAP and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.3)  # every chance for a third caller to get in
    finally:
        gate.release.set()
        for thread in threads:
            thread.join(15)
    assert not errors, errors
    return results


def test_parse_slot_admits_two_at_a_time():
    from app.core.parse_limits import PARSE_CONCURRENCY, parse_slot

    gate = Gate()

    def call(_index):
        with parse_slot():
            gate.enter()

    _run_concurrently(call, gate)
    assert PARSE_CONCURRENCY == CAP
    assert gate.peak == 2


def test_parse_slot_is_reentrant_within_a_thread():
    """A capped parser that calls another capped parser must not wait on its own slot, even
    when the other slot is held elsewhere."""
    from app.core import parse_limits
    from app.core.parse_limits import PARSE_CONCURRENCY, parse_slot

    other_holds = threading.Event()
    other_release = threading.Event()

    def other():
        with parse_slot():
            other_holds.set()
            other_release.wait(10)

    holder = threading.Thread(target=other, daemon=True)
    holder.start()
    assert other_holds.wait(5)
    finished = threading.Event()

    def nested():
        with parse_slot():
            with parse_slot():
                with parse_slot():
                    pass
            finished.set()

    runner = threading.Thread(target=nested, daemon=True)
    runner.start()
    try:
        assert finished.wait(5), "nested parse_slot deadlocked"
    finally:
        other_release.set()
        holder.join(5)
        runner.join(5)
    # Both slots are free again.
    for _ in range(PARSE_CONCURRENCY):
        assert parse_limits._parse_semaphore.acquire(timeout=1)
    for _ in range(PARSE_CONCURRENCY):
        parse_limits._parse_semaphore.release()


def test_po_line_matching_is_capped(monkeypatch):
    from app.services import matching_service

    gate = Gate()

    def slow_match(part_number, db, company_id):
        gate.enter()
        return matching_service.MatchResult(matched=False)

    monkeypatch.setattr(matching_service, "match_part", slow_match)

    results = _run_concurrently(
        lambda i: matching_service.match_po_line_items([{"part_number": f"P-{i}"}], db=None, company_id=1), gate
    )
    assert gate.peak == 2
    assert all(len(result) == 1 for result in results)


def test_bom_excel_table_parse_is_capped(monkeypatch, tmp_path):
    import openpyxl

    from app.api.endpoints.bom import _extract_excel_table

    gate = Gate()

    class _Sheet:
        def iter_rows(self, **kwargs):
            gate.enter()
            yield ("Part Number", "Qty")
            yield ("P-1", 2)

    monkeypatch.setattr(
        openpyxl, "load_workbook", lambda *a, **k: SimpleNamespace(worksheets=[_Sheet()], close=lambda: None)
    )
    path = tmp_path / "bom.xlsx"
    path.write_bytes(b"stand-in")

    results = _run_concurrently(lambda i: _extract_excel_table(str(path), ".xlsx"), gate)
    assert gate.peak == 2
    assert all(result == (["Part Number", "Qty"], [["P-1", "2"]]) for result in results)


def test_excel_text_extraction_is_capped(monkeypatch, tmp_path):
    import openpyxl

    from app.services.pdf_service import extract_text_from_excel

    gate = Gate()

    class _Sheet:
        title = "Sheet1"

        def iter_rows(self, **kwargs):
            gate.enter()
            yield ("P-1", "Bracket", 2)

    monkeypatch.setattr(
        openpyxl, "load_workbook", lambda *a, **k: SimpleNamespace(worksheets=[_Sheet()], close=lambda: None)
    )
    path = tmp_path / "po.xlsx"
    path.write_bytes(b"stand-in")

    results = _run_concurrently(lambda i: extract_text_from_excel(str(path)), gate)
    assert gate.peak == 2
    assert all("P-1 | Bracket | 2" in result.text for result in results)


def test_subprocess_pdf_reads_are_capped(monkeypatch):
    from app.services import pdf_text_reader
    from app.services.pdf_text_reader import PdfReadLimits, read_pdf_pages

    gate = Gate()

    def fake_run(command, *, input, stdout, **kwargs):
        gate.enter()
        stdout.write(b'{"page_count": 1}\n{"text": "page", "cut": false}\n{"end": "complete"}\n')
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(pdf_text_reader.subprocess, "run", fake_run)
    limits = PdfReadLimits(
        max_bytes=1024, max_pages=5, max_page_chars=100, max_total_chars=100, cpu_seconds=5, wall_seconds=5
    )

    results = _run_concurrently(lambda i: read_pdf_pages(b"%PDF-1.4", limits), gate)
    assert gate.peak == 2
    assert all(result.pages == ["page"] for result in results)


def test_a_failing_parser_releases_its_slot():
    from app.core import parse_limits
    from app.core.parse_limits import PARSE_CONCURRENCY, parse_slot

    with pytest.raises(ValueError):
        with parse_slot():
            raise ValueError("boom")
    for _ in range(PARSE_CONCURRENCY):
        assert parse_limits._parse_semaphore.acquire(timeout=1)
    for _ in range(PARSE_CONCURRENCY):
        parse_limits._parse_semaphore.release()


def test_ocr_slot_reports_a_timeout_without_holding_anything():
    from app.core import parse_limits

    held = 0
    try:
        for _ in range(parse_limits.OCR_CONCURRENCY):
            assert parse_limits._ocr_semaphore.acquire(timeout=1)
            held += 1
        with parse_limits.ocr_slot(timeout=0.05) as acquired:
            assert acquired is False
    finally:
        for _ in range(held):
            parse_limits._ocr_semaphore.release()
    with parse_limits.ocr_slot(timeout=1) as acquired:
        assert acquired is True
