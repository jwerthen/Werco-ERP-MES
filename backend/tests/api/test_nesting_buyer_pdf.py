"""Authenticated pure multipart PDF endpoint and exact company boundaries."""

import json
from io import BytesIO

import pytest
from pypdf import PdfReader
from sqlalchemy import event

from app.core.security import create_access_token
from app.models.role_permission import RolePermission
from app.models.user import UserRole
from tests.services.test_nesting_buyer_pdf import report

PATH = '/api/v1/quote-nesting/buyer-pdf'
pytestmark = [pytest.mark.api, pytest.mark.integration]


def upload(client, headers, value=None, expected='1'):
    return client.post(
        PATH,
        headers=headers,
        files={'report': ('report.json', json.dumps(value or report()), 'application/json')},
        data={'expected_company_id': expected},
    )


def test_actual_pdf_has_authenticated_identity_and_no_business_writes(client, admin_headers, admin_user, db_session):
    writes = []

    def watch(_conn, _cursor, sql, *_args):
        if sql.lstrip().split()[0].upper() in {'INSERT', 'UPDATE', 'DELETE'}:
            writes.append(sql)

    engine = db_session.get_bind()
    event.listen(engine, 'before_cursor_execute', watch)
    try:
        response = upload(client, admin_headers)
    finally:
        event.remove(engine, 'before_cursor_execute', watch)
    assert response.status_code == 200, response.text
    assert response.headers['content-type'] == 'application/pdf'
    assert response.headers['cache-control'] == 'private, no-store'
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert 'attachment' in response.headers['content-disposition']
    text = ''.join(page.extract_text() for page in PdfReader(BytesIO(response.content)).pages)
    assert admin_user.full_name in text and 'Werco Manufacturing' in text
    assert writes == []


def test_company_multipart_and_report_context_must_both_match(client, admin_headers):
    assert upload(client, admin_headers, expected='2').status_code == 409
    value = report()
    value['expectedCompanyId'] = 2
    assert upload(client, admin_headers, value).status_code == 409


def test_effective_permission_and_remnant_evidence_requirements(client, admin_headers, db_session):
    override = RolePermission(company_id=1, role=UserRole.ADMIN, permissions=['inventory:view'])
    db_session.add(override)
    db_session.commit()
    assert upload(client, admin_headers).status_code == 403
    override.permissions = ['purchasing:view']
    db_session.commit()
    assert upload(client, admin_headers).status_code == 200
    value = report()
    value['groups'][0]['selectionKind'] = 'recorded_piece'
    value['groups'][0]['baselinePurchaseSheets'] = [{'widthIn': 8, 'lengthIn': 12, 'quantity': 2}]
    assert upload(client, admin_headers, value).status_code == 403
    override.permissions = ['purchasing:view', 'inventory:view']
    db_session.commit()
    assert upload(client, admin_headers, value).status_code == 200


def test_auth_and_read_only_fences_before_parsing(client, admin_user):
    assert client.post(PATH, content=b'not multipart').status_code == 401
    token = create_access_token(subject=admin_user.id, company_id=1, read_only=True)
    assert upload(client, {'Authorization': 'Bearer ' + token}).status_code == 403


def test_exact_form_and_finite_structure(client, admin_headers):
    response = client.post(
        PATH,
        headers=admin_headers,
        files=[
            ('report', ('a.json', json.dumps(report()))),
            ('expected_company_id', (None, '1')),
            ('unexpected', (None, 'data')),
        ],
    )
    assert response.status_code in (400, 422)
    value = report()
    value['groups'][0]['sheets'][0]['lengthIn'] = float('nan')
    assert upload(client, admin_headers, value).status_code == 422
    value = report()
    value['groups'][0]['sheets'][0]['placements'][0]['originalInstance'] = True
    assert upload(client, admin_headers, value).status_code == 422
