"""Unit tests for the deterministic laser-nest PDF split service.

``laser_nest_pdf_split_service`` is pure pypdf mechanics — no LLM, no DB — so
these tests build tiny REAL PDFs with pypdf and exercise the actual split.

Pinned contract:
  * ``get_pdf_page_count`` — correct count; ``ValueError`` on unreadable/empty.
  * ``segment_file_name`` — ``nest-p{first:03d}.pdf`` / ``nest-p{first:03d}-p{last:03d}.pdf``
    with zero-padding so lexicographic order == page order (the property the
    preview's sorted-glob extraction fan-out relies on).
  * ``split_pdf_segments`` — one output PDF per segment carrying exactly the
    requested source pages under the deterministic name; ``ValueError`` on an
    unreadable source, an empty/out-of-range segment, or duplicate segments.
  * ``is_bare_pdf_upload`` — content-type or ``.pdf``-suffix detection.
"""

from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

from app.services.laser_nest_pdf_split_service import (
    get_pdf_page_count,
    is_bare_pdf_upload,
    segment_file_name,
    split_pdf_segments,
)

pytestmark = pytest.mark.unit

# Distinct width per page so a split segment's pages are identifiable: source
# page N (1-based) has width _BASE_WIDTH + N.
_BASE_WIDTH = 200.0
_PAGE_HEIGHT = 792.0


def _make_pdf(path: Path, page_count: int) -> Path:
    """Write a real PDF whose page N (1-based) is blank with width _BASE_WIDTH+N."""
    writer = PdfWriter()
    for n in range(1, page_count + 1):
        writer.add_blank_page(width=_BASE_WIDTH + n, height=_PAGE_HEIGHT)
    with open(path, "wb") as handle:
        writer.write(handle)
    return path


def _source_page_numbers(pdf_path: Path) -> list[int]:
    """Recover which SOURCE pages a (split) PDF contains, via the width tag."""
    reader = PdfReader(str(pdf_path))
    return [round(float(page.mediabox.width) - _BASE_WIDTH) for page in reader.pages]


# --------------------------------------------------------------------------- #
# get_pdf_page_count
# --------------------------------------------------------------------------- #
class TestGetPdfPageCount:
    def test_counts_pages_of_a_real_pdf(self, tmp_path):
        pdf = _make_pdf(tmp_path / "nests.pdf", 4)
        assert get_pdf_page_count(str(pdf)) == 4

    def test_single_page(self, tmp_path):
        pdf = _make_pdf(tmp_path / "one.pdf", 1)
        assert get_pdf_page_count(str(pdf)) == 1

    def test_unreadable_bytes_raise_value_error(self, tmp_path):
        garbage = tmp_path / "not-a-pdf.pdf"
        garbage.write_bytes(b"this is definitely not a pdf")
        with pytest.raises(ValueError, match="Could not read the PDF"):
            get_pdf_page_count(str(garbage))

    def test_missing_file_raises_value_error(self, tmp_path):
        with pytest.raises(ValueError, match="Could not read the PDF"):
            get_pdf_page_count(str(tmp_path / "missing.pdf"))

    def test_zero_page_pdf_raises_value_error(self, tmp_path):
        """A structurally valid PDF with no pages is unusable for nest import."""
        empty = tmp_path / "empty.pdf"
        with open(empty, "wb") as handle:
            PdfWriter().write(handle)
        with pytest.raises(ValueError, match="no pages"):
            get_pdf_page_count(str(empty))


# --------------------------------------------------------------------------- #
# segment_file_name — the deterministic row key
# --------------------------------------------------------------------------- #
class TestSegmentFileName:
    def test_single_page_name(self):
        assert segment_file_name([3]) == "nest-p003.pdf"

    def test_multi_page_range_name(self):
        assert segment_file_name([3, 4]) == "nest-p003-p004.pdf"
        assert segment_file_name([9, 10, 11]) == "nest-p009-p011.pdf"

    def test_empty_pages_raise(self):
        with pytest.raises(ValueError, match="at least one page"):
            segment_file_name([])

    def test_zero_padding_keeps_lexicographic_order_equal_to_page_order(self):
        """Sorting split files by NAME must reproduce page order — the preview
        relies on sorted-glob order matching segment order."""
        names = [segment_file_name([first]) for first in (1, 2, 9, 10, 11, 100)]
        assert names == sorted(names)

    def test_same_pages_always_derive_the_same_name(self):
        """Preview and import derive the row key independently; determinism is
        the whole point."""
        assert segment_file_name([7, 8]) == segment_file_name([7, 8])


# --------------------------------------------------------------------------- #
# split_pdf_segments — real pypdf split
# --------------------------------------------------------------------------- #
class TestSplitPdfSegments:
    def test_splits_into_named_segments_with_exact_pages(self, tmp_path):
        pdf = _make_pdf(tmp_path / "nests.pdf", 5)
        dest = tmp_path / "out"

        names = split_pdf_segments(str(pdf), [[1], [2, 3], [4, 5]], str(dest))

        # Deterministic names, in segment order (single-page and range shapes).
        assert names == ["nest-p001.pdf", "nest-p002-p003.pdf", "nest-p004-p005.pdf"]
        # Each output file exists and contains EXACTLY the requested source pages.
        assert _source_page_numbers(dest / "nest-p001.pdf") == [1]
        assert _source_page_numbers(dest / "nest-p002-p003.pdf") == [2, 3]
        assert _source_page_numbers(dest / "nest-p004-p005.pdf") == [4, 5]
        # And nothing else was written.
        assert sorted(p.name for p in dest.iterdir()) == sorted(names)

    def test_multi_page_segment_preserves_page_order(self, tmp_path):
        pdf = _make_pdf(tmp_path / "nests.pdf", 3)
        dest = tmp_path / "out"

        names = split_pdf_segments(str(pdf), [[1, 2, 3]], str(dest))

        assert names == ["nest-p001-p003.pdf"]
        assert _source_page_numbers(dest / "nest-p001-p003.pdf") == [1, 2, 3]

    def test_creates_missing_dest_dir(self, tmp_path):
        pdf = _make_pdf(tmp_path / "nests.pdf", 1)
        dest = tmp_path / "deep" / "nested" / "dir"

        names = split_pdf_segments(str(pdf), [[1]], str(dest))

        assert (dest / names[0]).is_file()

    @pytest.mark.parametrize("bad_page", [0, -1, 6], ids=["zero", "negative", "past_end"])
    def test_out_of_range_page_raises(self, tmp_path, bad_page):
        pdf = _make_pdf(tmp_path / "nests.pdf", 5)
        with pytest.raises(ValueError, match="out of range"):
            split_pdf_segments(str(pdf), [[bad_page]], str(tmp_path / "out"))

    def test_non_int_page_raises(self, tmp_path):
        pdf = _make_pdf(tmp_path / "nests.pdf", 2)
        with pytest.raises(ValueError, match="out of range"):
            split_pdf_segments(str(pdf), [["1"]], str(tmp_path / "out"))

    def test_empty_segment_raises(self, tmp_path):
        pdf = _make_pdf(tmp_path / "nests.pdf", 2)
        with pytest.raises(ValueError, match="at least one page"):
            split_pdf_segments(str(pdf), [[1], []], str(tmp_path / "out"))

    def test_duplicate_segment_raises(self, tmp_path):
        """Two identical ranges derive the same name — they would silently
        overwrite each other's bytes and double-import a nest."""
        pdf = _make_pdf(tmp_path / "nests.pdf", 3)
        with pytest.raises(ValueError, match="Duplicate nest segment"):
            split_pdf_segments(str(pdf), [[1, 2], [1, 2]], str(tmp_path / "out"))

    def test_duplicate_single_page_segment_raises(self, tmp_path):
        pdf = _make_pdf(tmp_path / "nests.pdf", 3)
        with pytest.raises(ValueError, match="Duplicate nest segment"):
            split_pdf_segments(str(pdf), [[2], [2]], str(tmp_path / "out"))

    def test_unreadable_source_raises(self, tmp_path):
        garbage = tmp_path / "garbage.pdf"
        garbage.write_bytes(b"nope")
        with pytest.raises(ValueError, match="Could not read the PDF"):
            split_pdf_segments(str(garbage), [[1]], str(tmp_path / "out"))

    def test_out_of_range_rejected_before_any_file_written(self, tmp_path):
        """A later-segment failure must not leave earlier outputs behind as a
        half-split (the validation loop runs per segment before its write, so an
        invalid FIRST segment writes nothing)."""
        pdf = _make_pdf(tmp_path / "nests.pdf", 2)
        dest = tmp_path / "out"
        with pytest.raises(ValueError):
            split_pdf_segments(str(pdf), [[99], [1]], str(dest))
        assert not any(dest.iterdir())


# --------------------------------------------------------------------------- #
# is_bare_pdf_upload — upload-shape detection
# --------------------------------------------------------------------------- #
class TestIsBarePdfUpload:
    @pytest.mark.parametrize(
        ("filename", "content_type", "expected"),
        [
            ("nests.pdf", "application/pdf", True),
            ("nests.pdf", None, True),
            ("NESTS.PDF", "application/octet-stream", True),  # suffix check is case-insensitive
            ("nests.pdf", "application/zip", True),  # suffix wins even with an odd content type
            ("upload.bin", "application/pdf", True),  # content type wins even with an odd name
            ("nests.zip", "application/zip", False),
            ("nests.zip", None, False),
            ("", None, False),
            ("nests", "application/octet-stream", False),
            ("nests.pdf.zip", "application/zip", False),
        ],
    )
    def test_detection_matrix(self, filename, content_type, expected):
        assert is_bare_pdf_upload(filename, content_type) is expected

    def test_none_filename_is_tolerated(self):
        # The endpoint passes `file.filename or ""`, but the helper guards anyway.
        assert is_bare_pdf_upload(None, None) is False


class TestAmplificationCap:
    """The cumulative-output cap: pypdf copies shared resources into every
    segment, so a crafted source could otherwise amplify to gigabytes."""

    def test_split_over_cumulative_cap_rejected_and_reaped(self, tmp_path, monkeypatch):
        import app.services.laser_nest_pdf_split_service as split_svc

        src = tmp_path / "src.pdf"
        _make_pdf(src, 4)
        # Force the cap under the real output size: any written segment trips it.
        monkeypatch.setattr(split_svc, "_MAX_SPLIT_TOTAL_BYTES_FLOOR", 1)
        monkeypatch.setattr(split_svc, "_MAX_SPLIT_SOURCE_MULTIPLE", 0)

        dest = tmp_path / "out"
        with pytest.raises(ValueError, match="unreasonable amount of data"):
            split_pdf_segments(str(src), [[1], [2]], str(dest))
        # Everything written before the abort is reaped.
        assert list(dest.glob("*.pdf")) == []

    def test_split_under_cap_unaffected(self, tmp_path):
        src = tmp_path / "src.pdf"
        _make_pdf(src, 3)
        dest = tmp_path / "out"
        names = split_pdf_segments(str(src), [[1, 2], [3]], str(dest))
        assert names == ["nest-p001-p002.pdf", "nest-p003.pdf"]
