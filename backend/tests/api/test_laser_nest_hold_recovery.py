"""A hold must remain reversible across office, shop-floor and kiosk reads."""

import pytest

from app.models.laser_nest import LaserNest
from app.models.work_order import WorkOrder, WorkOrderOperation
from tests.api import test_laser_nest_pool_quantity_rollup as pool_fixtures
from tests.api.test_kiosk_resume_fence import badge_headers

upload_dir = pool_fixtures.upload_dir
_no_ai = pool_fixtures._no_ai
pytestmark = [pytest.mark.api, pytest.mark.requires_db]


@pytest.mark.parametrize('surface', ['office', 'shop_floor', 'kiosk'])
@pytest.mark.parametrize('with_reason', [False, True])
def test_repeated_hold_read_clear_keeps_nest_active_and_returns_to_queue(client, db_session, surface, with_reason):
    admin = pool_fixtures.make_user(db_session)
    wc = pool_fixtures.make_laser_work_center(db_session)
    work_order = pool_fixtures._import_three_nest_wo(client, admin, wc)
    op_data = sorted(work_order['operations'], key=lambda op: op['sequence'])[-1]
    op_id, nest_id = op_data['id'], op_data['laser_nest']['id']
    headers = badge_headers(admin) if surface == 'kiosk' else pool_fixtures.headers_for(admin)
    for cycle in range(3):
        hold_body = {'source': 'kiosk' if surface == 'kiosk' else 'desktop'}
        if with_reason:
            hold_body.update({'category': 'other', 'note': 'Accidental hold regression'})
        response = client.put(f'/api/v1/shop-floor/operations/{op_id}/hold', json=hold_body, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'on_hold'
        if surface == 'office':
            response = client.get(f"/api/v1/work-orders/{work_order['id']}", headers=headers)
            assert response.status_code == 200, response.text
            held = next(op for op in response.json()['operations'] if op['id'] == op_id)
            assert held['laser_nest']['id'] == nest_id
            assert held['cancelled_nest_id'] is None
        elif surface == 'shop_floor':
            response = client.get('/api/v1/shop-floor/operations', params={'status': 'on_hold'}, headers=headers)
            assert response.status_code == 200, response.text
        else:
            response = client.get(f'/api/v1/shop-floor/work-center-queue/{wc.id}', headers=headers)
            assert response.status_code == 200, response.text
            assert any(row['operation_id'] == op_id for row in response.json()['held'])
        db_session.expire_all()
        assert db_session.get(LaserNest, nest_id).is_deleted is False
        assert float(db_session.get(WorkOrder, work_order['id']).quantity_ordered) == 9
        response = client.put(f'/api/v1/shop-floor/operations/{op_id}/resume', headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'ready'
        assert bool(response.json()['open_blockers']) == with_reason
        response = client.get(f'/api/v1/shop-floor/work-center-queue/{wc.id}', headers=headers)
        assert response.status_code == 200, response.text
        assert any(
            row['operation_id'] == op_id and row['laser_nest']['id'] == nest_id for row in response.json()['queue']
        )
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, op_id).status.value == 'ready'
        assert db_session.get(LaserNest, nest_id).work_order_operation_id == op_id


@pytest.mark.parametrize('surface', ['office', 'shop_floor', 'kiosk'])
def test_hold_of_cancelled_nest_refuses_before_recording_a_success(client, db_session, surface):
    from app.models.audit_log import AuditLog

    admin = pool_fixtures.make_user(db_session)
    wc = pool_fixtures.make_laser_work_center(db_session)
    wo = pool_fixtures._import_three_nest_wo(client, admin, wc)
    op = wo['operations'][0]
    admin_headers = pool_fixtures.headers_for(admin)
    assert client.delete(f"/api/v1/laser-nests/{op['laser_nest']['id']}", headers=admin_headers).status_code == 200
    before = db_session.query(AuditLog).count()
    headers = badge_headers(admin) if surface == 'kiosk' else admin_headers
    response = client.put(f"/api/v1/shop-floor/operations/{op['id']}/hold", json={'source': 'desktop'}, headers=headers)
    assert response.status_code == 409, response.text
    assert 'Restore the nest' in response.json()['detail']
    db_session.expire_all()
    assert db_session.query(AuditLog).count() == before
    assert db_session.get(LaserNest, op['laser_nest']['id']).is_deleted is True


@pytest.mark.parametrize('surface', ['office', 'shop_floor', 'kiosk'])
def test_holding_a_running_nest_closes_labor_and_can_be_cleared(client, db_session, surface):
    from app.models.time_entry import TimeEntry

    admin = pool_fixtures.make_user(db_session)
    wc = pool_fixtures.make_laser_work_center(db_session)
    wo = pool_fixtures._import_three_nest_wo(client, admin, wc)
    op = wo['operations'][-1]
    headers = badge_headers(admin) if surface == 'kiosk' else pool_fixtures.headers_for(admin)
    clocked = client.post(
        '/api/v1/shop-floor/clock-in',
        headers=headers,
        json={'work_order_id': wo['id'], 'operation_id': op['id'], 'work_center_id': wc.id, 'entry_type': 'run'},
    )
    assert clocked.status_code == 200, clocked.text
    held = client.put(f"/api/v1/shop-floor/operations/{op['id']}/hold", headers=headers)
    assert held.status_code == 200, held.text
    db_session.expire_all()
    entry = db_session.query(TimeEntry).filter(TimeEntry.operation_id == op['id']).one()
    assert entry.clock_out is not None
    clock_out = entry.clock_out
    resumed = client.put(f"/api/v1/shop-floor/operations/{op['id']}/resume", headers=headers)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()['status'] == 'in_progress'
    db_session.expire_all()
    assert db_session.get(LaserNest, op['laser_nest']['id']).is_deleted is False
    assert db_session.get(TimeEntry, entry.id).clock_out == clock_out
