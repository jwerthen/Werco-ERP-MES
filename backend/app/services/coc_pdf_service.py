"""Certificate of Conformance (CoC) PDF generator (G6-B).

A CoC is a frozen-snapshot compliance artifact: the row stores the immutable
certified facts at issue time and this module renders the PDF DETERMINISTICALLY
from those facts on download -- there is no filesystem blob. Mirrors
``quote_pdf_service`` exactly: a lazy reportlab import guarded so a missing
dependency raises ``RuntimeError`` rather than an obscure ``ImportError`` at
module-load, a ``BytesIO`` buffer + ``SimpleDocTemplate(letter)``, and
``bytes`` returned. Optional fields are omitted when falsy.
"""

from io import BytesIO
from typing import Any, List, Optional

# AS9100D-style conformance statement stamped onto every CoC (the certified facts
# above it identify the specific articles). Stored on the row at issue time so the
# rendered PDF is self-contained and the statement is frozen with the snapshot.
DEFAULT_COC_STATEMENT = (
    "We hereby certify that the articles identified above were manufactured, processed, "
    "and inspected in accordance with the applicable drawings, specifications, and "
    "purchase-order requirements, and that they conform thereto. Objective-evidence "
    "records are on file and available for review. This certificate is issued under our "
    "AS9100D / ISO 9001 quality management system."
)


def build_certificate_of_conformance_pdf(
    *,
    coc_number: str,
    customer_name: Optional[str],
    customer_po: Optional[str],
    work_order_number: Optional[str],
    part_number: Optional[str],
    part_name: Optional[str],
    revision: Optional[str],
    quantity: Optional[float],
    lot_number: Optional[str],
    serial_numbers: List[str],
    ship_date: Optional[str],
    conformance_statement: Optional[str],
    issued_by_name: Optional[str],
    issued_at: Optional[str],
) -> bytes:
    """Build a Certificate of Conformance PDF from frozen-snapshot facts.

    Returns the rendered PDF as ``bytes``. All optional fields are omitted from
    the rendered document when falsy (mirrors ``quote_pdf_service``).
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except Exception as exc:
        raise RuntimeError(f"reportlab is required for CoC PDF generation: {exc}")

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        topMargin=36,
        bottomMargin=36,
        leftMargin=36,
        rightMargin=36,
        title=f"Certificate of Conformance {coc_number}",
    )
    styles = getSampleStyleSheet()
    story: List[Any] = []

    # Title
    story.append(Paragraph("<b>Certificate of Conformance</b>", styles["Title"]))
    story.append(Spacer(1, 8))

    # Header block
    story.append(Paragraph(f"<b>Certificate Number:</b> {coc_number}", styles["Normal"]))
    if ship_date:
        story.append(Paragraph(f"<b>Ship Date:</b> {ship_date}", styles["Normal"]))
    if issued_at:
        story.append(Paragraph(f"<b>Date Issued:</b> {issued_at}", styles["Normal"]))
    if issued_by_name:
        story.append(Paragraph(f"<b>Issued By:</b> {issued_by_name}", styles["Normal"]))
    story.append(Spacer(1, 12))

    # Customer block
    if customer_name:
        story.append(Paragraph(f"<b>Customer:</b> {customer_name}", styles["Normal"]))
    if customer_po:
        story.append(Paragraph(f"<b>Customer PO:</b> {customer_po}", styles["Normal"]))
    story.append(Spacer(1, 12))

    # Product block
    if work_order_number:
        story.append(Paragraph(f"<b>Work Order:</b> {work_order_number}", styles["Normal"]))
    if part_number or part_name:
        part_display = part_number or ""
        if part_name:
            part_display = f"{part_display} — {part_name}" if part_display else part_name
        story.append(Paragraph(f"<b>Part:</b> {part_display}", styles["Normal"]))
    if revision:
        story.append(Paragraph(f"<b>Revision:</b> {revision}", styles["Normal"]))
    if quantity is not None:
        story.append(Paragraph(f"<b>Quantity:</b> {quantity:g}", styles["Normal"]))
    story.append(Spacer(1, 12))

    # Lot / serial table: per-serial rows when serialized, else a single lot row.
    if serial_numbers:
        table_rows = [["Serial Number", "Lot Number"]]
        for serial in serial_numbers:
            table_rows.append([str(serial), lot_number or "-"])
    else:
        table_rows = [["Lot Number"], [lot_number or "-"]]

    lot_table = Table(table_rows, repeatRows=1)
    lot_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1B4D9C")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
            ]
        )
    )
    story.append(lot_table)
    story.append(Spacer(1, 16))

    # Conformance statement
    statement = conformance_statement or DEFAULT_COC_STATEMENT
    story.append(Paragraph(statement, styles["Normal"]))
    story.append(Spacer(1, 24))

    # Signature block
    story.append(
        Paragraph(
            "Authorized Signature: ______________________&nbsp;&nbsp;&nbsp;Date: ____________",
            styles["Normal"],
        )
    )
    story.append(Spacer(1, 10))
    story.append(
        Paragraph(
            "Name: ______________________&nbsp;&nbsp;&nbsp;Title: ______________________",
            styles["Normal"],
        )
    )

    doc.build(story)
    return buffer.getvalue()
