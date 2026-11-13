"""Printable Corrective Action Report (CAR) in the layout of Werco's controlled form.

Werco's form ("AS9100 PROCESS - Corrective Action Report", ``CAR Template - 2.docx``) is
one bordered grid, top to bottom:

1. Initiated by | RMA # | Corrective Action Number
2. Date Submitted | (blank) | Date Due
3. Part number (if applicable) | P.O number (if C/A applies to a vendor) | Customer
4. Reason for initiation -- eight boxes, one ticked, "Other ____" filled in
5. Describe the Problem
6. Describe the Root Cause
7. Describe the Corrective Action taken
8. Corrective Action Cost: Labor / Material / Outside Services / Shipping-Transportation
9. Vendor Management Approval - signature and title (if C/A applies to a vendor) | Date
10. Management Action
11. Customer Approval | Date | Quality Manager Approval | Date

:func:`build_corrective_action_report_pdf` is a PURE builder: it takes primitives (already
resolved, tenant-scoped, by ``quality_record_service.car_print_context``), touches no
database, and returns ``bytes``. reportlab is imported lazily, like ``coc_pdf_service`` and
``quote_pdf_service``, so a missing dependency is a ``RuntimeError`` at render time rather
than an import failure at boot.

Three rules hold here:

* **Every value spliced into a ``Paragraph`` goes through ``pdf_escape``.** ``Paragraph``
  parses a mini-HTML dialect; an unescaped ``<`` in a problem description (``OD<ID``, a
  ``<REF>`` dimension) is a hard ``ValueError`` -- an HTTP 500 on a quality record -- and an
  unescaped ``<b>`` silently alters it. ``tests/test_escape_at_sink_guards.py`` scans this
  module. The literal ``<b>`` / ``<br/>`` / ``&nbsp;`` markup is ours and stays unescaped.
* **Checkboxes are ASCII** (``[X]`` / ``[ ]``): the standard Helvetica the PDF uses has no
  ballot-box glyphs, and a missing glyph prints as a black square that reads as "ticked".
* **Nothing is truncated.** A CAR is a quality record; cutting its text to fit one page
  would corrupt it. The free-text boxes may split across pages (``splitInRow``), and every
  page carries a footer with the CAR number and the page number.
"""

from io import BytesIO
from typing import Any, List, Optional, Sequence, Tuple

from app.services.pdf_text import pdf_escape

FORM_TITLE = "AS9100 PROCESS - Corrective Action Report"

# The eight "Reason for initiation" boxes, in the form's order. Values are
# ``app.models.quality.CARInitiationReason`` values (this module stays model-free).
REASON_BOXES: Tuple[Tuple[str, str], ...] = (
    ("management_review", "Management Review"),
    ("nonconformance_trend", "Non Conformance Trend"),
    ("customer_feedback", "Customer Feedback"),
    ("internal_audit", "Internal Audit"),
    ("production_problems", "Production Problems"),
    ("external_audit", "External Audit"),
    ("continuous_improvement", "Continuous Improvement"),
    ("other", "Other"),
)

_PAGE_MARGIN = 32
_CONTENT_WIDTH = 548  # letter width (612) less two 32pt margins


def checkbox(ticked: bool) -> str:
    """The ASCII checkbox the form's reason row prints."""
    return "[X]" if ticked else "[ ]"


def build_corrective_action_report_pdf(
    *,
    car_number: str,
    company_name: str,
    company_address: Optional[str],
    company_phone: Optional[str],
    initiated_by: Optional[str],
    rma_number: Optional[str],
    submitted_date: Optional[str],
    due_date: Optional[str],
    part_number: Optional[str],
    vendor_po_number: Optional[str],
    vendor_name: Optional[str],
    customer_name: Optional[str],
    initiation_reason: Optional[str],
    initiation_reason_other: Optional[str],
    problem_sections: Sequence[Tuple[Optional[str], Optional[str]]],
    root_cause_sections: Sequence[Tuple[Optional[str], Optional[str]]],
    corrective_action_sections: Sequence[Tuple[Optional[str], Optional[str]]],
    cost_labor: Optional[str],
    cost_material: Optional[str],
    cost_outside_services: Optional[str],
    cost_shipping: Optional[str],
    cost_total: Optional[str],
    vendor_approval_name: Optional[str],
    vendor_approval_title: Optional[str],
    vendor_approval_date: Optional[str],
    management_action: Optional[str],
    customer_approval_name: Optional[str],
    customer_approval_date: Optional[str],
    quality_approved_by: Optional[str],
    quality_approved_date: Optional[str],
    status_label: Optional[str] = None,
) -> bytes:
    """Render one CAR as a PDF in the controlled form's layout.

    Every argument is an already-formatted display string (or ``None`` for blank). The
    ``*_sections`` arguments are ``(label, text)`` pairs rendered in order inside their box:
    a ``None`` label prints the text alone, so the form's single answer stays unlabelled
    while related ERP text (e.g. the root-cause analysis) is printed under its own label
    rather than dropped. Sections whose text is blank are skipped.
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except Exception as exc:  # pragma: no cover - reportlab is a pinned dependency
        raise RuntimeError(f"reportlab is required for CAR PDF generation: {exc}")

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        topMargin=_PAGE_MARGIN,
        bottomMargin=40,
        leftMargin=_PAGE_MARGIN,
        rightMargin=_PAGE_MARGIN,
        title=f"Corrective Action Report {car_number}",
        invariant=1,
    )
    styles = getSampleStyleSheet()
    body = ParagraphStyle("CARBody", parent=styles["Normal"], fontSize=8.5, leading=11)
    heading = ParagraphStyle("CARHeading", parent=styles["Heading4"], spaceBefore=0, spaceAfter=2)

    def lines(value: Any) -> List[Any]:
        """One escaped Paragraph per line of ``value``, so its line breaks survive (blank -> [])."""
        if value is None or not str(value).strip():
            return []
        return [Paragraph(pdf_escape(line) if line.strip() else "&nbsp;", body) for line in str(value).splitlines()]

    def field(label: str, value: Any) -> List[Any]:
        """A grid cell: the form's bold label, then the value on the lines below it."""
        return [Paragraph(f"<b>{pdf_escape(label)}</b>", body), *lines(value)]

    def boxed(rows: List[List[Any]], col_widths: List[float], spans: Sequence[Tuple] = ()) -> Any:
        table = Table(rows, colWidths=col_widths, hAlign="LEFT", splitInRow=1)
        commands = [
            ("GRID", (0, 0), (-1, -1), 0.6, colors.black),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]
        commands.extend(("SPAN",) + span for span in spans)
        table.setStyle(TableStyle(commands))
        return table

    def text_box(label: str, sections: Sequence[Tuple[Optional[str], Optional[str]]]) -> Any:
        """A full-width box: the form's label, then each non-blank section (may split pages)."""
        cell: List[Any] = [Paragraph(f"<b>{pdf_escape(label)}</b>", body)]
        for section_label, section_text in sections:
            if section_text is None or not str(section_text).strip():
                continue
            if section_label:
                cell.append(Paragraph(f"<b>{pdf_escape(section_label)}:</b>", body))
            cell.extend(lines(section_text))
        if len(cell) == 1:
            cell.append(Spacer(1, 30))  # an empty box keeps room to write in, like the paper form
        return boxed([[cell]], [_CONTENT_WIDTH])

    def footer(canvas, document):
        # drawString draws glyphs and parses no markup, so no escaping applies here.
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.drawString(_PAGE_MARGIN, 20, f"{car_number} - {FORM_TITLE}")
        canvas.drawRightString(letter[0] - _PAGE_MARGIN, 20, f"Page {document.page}")
        canvas.restoreState()

    story: List[Any] = []

    # --- header: the company (from the Company record, never hard-coded) and the form title
    company_lines: List[Any] = [Paragraph(f"<b>{pdf_escape(company_name)}</b>", heading)]
    for line in (company_address or "").splitlines():
        if line.strip():
            company_lines.append(Paragraph(pdf_escape(line.strip()), body))
    if company_phone:
        company_lines.append(Paragraph(f"Phone {pdf_escape(company_phone)}", body))
    title_lines: List[Any] = [
        Paragraph(f"<b>{pdf_escape(FORM_TITLE)}</b>", heading),
        Paragraph(f"<b>Corrective Action Number:</b> {pdf_escape(car_number)}", body),
    ]
    if status_label:
        title_lines.append(Paragraph(f"<b>Status:</b> {pdf_escape(status_label)}", body))
    header = Table([[company_lines, title_lines]], colWidths=[290, 258], hAlign="LEFT")
    header.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, -1), 1, colors.black),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.extend([header, Spacer(1, 10)])

    # --- rows 1-3: the identification grid (three cells per row, as on the form)
    third = _CONTENT_WIDTH / 3
    vendor_cell = vendor_po_number or ""
    if vendor_name:
        vendor_cell = f"{vendor_cell}\nVendor: {vendor_name}" if vendor_cell else f"Vendor: {vendor_name}"
    story.append(
        boxed(
            [
                [
                    field("Initiated by:", initiated_by),
                    field("RMA #:", rma_number),
                    field("Corrective Action Number:", car_number),
                ],
                [field("Date Submitted:", submitted_date), "", field("Date Due:", due_date)],
                [
                    field("Part number (if applicable):", part_number),
                    field("P.O number (if C/A applies to a vendor):", vendor_cell),
                    field("Customer", customer_name),
                ],
            ],
            [third, third, third],
        )
    )

    # --- row 4: reason for initiation (ASCII boxes, four to a line like the form)
    def reason_box(value: str, label: str) -> Any:
        mark = checkbox(initiation_reason == value)
        if value != "other":
            return Paragraph(f"{pdf_escape(mark)} {pdf_escape(label)}", body)
        other_text = initiation_reason_other if initiation_reason == "other" and initiation_reason_other else None
        return Paragraph(f"{pdf_escape(mark)} {pdf_escape(label)}: {pdf_escape(other_text or '______________')}", body)

    boxes = [reason_box(value, label) for value, label in REASON_BOXES]
    reason_grid = Table([boxes[:4], boxes[4:]], colWidths=[(_CONTENT_WIDTH - 10) / 4] * 4, hAlign="LEFT")
    reason_grid.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(boxed([[[Paragraph("<b>Reason for initiation</b>", body), reason_grid]]], [_CONTENT_WIDTH]))

    # --- rows 5-7: the three narrative boxes
    story.append(text_box("Describe the Problem", problem_sections))
    story.append(
        text_box(
            "Describe the Root Cause (Use additional sheets of plain paper if more room is needed for any of "
            "these answers)",
            root_cause_sections,
        )
    )
    story.append(
        text_box(
            "Describe the Corrective Action taken (Be specific-tell how as well as what was done.)",
            corrective_action_sections,
        )
    )

    # --- row 8: cost of the corrective action (blank is not zero: an unentered bucket prints blank)
    def money(value: Optional[str]) -> str:
        return f"$ {value}" if value else "$ ________"

    fifth = _CONTENT_WIDTH / 5
    story.append(
        boxed(
            [
                [Paragraph("<b>Corrective Action Cost</b>", body), "", "", "", ""],
                [
                    Paragraph(f"<b>Labor Cost</b><br/>{pdf_escape(money(cost_labor))}", body),
                    Paragraph(f"<b>Material</b><br/>{pdf_escape(money(cost_material))}", body),
                    Paragraph(f"<b>Outside Services</b><br/>{pdf_escape(money(cost_outside_services))}", body),
                    Paragraph(f"<b>Shipping/Transportation</b><br/>{pdf_escape(money(cost_shipping))}", body),
                    Paragraph(f"<b>Total</b><br/>{pdf_escape(money(cost_total))}", body),
                ],
            ],
            [fifth] * 5,
            spans=[((0, 0), (-1, 0))],
        )
    )

    # --- row 9: vendor management approval (signature line, then the recorded name / title)
    signed_by = ", ".join(part for part in (vendor_approval_name, vendor_approval_title) if part)
    story.append(
        boxed(
            [
                [
                    Paragraph(
                        "<b>Vendor Management Approval - signature and title (if C/A applies to a vendor)</b>"
                        f"<br/><br/>Signature: ________________________________<br/>{pdf_escape(signed_by)}",
                        body,
                    ),
                    field("Date", vendor_approval_date),
                ]
            ],
            [_CONTENT_WIDTH * 0.72, _CONTENT_WIDTH * 0.28],
        )
    )

    # --- row 10: management action
    story.append(text_box("Management Action", [(None, management_action)]))

    # --- row 11: customer approval / date / quality manager approval / date
    quarter = _CONTENT_WIDTH / 4
    story.append(
        boxed(
            [
                [
                    field("Customer Approval", customer_approval_name),
                    field("Date", customer_approval_date),
                    Paragraph(
                        "<b>Quality Manager Approval</b>"
                        f"<br/>{pdf_escape(quality_approved_by)}<br/><br/>Signature: ______________",
                        body,
                    ),
                    field("Date", quality_approved_date),
                ]
            ],
            [quarter * 1.15, quarter * 0.85, quarter * 1.15, quarter * 0.85],
        )
    )

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()
