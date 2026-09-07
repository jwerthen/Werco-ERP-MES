from datetime import date, datetime, timedelta, timezone

import pytest

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.inventory import InventoryItem
from app.models.operations_inbox import OperationalInboxState
from app.models.part import Part
from app.models.purchasing import POStatus, PurchaseOrder, PurchaseOrderLine, Vendor
from app.models.quality import NCRSource, NCRStatus, NonConformanceReport
from app.models.role_permission import RolePermission
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker
from app.services.operations_inbox_service import OperationalInboxService

URL = '/api/v1/operations-inbox/'


def headers(user, **kwargs):
    return {
        'Authorization': 'Bearer ' + create_access_token(subject=user.id, company_id=user.company_id, **kwargs),
        'X-Requested-With': 'XMLHttpRequest',
    }


def seed(db, company_id=1):
    part = Part(
        company_id=company_id,
        part_number=f'INBOX-{company_id}',
        name='Plate',
        part_type='manufactured',
        unit_of_measure='each',
        reorder_point=10,
        safety_stock=3,
    )
    vendor = Vendor(company_id=company_id, code=f'INBOX-V-{company_id}', name='Synthetic supplier')
    db.add_all([part, vendor])
    db.flush()
    wo = WorkOrder(
        company_id=company_id,
        part_id=part.id,
        work_order_number=f'INBOX-WO-{company_id}',
        quantity_ordered=10,
        status=WorkOrderStatus.RELEASED,
        due_date=date.today() - timedelta(days=2),
    )
    po = PurchaseOrder(
        company_id=company_id,
        vendor_id=vendor.id,
        po_number=f'INBOX-PO-{company_id}',
        status=POStatus.SENT,
        expected_date=date.today() - timedelta(days=2),
    )
    db.add_all([wo, po])
    db.flush()
    line = PurchaseOrderLine(
        company_id=company_id,
        purchase_order_id=po.id,
        line_number=1,
        part_id=part.id,
        quantity_ordered=10,
        quantity_received=3,
        unit_price=1,
    )
    blocker = WorkOrderBlocker(
        company_id=company_id,
        work_order_id=wo.id,
        title=f'Material missing {company_id}',
        category='material_missing',
        severity='high',
        status='open',
    )
    ncr = NonConformanceReport(
        company_id=company_id,
        ncr_number=f'INBOX-NCR-{company_id}',
        title='Inspect edge',
        description='Burr observed',
        source=NCRSource.IN_PROCESS,
        status=NCRStatus.OPEN,
    )
    stock = InventoryItem(company_id=company_id, part_id=part.id, location='INBOX', quantity_on_hand=2)
    db.add_all([line, blocker, ncr, stock])
    db.commit()
    return {'part': part, 'wo': wo, 'po': po, 'line': line, 'blocker': blocker, 'ncr': ncr, 'stock': stock}


def get_items(client, auth):
    response = client.get(URL, headers=auth)
    assert response.status_code == 200, response.text
    return response.json()['items']


def patch(client, auth, item, **changes):
    return client.patch(
        f"{URL}{item['source_kind']}/{item['source_id']}",
        headers=auth,
        json={'expected_version': item['version'], 'occurrence': item['occurrence'], **changes},
    )


def test_live_sources_exclude_resolved_and_outside_tenant(client, db_session, auth_headers):
    rows = seed(db_session)
    db_session.add(Company(id=2, name='Other', slug='inbox-other'))
    db_session.commit()
    foreign = seed(db_session, 2)
    items = get_items(client, auth_headers)
    assert {item['source_kind'] for item in items} == {
        'late_work_order',
        'blocker',
        'low_stock',
        'quality_ncr',
        'overdue_po_line',
    }
    assert all(item['source_id'] != foreign['blocker'].id for item in items if item['source_kind'] == 'blocker')
    assert next(item for item in items if item['source_kind'] == 'overdue_po_line')['detail'].startswith('7 units')
    rows['wo'].status = WorkOrderStatus.COMPLETE
    rows['ncr'].status = NCRStatus.CLOSED
    rows['line'].quantity_received = 10
    rows['stock'].quantity_on_hand = 10
    db_session.commit()
    assert get_items(client, auth_headers) == []


def test_custom_permissions_and_readonly_role_are_enforced(client, db_session, test_user):
    seed(db_session)
    db_session.add(RolePermission(company_id=1, role=UserRole.MANAGER, permissions=['quality:view']))
    db_session.commit()
    auth = headers(test_user)
    response = client.get(URL, headers=auth).json()
    assert {item['source_kind'] for item in response['items']} == {'quality_ncr'}
    assert all(not item['can_manage'] for item in response['items'])
    assert all(assignee['sources'] == ['quality_ncr'] for assignee in response['assignees'])
    assert patch(client, auth, response['items'][0], acknowledge=True).status_code == 403
    assert (
        client.patch(
            URL + 'blocker/1', headers=auth, json={'expected_version': 0, 'occurrence': 'a' * 64, 'acknowledge': True}
        ).status_code
        == 404
    )


def test_assignment_is_shared_audited_and_uses_existing_blocker_owner(client, db_session, auth_headers, operator_user):
    rows = seed(db_session)
    item = next(item for item in get_items(client, auth_headers) if item['source_kind'] == 'blocker')
    response = patch(
        client, auth_headers, item, owner_id=operator_user.id, next_action='Check material rack', acknowledge=True
    )
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved['owner_id'] == operator_user.id and saved['acknowledged']
    db_session.refresh(rows['blocker'])
    assert rows['blocker'].assigned_to == operator_user.id and rows['blocker'].status == 'acknowledged'
    operator_item = next(item for item in get_items(client, headers(operator_user)) if item['key'] == saved['key'])
    assert operator_item['next_action'] == 'Check material rack' and not operator_item['can_manage']
    assert db_session.query(AuditLog).filter(AuditLog.resource_type == 'operational_inbox').count() == 1
    stale = patch(client, auth_headers, item, next_action='Overwrite')
    assert stale.status_code == 409
    assert patch(client, auth_headers, saved, owner_id=None).status_code == 200
    db_session.refresh(rows['blocker'])
    assert rows['blocker'].assigned_to is None


def test_foreign_or_inactive_assignee_and_foreign_source_rejected(client, db_session, auth_headers):
    seed(db_session)
    db_session.add(Company(id=2, name='Other', slug='inbox-assignee-other'))
    foreign_user = User(
        company_id=2,
        email='foreign@example.test',
        employee_id='F1',
        first_name='Foreign',
        last_name='User',
        hashed_password='unused',
        role=UserRole.MANAGER,
    )
    db_session.add(foreign_user)
    db_session.commit()
    foreign = seed(db_session, 2)
    item = get_items(client, auth_headers)[0]
    assert patch(client, auth_headers, item, owner_id=foreign_user.id).status_code == 422
    assert (
        client.patch(
            URL + f"blocker/{foreign['blocker'].id}",
            headers=auth_headers,
            json={'expected_version': 0, 'occurrence': 'a' * 64, 'acknowledge': True},
        ).status_code
        == 404
    )
    assert db_session.query(OperationalInboxState).count() == 0
    response = client.get(URL, headers=auth_headers).json()
    assert foreign_user.id not in [u['id'] for u in response['assignees']]


def test_snooze_is_occurrence_specific_and_expiry_restores_attention(client, db_session, auth_headers, test_user):
    rows = seed(db_session)
    item = next(item for item in get_items(client, auth_headers) if item['source_kind'] == 'low_stock')
    response = patch(client, auth_headers, item, snooze_hours=24, acknowledge=True, next_action='Review purchase needs')
    assert response.status_code == 200, response.text
    assert response.json()['snoozed_until'] and response.json()['acknowledged']
    # Source change makes the warning visible again without losing the shared plan.
    rows['stock'].quantity_on_hand = 1
    db_session.commit()
    changed = next(item for item in get_items(client, auth_headers) if item['source_kind'] == 'low_stock')
    assert changed['snoozed_until'] is None and not changed['acknowledged']
    assert changed['next_action'] == 'Review purchase needs'
    assert patch(client, auth_headers, response.json(), snooze_hours=24).status_code == 409
    again = patch(client, auth_headers, changed, snooze_hours=24)
    assert again.status_code == 200
    service = OperationalInboxService(db_session, test_user, 1, now=datetime.now(timezone.utc) + timedelta(hours=25))
    expired = next(item for item in service.list().items if item.source_kind == 'low_stock')
    assert expired.snoozed_until is None


def test_new_issue_is_not_hidden_by_old_ack_or_source_resolution(client, db_session, auth_headers):
    rows = seed(db_session)
    first = next(item for item in get_items(client, auth_headers) if item['source_kind'] == 'blocker')
    assert patch(client, auth_headers, first, acknowledge=True, snooze_hours=24).status_code == 200
    second = WorkOrderBlocker(
        company_id=1,
        work_order_id=rows['wo'].id,
        title='Second material issue',
        category='material_missing',
        status='open',
    )
    db_session.add(second)
    db_session.commit()
    second_item = next(
        item
        for item in get_items(client, auth_headers)
        if item['source_kind'] == 'blocker' and item['source_id'] == second.id
    )
    assert not second_item['acknowledged'] and second_item['snoozed_until'] is None
    rows['blocker'].status = 'resolved'
    db_session.commit()
    assert patch(client, auth_headers, first, next_action='Stale source').status_code == 404


@pytest.mark.parametrize(
    'change',
    [{'snooze_hours': 169}, {'snooze_hours': -1}, {'next_action': None}, {'acknowledge': None}, {'resolve': True}],
)
def test_invalid_or_business_resolution_actions_rejected(client, db_session, auth_headers, change):
    seed(db_session)
    assert patch(client, auth_headers, get_items(client, auth_headers)[0], **change).status_code == 422


def test_company_local_day_and_truncation_are_explicit(db_session, test_user, monkeypatch):
    rows = seed(db_session)
    rows['wo'].due_date = date(2026, 9, 6)
    db_session.commit()
    service = OperationalInboxService(db_session, test_user, 1, now=datetime(2026, 9, 7, 2, tzinfo=timezone.utc))
    assert not any(item.source_kind == 'late_work_order' for item in service.list().items)
    monkeypatch.setattr('app.services.operations_inbox_service.SOURCE_LIMIT', 0)
    result = service.list()
    assert result.items == [] and result.truncated_sources


def test_read_only_company_context_cannot_triage(client, db_session, test_user):
    seed(db_session)
    auth = headers(test_user, read_only=True)
    items = get_items(client, auth)
    assert items and all(not item['can_manage'] for item in items)
    assert patch(client, auth, items[0], acknowledge=True).status_code == 403
    assert db_session.query(OperationalInboxState).count() == 0


def test_inactive_or_workflow_ineligible_assignee_is_rejected(client, db_session, auth_headers):
    seed(db_session)
    inactive = User(
        company_id=1,
        email='inactive-inbox@example.test',
        employee_id='INBOX-INACTIVE',
        first_name='Inactive',
        last_name='Owner',
        hashed_password='unused',
        role=UserRole.MANAGER,
        is_active=False,
    )
    shipping = User(
        company_id=1,
        email='shipping-inbox@example.test',
        employee_id='INBOX-SHIPPING',
        first_name='Shipping',
        last_name='Owner',
        hashed_password='unused',
        role=UserRole.SHIPPING,
        is_active=True,
    )
    db_session.add_all([inactive, shipping])
    db_session.commit()
    quality = next(item for item in get_items(client, auth_headers) if item['source_kind'] == 'quality_ncr')
    assert patch(client, auth_headers, quality, owner_id=inactive.id).status_code == 422
    assert patch(client, auth_headers, quality, owner_id=shipping.id).status_code == 422
    assert db_session.query(OperationalInboxState).count() == 0


def test_projected_shortages_use_latest_completed_run_and_keep_supply_draft_followup(client, db_session, auth_headers):
    from app.models.mrp import MRPAction, MRPRun, MRPRunStatus, PlanningAction

    rows = seed(db_session)
    old = MRPRun(
        company_id=1, run_number='INBOX-MRP-OLD', status=MRPRunStatus.COMPLETE, completed_at=datetime(2026, 9, 5)
    )
    current = MRPRun(
        company_id=1, run_number='INBOX-MRP-CURRENT', status=MRPRunStatus.COMPLETE, completed_at=datetime(2026, 9, 6)
    )
    failed = MRPRun(
        company_id=1, run_number='INBOX-MRP-FAILED', status=MRPRunStatus.ERROR, completed_at=datetime(2026, 9, 7)
    )
    db_session.add_all([old, current, failed])
    db_session.flush()

    def action(run):
        return MRPAction(
            company_id=1,
            mrp_run_id=run.id,
            part_id=rows['part'].id,
            action_type=PlanningAction.ORDER,
            quantity=5,
            required_date=date.today(),
            suggested_order_date=date.today(),
        )

    old_action, new_action, failed_action = action(old), action(current), action(failed)
    new_action.result_po_id = rows['po'].id
    db_session.add_all([old_action, new_action, failed_action])
    db_session.commit()
    shortages = [item for item in get_items(client, auth_headers) if item['source_kind'] == 'mrp_shortage']
    assert len(shortages) == 1 and shortages[0]['source_id'] == new_action.id
    assert shortages[0]['href'] == f'/mrp?run={current.id}&action={new_action.id}'
    assert 'supply draft is already linked' in shortages[0]['detail']
    assert patch(client, auth_headers, shortages[0], snooze_hours=24).status_code == 200
    new_action.is_processed = True
    db_session.commit()
    assert not any(item['source_kind'] == 'mrp_shortage' for item in get_items(client, auth_headers))
    later = MRPRun(
        company_id=1, run_number='INBOX-MRP-LATER', status=MRPRunStatus.COMPLETE, completed_at=datetime(2026, 9, 8)
    )
    db_session.add(later)
    db_session.flush()
    db_session.add(action(later))
    db_session.commit()
    recurring = next(item for item in get_items(client, auth_headers) if item['source_kind'] == 'mrp_shortage')
    assert recurring['snoozed_until'] is None and recurring['source_id'] != new_action.id
