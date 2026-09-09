"""Independent tenant, read-purity and pre-parser boundary checks for buyer PDFs."""

import json
from copy import deepcopy

import pytest
from fastapi import HTTPException
from sqlalchemy import event
from starlette.requests import Request

from app.api.endpoints import nesting_buyer_pdf as endpoint
from app.core.security import create_access_token
from app.middleware.nesting_buyer_pdf_body_limit import NestingBuyerPdfBodyLimitMiddleware
from app.models.company import Company
from app.models.role_permission import RolePermission
from app.models.user import UserRole
from app.schemas.nesting_buyer_pdf import MAX_REQUEST_BYTES
from app.services.nesting_buyer_pdf import parse_report

BASE = '/api/v1/quote-nesting/buyer-pdf'


def report(company=1, conditional=False):
    outer = {'type': 'poly', 'points': [{'x': 0, 'y': 0}, {'x': 8, 'y': 0}, {'x': 8, 'y': 4}, {'x': 0, 'y': 4}]}
    return {
        'version': 1,
        'units': 'in',
        'expectedCompanyId': company,
        'projectName': 'Synthetic buyer check',
        'notes': '',
        'inputSha256': 'a' * 64,
        'solverVersion': 'werco-contour-v7',
        'groups': [
            {
                'id': 'group',
                'name': 'Group',
                'material': 'Carbon steel',
                'materialDescription': 'Grade needs confirmation',
                'thicknessIn': 0.125,
                'selectionKind': 'recorded_piece' if conditional else 'full_sheet',
                'partRequirements': [{'id': 'part', 'label': 'P1', 'name': 'Plate', 'revision': '', 'quantity': 1}],
                'baselinePurchaseSheets': [{'widthIn': 4, 'lengthIn': 8, 'quantity': 1}] if conditional else [],
                'sheets': [
                    {
                        'number': 1,
                        'source': 'recorded_piece' if conditional else 'purchase',
                        'sourceLabel': 'Synthetic stock',
                        'widthIn': 4,
                        'lengthIn': 8,
                        'marginIn': 0.375,
                        'gapIn': 0.125,
                        'outer': outer,
                        'holes': [],
                        'exclusions': [],
                        'placements': [
                            {
                                'partId': 'part',
                                'originalInstance': 0,
                                'loops': [{'type': 'circle', 'cx': 2, 'cy': 2, 'r': 0.5}],
                            }
                        ],
                    }
                ],
            }
        ],
    }


def upload(client, headers, value=None, company=1):
    return client.post(
        BASE,
        headers=headers,
        data={'expected_company_id': str(company)},
        files={'report': ('synthetic.json', json.dumps(value or report()).encode(), 'application/json')},
    )


def test_active_tenant_metadata_and_generation_do_not_write_business_data(client, admin_user, db_session, monkeypatch):
    db_session.add(Company(id=2, name='Synthetic active company', slug='synthetic-active', is_active=True))
    admin_user.is_superuser = True
    db_session.commit()
    headers = {'Authorization': 'Bearer ' + create_access_token(subject=admin_user.id, company_id=2)}
    rendered = []
    statements = []

    def render(value, **context):
        rendered.append((value, context))
        return b'%PDF-1.7\nSynthetic renderer result'

    def record_sql(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().split(None, 1)[0].upper())

    monkeypatch.setattr(endpoint, 'build_buyer_pdf', render)
    engine = db_session.get_bind()
    event.listen(engine, 'before_cursor_execute', record_sql)
    try:
        response = upload(client, headers, report(2), company=2)
    finally:
        event.remove(engine, 'before_cursor_execute', record_sql)
    assert response.status_code == 200, response.text
    assert len(rendered) == 1
    assert rendered[0][1] == {
        'company_name': 'Synthetic active company',
        'company_id': 2,
        'prepared_by': admin_user.full_name,
        'user_id': admin_user.id,
    }
    assert statements and not {'INSERT', 'UPDATE', 'DELETE', 'REPLACE'}.intersection(statements)
    assert response.headers['cache-control'] == 'private, no-store'
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['content-disposition'].startswith('attachment;')
    assert upload(client, headers, report(1), company=2).status_code == 409
    assert upload(client, headers, report(2), company=1).status_code == 409
    assert len(rendered) == 1


def test_effective_permissions_are_company_scoped_and_conditional_evidence_needs_inventory(
    client, admin_headers, db_session, monkeypatch
):
    monkeypatch.setattr(endpoint, 'build_buyer_pdf', lambda *args, **kwargs: b'%PDF-1.7\nSynthetic')
    db_session.add(Company(id=2, name='Other', slug='other-buyer', is_active=True))
    db_session.add(RolePermission(company_id=2, role=UserRole.ADMIN, permissions=[]))
    current = RolePermission(company_id=1, role=UserRole.ADMIN, permissions=['purchasing:view'])
    db_session.add(current)
    db_session.commit()
    assert upload(client, admin_headers).status_code == 200
    assert upload(client, admin_headers, report(conditional=True)).status_code == 403
    current.permissions = ['purchasing:view', 'inventory:view']
    db_session.commit()
    assert upload(client, admin_headers, report(conditional=True)).status_code == 200
    current.permissions = ['inventory:view', 'purchasing:create']
    db_session.commit()
    assert upload(client, admin_headers).status_code == 403


@pytest.mark.parametrize('claims', [{'read_only': True}, {'scope': 'kiosk'}])
def test_existing_restricted_token_fences_apply_before_multipart_parse(
    client, admin_user, db_session, monkeypatch, claims
):
    admin_user.is_superuser = True
    db_session.commit()

    def forbidden_parser(*args, **kwargs):
        raise AssertionError('Restricted credential reached multipart parsing')

    monkeypatch.setattr(Request, 'form', forbidden_parser)
    token = create_access_token(subject=admin_user.id, company_id=1, **claims)
    response = upload(client, {'Authorization': 'Bearer ' + token})
    assert response.status_code == 403


def test_unauthenticated_request_never_reaches_multipart_parser(client, monkeypatch):
    def forbidden_parser(*args, **kwargs):
        raise AssertionError('Unauthenticated request reached multipart parsing')

    monkeypatch.setattr(Request, 'form', forbidden_parser)
    assert upload(client, {}).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize('suffix', ['', '/'])
@pytest.mark.parametrize('declared', [None, b'1', b'not-a-number'])
async def test_streamed_raw_cap_rejects_before_parser_despite_missing_or_false_length(suffix, declared):
    invoked = []
    sent = []
    chunks = [b'x' * MAX_REQUEST_BYTES, b'x']

    async def downstream(scope, receive, send):
        invoked.append(True)

    async def receive():
        chunk = chunks.pop(0)
        return {'type': 'http.request', 'body': chunk, 'more_body': bool(chunks)}

    async def send(message):
        sent.append(message)

    headers = [(b'content-length', declared)] if declared is not None else []
    await NestingBuyerPdfBodyLimitMiddleware(downstream)(
        {'type': 'http', 'method': 'POST', 'path': BASE + suffix, 'headers': headers}, receive, send
    )
    assert not invoked and not chunks
    assert sent[0]['type'] == 'http.response.start' and sent[0]['status'] == 413


@pytest.mark.parametrize('body_change', ['bow_tie', 'duplicate_instance', 'extra_field', 'bool_quantity'])
def test_report_cannot_misrepresent_a_rectangle_or_original_instance_set(body_change):
    value = deepcopy(report())
    group = value['groups'][0]
    sheet = group['sheets'][0]
    if body_change == 'bow_tie':
        points = sheet['outer']['points']
        sheet['outer']['points'] = [points[0], points[2], points[1], points[3]]
    elif body_change == 'duplicate_instance':
        group['partRequirements'][0]['quantity'] = 2
        sheet['placements'].append(deepcopy(sheet['placements'][0]))
    elif body_change == 'extra_field':
        value['companyName'] = 'Forged authoritative company'
    else:
        group['partRequirements'][0]['quantity'] = True
    with pytest.raises(HTTPException) as caught:
        parse_report(json.dumps(value).encode())
    assert caught.value.status_code == 422
