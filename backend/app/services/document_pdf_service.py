"""Shared customer/supplier document rendering for downloads and email snapshots."""

from datetime import datetime
from io import BytesIO

from fastapi import HTTPException

from app.services.pdf_text import pdf_escape
from app.services.quote_pdf_service import build_customer_quote_pdf


def quote_pdf_context(db, quote, company_id, lock=False):
    from app.models.rfq_quote import QuoteEstimate, QuoteLineSummary, RfqPackage

    query = (
        db.query(QuoteEstimate)
        .filter(QuoteEstimate.quote_id == quote.id, QuoteEstimate.company_id == company_id)
        .order_by(QuoteEstimate.created_at.desc(), QuoteEstimate.id.desc())
    )
    estimate = (query.with_for_update() if lock else query).populate_existing().first()
    if not estimate:
        return dict(rfq_reference=None, assumptions=[], line_metadata=[])
    package_query = db.query(RfqPackage).filter(
        RfqPackage.id == estimate.rfq_package_id, RfqPackage.company_id == company_id
    )
    package = (package_query.with_for_update() if lock else package_query).populate_existing().first()
    lines_query = (
        db.query(QuoteLineSummary)
        .filter(QuoteLineSummary.quote_estimate_id == estimate.id, QuoteLineSummary.company_id == company_id)
        .order_by(QuoteLineSummary.id)
    )
    metadata = (lines_query.with_for_update() if lock else lines_query).populate_existing().all()
    return dict(
        rfq_reference=(package.rfq_reference or package.rfq_number) if package else None,
        assumptions=estimate.assumptions or [],
        line_metadata=[
            dict(
                part_number=line.part_number,
                part_name=line.part_name,
                material=line.material,
                thickness=line.thickness,
                finish=line.finish,
            )
            for line in metadata
        ],
    )


def build_quote_document(db, quote, company_id):
    context = quote_pdf_context(db, quote, company_id)

    def formatted(value):
        return value.strftime('%m/%d/%Y') if value else None

    # Commercial quantities/prices always come from the current saved quote.
    # AI estimate assumptions remain useful context, but its old pricing is not
    # authoritative after a planner edits the quote.
    lines = []
    for line in sorted(quote.lines, key=lambda row: (row.line_number, row.id)):
        if line.company_id != company_id or (line.part and line.part.company_id != company_id):
            raise HTTPException(404, 'Quote line part not found')
        matching = [
            meta
            for meta in context['line_metadata']
            if (line.part and meta['part_number'] == line.part.part_number)
            or (not line.part and meta['part_name'] == line.description)
        ]
        metadata = matching[0] if len(matching) == 1 else {}
        lines.append(
            dict(
                part_display=f'{line.part.part_number} - {line.description}' if line.part else line.description,
                qty=line.quantity,
                material=metadata.get("material"),
                thickness=metadata.get("thickness"),
                finish=metadata.get("finish"),
                part_total=line.line_total,
            )
        )
    return build_customer_quote_pdf(
        quote_number=quote.quote_number,
        revision=quote.revision or 'A',
        customer_name=quote.customer_name,
        customer_contact=quote.customer_contact,
        customer_email=quote.customer_email,
        rfq_reference=context["rfq_reference"],
        quote_date=formatted(quote.quote_date) or '',
        valid_until=formatted(quote.valid_until),
        lead_time_label=f'{quote.lead_time_days} business days' if quote.lead_time_days else None,
        total_amount=float(quote.total or 0),
        line_summaries=lines,
        assumptions=context["assumptions"],
        exclusions=[
            'Quote excludes taxes, freight, and duties unless stated otherwise.',
            'Subject to drawing/specification review at order entry.',
            'Operation-level cycle times are internal and not included in customer quote.',
        ],
    )


def build_purchase_order_document(print_data: dict) -> bytes:
    """Server PDF of the existing PrintPurchaseOrder vendor-facing template.

    Same print-data contract, grouped delivery dates, acknowledgement, received/
    backorder columns, contact/address/shipping blocks and monetary totals.
    Long descriptions wrap and table headers repeat across page breaks.
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    data = print_data
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        topMargin=32,
        bottomMargin=34,
        leftMargin=32,
        rightMargin=32,
        title=f"Purchase Order {data['po_number']}",
        invariant=1,
    )
    styles = getSampleStyleSheet()
    small = ParagraphStyle('PODetail', parent=styles['Normal'], fontSize=8, leading=11)
    cell = ParagraphStyle('POCell', parent=small, fontSize=7.5, leading=10)

    def paragraph(value, style=small):
        return Paragraph(pdf_escape(str(value or '-')).replace('\n', '<br/>'), style)

    def block(title, lines):
        return [
            Paragraph(f'<b>{pdf_escape(title)}</b>', styles['Heading4']),
            *[paragraph(line) for line in lines if line],
        ]

    def money_number(value):
        return float(str(value or '0').replace(',', '').replace('$', ''))

    def footer(canvas, document):
        canvas.setFont('Helvetica', 8)
        canvas.drawString(32, 18, data['po_number'])
        canvas.drawRightString(letter[0] - 32, 18, f'Page {document.page}')

    story = []
    heading = Table(
        [
            [
                block(
                    'WERCO MANUFACTURING',
                    ['415 East Houston Street', 'Broken Arrow, OK 74012', 'Phone 918.251.6880 - Fax 918.251.5397'],
                ),
                block('PURCHASE ORDER', [data['po_number'], f"Printed {data['printed_at']}"]),
            ]
        ],
        colWidths=[320, 228],
    )
    heading.setStyle(
        TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LINEBELOW', (0, 0), (-1, -1), 1, colors.black)])
    )
    story.extend([heading, Spacer(1, 12)])
    boxes = Table(
        [
            [
                block(
                    'Supplier',
                    [
                        data['vendor_name'],
                        data.get('vendor_address'),
                        f"Contact: {data['vendor_contact']}" if data.get('vendor_contact') else None,
                        f"Phone: {data['vendor_phone']}" if data.get('vendor_phone') else None,
                        f"Email: {data['vendor_email']}" if data.get('vendor_email') else None,
                    ],
                ),
                block(
                    'PO Details',
                    [
                        f"Order Date: {data.get('order_date') or '-'}",
                        f"Required Date: {data.get('required_date') or '-'}",
                        f"Expected Date: {data.get('expected_date') or '-'}",
                        f"Buyer: {data.get('buyer_name') or 'Werco Purchasing'}",
                        f"Buyer Email: {data['buyer_email']}" if data.get('buyer_email') else None,
                    ],
                ),
            ],
            [
                block('Ship To', [data.get('ship_to') or 'Werco Manufacturing - Receiving']),
                block('Shipping Method', [data.get('shipping_method') or '-']),
            ],
        ],
        colWidths=[274, 274],
    )
    boxes.setStyle(
        TableStyle(
            [
                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#a0a0a0')),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('LEFTPADDING', (0, 0), (-1, -1), 8),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 9),
            ]
        )
    )
    story.extend(
        [
            boxes,
            Spacer(1, 12),
            paragraph(
                'Please acknowledge orders with price and delivery. Include PO number on invoices, B/L, bundles, cases, and packing lists.'
            ),
            Spacer(1, 8),
        ]
    )
    grouped = {}
    for line in data['lines']:
        grouped.setdefault(line.get('required_date') or 'AS AVAILABLE', []).append(line)
    rows = [
        [
            paragraph(label, cell)
            for label in ['Qty', 'Received', 'Backorder', 'Material Description', 'Price Each', 'Ext. Total']
        ]
    ]
    spans = []
    for group in sorted(
        grouped, key=lambda key: datetime.strptime(key, '%m/%d/%Y') if key != 'AS AVAILABLE' else datetime.max
    ):
        spans.append(len(rows))
        rows.append(
            [
                paragraph(
                    'TO BE DELIVERED AS AVAILABLE' if group == 'AS AVAILABLE' else f'TO BE DELIVERED ON {group}', cell
                ),
                '',
                '',
                '',
                '',
                '',
            ]
        )
        for line in grouped[group]:
            remaining = max(0, money_number(line['quantity_ordered']) - money_number(line['quantity_received']))
            rows.append(
                [
                    paragraph(value, cell)
                    for value in [
                        line['quantity_ordered'],
                        line['quantity_received'],
                        f'{remaining:g}',
                        f"{line['part_number']}\n{line['part_name']}",
                        line['unit_price'],
                        line['line_total'],
                    ]
                ]
            )
    table = Table(rows, colWidths=[38, 45, 49, 274, 69, 73], repeatRows=1, hAlign='LEFT')
    commands = [
        ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#a0a0a0')),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e5e7eb')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]
    for row in spans:
        commands.extend(
            [('SPAN', (0, row), (-1, row)), ('BACKGROUND', (0, row), (-1, row), colors.HexColor('#f3f4f6'))]
        )
    table.setStyle(TableStyle(commands))
    story.extend([table, Spacer(1, 12)])
    totals = Table(
        [
            [paragraph(label), paragraph(data[key])]
            for label, key in [('Subtotal', 'subtotal'), ('Tax', 'tax'), ('Shipping', 'shipping'), ('Total', 'total')]
        ],
        colWidths=[115, 115],
        hAlign='RIGHT',
    )
    totals.setStyle(TableStyle([('LINEABOVE', (0, -1), (-1, -1), 0.7, colors.black)]))
    story.append(KeepTogether([totals, Spacer(1, 8)]))
    if data.get('notes'):
        story.extend(block('Notes', [data['notes']]))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()
