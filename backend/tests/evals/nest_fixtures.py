"""Synthetic laser-nest report PDF fixtures for the native-PDF eval.

Real CAM nest reports (SigmaNEST / Ermaksan) aren't in the repo and can't be —
they'd carry customer part numbers. So we SYNTHESIZE digital nest-report-style
PDFs with reportlab (already a project dep) where the field values are KNOWN.

The whole point of the native-PDF path is layout-aware vision: it reads each
field from its own labeled position on a 2-D sheet instead of a flattened 1-D
string. To exercise that, the synthetic sheet deliberately:

* puts the material grade on a SEPARATE "machine" line from the CNC number and
  thickness (the classic "machine name confused for material" trap), and
* places numeric fields (CNC number, thickness, sheet size) in distinct labeled
  blocks rather than glued together.

Two variants are produced from the SAME known values:

* ``build_digital_nest_pdf`` — a real text-layer PDF (the layout-aware win case).
* ``build_scanned_nest_pdf`` — an image-only PDF (no text layer) standing in for
  a scanned sheet. Prefers rasterizing the digital PDF via pdf2image (needs
  poppler); falls back to drawing the layout straight to a PIL raster so the
  fixture is available even without poppler installed.

All fixture values are invented and contain no real customer/vendor content.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class NestFixture:
    """A synthetic nest report plus the ground-truth fields it encodes."""

    pdf_bytes: bytes
    file_name: str
    expected: Dict[str, str]


# Known ground truth shared by both variants. Values are invented.
_EXPECTED: Dict[str, str] = {
    "cnc_number": "05749",
    "material": "A36",
    "thickness": "0.250 in",
    "sheet_size": "60 x 120",
}
_FILE_NAME = "05749.pdf"

# Layout lines: (x, y, text). The material grade lives on the machine line,
# separated from the CNC number / thickness blocks on purpose.
_LAYOUT = [
    (72, 740, "WERCO NEST REPORT"),
    (72, 712, "CNC Program No: 05749"),
    (360, 712, "Date: 2026-06-24"),
    (72, 672, "MACHINE: Ermaksan Fiber 6kW    Grade / Material: A36"),
    (72, 632, "Thickness: 0.250 in"),
    (360, 632, "Sheet Size: 60 x 120"),
    (72, 592, "Parts on sheet: 14    Utilization: 78.4%"),
    (72, 552, "Planned Runs: 3"),
]


def _draw_layout(canvas_obj) -> None:
    canvas_obj.setFont("Helvetica", 11)
    for x, y, text in _LAYOUT:
        canvas_obj.drawString(x, y, text)


def build_digital_nest_pdf() -> NestFixture:
    """A digital (text-layer) nest-report PDF with the known field values."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    _draw_layout(c)
    c.showPage()
    c.save()
    return NestFixture(pdf_bytes=buf.getvalue(), file_name=_FILE_NAME, expected=dict(_EXPECTED))


def _scanned_via_pdf2image() -> bytes:
    """Rasterize the digital PDF to an image-only PDF (needs poppler). Raises if
    poppler/pdf2image isn't available so the caller can fall back."""
    from pdf2image import convert_from_bytes
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    digital = build_digital_nest_pdf().pdf_bytes
    pages = convert_from_bytes(digital, dpi=200)
    image = pages[0]

    out = io.BytesIO()
    width, height = letter
    c = canvas.Canvas(out, pagesize=letter)
    c.drawImage(ImageReader(image), 0, 0, width=width, height=height)
    c.showPage()
    c.save()
    return out.getvalue()


def _scanned_via_pil() -> bytes:
    """Draw the layout straight to a raster (no poppler needed) and embed it as an
    image-only PDF. The result has NO text layer, like a scan."""
    from PIL import Image, ImageDraw
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    # ~200 dpi letter canvas.
    raster_w, raster_h = 1700, 2200
    image = Image.new("RGB", (raster_w, raster_h), "white")
    draw = ImageDraw.Draw(image)
    page_w, page_h = letter
    scale_x = raster_w / page_w
    scale_y = raster_h / page_h
    for x, y, text in _LAYOUT:
        # Reportlab origin is bottom-left; PIL origin is top-left -> flip y.
        draw.text((x * scale_x, (page_h - y) * scale_y), text, fill="black")

    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=letter)
    c.drawImage(ImageReader(image), 0, 0, width=page_w, height=page_h)
    c.showPage()
    c.save()
    return out.getvalue()


def build_scanned_nest_pdf() -> NestFixture:
    """An image-only (no text layer) nest report with the known field values.

    Prefers pdf2image rasterization (closest to a real scan); falls back to a
    PIL-drawn raster when poppler isn't installed so the fixture always builds.
    """
    try:
        pdf_bytes = _scanned_via_pdf2image()
    except Exception:
        pdf_bytes = _scanned_via_pil()
    return NestFixture(pdf_bytes=pdf_bytes, file_name=_FILE_NAME, expected=dict(_EXPECTED))
