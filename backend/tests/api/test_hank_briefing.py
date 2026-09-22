"""Hank briefs live operational evidence without changing records or widening access."""

from datetime import date, datetime, timedelta, timezone

from app.models.company import Company
from app.models.operations_inbox import OperationalInboxState
from app.models.role_permission import RolePermission
from app.models.shipping import Shipment, ShipmentStatus
from app.models.time_entry import TimeEntry
from app.models.user import UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.services.hank_briefing_service import HankBriefingService
from app.services.operations_inbox_service import OperationalInboxService

from .test_operations_inbox import headers, seed

URL = '/api/v1/hank/briefing'
NOW = datetime(2026, 9, 22, 2, tzinfo=timezone.utc)  # Still September 21 in Chicago.
TODAY = date(2026, 9, 21)


def section(result, key):
    return next(item for item in result.sections if item.key == key)


def job(db, part, number, **kwargs):
    row = WorkOrder(
        company_id=kwargs.pop('company_id', 1),
        part_id=part.id,
        work_order_number=number,
        quantity_ordered=10,
        status=kwargs.pop('status', WorkOrderStatus.RELEASED),
        **kwargs,
    )
    db.add(row)
    db.flush()
    return row


def test_auth_and_fresh_read_only_contract(client, db_session, auth_headers):
    assert client.get(URL).status_code == 401
    rows = seed(db_session)
    response = client.get(URL, headers=auth_headers)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['checked_at'].endswith('Z')
    assert result['sections'][0]['key'] == 'shop'
    assert 'quality_ncr' in {item['source_kind'] for group in result['sections'] for item in group['items']}
    assert db_session.query(OperationalInboxState).count() == 0
    db_session.refresh(rows['blocker'])
    assert rows['blocker'].status == 'open'


def test_effective_permission_overrides_gate_every_section(client, db_session, test_user):
    seed(db_session)
    override = RolePermission(company_id=1, role=test_user.role, permissions=['quality:view'])
    db_session.add(override)
    db_session.commit()
    result = client.get(URL, headers=headers(test_user)).json()
    assert [group['key'] for group in result['sections']] == ['quality']
    assert {item['source_kind'] for item in result['sections'][0]['items']} == {'quality_ncr'}
    override.permissions = ['shipping:view']
    db_session.commit()
    result = client.get(URL, headers=headers(test_user)).json()
    assert result['sections'] == []  # Shipping must not expose denied work-order facts.
    assert any('no access' in note for note in result['coverage_notes'])


def test_operator_clocked_work_is_personal_tenant_scoped_and_live(db_session, operator_user, test_user, test_part):
    db_session.add(Company(id=2, name='Other shop', slug='briefing-other'))
    own = job(db_session, test_part, 'OWN', due_date=TODAY)
    other_employee = job(db_session, test_part, 'OTHER-EMPLOYEE', due_date=TODAY)
    foreign = job(db_session, test_part, 'FOREIGN', company_id=2, due_date=TODAY)
    deleted = job(db_session, test_part, 'DELETED', is_deleted=True)
    ended = job(db_session, test_part, 'ENDED')
    for wo, user_id, company_id, clock_out in (
        (own, operator_user.id, 1, None),
        (other_employee, test_user.id, 1, None),
        (foreign, operator_user.id, 2, None),
        (deleted, operator_user.id, 1, None),
        (ended, operator_user.id, 1, NOW),
        # Malformed cross-tenant time entry cannot make a local WO "mine".
        (other_employee, operator_user.id, 2, None),
    ):
        db_session.add(
            TimeEntry(
                company_id=company_id,
                work_order_id=wo.id,
                user_id=user_id,
                clock_in=NOW - timedelta(hours=1),
                clock_out=clock_out,
            )
        )
    db_session.commit()
    result = HankBriefingService(db_session, operator_user, 1, now=NOW).briefing()
    assert result.sections[0].key == 'my_work'
    assert [item.source_id for item in section(result, 'my_work').items] == [own.id]
    assert section(result, 'my_work').items[0].is_mine
    assert 'not personal assignments' in section(result, 'my_work').description
    assert 'purchasing' not in [group.key for group in result.sections]
    assert 'shipping' not in [group.key for group in result.sections]


def test_quality_assigned_first_shared_sources_tombstones_and_other_tenant_excluded(db_session, test_user):
    rows = seed(db_session)
    db_session.add(Company(id=2, name='Other quality', slug='briefing-other-quality'))
    foreign = seed(db_session, 2)
    test_user.role = UserRole.QUALITY
    rows['ncr'].assigned_to = test_user.id
    db_session.commit()
    result = HankBriefingService(db_session, test_user, 1).briefing()
    assert result.sections[0].key == 'quality'
    item = result.sections[0].items[0]
    assert item.source_id == rows['ncr'].id and item.is_mine
    assert item.owner_name == test_user.full_name
    assert foreign['ncr'].ncr_number not in result.model_dump_json()
    rows['ncr'].is_deleted = True
    db_session.commit()
    assert section(HankBriefingService(db_session, test_user, 1).briefing(), 'quality').items == []


def test_shipping_horizon_uses_shop_day_and_must_ship_date(db_session, test_user, test_part):
    test_user.role = UserRole.SHIPPING
    inside = job(db_session, test_part, 'INSIDE', due_date=TODAY + timedelta(days=2))
    outside = job(db_session, test_part, 'OUTSIDE', due_date=TODAY + timedelta(days=3))
    overdue = job(db_session, test_part, 'OVERDUE', due_date=TODAY - timedelta(days=2))
    must_leave = job(db_session, test_part, 'MUST-LEAVE', due_date=TODAY + timedelta(days=7), must_ship_by=TODAY)
    later_leave = job(db_session, test_part, 'LATER-LEAVE', due_date=TODAY, must_ship_by=TODAY + timedelta(days=3))
    fully_sent = job(db_session, test_part, 'SENT', due_date=TODAY, status=WorkOrderStatus.COMPLETE)
    cancelled = job(db_session, test_part, 'CANCELLED', due_date=TODAY, status=WorkOrderStatus.CANCELLED)
    deleted = job(db_session, test_part, 'DELETED', due_date=TODAY, is_deleted=True)
    db_session.add(
        Shipment(
            company_id=1,
            work_order_id=fully_sent.id,
            shipment_number='FULL-SENT',
            quantity_shipped=10,
            status=ShipmentStatus.SHIPPED,
        )
    )
    db_session.commit()
    result = HankBriefingService(db_session, test_user, 1, now=NOW).briefing()
    assert result.checked_at == NOW
    assert result.sections[0].key == 'shipping'
    shipping = section(result, 'shipping')
    assert {item.source_id for item in shipping.items} == {inside.id, overdue.id, must_leave.id}
    assert not {outside.id, later_leave.id, fully_sent.id, cancelled.id, deleted.id} & {
        item.source_id for item in shipping.items
    }
    assert shipping.items[0].source_id == overdue.id
    assert shipping.items[0].severity == 'high'
    assert 'Readiness is not verified' in shipping.description


def test_snoozed_signal_omitted_until_source_changes(db_session, test_user):
    rows = seed(db_session)
    inbox = OperationalInboxService(db_session, test_user, 1, now=NOW)
    signal = next(item for item in inbox.list().items if item.source_kind == 'blocker')
    db_session.add(
        OperationalInboxState(
            company_id=1,
            source_kind='blocker',
            source_id=signal.source_id,
            updated_by=test_user.id,
            version=1,
            snoozed_occurrence=signal.occurrence,
            snoozed_until=NOW + timedelta(hours=1),
        )
    )
    db_session.commit()
    result = HankBriefingService(db_session, test_user, 1, now=NOW).briefing()
    assert not any(item.key == signal.key for item in section(result, 'shop').items)
    rows['blocker'].status = 'acknowledged'
    db_session.commit()
    result = HankBriefingService(db_session, test_user, 1, now=NOW).briefing()
    assert any(item.key == signal.key for item in section(result, 'shop').items)


def test_results_are_bounded_and_partial_coverage_is_explicit(db_session, test_user, test_part, monkeypatch):
    for n in range(8):
        job(db_session, test_part, f'LATE-{n}', due_date=TODAY - timedelta(days=1))
    db_session.commit()
    result = HankBriefingService(db_session, test_user, 1, now=NOW).briefing()
    shop = section(result, 'shop')
    assert shop.total == 8 and shop.truncated and len(shop.items) == 5
    monkeypatch.setattr('app.services.operations_inbox_service.SOURCE_LIMIT', 3)
    result = HankBriefingService(db_session, test_user, 1, now=NOW).briefing()
    shop = section(result, 'shop')
    assert shop.total == 3 and shop.truncated and len(shop.items) == 3
    assert any('lower bounds' in note for note in result.coverage_notes)
