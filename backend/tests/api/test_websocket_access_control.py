"""Real socket admission, live revocation and the shared HTTP tenant boundary.

Synthetic JWTs and fixture database sessions only. Manager sends execute on the
TestClient portal's server event loop, including the before-delivery authorizer.
"""

import json
from datetime import datetime, timedelta, timezone

import anyio
import pytest
from jose import jwt
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from app.api import websocket as ws_api
from app.core import websocket as ws_core
from app.core.config import settings
from app.core.security import create_access_token
from app.models.company import Company
from app.models.user import UserRole
from tests.api.kiosk_test_helpers import (
    ensure_company,
    make_user,
    make_wo_with_operation,
    make_work_center,
    user_headers,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]
ROUTES = ['updates', 'shop-floor', 'work-order']


@pytest.fixture(autouse=True)
def socket_sessions(db_session, monkeypatch):
    # A fresh manager isolates presence and authorizers between tests. Each live
    # identity check gets a short session against ONLY this test's in-memory DB.
    manager = ws_core.ConnectionManager()
    monkeypatch.setattr(ws_api, 'manager', manager)
    monkeypatch.setattr(ws_core, 'manager', manager)
    monkeypatch.setattr(ws_api, 'SessionLocal', lambda: Session(bind=db_session.get_bind()))
    return manager


@pytest.fixture
def resources(db_session):
    center = make_work_center(db_session)
    order, _ = make_wo_with_operation(db_session, work_center=center)
    return center, order


def url(route, resources, token=None):
    suffix = '' if route == 'updates' else f'/{resources[0 if route == "shop-floor" else 1].id}'
    path = f'/api/v1/ws/{route}{suffix}'
    return f'{path}?token={token}' if token is not None else path


def signed_claims(user, **changes):
    claims = jwt.get_unverified_claims(create_access_token(user.id, company_id=user.company_id))
    claims.update(changes)
    return jwt.encode(claims, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def receive_message(socket):
    # Starlette exposes no receive timeout. Bound its test stream wait so an
    # authorization regression fails promptly instead of hanging the test run.
    async def bounded_receive():
        with anyio.fail_after(3):
            return await socket._send_rx.receive()

    return socket.portal.call(bounded_receive)


def welcome(socket):
    message = receive_message(socket)
    assert message['type'] == 'websocket.send', message
    payload = json.loads(message['text'])
    assert payload['type'] == 'connected'
    return payload


def assert_policy_close(socket):
    assert receive_message(socket) == {'type': 'websocket.close', 'code': 1008, 'reason': ''}


def assert_empty_presence(manager):
    assert manager.active_connections == []
    assert manager.user_connections == {}
    assert manager.user_connected_at == {}
    assert manager.company_connections == {}
    assert manager.connection_company == {}
    assert manager.connection_authorizers == {}


@pytest.mark.parametrize('route', ROUTES)
@pytest.mark.parametrize(
    'invalid', ['disabled', 'kiosk', 'unknown_scope', 'inactive_company', 'foreign_company', 'missing_user']
)
def test_routes_refuse_invalid_live_principal(
    client, db_session, test_user, resources, socket_sessions, route, invalid
):
    company_id = test_user.company_id
    scope = None
    if invalid == 'disabled':
        test_user.is_active = False
    elif invalid == 'inactive_company':
        db_session.get(Company, company_id).is_active = False
    elif invalid == 'foreign_company':
        ensure_company(db_session, 2)
        company_id = 2
    elif invalid == 'missing_user':
        pass
    else:
        scope = 'kiosk' if invalid == 'kiosk' else 'unknown'
    db_session.commit()
    token = create_access_token(
        test_user.id if invalid != 'missing_user' else 999999, company_id=company_id, scope=scope
    )
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect(url(route, resources, token)):
            pass
    assert refused.value.code == 1008
    assert_empty_presence(socket_sessions)


@pytest.mark.parametrize('route', ROUTES)
@pytest.mark.parametrize(
    'credential', ['missing', 'malformed', 'expired', 'refresh', 'api', 'display', 'signin', 'kiosk_station']
)
def test_routes_refuse_noninteractive_credentials(client, test_user, resources, socket_sessions, route, credential):
    if credential == 'missing':
        token = None
    elif credential == 'malformed':
        token = 'not-a-token'
    elif credential == 'expired':
        token = create_access_token(test_user.id, company_id=test_user.company_id, expires_delta=timedelta(seconds=-5))
    else:
        token = signed_claims(test_user, type='kiosk' if credential == 'kiosk_station' else credential)
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect(url(route, resources, token)):
            pass
    assert refused.value.code == 1008
    assert_empty_presence(socket_sessions)


@pytest.mark.parametrize('route', ROUTES)
@pytest.mark.parametrize(
    'claim',
    [
        {'cid': True},
        {'cid': '1'},
        {'cid': 1.0},
        {'cid': -1},
        {'cid': 2**64},
        {'cid': {}},
        {'sub': '0'},
        {'sub': '-1'},
        {'sub': '99999999999999999999999'},
        {'sub': 'not-an-id'},
    ],
)
def test_routes_refuse_malformed_identity_claims(client, test_user, resources, route, claim):
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect(url(route, resources, signed_claims(test_user, **claim))):
            pass
    assert refused.value.code == 1008


@pytest.mark.parametrize('route', ROUTES)
@pytest.mark.parametrize('legacy', [False, True])
def test_live_principal_connects_and_cleans_presence(client, test_user, resources, socket_sessions, route, legacy):
    token = create_access_token(test_user.id, company_id=None if legacy else test_user.company_id)
    with client.websocket_connect(url(route, resources, token)) as socket:
        connected = welcome(socket)
        assert connected['data']['user_id'] == str(test_user.id)
        assert socket_sessions.get_connected_user_ids() == [str(test_user.id)]
        assert socket_sessions.get_connected_since(str(test_user.id)) is not None
    assert_empty_presence(socket_sessions)


@pytest.mark.parametrize('route', ['shop-floor', 'work-order'])
def test_resource_channel_refuses_another_company_resource(client, db_session, test_user, resources, route):
    foreign_center = make_work_center(db_session, company_id=2)
    foreign_order, _ = make_wo_with_operation(db_session, company_id=2, work_center=foreign_center)
    token = create_access_token(test_user.id, company_id=1)
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect(url(route, (foreign_center, foreign_order), token)):
            pass
    assert refused.value.code == 1008


def test_resource_channel_refuses_deleted_work_order(client, db_session, test_user, resources):
    resources[1].is_deleted = True
    db_session.commit()
    token = create_access_token(test_user.id, company_id=1)
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect(url('work-order', resources, token)):
            pass
    assert refused.value.code == 1008


@pytest.mark.parametrize('route', ROUTES)
def test_platform_principal_can_use_active_tenant_switch(client, db_session, resources, socket_sessions, route):
    platform = make_user(db_session, company_id=2, role=UserRole.PLATFORM_ADMIN)
    token = create_access_token(platform.id, company_id=1, read_only=True)
    with client.websocket_connect(url(route, resources, token)) as socket:
        assert welcome(socket)['data']['user_id'] == str(platform.id)
        assert set(socket_sessions.company_connections) == {1}
    assert_empty_presence(socket_sessions)


@pytest.mark.parametrize('delivery', ['company', 'global', 'user'])
@pytest.mark.parametrize(
    'revoked', ['disabled', 'moved', 'inactive_company', 'expired', 'deleted_work_order', 'foreign_center']
)
def test_live_revocation_prevents_delivery_and_cleans_presence(
    client, db_session, test_user, resources, socket_sessions, monkeypatch, delivery, revoked
):
    manager = socket_sessions
    route = (
        'work-order' if revoked == 'deleted_work_order' else 'shop-floor' if revoked == 'foreign_center' else 'updates'
    )
    token = create_access_token(test_user.id, company_id=1, expires_delta=timedelta(minutes=2))
    user_id = str(test_user.id)
    with client.websocket_connect(url(route, resources, token)) as socket:
        welcome(socket)
        if revoked == 'disabled':
            test_user.is_active = False
        elif revoked == 'moved':
            ensure_company(db_session, 2)
            test_user.company_id = 2
        elif revoked == 'inactive_company':
            db_session.get(Company, 1).is_active = False
        elif revoked == 'deleted_work_order':
            resources[1].is_deleted = True
        elif revoked == 'foreign_center':
            ensure_company(db_session, 2)
            resources[0].company_id = 2
        else:
            advanced = datetime.now(timezone.utc) + timedelta(minutes=10)

            class ExpiredClock(datetime):
                @classmethod
                def now(cls, tz=None):
                    return advanced.astimezone(tz) if tz is not None else advanced.replace(tzinfo=None)

            monkeypatch.setattr(jwt, 'datetime', ExpiredClock)
        db_session.commit()
        secret_payload = {'private': 'must never reach a revoked socket'}
        if delivery == 'company':
            client.portal.call(manager.broadcast_to_company, 1, secret_payload)
        elif delivery == 'global':
            client.portal.call(manager.broadcast, secret_payload)
        else:
            client.portal.call(manager.send_to_user, user_id, secret_payload)
        # Receiving CLOSE next proves no final business payload preceded it.
        assert_policy_close(socket)
        assert_empty_presence(manager)


def test_idle_socket_rechecks_without_inbound_or_broadcast(
    client, db_session, test_user, resources, socket_sessions, monkeypatch
):
    monkeypatch.setattr(ws_api, 'WS_AUTH_RECHECK_SECONDS', 0.02)
    token = create_access_token(test_user.id, company_id=1)
    with client.websocket_connect(url('updates', resources, token)) as socket:
        welcome(socket)
        test_user.is_active = False
        db_session.commit()
        assert_policy_close(socket)
    assert_empty_presence(socket_sessions)


def test_incoming_heartbeat_rechecks_live_user(client, db_session, test_user, resources, socket_sessions):
    token = create_access_token(test_user.id, company_id=1)
    with client.websocket_connect(url('updates', resources, token)) as socket:
        welcome(socket)
        test_user.is_active = False
        db_session.commit()
        socket.send_json({'type': 'heartbeat'})
        assert_policy_close(socket)
    assert_empty_presence(socket_sessions)


def test_company_broadcast_keeps_other_tenant_out(client, db_session, test_user, resources, socket_sessions):
    other = make_user(db_session, company_id=2)
    token_a = create_access_token(test_user.id, company_id=1)
    token_b = create_access_token(other.id, company_id=2)
    with client.websocket_connect(url('updates', resources, token_a)) as socket_a:
        welcome(socket_a)
        with client.websocket_connect(url('updates', resources, token_b)) as socket_b:
            welcome(socket_b)
            client.portal.call(socket_sessions.broadcast_to_company, 1, {'tenant': 'A'})
            assert json.loads(receive_message(socket_a)['text'])['data'] == {'tenant': 'A'}
            with pytest.raises(anyio.WouldBlock):
                client.portal.call(socket_b._send_rx.receive_nowait)
    assert_empty_presence(socket_sessions)


def test_resource_revocation_keeps_users_other_authorized_socket(
    client, db_session, test_user, resources, socket_sessions
):
    token = create_access_token(test_user.id, company_id=1)
    with client.websocket_connect(url('updates', resources, token)) as general:
        welcome(general)
        connected_since = socket_sessions.get_connected_since(str(test_user.id))
        with client.websocket_connect(url('work-order', resources, token)) as specific:
            welcome(specific)
            resources[1].is_deleted = True
            db_session.commit()
            client.portal.call(
                socket_sessions.send_to_user, str(test_user.id), {'visible': 'authorized general channel'}
            )
            assert_policy_close(specific)
            assert json.loads(receive_message(general)['text'])['data']['visible'] == 'authorized general channel'
            assert socket_sessions.get_user_connection_count(str(test_user.id)) == 1
            assert socket_sessions.get_connected_since(str(test_user.id)) == connected_since
    assert_empty_presence(socket_sessions)


def test_database_unavailable_drops_delivery_without_payload(
    client, test_user, resources, socket_sessions, monkeypatch
):
    token = create_access_token(test_user.id, company_id=1)
    with client.websocket_connect(url('updates', resources, token)) as socket:
        welcome(socket)

        def unavailable():
            raise RuntimeError('Synthetic database unavailable')

        monkeypatch.setattr(ws_api, 'SessionLocal', unavailable)
        client.portal.call(socket_sessions.broadcast_to_company, 1, {'private': 'never sent'})
        assert_policy_close(socket)
        assert_empty_presence(socket_sessions)


@pytest.mark.parametrize(
    'changed,expected', [('disabled', 403), ('inactive_company', 403), ('moved', 401), ('missing_company', 403)]
)
def test_http_shared_identity_refuses_withdrawn_access(client, db_session, test_user, changed, expected):
    headers = user_headers(test_user)
    if changed == 'disabled':
        test_user.is_active = False
    elif changed == 'inactive_company':
        db_session.get(Company, 1).is_active = False
    elif changed == 'moved':
        ensure_company(db_session, 2)
        test_user.company_id = 2
    else:
        # A platform token can name a company that has since been removed.
        test_user.role = UserRole.PLATFORM_ADMIN
        headers = user_headers(test_user, active_company_id=999999)
    db_session.commit()
    response = client.get('/api/v1/parts/', headers=headers)
    assert response.status_code == expected, response.text


@pytest.mark.parametrize('action', ['logout', 'switch_company'])
def test_platform_can_leave_inactive_tenant_context(client, db_session, action):
    platform = make_user(db_session, role=UserRole.PLATFORM_ADMIN, last_name='Administrator')
    # Company-switch returns UserResponse; this fixture must satisfy its public
    # email/name contract (the shared kiosk factory uses intentionally raw names).
    platform.email = 'platform@example.com'
    company = ensure_company(db_session, 2)
    headers = user_headers(platform, active_company_id=2)
    company.is_active = False
    db_session.commit()
    path = '/api/v1/auth/logout' if action == 'logout' else '/api/v1/auth/switch-company/1'
    response = client.post(path, headers=headers)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize('action', ['logout', 'switch_company'])
def test_disabled_platform_cannot_use_inactive_context_escape(client, db_session, action):
    platform = make_user(db_session, role=UserRole.PLATFORM_ADMIN, is_active=False)
    company = ensure_company(db_session, 2)
    company.is_active = False
    db_session.commit()
    headers = user_headers(platform, active_company_id=2)
    path = '/api/v1/auth/logout' if action == 'logout' else '/api/v1/auth/switch-company/1'
    assert client.post(path, headers=headers).status_code == 403


@pytest.mark.parametrize('action', ['logout', 'switch_company'])
def test_inactive_context_escape_still_requires_existing_company(client, db_session, action):
    platform = make_user(db_session, role=UserRole.PLATFORM_ADMIN)
    headers = user_headers(platform, active_company_id=999999)
    path = '/api/v1/auth/logout' if action == 'logout' else '/api/v1/auth/switch-company/1'
    assert client.post(path, headers=headers).status_code == 403


def test_http_platform_switch_reads_only_selected_tenant(client, db_session):
    platform = make_user(db_session, role=UserRole.PLATFORM_ADMIN)
    own_center = make_work_center(db_session)
    foreign_center = make_work_center(db_session, company_id=2)
    response = client.get('/api/v1/work-centers/', headers=user_headers(platform, active_company_id=2))
    assert response.status_code == 200, response.text
    identifiers = {row['id'] for row in response.json()}
    assert foreign_center.id in identifiers and own_center.id not in identifiers


@pytest.mark.parametrize('role', [UserRole.PLATFORM_ADMIN, UserRole.ADMIN])
def test_only_platform_can_reactivate_its_inactive_home_company(client, db_session, role):
    user = make_user(db_session, role=role)
    headers = user_headers(user)
    company = db_session.get(Company, user.company_id)
    company.is_active = False
    db_session.commit()
    assert client.get('/api/v1/parts/', headers=headers).status_code == 403
    response = client.put(f'/api/v1/platform/companies/{company.id}', headers=headers, json={'is_active': True})
    expected = 200 if role == UserRole.PLATFORM_ADMIN else 403
    assert response.status_code == expected, response.text
    db_session.refresh(company)
    assert company.is_active is (role == UserRole.PLATFORM_ADMIN)


def test_disabled_platform_cannot_use_platform_recovery(client, db_session):
    platform = make_user(db_session, role=UserRole.PLATFORM_ADMIN, is_active=False)
    company = db_session.get(Company, platform.company_id)
    company.is_active = False
    db_session.commit()
    response = client.put(
        f'/api/v1/platform/companies/{company.id}', headers=user_headers(platform), json={'is_active': True}
    )
    assert response.status_code == 403
    db_session.refresh(company)
    assert company.is_active is False
