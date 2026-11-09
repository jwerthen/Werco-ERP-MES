"""Process-wide caps on CPU-heavy document parsing and OCR.

Moving parsing off the event loop does not move it off the GIL: every pure-Python parser
(pypdf, openpyxl, fuzzy line matching) running in a worker thread competes with the loop for
the interpreter. Measured wake delay of an idle loop: p50 12 ms / p99 13 ms beside one
CPU-bound thread, against p50 77 ms / p99 381 ms beside eight. These two caps keep that bounded
no matter how many uploads arrive at once.

* ``parse_slot()`` (cap ``PARSE_CONCURRENCY``) wraps every subprocess PDF reader call
  (``pdf_text_reader.read_pdf_pages``) and the in-process CPU parsers: openpyxl/xlrd in the
  BOM Excel table parser and ``pdf_service.extract_text_from_excel``, and PO line matching.
  It waits as long as it takes (today's frozen event loop already serializes this work, so a
  wait is never worse than the status quo), and it is re-entrant per thread, so a parser that
  calls another capped parser cannot deadlock on its own slot.
* ``ocr_slot(timeout)`` (cap ``OCR_CONCURRENCY``) wraps one document's OCR pass. Rasterizing
  and recognizing a scanned page costs ~75 MB and a full CPU, so OCR gets its own cap rather
  than sharing the parse one. The wait is bounded by the caller's OCR budget.

Both are ``threading.BoundedSemaphore``s, not asyncio/anyio primitives: the work they guard
runs in worker threads, on the event loop thread (until the off-loop PRs move it), and in the
ARQ worker, and a thread semaphore is the one primitive that works from all three.
"""

import functools
import threading
from contextlib import contextmanager
from typing import Callable, Iterator, ParamSpec, TypeVar

PARSE_CONCURRENCY = 2
OCR_CONCURRENCY = 2

_parse_semaphore = threading.BoundedSemaphore(PARSE_CONCURRENCY)
_ocr_semaphore = threading.BoundedSemaphore(OCR_CONCURRENCY)
_held = threading.local()

P = ParamSpec("P")
R = TypeVar("R")


@contextmanager
def parse_slot() -> Iterator[None]:
    """Hold one of the ``PARSE_CONCURRENCY`` process-wide parse slots for the block.

    Re-entrant per thread: a nested ``parse_slot()`` in a thread that already holds one is a
    no-op, never a second acquire (two threads each holding one slot and waiting for a second
    would otherwise deadlock).
    """
    depth = getattr(_held, "parse_depth", 0)
    if depth:
        _held.parse_depth = depth + 1
        try:
            yield
        finally:
            _held.parse_depth -= 1
        return
    _parse_semaphore.acquire()
    _held.parse_depth = 1
    try:
        yield
    finally:
        _held.parse_depth = 0
        _parse_semaphore.release()


def holds_parse_slot(fn: Callable[P, R]) -> Callable[P, R]:
    """Decorator form of :func:`parse_slot` for a whole parsing function."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        with parse_slot():
            return fn(*args, **kwargs)

    return wrapper


@contextmanager
def ocr_slot(timeout: float) -> Iterator[bool]:
    """Wait up to ``timeout`` seconds for one of the ``OCR_CONCURRENCY`` OCR slots.

    Yields True while the slot is held, or False when none freed up in time (nothing is held
    then, and the caller should skip OCR with a warning rather than wait past its budget).
    """
    acquired = _ocr_semaphore.acquire(timeout=max(0.0, timeout))
    try:
        yield acquired
    finally:
        if acquired:
            _ocr_semaphore.release()
