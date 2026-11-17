"""Isolated, resource-bounded native text extraction for uploaded PDFs.

pypdf is pure Python: on an uploaded PDF it holds the GIL for as long as the document makes it,
and its memory use follows whatever the file declares. Reading the text layer in a child
process bounds both: the child runs under ``RLIMIT_CPU``, ``RLIMIT_AS`` (Linux), ``RLIMIT_FSIZE``
on its output and a parent wall-clock timeout, and the parent holds a :func:`parse_slot` for the
duration, so at most ``PARSE_CONCURRENCY`` readers run per process (plus, until #9-2a, one on the
event-loop thread, which takes no slot; see ``app.core.parse_limits``).

The cost is a child start per read: 0.08-0.25 s measured (interpreter start plus importing pypdf)
before the first page. Until #9-2a moves the PO upload, routing generation and QMS upload parses
off the event loop, that start is added to the time those routes hold the loop for each PDF.

Generalized from ``hank_pdf_reader`` (which is unchanged: its 25-page / 10 MB / refuse-encrypted
limits are a Hank contract). Unlike Hank, this reader must return EXACTLY what in-process pypdf
returns for a normal document, because its callers (``pdf_service.extract_text_from_pdf`` and the
QMS clause upload) feed that text to existing prompts. So it does not refuse encrypted files
(pypdf opens owner-password-only PDFs with the empty password, as it always has), does not
require a leading ``%PDF-``, and never strips or rewrites page text. A byte-equality test pins
that against in-process pypdf.

What changes is only what happens past the limits, and every such case is reported as a
plain-language warning instead of a silent change:

* more pages than ``max_pages``: the first ``max_pages`` are read;
* a page longer than ``max_page_chars``, or a document longer than ``max_total_chars``: the text
  is cut there;
* out of CPU, memory or wall-clock time, or a parser error part-way through: the pages read
  before that point are kept. The child writes one JSON line per page and flushes it, so a
  killed child still leaves every finished page behind. A parser error (as opposed to a limit)
  also sets ``PdfReadResult.stopped_on_error``: in-process pypdf raised there and its callers
  treated the whole read as failed, so a caller that must keep that behavior can.

A document that cannot be opened at all raises :class:`PdfReadError` with a plain message.

Child protocol (stdout, one JSON object per line): ``{"page_count": N}`` once the document is
open, then ``{"text": str, "cut": bool}`` per page in document order, then ``{"end": reason}``
(``complete`` | ``page_limit`` | ``char_limit``), or ``{"error": type_name}`` on a failure. The
parent treats that output as untrusted (the child just parsed a hostile file) and validates
every record against the limits it set.
"""

import argparse
import io
import json
import logging
import resource
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Dict, List, Optional, Sequence

from app.core.parse_limits import parse_slot

logger = logging.getLogger(__name__)

MIB = 1024 * 1024
DEFAULT_MEMORY_BYTES = 512 * MIB

# The ``-m`` module path only resolves from the backend root; tests (and any caller) may have
# chdir'd elsewhere, so the child always starts there. It reads stdin and nothing else.
_BACKEND_ROOT = Path(__file__).resolve().parents[2]

_END_REASONS = ("complete", "page_limit", "char_limit")
# Child failures that mean "this document costs too much to read", not "this is not a PDF".
_RESOURCE_ERRORS = ("MemoryError", "RecursionError")


@dataclass(frozen=True)
class PdfReadLimits:
    """Bounds for one read. Every field is enforced in the child and re-checked in the parent."""

    max_bytes: int
    max_pages: int
    max_page_chars: int
    max_total_chars: int
    cpu_seconds: int
    wall_seconds: float
    memory_bytes: int = DEFAULT_MEMORY_BYTES

    @property
    def max_output_bytes(self) -> int:
        # json.dumps escapes every non-ASCII character to at most 12 bytes (a surrogate pair),
        # plus a small per-line envelope. RLIMIT_FSIZE on the child's output file uses this.
        return self.max_total_chars * 12 + (self.max_pages + 3) * 64 + 4096


@dataclass(frozen=True)
class PdfReadResult:
    """What was read: page texts in document order (index 0 is page 1, empty pages included).

    ``stopped_on_error`` is True when the read ended on a parser exception part-way through (not
    on a page, character, CPU, memory or time limit). In-process pypdf raised in that case, so
    ``pdf_service`` still treats such a read as no text layer and OCRs the whole document.
    """

    pages: List[str]
    page_count: int
    warnings: List[str]
    stopped_on_error: bool = False


class PdfReadError(ValueError):
    """Nothing could be read. ``str(exc)`` is a plain-language sentence safe to show a user.

    ``reason`` is one of ``too_large``, ``timeout``, ``too_complex`` (killed at its CPU or memory
    limit), ``unreadable``, ``unsafe`` or ``unavailable``.
    """

    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        self.reason = reason


def _megabytes(value: int) -> str:
    return f"{value / MIB:g}"


def pages_read_phrase(read: int, total: Optional[int] = None) -> str:
    """'pages 1–14 of 120 read' / 'page 1 read' / 'no pages read' (shared with OCR warnings)."""
    if read == 0:
        return "no pages read"
    of = f" of {total}" if total is not None else ""
    if read == 1:
        return f"page 1{of} read"
    return f"pages 1–{read}{of} read"


def _child_command(limits: PdfReadLimits) -> List[str]:
    """The child's argv (a seam: tests swap in a stand-in child to exercise the kill paths)."""
    return [
        sys.executable,
        "-m",
        "app.services.pdf_text_reader",
        "--max-bytes",
        str(limits.max_bytes),
        "--max-pages",
        str(limits.max_pages),
        "--max-page-chars",
        str(limits.max_page_chars),
        "--max-total-chars",
        str(limits.max_total_chars),
        "--cpu-seconds",
        str(limits.cpu_seconds),
        "--memory-bytes",
        str(limits.memory_bytes),
        "--max-output-bytes",
        str(limits.max_output_bytes),
    ]


def read_pdf_pages(content: bytes, limits: PdfReadLimits) -> PdfReadResult:
    """Read the text layer of ``content`` in a resource-limited child process.

    Holds a process-wide parse slot for the duration. Raises :class:`PdfReadError` when the
    document could not be opened at all; a partial read returns the pages read plus warnings.
    """
    if len(content) > limits.max_bytes:
        raise PdfReadError(
            f"The PDF is larger than {_megabytes(limits.max_bytes)} MB, so its text was not read.",
            reason="too_large",
        )
    command = _child_command(limits)
    timed_out = killed = False
    with parse_slot():
        try:
            # Output goes to a file, not a pipe: RLIMIT_FSIZE then bounds what the child can make
            # the parent read, and a killed child's flushed pages are still there to collect.
            with tempfile.TemporaryFile() as output:
                try:
                    completed = subprocess.run(  # nosec B603 - fixed argv (sys.executable -m our module), no shell
                        command,
                        input=content,
                        stdout=output,
                        stderr=subprocess.DEVNULL,
                        timeout=limits.wall_seconds,
                        cwd=_BACKEND_ROOT,
                        check=False,
                    )
                    killed = completed.returncode < 0  # a signal: RLIMIT_CPU, or the OOM killer
                except subprocess.TimeoutExpired:
                    timed_out = True
                output.seek(0)
                raw = output.read(limits.max_output_bytes + 1)
        except OSError as exc:
            logger.error("PDF text reader could not be run: %s", exc)
            raise PdfReadError(
                "The PDF reader could not be started, so the PDF's text was not read.", reason="unavailable"
            ) from exc
    return parse_reader_output(raw, limits, timed_out=timed_out, killed=killed)


def _unsafe() -> PdfReadError:
    return PdfReadError("The PDF could not be safely read.", reason="unsafe")


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def parse_reader_output(
    raw: bytes, limits: PdfReadLimits, *, timed_out: bool = False, killed: bool = False
) -> PdfReadResult:
    """Validate the child's JSON-lines output and turn it into a result (or :class:`PdfReadError`).

    Only the final line can be torn (the child was killed mid-write); reading stops there. Any
    record that breaks the protocol or the limits makes the whole read unsafe.
    """
    if len(raw) > limits.max_output_bytes:
        raise _unsafe()
    page_count: Optional[int] = None
    pages: List[str] = []
    cut_pages: List[int] = []
    total_chars = 0
    end: Optional[str] = None
    error: Optional[str] = None
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            break
        if not isinstance(record, dict):
            raise _unsafe()
        if "error" in record:
            error = str(record["error"])[:100]
            break
        if page_count is None:
            declared = record.get("page_count")
            if not _is_count(declared):
                raise _unsafe()
            page_count = declared
            continue
        if "text" in record:
            text = record["text"]
            if not isinstance(text, str) or len(text) > limits.max_page_chars:
                raise _unsafe()
            if len(pages) >= min(page_count, limits.max_pages):
                raise _unsafe()
            total_chars += len(text)
            if total_chars > limits.max_total_chars:
                raise _unsafe()
            pages.append(text)
            if record.get("cut") is True:
                cut_pages.append(len(pages))
            continue
        if "end" in record:
            end = record["end"]
            if end not in _END_REASONS:
                raise _unsafe()
            break
        raise _unsafe()

    if page_count is None:
        if error == "too_large":
            raise PdfReadError(
                f"The PDF is larger than {_megabytes(limits.max_bytes)} MB, so its text was not read.",
                reason="too_large",
            )
        if timed_out:
            raise PdfReadError(
                f"Reading the PDF took longer than {limits.wall_seconds:g} seconds, so its text was not read.",
                reason="timeout",
            )
        if killed or error in _RESOURCE_ERRORS:
            raise PdfReadError(
                "The PDF needs more processing than can be done safely, so its text was not read.",
                reason="too_complex",
            )
        if error:
            logger.info("PDF text reader could not open the document: %s", error)
        raise PdfReadError("The PDF could not be read. It may be damaged or not a PDF.", reason="unreadable")

    read = len(pages)
    warnings: List[str] = []
    if end == "complete":
        if read != page_count:
            raise _unsafe()
    elif end == "page_limit":
        if page_count <= limits.max_pages or read != limits.max_pages:
            raise _unsafe()
        warnings.append(f"Only the first {read} of {page_count} pages were read.")
    elif end == "char_limit":
        if cut_pages and cut_pages[-1] == read:
            cut_pages.pop()  # the char-limit sentence already covers the page it stopped on
        warnings.append(
            f"The PDF's text was cut off after {limits.max_total_chars:,} characters "
            f"({pages_read_phrase(read, page_count)})."
        )
    elif timed_out:
        warnings.append(
            f"Reading the PDF's text stopped after {limits.wall_seconds:g} seconds "
            f"({pages_read_phrase(read, page_count)})."
        )
    else:
        if error:
            logger.info("PDF text reader stopped at page %d: %s", read + 1, error)
        warnings.append(
            f"Reading the PDF's text stopped at page {read + 1} of {page_count} ({pages_read_phrase(read)})."
        )
    for page in cut_pages:
        warnings.append(
            f"Page {page} has more text than can be read; only its first {limits.max_page_chars:,} characters were read."
        )
    return PdfReadResult(pages=pages, page_count=page_count, warnings=warnings, stopped_on_error=error is not None)


# --------------------------------------------------------------------------- #
# Child side (``python -m app.services.pdf_text_reader``)
# --------------------------------------------------------------------------- #
def _emit(out: IO[str], record: Dict[str, Any]) -> None:
    out.write(json.dumps(record))
    out.write("\n")
    out.flush()


def stream_pages(content: bytes, *, max_pages: int, max_page_chars: int, max_total_chars: int, out: IO[str]) -> None:
    """Write the protocol records for ``content`` to ``out``. Runs in the child after the limits apply.

    Page text is exactly ``page.extract_text()`` (``None`` becomes ``""``), truncated only past
    the limits. Kept separate from :func:`main` so tests can exercise it without ``setrlimit``.
    """
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    pages = reader.pages
    page_count = len(pages)
    _emit(out, {"page_count": page_count})
    total = 0
    for index in range(min(page_count, max_pages)):
        text = pages[index].extract_text() or ""
        cut = len(text) > max_page_chars
        if cut:
            text = text[:max_page_chars]
        room = max_total_chars - total
        if len(text) > room:
            _emit(out, {"text": text[:room], "cut": True})
            _emit(out, {"end": "char_limit"})
            return
        total += len(text)
        _emit(out, {"text": text, "cut": cut})
    _emit(out, {"end": "page_limit" if page_count > max_pages else "complete"})


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "max-bytes",
        "max-pages",
        "max-page-chars",
        "max-total-chars",
        "cpu-seconds",
        "memory-bytes",
        "max-output-bytes",
    ):
        parser.add_argument(f"--{name}", type=int, required=True)
    return parser.parse_args(argv)


def _lower_limit(which: int, value: int) -> None:
    """Set soft and hard ``which`` to ``value``, or to the inherited hard limit if that is lower.

    Never raises for an already-stricter inherited limit (setrlimit refuses to raise a hard limit).
    """
    _soft, hard = resource.getrlimit(which)
    if hard != resource.RLIM_INFINITY:
        value = min(value, hard)
    resource.setrlimit(which, (value, value))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    # Apply every limit before importing the parser. RLIMIT_CPU kills a parser stuck in a loop,
    # RLIMIT_FSIZE bounds the output file, RLIMIT_CORE keeps those kills from writing core files.
    _lower_limit(resource.RLIMIT_CORE, 0)
    _lower_limit(resource.RLIMIT_CPU, args.cpu_seconds)
    _lower_limit(resource.RLIMIT_FSIZE, args.max_output_bytes)
    # macOS does not enforce address-space limits consistently. Production is Linux;
    # development still has the CPU, output and parent wall-clock limits.
    if sys.platform.startswith("linux"):
        _lower_limit(resource.RLIMIT_AS, args.memory_bytes)
    content = sys.stdin.buffer.read(args.max_bytes + 1)
    if len(content) > args.max_bytes:
        _emit(sys.stdout, {"error": "too_large"})
        return 2
    try:
        stream_pages(
            content,
            max_pages=args.max_pages,
            max_page_chars=args.max_page_chars,
            max_total_chars=args.max_total_chars,
            out=sys.stdout,
        )
    except Exception as exc:  # report the type only: the parent never shows parser messages
        _emit(sys.stdout, {"error": type(exc).__name__})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
