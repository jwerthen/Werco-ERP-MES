"""Download a bounded buyer planning PDF without saving or mutating business data."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from app.api.deps import get_current_company_id
from app.api.endpoints.quote_nesting_drafts import read_user
from app.db.database import get_db
from app.models.company import Company
from app.models.user import User
from app.schemas.nesting_buyer_pdf import MAX_REPORT_BYTES
from app.services.nesting_buyer_pdf import build_buyer_pdf, parse_report
from app.services.remnant_planning import require_inventory_access

router = APIRouter()
UPLOAD_SCHEMA = {
    'requestBody': {
        'required': True,
        'content': {
            'multipart/form-data': {
                'schema': {
                    'type': 'object',
                    'required': ['report', 'expected_company_id'],
                    'additionalProperties': False,
                    'properties': {
                        'report': {'type': 'string', 'format': 'binary'},
                        'expected_company_id': {'type': 'integer', 'minimum': 1},
                    },
                }
            }
        },
    }
}


@router.post(
    '/buyer-pdf',
    response_class=Response,
    openapi_extra=UPLOAD_SCHEMA,
    responses={200: {'content': {'application/pdf': {}}}},
)
async def generate_nesting_buyer_pdf(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(read_user),
    company_id: int = Depends(get_current_company_id),
):
    """Format complete selected local layouts; no geometry certification or inventory/quote writes."""
    if not request.headers.get('content-type', '').lower().startswith('multipart/form-data'):
        raise HTTPException(415, 'Upload a report JSON file as multipart/form-data')
    # Auth dependencies run before this parser; raw ASGI receive is already bounded.
    async with request.form(max_files=1, max_fields=1, max_part_size=4096) as form:
        if len(form.multi_items()) != 2 or set(form) != {'report', 'expected_company_id'}:
            raise HTTPException(422, 'Provide exactly report and expected_company_id, without duplicates')
        report_file, expected = form['report'], form['expected_company_id']
        if not isinstance(report_file, UploadFile):
            raise HTTPException(422, 'report must be a JSON file')
        if not isinstance(expected, str) or not expected.isascii() or not expected.isdigit() or len(expected) > 10:
            raise HTTPException(422, 'expected_company_id must be a positive integer')
        if not 1 <= int(expected) <= 2147483647:
            raise HTTPException(422, 'expected_company_id is outside the identifier range')
        if int(expected) != company_id:
            raise HTTPException(409, 'Active company changed. Reopen the report for this company.')
        content = await report_file.read(MAX_REPORT_BYTES + 1)
        if len(content) > MAX_REPORT_BYTES:
            raise HTTPException(413, 'Buyer PDF report is limited to 8 MiB')

    def render():
        report = parse_report(content)
        if report.expectedCompanyId != company_id:
            raise HTTPException(409, 'Report belongs to a different active company')
        if any(
            group.selectionKind == 'recorded_piece' or any(s.source == 'recorded_piece' for s in group.sheets)
            for group in report.groups
        ):
            require_inventory_access(db, user, company_id)
        company = db.query(Company).filter(Company.id == company_id, Company.is_active.is_(True)).first()
        if company is None:
            raise HTTPException(403, 'The active company is unavailable')
        return build_buyer_pdf(
            report, company_name=company.name, company_id=company_id, prepared_by=user.full_name, user_id=user.id
        )

    content = await run_in_threadpool(render)
    return Response(
        content,
        media_type='application/pdf',
        headers={
            'Content-Disposition': 'attachment; filename="werco-material-plan.pdf"',
            'Cache-Control': 'private, no-store',
            'X-Content-Type-Options': 'nosniff',
        },
    )
