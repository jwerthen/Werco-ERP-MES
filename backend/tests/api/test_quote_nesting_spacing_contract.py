"""Independent authorization, publication timing and exact snapshot regressions."""

import copy
import math
from datetime import datetime, timedelta
from decimal import Inexact, Rounded, localcontext

import pytest

from app.core.security import create_access_token
from app.db.database import atomic_transaction
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.quote_nesting_draft import QuoteNestingRevision
from app.models.quote_nesting_spacing_policy import QuoteNestingSpacingEvent as Event
from app.models.quote_nesting_spacing_policy import QuoteNestingSpacingPolicy as Policy
from app.models.role_permission import RolePermission
from app.models.user import UserRole
from app.schemas.quote_nesting_spacing import SpacingBand, normalize_thickness, resolve_band
from app.services import quote_nesting_spacing as service
from app.services.api_token_service import issue_api_token
from app.services.audit_service import AuditService
from tests.api.test_quote_nesting_spacing import (
    BASE,
    command,
    content,
    created,
    policy_estimate,
    published,
    resolved,
    save,
)
from tests.services.test_quote_nesting_runs_service import estimate

pytestmark = pytest.mark.api


def test_decimal_profile_ignores_ambient_precision_rounding_and_traps():
    band = SpacingBand.model_validate(
        {**content()['bands'][0], 'minimum_gap_in': '0', 'gap_thickness_multiplier': '0.333333333'}
    )
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        assert normalize_thickness('0.1250000005') == '0.125000001'
        assert resolve_band(band, '0.125') == ('0.041666667', '0.375')
    with pytest.raises(ValueError):
        normalize_thickness('4.0000000001')


def test_future_withdrawal_leaves_current_until_boundary_then_never_falls_back(
    client, admin_headers, db_session, monkeypatch
):
    first, _ = published(client, admin_headers)
    future = datetime.utcnow() + timedelta(days=1)
    scheduled = client.post(
        BASE + '/publications',
        headers=admin_headers,
        json=command(
            2,
            revision_number=1,
            content_sha256=first['revision']['content_sha256'],
            effective_at=future.isoformat() + 'Z',
        ),
    )
    assert scheduled.status_code == 200, scheduled.text
    target = scheduled.json()['publication']['id']
    withdrawal = client.post(BASE + f'/publications/{target}/withdraw', headers=admin_headers, json=command(3))
    assert withdrawal.status_code == 200

    class Clock(datetime):
        current = future - timedelta(microseconds=1)

        @classmethod
        def utcnow(cls):
            return cls.current

    monkeypatch.setattr(service, 'datetime', Clock)
    assert (
        service.resolve(db_session, 1, 'Carbon steel', '0.125')['policy']['publication_id']
        == first['publication']['id']
    )
    Clock.current = future
    assert service.resolve(db_session, 1, 'Carbon steel', '0.125')['status'] == 'unavailable'
    state = service.state(db_session, 1, page=1, per_page=20)
    assert state['current_publication']['id'] == target
    assert state['current_publication']['status'] == 'withdrawn'


@pytest.mark.parametrize('kind', ['publish', 'withdraw'])
def test_required_audit_failure_rolls_back_governance_and_exact_key_can_retry(
    client, admin_headers, db_session, monkeypatch, kind
):
    if kind == 'publish':
        prior = created(client, admin_headers)
        route = BASE + '/publications'
        body = command(1, revision_number=1, content_sha256=prior['revision']['content_sha256'], effective_at=None)
    else:
        prior, _ = published(client, admin_headers)
        route = BASE + f"/publications/{prior['publication']['id']}/withdraw"
        body = command(2)
    version = prior['policy_version']
    audit_count = db_session.query(AuditLog).count()
    with monkeypatch.context() as patch:
        patch.setattr(AuditService, 'log', lambda *args, **kwargs: None)
        response = client.post(route, headers=admin_headers, json=body)
        assert response.status_code == 503, response.text
    assert db_session.query(Policy).one().version == version
    assert db_session.query(Event).count() == version
    assert db_session.query(AuditLog).count() == audit_count
    assert client.post(route, headers=admin_headers, json=body).status_code == 200
    assert db_session.query(Policy).one().version == version + 1


def test_admin_role_does_not_override_effective_nesting_permission_restrictions(
    client, admin_headers, manager_headers, db_session
):
    assert client.get(BASE, headers=manager_headers).status_code == 200
    assert client.post(BASE + '/revisions', headers=manager_headers, json=command(content=content())).status_code == 403
    row = RolePermission(company_id=1, role=UserRole.ADMIN, permissions=['purchasing:view'])
    db_session.add(row)
    db_session.commit()
    assert client.get(BASE, headers=admin_headers).status_code == 200
    assert client.post(BASE + '/revisions', headers=admin_headers, json=command(content=content())).status_code == 403
    row.permissions = ['purchasing:create']
    db_session.commit()
    assert client.get(BASE, headers=admin_headers).status_code == 403
    assert db_session.query(Event).count() == 0


def test_company_and_read_only_context_never_reuse_foreign_revision_or_publication(
    client, admin_headers, admin_user, db_session
):
    first, _ = published(client, admin_headers)
    db_session.add(Company(id=2, name='Synthetic other company', slug='policy-other'))
    admin_user.role = UserRole.PLATFORM_ADMIN
    db_session.commit()

    def headers(read_only):
        token = create_access_token(subject=admin_user.id, company_id=2, read_only=read_only)
        return {'Authorization': 'Bearer ' + token, 'X-Requested-With': 'XMLHttpRequest'}

    readonly = headers(True)
    assert client.get(BASE, headers=readonly).json()['policy'] is None
    assert (
        client.post(
            BASE + '/revisions', headers=readonly, json=command(expected_company_id=2, content=content())
        ).status_code
        == 403
    )
    switched = headers(False)
    assert client.get(BASE + '/revisions/1', headers=switched).status_code == 404
    created_other = client.post(
        BASE + '/revisions', headers=switched, json=command(expected_company_id=2, content=content())
    )
    assert created_other.status_code == 200, created_other.text
    assert (
        client.post(
            BASE + f"/publications/{first['publication']['id']}/withdraw",
            headers=switched,
            json=command(1, expected_company_id=2),
        ).status_code
        == 404
    )
    assert db_session.query(Event).filter(Event.company_id == 1).count() == 2


@pytest.mark.parametrize('invalidity', ['revoked', 'expired'])
def test_api_tokens_are_attributed_and_then_fail_closed(client, admin_user, db_session, invalidity):
    with atomic_transaction(db_session):
        row, token = issue_api_token(
            db_session,
            company_id=1,
            user_id=admin_user.id,
            label='Synthetic policy actor',
            expires_days=1,
            created_by=admin_user.id,
            audit=AuditService(db_session, admin_user),
        )
    headers = {'Authorization': 'Bearer ' + token, 'X-Requested-With': 'XMLHttpRequest'}
    first = created(client, headers)
    evidence = db_session.query(AuditLog).filter(AuditLog.resource_type == 'quote_nesting_spacing_event').one()
    assert evidence.extra_data['credential']['api_token_id'] == row.id
    if invalidity == 'revoked':
        row.revoked = True
    else:
        row.expires_at = datetime.utcnow() - timedelta(seconds=1)
    db_session.commit()
    result = client.post(
        BASE + '/publications',
        headers=headers,
        json=command(1, revision_number=1, content_sha256=first['revision']['content_sha256'], effective_at=None),
    )
    assert result.status_code == 401
    assert db_session.query(Event).count() == 1


def test_shared_uuid_cannot_change_command_kind_or_content(client, admin_headers, db_session):
    body = command(content=content())
    first = client.post(BASE + '/revisions', headers=admin_headers, json=body).json()
    reused = command(
        1,
        request_key=body['request_key'],
        revision_number=1,
        content_sha256=first['revision']['content_sha256'],
        effective_at=None,
    )
    assert client.post(BASE + '/publications', headers=admin_headers, json=reused).status_code == 409
    changed = copy.deepcopy(body)
    changed['content']['name'] = 'Different reviewed content'
    assert client.post(BASE + '/revisions', headers=admin_headers, json=changed).status_code == 409
    assert db_session.query(Event).count() == 1


@pytest.mark.parametrize(
    'mutate',
    [
        lambda source: source.update(version=True),
        lambda source: source['groups'][0]['quote']['spacingPolicy'].update(schema_version=True),
        lambda source: source['groups'][0]['quote']['spacingPolicy'].update(resolved_at='2099-01-01T00:00:00Z'),
        lambda source: source['groups'][0]['quote']['spacingPolicy']['band'].update(minimum_gap_in='0.124'),
        lambda source: source['groups'][0]['quote'].update(gap=math.nextafter(0.125, 0)),
        lambda source: source['groups'][0]['quote'].update(margin=math.nextafter(0.375, 0)),
        lambda source: source['groups'][0]['quote'].update(
            spacingOverride={'schema_version': 1, 'reason': 'x', 'changed_at': '2026-09-08T12:00:00Z'}
        ),
    ],
)
def test_snapshot_tampering_and_single_ulp_understatement_never_save(client, admin_headers, mutate):
    published(client, admin_headers)
    source = policy_estimate(resolved(client, admin_headers).json()['policy'])
    mutate(source)
    assert save(client, admin_headers, source).status_code == 422


@pytest.mark.parametrize('quote_version,project_version', [(3, 4), (7, 6), (9, 10)])
@pytest.mark.parametrize('field', ['spacingPolicy', 'spacingOverride'])
def test_explicit_null_governance_is_not_treated_as_an_absent_legacy_field(
    client, admin_headers, db_session, quote_version, project_version, field
):
    source = estimate()
    source['version'] = project_version
    quote = source['groups'][0]['quote']
    quote['version'] = quote_version
    if quote_version == 9:
        if field == 'spacingPolicy':
            quote.update(
                spacingMode='manual',
                spacingOverride={
                    'schema_version': 1,
                    'reason': 'Synthetic explicit custom allowance',
                    'changed_at': '2026-09-08T12:00:00Z',
                },
            )
        else:
            published(client, admin_headers)
            source = policy_estimate(resolved(client, admin_headers).json()['policy'])
            quote = source['groups'][0]['quote']
    quote[field] = None
    before = db_session.query(AuditLog).count()
    response = save(client, admin_headers, source)
    assert response.status_code == 422, response.text
    assert db_session.query(QuoteNestingRevision).count() == 0
    assert db_session.query(AuditLog).count() == before
