"""Pure buyer planning PDF: local validated geometry, no persistence or purchase action."""

import json
from collections import Counter
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import ValidationError

from app.schemas.nesting_buyer_pdf import (
    MAX_PDF_BYTES,
    MAX_REPORT_BYTES,
    BuyerPdfReport,
    Circle,
    Group,
    Sheet,
)
from app.services.pdf_text import pdf_escape

_FONT_LOCK = Lock()
_FONT_GLYPHS: frozenset[int] | None = None
NAVY = '#102a43'
BLUE = '#1565a8'


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate report fields are not permitted')
        result[key] = value
    return result


def parse_report(content: bytes) -> BuyerPdfReport:
    if len(content) > MAX_REPORT_BYTES:
        raise HTTPException(413, 'Buyer PDF report is limited to 8 MiB')
    try:
        value = json.loads(
            content.decode('utf-8'),
            object_pairs_hook=_unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Report numbers must be finite')),
        )
        return BuyerPdfReport.model_validate(value)
    except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError, ValidationError) as exc:
        if isinstance(exc, ValidationError):
            error = exc.errors(include_input=False, include_url=False)[0]
            message = '.'.join(str(k) for k in error['loc'])[:160] + ': ' + error['msg'][:200]
        else:
            message = 'Report is not valid bounded JSON'
        raise HTTPException(422, message) from exc


def _fonts() -> frozenset[int]:
    global _FONT_GLYPHS
    import reportlab
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    with _FONT_LOCK:
        if _FONT_GLYPHS is None:
            root = Path(reportlab.__file__).parent / 'fonts'
            for suffix, filename in [
                ('', 'Vera.ttf'),
                ('-Bold', 'VeraBd.ttf'),
                ('-Italic', 'VeraIt.ttf'),
                ('-BoldItalic', 'VeraBI.ttf'),
            ]:
                pdfmetrics.registerFont(TTFont('BuyerVera' + suffix, str(root / filename)))
            pdfmetrics.registerFontFamily(
                'BuyerVera',
                normal='BuyerVera',
                bold='BuyerVera-Bold',
                italic='BuyerVera-Italic',
                boldItalic='BuyerVera-BoldItalic',
            )
            _FONT_GLYPHS = frozenset(pdfmetrics.getFont('BuyerVera').face.charToGlyph)
    return _FONT_GLYPHS


class _Text:
    def __init__(self):
        self.glyphs = _fonts()
        self.unsupported: set[int] = set()

    def plain(self, value: Any) -> str:
        chars = []
        for char in str(value):
            code = ord(char)
            if char in '\n\r\t' or code in self.glyphs:
                chars.append(char)
            else:
                self.unsupported.add(code)
                chars.append(f'[U+{code:04X}]')
        return ''.join(chars)

    def markup(self, value: Any) -> str:
        return pdf_escape(self.plain(value)).replace('\r\n', '\n').replace('\r', '\n').replace('\n', '<br/>')


def inch(value: float) -> str:
    return format(value, '.8g')


def _metadata(report, company_name, prepared_by):
    yield from (report.projectName, report.notes, company_name, prepared_by)
    for group in report.groups:
        yield from (group.name, group.material, group.materialDescription)
        for part in group.partRequirements:
            yield from (part.label, part.name, part.revision)
        for sheet in group.sheets:
            yield sheet.sourceLabel


class _CappedBuffer(BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_PDF_BYTES:
            raise HTTPException(413, 'Buyer PDF exceeds the 20 MiB output limit')
        return super().write(data)


def build_buyer_pdf(
    report: BuyerPdfReport,
    *,
    company_name: str,
    company_id: int,
    prepared_by: str,
    user_id: int,
    generated_at: datetime | None = None,
) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.platypus import Flowable, PageBreak, Paragraph, SimpleDocTemplate, Table, TableStyle

    text = _Text()
    for value in _metadata(report, company_name, prepared_by):
        text.plain(value)
    width, height = landscape(letter)
    doc_width = width - 64
    regular = ParagraphStyle(
        'BuyerBody',
        fontName='BuyerVera',
        fontSize=8,
        leading=11,
        textColor=colors.HexColor(NAVY),
        splitLongWords=1,
        spaceAfter=5,
    )
    small = ParagraphStyle('BuyerSmall', parent=regular, fontSize=7, leading=9)
    title = ParagraphStyle(
        'BuyerTitle', parent=regular, fontName='BuyerVera-Bold', fontSize=20, leading=25, spaceAfter=8
    )
    heading = ParagraphStyle(
        'BuyerHeading', parent=regular, fontName='BuyerVera-Bold', fontSize=12, leading=16, spaceBefore=8, spaceAfter=7
    )

    def p(value, style=regular):
        return Paragraph(text.markup(value), style)

    def table(headers, rows, widths):
        data = [[p(value, small) for value in headers], *[[p(value, small) for value in row] for row in rows]]
        result = Table(data, colWidths=widths, repeatRows=1, hAlign='LEFT', splitByRow=1, splitInRow=1)
        result.setStyle(
            TableStyle(
                [
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e5eff8')),
                    ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#bbcbd8')),
                    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                    ('LEFTPADDING', (0, 0), (-1, -1), 6),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 6),
                    ('TOPPADDING', (0, 0), (-1, -1), 5),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
                ]
            )
        )
        return result

    # Keep detached continuation pages identifiable without overwriting the Werco masthead.
    project_reference = 'Project: ' + ' '.join(text.plain(report.projectName).split())
    maximum_reference_width = doc_width - stringWidth('WERCO  /  MATERIAL PLANNING', 'BuyerVera-Bold', 8) - 24
    if stringWidth(project_reference, 'BuyerVera-Bold', 8) > maximum_reference_width:
        while (
            project_reference and stringWidth(project_reference + '...', 'BuyerVera-Bold', 8) > maximum_reference_width
        ):
            project_reference = project_reference[:-1]
        project_reference += '...'

    def footer(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor(BLUE))
        canvas.line(32, height - 23, width - 32, height - 23)
        canvas.setFont('BuyerVera-Bold', 8)
        canvas.setFillColor(colors.HexColor(NAVY))
        canvas.drawString(32, height - 18, 'WERCO  /  MATERIAL PLANNING')
        canvas.drawRightString(width - 32, height - 18, project_reference)
        canvas.setFont('BuyerVera', 7)
        canvas.drawString(32, 18, 'Planning only - not a purchase order, NC program or material certification')
        canvas.drawRightString(width - 32, 18, f'Page {document.page}')
        canvas.restoreState()

    class Layout(Flowable):
        def __init__(self, sheet: Sheet, group: Group):
            super().__init__()
            self.width = doc_width
            self.height = 305
            self.sheet = sheet
            self.refs = {part.id: str(index + 1) for index, part in enumerate(group.partRequirements)}

        def draw(self):
            canvas = self.canv
            sheet = self.sheet
            scale = min((self.width - 28) / sheet.lengthIn, (self.height - 32) / sheet.widthIn)
            left = (self.width - sheet.lengthIn * scale) / 2
            bottom = (self.height - sheet.widthIn * scale) / 2
            canvas.saveState()
            canvas.translate(left, bottom)

            def path_for(loops):
                path = canvas.beginPath()
                for loop in loops:
                    if isinstance(loop, Circle):
                        # Four cubic arcs draw an analytic circle at PDF display scale.
                        path.ellipse(
                            (loop.cx - loop.r) * scale,
                            (loop.cy - loop.r) * scale,
                            2 * loop.r * scale,
                            2 * loop.r * scale,
                        )
                    else:
                        path.moveTo(loop.points[0].x * scale, loop.points[0].y * scale)
                        for point in loop.points[1:]:
                            path.lineTo(point.x * scale, point.y * scale)
                        path.close()
                return path

            canvas.setLineWidth(0.8)
            canvas.setStrokeColor(colors.HexColor(NAVY))
            canvas.setFillColor(colors.HexColor('#f3f6f8'))
            canvas.drawPath(path_for([sheet.outer, *sheet.holes]), stroke=1, fill=1, fillMode=0)
            if sheet.source == 'purchase' and 2 * sheet.marginIn < min(sheet.lengthIn, sheet.widthIn):
                canvas.saveState()
                canvas.setDash(3, 3)
                canvas.setStrokeColor(colors.HexColor('#607d8b'))
                canvas.rect(
                    sheet.marginIn * scale,
                    sheet.marginIn * scale,
                    (sheet.lengthIn - 2 * sheet.marginIn) * scale,
                    (sheet.widthIn - 2 * sheet.marginIn) * scale,
                    stroke=1,
                    fill=0,
                )
                canvas.restoreState()
            # Physical holes remain empty; unavailable zones retain their actual outline.
            for zone in sheet.exclusions:
                canvas.setFillColor(colors.HexColor('#f9dfb4'))
                canvas.setStrokeColor(colors.HexColor('#a35a16'))
                canvas.drawPath(path_for([zone.outline]), stroke=1, fill=1)
            for placed in sheet.placements:
                canvas.setFillColor(colors.HexColor('#cbe1f4'))
                canvas.setStrokeColor(colors.HexColor(BLUE))
                canvas.drawPath(path_for(placed.loops), stroke=1, fill=1, fillMode=0)
                outer = placed.loops[0]
                if isinstance(outer, Circle):
                    x, y = outer.cx, outer.cy
                else:
                    x = sum(point.x for point in outer.points) / len(outer.points)
                    y = sum(point.y for point in outer.points) / len(outer.points)
                canvas.setFont('BuyerVera-Bold', 7)
                canvas.setFillColor(colors.HexColor(NAVY))
                canvas.drawCentredString(x * scale, y * scale, self.refs[placed.partId])
            canvas.restoreState()

    buffer = _CappedBuffer()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=(width, height),
        leftMargin=32,
        rightMargin=32,
        topMargin=36,
        bottomMargin=32,
        title=text.plain(report.projectName),
        author=text.plain(prepared_by),
        invariant=1,
    )
    generated = (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    story: list[Any] = [
        p('MATERIAL BUY LIST', title),
        p(report.projectName, heading),
        p(f'{company_name} (company {company_id}) | Prepared by {prepared_by} (user {user_id})'),
        p('Generated ' + generated.astimezone(ZoneInfo('America/Chicago')).strftime('%Y-%m-%d %I:%M:%S %p %Z')),
        p(
            'Selected local planning layouts. Counts describe the selected alternatives only. '
            'This formatter does not certify geometry, material eligibility or physical availability.'
        ),
        p(f'Input fingerprint (SHA-256): {report.inputSha256} | Solver: {report.solverVersion}', small),
    ]
    if text.unsupported:
        story.append(
            p(
                'Font notice: unsupported characters are represented visibly as [U+XXXX] code points; '
                'these markers preserve the original character identity. Codes: '
                + ', '.join(f'U+{value:04X}' for value in sorted(text.unsupported)),
                small,
            )
        )
    if report.notes:
        story.extend([p('Buyer notes', heading), p(report.notes)])
    rows = []
    for index, group in enumerate(report.groups, 1):
        counts = Counter((s.widthIn, s.lengthIn) for s in group.sheets if s.source == 'purchase')
        for (sheet_width, sheet_length), count in counts.items():
            rows.append(
                [
                    f'G{index} / {group.name}',
                    group.material + '\n' + group.materialDescription,
                    inch(group.thicknessIn),
                    f'{inch(sheet_width)} x {inch(sheet_length)}',
                    str(count),
                ]
            )
        if not counts:
            rows.append(
                [
                    f'G{index} / {group.name}',
                    group.material + '\n' + group.materialDescription,
                    inch(group.thicknessIn),
                    'No full sheets in this conditional selection',
                    '0',
                ]
            )
    story.extend(
        [
            p('Purchase summary - full sheets only', heading),
            table(
                ['Group', 'Material / description', 'Thickness (in)', 'Width x length (in)', 'Buy sheets'],
                rows,
                [145, 250, 88, 155, 90],
            ),
        ]
    )
    for index, group in enumerate(report.groups, 1):
        if group.selectionKind == 'recorded_piece':
            story.append(
                p(f'CONDITIONAL G{index} - verify the recorded piece before relying on this buy list', heading)
            )
            pieces = [s.sourceLabel for s in group.sheets if s.source == 'recorded_piece']
            story.append(
                p(
                    'Reported piece used once: '
                    + ('; '.join(pieces) if pieces else 'none placed')
                    + '. Availability and eligibility unverified; not reserved or consumed. No credit assigned.'
                )
            )
            story.append(
                p(
                    f'If the piece cannot be used, the fallback below REPLACES the selected purchase quantities for G{index}. '
                    'Do not add these fallback sheets to the selected purchase quantities for that group.'
                )
            )
            story.append(
                table(
                    [f'G{index} full-sheet fallback - replacement quantities', 'Width x length (in)', 'Buy sheets'],
                    [
                        [
                            group.material + ' / ' + inch(group.thicknessIn) + ' in',
                            f'{inch(s.widthIn)} x {inch(s.lengthIn)}',
                            str(s.quantity),
                        ]
                        for s in group.baselinePurchaseSheets
                    ],
                    [395, 243, 90],
                )
            )
        story.extend(
            [
                p(f'G{index} - {group.name}: complete part requirements', heading),
                table(
                    [f'G{index} / Ref', 'Part / drawing', 'Revision', 'Total quantity'],
                    [
                        [f'{n+1} / {part.label}', part.name, part.revision or 'Not specified', str(part.quantity)]
                        for n, part in enumerate(group.partRequirements)
                    ],
                    [98, 400, 140, 90],
                ),
            ]
        )
    for index, group in enumerate(report.groups, 1):
        for sheet in group.sheets:
            story.extend(
                [
                    PageBreak(),
                    p(
                        f'G{index} / Sheet {sheet.number} - '
                        + ('Purchased full sheet' if sheet.source == 'purchase' else 'Recorded piece - unverified'),
                        heading,
                    ),
                    p(
                        f'{group.material} | {inch(group.thicknessIn)} in thick | '
                        f'{inch(sheet.widthIn)} x {inch(sheet.lengthIn)} in (width x length)'
                    ),
                    p(sheet.sourceLabel, small),
                    p(
                        f'Margin {inch(sheet.marginIn)} in | Part gap {inch(sheet.gapIn)} in | '
                        'Drawing fitted to page; not 1:1. Blue: parts. Amber: unavailable areas. '
                        + (
                            'Dashed line: sheet margin.'
                            if sheet.source == 'purchase'
                            else 'Physical source boundary shown.'
                        ),
                        small,
                    ),
                    Layout(sheet, group),
                ]
            )
            if sheet.exclusions:
                story.append(
                    p(
                        'Additional exclusion clearances (in), in shown source order: '
                        + ', '.join(inch(zone.clearanceIn) for zone in sheet.exclusions),
                        small,
                    )
                )
            counts = Counter(placed.partId for placed in sheet.placements)
            story.append(
                table(
                    [f'G{index} / Sheet {sheet.number} / Ref', 'Part / drawing', 'Revision', 'On this sheet'],
                    [
                        [f'{n+1} / {part.label}', part.name, part.revision or 'Not specified', str(counts[part.id])]
                        for n, part in enumerate(group.partRequirements)
                        if counts[part.id]
                    ],
                    [98, 400, 140, 90],
                )
            )
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()
