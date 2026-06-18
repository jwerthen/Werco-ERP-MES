"""4x6 thermal receiving-label PDF renderer.

A receiving label is a deterministic, monochrome artifact rendered with reportlab
onto a fixed 4x6 inch page (the WHTP203e direct-thermal media). It is sent to a
ProxyBox Zero bridge as a base64 PDF, so this module returns ``bytes`` and never
touches the filesystem.

DESIGN NOTES / DECISIONS:
- Fixed page geometry via ``canvas.Canvas(pagesize=(4*inch, 6*inch))`` -- NOT
  ``SimpleDocTemplate``. A label has an exact physical size; we lay out top->bottom
  with absolute coordinates.
- MONOCHROME ONLY. The WHTP203e is a direct-thermal printer with no color; the
  CRITICAL banner is a filled black rectangle with reversed (white) text, never a
  colored fill.
- ``canvas.drawString`` does NOT wrap. Every free-text field is measured with
  ``pdfmetrics.stringWidth`` and truncated with an ellipsis to fit the print
  width (the ``_fit`` helper).
- Barcode: Code128 of the lot number via ``reportlab.graphics.barcode.code128``
  (no new dependency), with the human-readable lot drawn beneath the bars.
- ``fmt`` is reserved so a ZPL fast-path can be added later; only ``"pdf"`` is
  implemented now (anything else raises ``ValueError``).
- The reportlab import is lazy + guarded: a missing dependency raises a clear
  ``RuntimeError`` rather than an obscure ``ImportError`` at module load (mirrors
  ``coc_pdf_service`` / ``quote_pdf_service``).
"""

from __future__ import annotations

from io import BytesIO
from typing import List, Optional

# Page geometry constants are expressed in points (1 inch = 72 pt). They are kept
# module-level (not inside the guarded function) so they are documented in one place;
# they do not import reportlab.
_PAGE_WIDTH_IN = 4.0
_PAGE_HEIGHT_IN = 6.0
_MARGIN_PT = 14.0  # ~0.19" quiet zone on every edge


def _fit(text: Optional[str], font: str, size: float, max_width: float) -> str:
    """Truncate ``text`` (with an ellipsis) so it fits ``max_width`` at ``font``/``size``.

    ``canvas.drawString`` does not wrap, so any free-text field that could overflow
    the label width is clamped here. Returns "" for falsy input. Measures with
    ``pdfmetrics.stringWidth`` (the same metric the canvas uses).
    """
    from reportlab.pdfbase.pdfmetrics import stringWidth

    if not text:
        return ""
    text = str(text)
    if stringWidth(text, font, size) <= max_width:
        return text

    ellipsis = "…"
    # Drop trailing characters until the text + ellipsis fits.
    trimmed = text
    while trimmed and stringWidth(trimmed + ellipsis, font, size) > max_width:
        trimmed = trimmed[:-1]
    return (trimmed + ellipsis) if trimmed else ellipsis


def build_receiving_label_pdf(
    *,
    part_number: Optional[str],
    revision: Optional[str],
    part_description: Optional[str],
    quantity,
    unit_of_measure: Optional[str],
    lot_number: str,
    serial_numbers: Optional[str] = None,
    heat_number: Optional[str] = None,
    po_number: Optional[str] = None,
    vendor_name: Optional[str] = None,
    receipt_number: Optional[str] = None,
    received_date: Optional[str] = None,
    location_code: Optional[str] = None,
    is_critical: bool = False,
    fmt: str = "pdf",
) -> bytes:
    """Render a 4x6 receiving label and return it as ``bytes`` (starts with ``%PDF``).

    Layout, top -> bottom:
      1. Reversed solid-black "CRITICAL CHARACTERISTIC" banner (only when
         ``is_critical``).
      2. Part number + Rev (largest, bold).
      3. Description (measured + truncated to the print width).
      4. QTY + UOM (bold, prominent).
      5. Traceability block: Lot (always), Heat (if present), Serial (if present).
      6. Source block: PO #, Vendor, Receipt #, Received date.
      7. Destination: "BIN: {location_code}" (bold).
      8. Code128 barcode of the lot number, with the human-readable lot beneath.

    ``fmt`` is reserved for a future ZPL fast-path; only ``"pdf"`` is implemented.
    """
    if fmt != "pdf":
        # Keep the renderer behind a format arg so a ZPL fast-path can slot in later
        # without changing the call sites; only PDF is implemented now.
        raise ValueError(f"Unsupported label format {fmt!r}; only 'pdf' is implemented")

    try:
        from reportlab.graphics.barcode import code128
        from reportlab.lib.units import inch
        from reportlab.pdfbase.pdfmetrics import stringWidth
        from reportlab.pdfgen import canvas
    except Exception as exc:  # noqa: BLE001 - surface a clear setup error, not ImportError
        raise RuntimeError(f"reportlab is required for receiving-label PDF generation: {exc}")

    page_width = _PAGE_WIDTH_IN * inch
    page_height = _PAGE_HEIGHT_IN * inch
    left = _MARGIN_PT
    right = page_width - _MARGIN_PT
    content_width = right - left

    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=(page_width, page_height))
    c.setTitle(f"Receiving label {receipt_number or lot_number}")

    # Cursor walks DOWN from the top margin (PDF origin is bottom-left).
    y = page_height - _MARGIN_PT

    def draw_line(text: str, font: str, size: float, *, gap: float = 3.0) -> None:
        """Draw one left-aligned, width-fitted line and advance the cursor."""
        nonlocal y
        y -= size
        c.setFont(font, size)
        c.drawString(left, y, _fit(text, font, size, content_width))
        y -= gap

    def draw_label_value(label: str, value: str, font_size: float = 9.0) -> None:
        """Draw "LABEL: value" with the label bold and the value regular, width-fitted."""
        nonlocal y
        y -= font_size
        prefix = f"{label}: "
        c.setFont("Helvetica-Bold", font_size)
        c.drawString(left, y, prefix)
        prefix_w = stringWidth(prefix, "Helvetica-Bold", font_size)
        c.setFont("Helvetica", font_size)
        c.drawString(left + prefix_w, y, _fit(value, "Helvetica", font_size, content_width - prefix_w))
        y -= 3.0

    # 1. CRITICAL banner -- reversed (white-on-black), monochrome.
    if is_critical:
        banner_h = 22.0
        y -= banner_h
        c.setFillGray(0.0)  # black fill
        c.rect(left, y, content_width, banner_h, stroke=0, fill=1)
        c.setFillGray(1.0)  # white text
        c.setFont("Helvetica-Bold", 11)
        banner_text = "! CRITICAL CHARACTERISTIC !"
        text_w = stringWidth(banner_text, "Helvetica-Bold", 11)
        c.drawString(left + (content_width - text_w) / 2.0, y + (banner_h - 11) / 2.0 + 1.0, banner_text)
        c.setFillGray(0.0)  # restore black for the rest of the label
        y -= 8.0

    # 2. Part number + revision (largest, bold).
    part_line = part_number or "(no part number)"
    if revision:
        part_line = f"{part_line}  Rev {revision}"
    draw_line(part_line, "Helvetica-Bold", 18, gap=2.0)

    # 3. Description (truncated to fit).
    if part_description:
        draw_line(part_description, "Helvetica", 9, gap=6.0)
    else:
        y -= 4.0

    # 4. Quantity + UOM (bold, prominent).
    qty_text = _format_quantity(quantity)
    uom = (unit_of_measure or "").strip()
    qty_line = f"QTY: {qty_text}{(' ' + uom) if uom else ''}"
    draw_line(qty_line, "Helvetica-Bold", 16, gap=8.0)

    # 5. Traceability block.
    draw_label_value("LOT", lot_number, font_size=11)
    if heat_number:
        draw_label_value("HEAT", heat_number, font_size=9)
    serials = _format_serials(serial_numbers)
    if serials:
        draw_label_value("SERIAL", serials, font_size=9)
    y -= 5.0

    # 6. Source block.
    if po_number:
        draw_label_value("PO", po_number, font_size=9)
    if vendor_name:
        draw_label_value("VENDOR", vendor_name, font_size=9)
    if receipt_number:
        draw_label_value("RECEIPT", receipt_number, font_size=9)
    if received_date:
        draw_label_value("RECEIVED", received_date, font_size=9)
    y -= 6.0

    # 7. Destination bin (bold).
    if location_code:
        draw_line(f"BIN: {location_code}", "Helvetica-Bold", 14, gap=8.0)

    # 8. Code128 barcode of the lot number, with the human-readable lot beneath.
    # Anchor it near the bottom margin regardless of how much text was drawn above.
    _draw_lot_barcode(
        c,
        code128_module=code128,
        string_width=stringWidth,
        lot_number=lot_number,
        left=left,
        content_width=content_width,
        bottom=_MARGIN_PT,
    )

    c.showPage()
    c.save()
    return buffer.getvalue()


def _draw_lot_barcode(
    c,
    *,
    code128_module,
    string_width,
    lot_number: str,
    left: float,
    content_width: float,
    bottom: float,
) -> None:
    """Draw a Code128 barcode of ``lot_number`` anchored at the page bottom.

    The bar width is chosen so the symbol fits the content width; the
    human-readable lot is centered beneath the bars (drawString does not wrap, so
    it is width-fitted).
    """
    barcode_height = 46.0
    human_size = 9.0
    human_gap = 3.0

    # Pick the largest bar width whose symbol still fits the content width.
    bar_width = 1.1
    barcode = None
    for candidate in (1.4, 1.2, 1.0, 0.85, 0.7, 0.6, 0.5):
        bc = code128_module.Code128(lot_number, barHeight=barcode_height, barWidth=candidate)
        if bc.width <= content_width:
            barcode = bc
            bar_width = candidate
            break
    if barcode is None:
        # Fall back to the smallest bar width even if it slightly overflows.
        barcode = code128_module.Code128(lot_number, barHeight=barcode_height, barWidth=bar_width)

    # Center the symbol horizontally within the content area.
    x = left + max(0.0, (content_width - barcode.width) / 2.0)
    human_y = bottom
    bars_y = bottom + human_size + human_gap
    barcode.drawOn(c, x, bars_y)

    # Human-readable lot, centered beneath the bars.
    c.setFont("Helvetica", human_size)
    human_text = _fit(lot_number, "Helvetica", human_size, content_width)
    text_w = string_width(human_text, "Helvetica", human_size)
    c.drawString(left + max(0.0, (content_width - text_w) / 2.0), human_y, human_text)


def _format_quantity(quantity) -> str:
    """Render a quantity without a trailing ``.0`` for whole numbers."""
    if quantity is None:
        return "0"
    try:
        value = float(quantity)
    except (TypeError, ValueError):
        return str(quantity)
    if value == int(value):
        return str(int(value))
    # Trim trailing zeros on fractional quantities.
    return f"{value:g}"


def _format_serials(serial_numbers: Optional[str]) -> Optional[str]:
    """Normalize a comma/JSON-ish serial field into a compact display string.

    ``POReceipt.serial_numbers`` is free text ("comma-separated or JSON"); we only
    need a human-readable summary on the label, so we collapse whitespace and join
    on commas. Returns ``None`` when there is nothing to show.
    """
    if not serial_numbers:
        return None
    raw = str(serial_numbers).strip()
    if not raw:
        return None
    # Best-effort: split on commas, drop empties, re-join. JSON arrays render fine
    # as-is for a label summary, so we do not attempt to parse them.
    parts: List[str] = [p.strip() for p in raw.replace("\n", ",").split(",")]
    parts = [p for p in parts if p]
    return ", ".join(parts) if parts else None
