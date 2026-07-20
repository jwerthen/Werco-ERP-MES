"""Deterministic page splitting for multi-nest laser-report PDFs.

A bare multi-page PDF upload is segmented (by the AI segmentation pass, or by
the planner-confirmed rows on import) into per-nest page ranges; this module
turns those ranges into real per-segment PDF files with DETERMINISTIC names so
the preview and the later confirm-and-commit import derive the exact same file
name for the same page range — the name is the row key that survives the round
trip through the wizard, with no AI and no server-side state in between.

Everything here is pure pypdf mechanics: no LLM calls, no DB access, no tenant
data. Errors raise ``ValueError`` for the endpoints to translate into a 400.
"""

from __future__ import annotations

import os
from pathlib import Path

from pypdf import PdfReader, PdfWriter

# Cumulative output cap for one split. pypdf copies each page's full resource
# tree into its output, so shared resources (one big image XObject referenced
# by every page) multiply: a crafted 45 MB 50-page PDF could otherwise write
# ~2 GB of segment files per request. Benign shared-letterhead documents stay
# an order of magnitude below 10x source; the absolute floor keeps small
# sources from tripping on legitimately image-heavy pages.
_MAX_SPLIT_TOTAL_BYTES_FLOOR = 256 * 1024 * 1024
_MAX_SPLIT_SOURCE_MULTIPLE = 10


def get_pdf_page_count(pdf_path: str) -> int:
    """Return the page count of a local PDF. Raises ``ValueError`` when the file
    cannot be parsed as a PDF (corrupt, encrypted-unreadable, or not a PDF)."""
    try:
        reader = PdfReader(pdf_path)
        count = len(reader.pages)
    except Exception as exc:  # noqa: BLE001 - pypdf raises a zoo of types; all mean "unreadable"
        raise ValueError(f"Could not read the PDF: {exc}") from exc
    if count < 1:
        raise ValueError("Could not read the PDF: it has no pages")
    return count


def segment_file_name(pages: list[int]) -> str:
    """Deterministic per-segment file name for a 1-based, ascending page list.

    ``nest-p{first:03d}.pdf`` for a single page, ``nest-p{first:03d}-p{last:03d}.pdf``
    for a range. Segments partition the document (no two share a first page) and
    the zero-padding keeps lexicographic order == page order, so sorting the
    split files by name reproduces segment order — the property the preview's
    stable-ordered extraction fan-out relies on.
    """
    if not pages:
        raise ValueError("A nest segment must contain at least one page")
    first, last = pages[0], pages[-1]
    if len(pages) == 1:
        return f"nest-p{first:03d}.pdf"
    return f"nest-p{first:03d}-p{last:03d}.pdf"


def split_pdf_segments(pdf_path: str, segments: list[list[int]], dest_dir: str) -> list[str]:
    """Write one PDF per segment (1-based page-number lists) into ``dest_dir``.

    Returns the relative file names in segment order, named by
    ``segment_file_name`` so preview and import derive identical names for
    identical ranges. Raises ``ValueError`` on an unreadable source PDF, an
    empty/out-of-range segment, two segments with the same derived name
    (identical ranges) — duplicates would silently overwrite one another's
    bytes and double-import a nest — or when the cumulative bytes written
    exceed the amplification cap (see module constants).
    """
    try:
        source_size = os.path.getsize(pdf_path)
        reader = PdfReader(pdf_path)
        page_count = len(reader.pages)
    except Exception as exc:  # noqa: BLE001 - see get_pdf_page_count
        raise ValueError(f"Could not read the PDF: {exc}") from exc

    total_bytes_cap = max(source_size * _MAX_SPLIT_SOURCE_MULTIPLE, _MAX_SPLIT_TOTAL_BYTES_FLOOR)
    total_bytes_written = 0

    os.makedirs(dest_dir, exist_ok=True)
    names: list[str] = []
    seen: set[str] = set()
    for pages in segments:
        if not pages:
            raise ValueError("A nest segment must contain at least one page")
        for page in pages:
            if not isinstance(page, int) or page < 1 or page > page_count:
                raise ValueError(f"Nest segment page {page} is out of range (PDF has {page_count} pages)")
        name = segment_file_name(pages)
        if name in seen:
            raise ValueError(f"Duplicate nest segment: pages {pages} appear more than once")
        seen.add(name)

        writer = PdfWriter()
        for page in pages:
            writer.add_page(reader.pages[page - 1])
        out_path = Path(dest_dir) / name
        with open(out_path, "wb") as handle:
            writer.write(handle)
        total_bytes_written += out_path.stat().st_size
        if total_bytes_written > total_bytes_cap:
            # Reap everything this call wrote before failing, so an aborted
            # split can't leave gigabytes behind in the scan/package dir.
            for written in names + [name]:
                try:
                    (Path(dest_dir) / written).unlink(missing_ok=True)
                except OSError:
                    pass
            raise ValueError(
                "Splitting this PDF would write an unreasonable amount of data "
                "(its pages share large embedded resources). Split the document "
                "into smaller files and upload those instead."
            )
        names.append(name)
    return names


def is_bare_pdf_upload(filename: str, content_type: str | None) -> bool:
    """True when an upload is a single bare PDF (vs the ZIP package shape)."""
    if content_type == "application/pdf":
        return True
    return (filename or "").lower().endswith(".pdf")
