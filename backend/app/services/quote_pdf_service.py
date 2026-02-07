"""Customer-ready quote PDF generator for AI RFQ estimates."""

from io import BytesIO
from typing import Any, Dict, List, Optional


def build_customer_quote_pdf(
    *,
    quote_number: str,
    revision: str,
    customer_name: str,
    customer_contact: Optional[str],
    customer_email: Optional[str],
    rfq_reference: Optional[str],
    quote_date: str,
    valid_until: Optional[str],
    lead_time_label: Optional[str],
    total_amount: float,
    line_summaries: List[Dict[str, Any]],
    assumptions: List[Dict[str, Any]],
    exclusions: List[str],
) -> bytes:
    """
    Build a customer-ready quote PDF that omits operation-time line details.
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except Exception as exc:
        raise RuntimeError(f"reportlab is required for quote PDF generation: {exc}")

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        topMargin=36,
        bottomMargin=36,
        leftMargin=36,
        rightMargin=36,
        title=f"Quote {quote_number}",
    )
    styles = getSampleStyleSheet()
    story: List[Any] = []

    story.append(Paragraph(f"<b>Customer Quote {quote_number} Rev {revision}</b>", styles["Title"]))
    story.append(Spacer(1, 8))
    story.append(Paragraph(f"<b>Customer:</b> {customer_name}", styles["Normal"]))
    if customer_contact:
        story.append(Paragraph(f"<b>Contact:</b> {customer_contact}", styles["Normal"]))
    if customer_email:
        story.append(Paragraph(f"<b>Email:</b> {customer_email}", styles["Normal"]))
    if rfq_reference:
        story.append(Paragraph(f"<b>RFQ Reference:</b> {rfq_reference}", styles["Normal"]))
    story.append(Paragraph(f"<b>Quote Date:</b> {quote_date}", styles["Normal"]))
    if valid_until:
        story.append(Paragraph(f"<b>Valid Until:</b> {valid_until}", styles["Normal"]))
    if lead_time_label:
        story.append(Paragraph(f"<b>Lead Time:</b> {lead_time_label}", styles["Normal"]))
    story.append(Spacer(1, 12))

    table_rows = [["Part", "Qty", "Material", "Thickness", "Finish", "Line Total"]]
    for line in line_summaries:
        table_rows.append(
            [
                line.get("part_display") or "-",
                str(line.get("qty") or 1),
                line.get("material") or "TBD",
                line.get("thickness") or "TBD",
                line.get("finish") or "-",
                f"${float(line.get('part_total') or 0):,.2f}",
            ]
        )

    table = Table(table_rows, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                ("ALIGN", (1, 1), (1, -1), "RIGHT"),
                ("ALIGN", (-1, 1), (-1, -1), "RIGHT"),
            ]
        )
    )
    story.append(table)
    story.append(Spacer(1, 12))

    story.append(Paragraph(f"<b>Total Quote:</b> ${total_amount:,.2f}", styles["Heading3"]))
    story.append(Spacer(1, 10))

    if assumptions:
        story.append(Paragraph("<b>Assumptions</b>", styles["Heading4"]))
        for item in assumptions:
            field = item.get("field", "item")
            assumption = item.get("assumption", "")
            story.append(Paragraph(f"- {field}: {assumption}", styles["Normal"]))
        story.append(Spacer(1, 8))

    if exclusions:
        story.append(Paragraph("<b>Exclusions / Notes</b>", styles["Heading4"]))
        for exclusion in exclusions:
            story.append(Paragraph(f"- {exclusion}", styles["Normal"]))

    doc.build(story)
    return buffer.getvalue()
