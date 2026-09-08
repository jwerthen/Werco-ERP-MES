"""Independent atomicity, tenancy, authorization and bounded upload checks."""

import hashlib
import json
from uuid import uuid4

import pytest

from app.core.security import create_access_token
from app.middleware.nesting_draft_body_limit import NestingDraftBodyLimitMiddleware
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.quote_nesting_draft import QuoteNestingDraft, QuoteNestingRevision
from app.models.role_permission import RolePermission
from app.models.user import UserRole
from app.schemas.quote_nesting_drafts import MAX_DRAFT_REQUEST_BYTES
from app.services.audit_service import AuditService

pytestmark = [pytest.mark.api, pytest.mark.integration]
BASE = '/api/v1/quote-nesting/drafts'


def project(name='Synthetic inputs'):
    return {
        'version': 6,
        'units': 'in',
        'currency': 'USD',
        'name': name,
        'activeGroupId': 'group',
        'groups': [
            {
                'id': 'group',
                'quote': {
                    'version': 7,
                    'units': 'in',
                    'currency': 'USD',
                    'name': 'Synthetic carbon',
                    'material': 'Carbon steel',
                    'thickness': 0.125,
                    'margin': 0.375,
                    'gap': 0.125,
                    'objective': 'area',
                    'spacingMode': 'auto',
                    'grainAxis': 'x',
                    'parts': [
                        {
                            'id': 'part',
                            'name': 'Synthetic disk',
                            'quantity': 1,
                            'rotate': True,
                            'color': 0,
                            'rotationMode': 'half-turn',
                            'grainAxis': 'x',
                            'loops': [{'type': 'circle', 'cx': 1, 'cy': 1, 'r': 1}],
                        }
                    ],
                    'options': [{'id': 'sheet', 'width': 96, 'height': 48, 'enabled': True, 'price': None}],
                },
            }
        ],
    }


def upload(client, headers, value=None, *, key=None, company=1, draft=None, version=None):
    data = {'request_key': key or str(uuid4()), 'expected_company_id': str(company)}
    if version is not None:
        data['expected_version'] = str(version)
    content = json.dumps(value if value is not None else project(), ensure_ascii=False).encode()
    return client.post(
        BASE + (f'/{draft}/revisions' if draft is not None else ''),
        headers=headers,
        data=data,
        files={'estimate': ('synthetic.json', content, 'application/json')},
    )


def test_save_has_canonical_hash_and_one_atomic_audit_but_reads_never_add_history(client, admin_headers, db_session):
    value = project('Synthetic <REF> "drawing"')
    response = upload(client, admin_headers, value)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved['estimate'] == value
    expected = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    assert saved['content_sha256'] == hashlib.sha256(expected).hexdigest()
    assert saved['payload_bytes'] == len(expected)
    assert saved['status'] == 'DRAFT'
    assert saved['created_at'].endswith('Z')
    assert saved['review_issues'][0]['code'] == 'unapproved_client_snapshot'
    audit = db_session.query(AuditLog).filter(AuditLog.resource_type == 'quote_nesting_revision').one()
    assert audit.company_id == 1
    assert audit.new_values['content_sha256'] == saved['content_sha256']
    assert 'estimate' not in audit.new_values
    for path in ('', f"/{saved['draft_id']}/revisions", f"/{saved['draft_id']}/revisions/1"):
        result = client.get(BASE + path, headers=admin_headers)
        assert result.status_code == 200
        if 'items' in result.json():
            assert 'estimate' not in result.json()['items'][0]
    assert db_session.query(QuoteNestingRevision).count() == 1
    assert db_session.query(AuditLog).filter(AuditLog.resource_type == 'quote_nesting_revision').count() == 1


def test_failed_append_audit_rolls_back_header_and_allows_exact_retry(client, admin_headers, db_session, monkeypatch):
    first = upload(client, admin_headers).json()
    key = str(uuid4())
    with monkeypatch.context() as patch:
        patch.setattr(AuditService, 'log', lambda *args, **kwargs: None)
        failed = upload(client, admin_headers, project('Changed name'), key=key, draft=first['draft_id'], version=1)
    assert failed.status_code == 503
    header = db_session.query(QuoteNestingDraft).one()
    assert header.version == header.latest_revision_number == 1
    assert header.name == first['name']
    assert db_session.query(QuoteNestingRevision).count() == 1
    assert db_session.query(AuditLog).filter(AuditLog.resource_type == 'quote_nesting_revision').count() == 1
    retry = upload(client, admin_headers, project('Changed name'), key=key, draft=first['draft_id'], version=1)
    assert retry.status_code == 200, retry.text
    assert retry.json()['draft_version'] == 2
    assert db_session.query(QuoteNestingRevision).count() == 2


def test_effective_permissions_require_read_and_create_for_writes(client, admin_headers, admin_user, db_session):
    permissions = RolePermission(company_id=1, role=UserRole.ADMIN, permissions=['purchasing:view'])
    db_session.add(permissions)
    db_session.commit()
    assert client.get(BASE, headers=admin_headers).status_code == 200
    assert upload(client, admin_headers).status_code == 403
    permissions.permissions = ['purchasing:create']
    db_session.commit()
    assert client.get(BASE, headers=admin_headers).status_code == 403
    assert upload(client, admin_headers).status_code == 403
    assert db_session.query(QuoteNestingRevision).count() == 0
    permissions.permissions = ['purchasing:view', 'purchasing:create']
    db_session.commit()
    assert upload(client, admin_headers).status_code == 200


def test_foreign_tenant_reads_appends_and_context_switches_cannot_add_history(
    client, admin_headers, test_user, db_session
):
    first = upload(client, admin_headers).json()
    db_session.add(Company(id=2, name='Synthetic other company', slug='synthetic-other', is_active=True))
    test_user.company_id = 2
    test_user.role = UserRole.ADMIN
    db_session.commit()
    foreign = {'Authorization': 'Bearer ' + create_access_token(subject=test_user.id, company_id=2)}
    assert client.get(BASE, headers=foreign).json()['total'] == 0
    assert client.get(BASE + f"/{first['draft_id']}/revisions", headers=foreign).status_code == 404
    assert client.get(BASE + f"/{first['draft_id']}/revisions/1", headers=foreign).status_code == 404
    assert upload(client, foreign, company=2, draft=first['draft_id'], version=1).status_code == 404
    assert upload(client, foreign, company=1).status_code == 409
    assert db_session.query(QuoteNestingRevision).count() == 1


def test_same_key_is_bound_to_actor_and_payload(client, admin_headers, test_user, db_session):
    key = str(uuid4())
    first = upload(client, admin_headers, key=key)
    assert first.status_code == 200
    test_user.role = UserRole.ADMIN
    db_session.commit()
    other = {'Authorization': 'Bearer ' + create_access_token(subject=test_user.id, company_id=1)}
    assert upload(client, other, key=key).status_code == 409
    assert upload(client, admin_headers, project('Different inputs'), key=key).status_code == 409
    replay = upload(client, admin_headers, key=key)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert db_session.query(QuoteNestingRevision).count() == 1


def test_read_only_company_token_and_kiosk_token_cannot_write(client, admin_user, db_session):
    admin_user.is_superuser = True
    db_session.commit()
    for claims in ({'read_only': True}, {'scope': 'kiosk'}):
        token = create_access_token(subject=admin_user.id, company_id=1, **claims)
        assert upload(client, {'Authorization': 'Bearer ' + token}).status_code == 403
    assert db_session.query(QuoteNestingRevision).count() == 0


def test_duplicate_multipart_and_unknown_snapshot_fields_never_persist(client, admin_headers, db_session):
    response = client.post(
        BASE,
        headers=admin_headers,
        files=[
            ('estimate', ('synthetic.json', json.dumps(project()).encode(), 'application/json')),
            ('request_key', (None, str(uuid4()))),
            ('request_key', (None, str(uuid4()))),
            ('expected_company_id', (None, '1')),
        ],
    )
    assert response.status_code in {400, 422}
    assert upload(client, admin_headers, {**project(), 'approved': True}).status_code == 422
    assert upload(client, admin_headers, {**project(), 'units': 'mm'}).status_code == 422
    assert db_session.query(QuoteNestingDraft).count() == 0


@pytest.mark.asyncio
async def test_chunked_upload_is_capped_before_auth_or_multipart_parser():
    invoked = False
    sent = []
    chunks = iter(
        [
            {'type': 'http.request', 'body': b'x' * MAX_DRAFT_REQUEST_BYTES, 'more_body': True},
            {'type': 'http.request', 'body': b'x', 'more_body': False},
        ]
    )

    async def app(scope, receive, send):
        nonlocal invoked
        invoked = True

    async def receive():
        return next(chunks)

    async def send(message):
        sent.append(message)

    await NestingDraftBodyLimitMiddleware(app)(
        {
            'type': 'http',
            'method': 'POST',
            'path': BASE + '/1/revisions',
            'headers': [],
        },
        receive,
        send,
    )
    assert not invoked
    assert sent[0]['status'] == 413
