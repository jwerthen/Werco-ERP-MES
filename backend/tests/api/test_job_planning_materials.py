"""Calendar and material dates reflect evidence without duplicate supply."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.models.audit_log import AuditLog
from app.models.bom import BOM, BOMItem
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.part import Part
from app.models.purchasing import POStatus, PurchaseOrder, PurchaseOrderLine, Vendor
from app.models.work_order_material import AllocationSource, AllocationStatus, WorkOrderMaterialAllocation
from app.models.working_calendar import WorkingCalendar
from app.services.material_readiness_service import material_readiness
from app.services.prediction_service import PredictionService
from tests.api.kiosk_test_helpers import make_user
from tests.api.test_scheduling_impact import APPLY, fixture_job, preview


def material_job(db, quantity=10):
    wo, op, center = fixture_job(db)
    wo.quantity_ordered = quantity
    part = Part(
        company_id=1, part_number=f'MATERIAL-{wo.id}', name='Sheet', part_type='purchased', unit_of_measure='each'
    )
    db.add(part)
    db.flush()
    bom = BOM(company_id=1, part_id=wo.part_id, status='released', is_active=True)
    db.add(bom)
    db.flush()
    db.add(
        BOMItem(
            company_id=1,
            bom_id=bom.id,
            component_part_id=part.id,
            item_number=10,
            quantity=1,
            item_type='buy',
            unit_of_measure='each',
        )
    )
    db.commit()
    return wo, op, center, part


def stock(db, part, quantity, **kw):
    row = InventoryItem(company_id=1, part_id=part.id, quantity_on_hand=quantity, location='QA', **kw)
    db.add(row)
    db.commit()
    return row


def supply(db, part, quantity, arrival, confirmed=True):
    vendor = Vendor(company_id=1, code=f'V-{part.id}', name='Fixture supply')
    db.add(vendor)
    db.flush()
    po = PurchaseOrder(
        company_id=1,
        po_number=f'PO-{part.id}',
        vendor_id=vendor.id,
        status=POStatus.SENT,
        expected_date=arrival,
        required_date=arrival,
    )
    po.supplier_confirmed_date = arrival if confirmed else None
    po.supplier_acknowledged_at = datetime.utcnow() if confirmed else None
    db.add(po)
    db.flush()
    line = PurchaseOrderLine(
        company_id=1,
        purchase_order_id=po.id,
        line_number=1,
        part_id=part.id,
        quantity_ordered=quantity,
        quantity_received=0,
        unit_price=1,
    )
    db.add(line)
    db.commit()
    return po, line


def test_preview_uses_confirmed_arrival_and_calendar_without_writes(client, auth_headers, db_session):
    wo, op, center, part = material_job(db_session)
    today = datetime.now(ZoneInfo('America/Chicago')).date()
    saturday = today + timedelta(days=(5 - today.weekday()) % 7 + 7)
    stock(db_session, part, 4)
    supply(db_session, part, 8, saturday)
    db_session.add(
        WorkingCalendar(
            company_id=1,
            work_center_id=center.id,
            weekly_hours=[8] * 5 + [0, 0],
            overrides=[],
            version=1,
            updated_by=make_user(db_session).id,
        )
    )
    db_session.commit()
    audits = db_session.query(AuditLog).count()
    plan = preview(client, auth_headers, [wo.id], action='earliest', shift_days=0)
    job = plan['jobs'][0]
    assert job['materials']['ready_date'] == saturday.isoformat()
    assert job['operations'][0]['after_start'][:10] == (saturday + timedelta(days=2)).isoformat()
    assert [s['quantity'] for s in job['materials']['lines'][0]['sources']] == [4, 6]
    db_session.expire_all()
    assert op.scheduled_start is None
    assert db_session.query(AuditLog).count() == audits


def test_selected_jobs_share_stock_and_remaining_po_once(client, auth_headers, db_session):
    wo, _, center, part = material_job(db_session)
    other, _, _ = fixture_job(db_session, center=center)
    other.part_id = wo.part_id
    db_session.commit()
    stock(db_session, part, 8, quantity_allocated=3)
    _, line = supply(db_session, part, 10, date.today() + timedelta(days=2))
    line.quantity_received = 5
    db_session.commit()
    plan = preview(client, auth_headers, [wo.id, other.id], action='earliest', shift_days=0)
    first, second = plan['jobs']
    assert first['materials']['lines'][0]['covered_quantity'] == 10
    assert second['outcome'] == 'blocked' and second['after_finish'] is None
    assert second['materials']['lines'][0]['shortage_quantity'] == 10
    assert (
        sum(
            source['quantity'] for job in plan['jobs'] for row in job['materials']['lines'] for source in row['sources']
        )
        == 10
    )


@pytest.mark.parametrize('case', ['held', 'negative', 'expired', 'unconfirmed', 'overdue', 'withdrawn'])
def test_unknown_supply_never_invents_a_date(client, auth_headers, db_session, case):
    wo, _, _, part = material_job(db_session)
    today = datetime.now(ZoneInfo('America/Chicago')).date()
    if case == 'held':
        stock(db_session, part, 100, status='on_hold')
    elif case == 'negative':
        stock(db_session, part, -3)
    elif case == 'expired':
        stock(db_session, part, 100, expiration_date=datetime.combine(today - timedelta(days=1), datetime.min.time()))
    else:
        po, _ = supply(
            db_session,
            part,
            100,
            today - timedelta(days=1) if case == 'overdue' else today + timedelta(days=2),
            confirmed=case != 'unconfirmed',
        )
        if case == 'withdrawn':
            po.supplier_acknowledged_at = None
            db_session.commit()
    result = preview(client, auth_headers, [wo.id], action='earliest', shift_days=0)
    assert result['plan_token'] is None
    assert result['jobs'][0]['materials']['ready_date'] is None
    forecast = PredictionService(db_session, 1).predict_delivery(wo.id)
    assert forecast.predicted_completion is None and forecast.on_time_probability is None


def test_tie_replaces_bom_and_nets_actual_ledger_not_cache(db_session):
    wo, op, _, part = material_job(db_session)
    tie = WorkOrderMaterialAllocation(
        company_id=1,
        work_order_id=wo.id,
        work_order_operation_id=op.id,
        part_id=part.id,
        source=AllocationSource.MANUAL,
        status=AllocationStatus.OPEN,
        qty_planned=5,
        qty_per_run=0.5,
        qty_consumed=99,
        unit_of_measure='each',
    )
    db_session.add(tie)
    db_session.flush()
    user = make_user(db_session)
    db_session.add(
        InventoryTransaction(
            company_id=1,
            part_id=part.id,
            transaction_type=TransactionType.ISSUE,
            quantity=-2,
            reference_type='work_order_operation',
            reference_id=op.id,
            allocation_id=tie.id,
            created_by=user.id,
        )
    )
    db_session.commit()
    stock(db_session, part, 3)
    row = material_readiness(db_session, 1, [wo], date.today())['jobs'][wo.id]
    assert len(row['lines']) == 1 and row['lines'][0]['required_quantity'] == 3 and row['status'] == 'ready'


@pytest.mark.parametrize('change', ['stock', 'reserved', 'arrival', 'receipt', 'bom'])
def test_apply_refuses_material_changes_after_review(client, auth_headers, db_session, change):
    wo, _, _, part = material_job(db_session)
    inv = stock(db_session, part, 10)
    po, line = supply(db_session, part, 10, date.today() + timedelta(days=10))
    plan = preview(client, auth_headers, [wo.id], action='earliest', shift_days=0)
    assert plan['plan_token']
    if change == 'stock':
        inv.quantity_on_hand -= 1
    if change == 'reserved':
        inv.quantity_allocated = 1
    if change == 'arrival':
        po.supplier_confirmed_date += timedelta(days=1)
    if change == 'receipt':
        line.quantity_received += 1
    if change == 'bom':
        db_session.query(BOMItem).filter(BOMItem.component_part_id == part.id).first().quantity = 2
    db_session.commit()
    response = client.post(APPLY, headers=auth_headers, json={'plan_token': plan['plan_token']})
    assert response.status_code == 409, response.text


def test_delivery_and_weekly_capacity_share_short_shift_shutdown_calendar(db_session, monkeypatch):
    import app.services.prediction_service as module

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 11, 12, tzinfo=tz)

        @classmethod
        def utcnow(cls):
            return cls(2026, 9, 11, 17)

    monkeypatch.setattr(module, 'datetime', Clock)
    wo, op, center = fixture_job(db_session, hours=12)
    db_session.add(
        WorkingCalendar(
            company_id=1,
            work_center_id=center.id,
            weekly_hours=[8, 8, 8, 8, 4, 0, 0],
            overrides=[{'date': '2026-09-14', 'hours': 0, 'reason': 'Shutdown'}],
            version=1,
            updated_by=make_user(db_session).id,
        )
    )
    db_session.commit()
    service = PredictionService(db_session, 1)
    forecast = service.predict_delivery(wo.id)
    assert forecast.operations[0].queue_position == 0
    assert forecast.predicted_completion.date() == date(2026, 9, 15)
    assert forecast.operations[0].predicted_start.date() == date(2026, 9, 11)
    assert forecast.materials.status == 'not_defined'
    assert service.forecast_capacity(2).weeks[0].work_centers[0].available_hours == 28


def test_closed_calendar_returns_unknown_completion_and_visible_capacity_conflict(db_session):
    wo, _, center = fixture_job(db_session)
    db_session.add(
        WorkingCalendar(
            company_id=1,
            work_center_id=center.id,
            weekly_hours=[0] * 7,
            overrides=[],
            version=1,
            updated_by=make_user(db_session).id,
        )
    )
    db_session.commit()
    service = PredictionService(db_session, 1)
    forecast = service.predict_delivery(wo.id)
    assert forecast.predicted_completion is None
    assert any('No working capacity' in message for message in forecast.warnings)
    capacity = service.forecast_capacity(1)
    assert capacity.weeks[0].work_centers[0].available_hours == 0
    assert capacity.weeks[0].work_centers[0].is_overloaded
    assert 'no working hours' in capacity.alerts[0]['message']


@pytest.mark.parametrize('route', ['manual', 'earliest', 'operation', 'bulk', 'finite'])
def test_legacy_writers_cannot_bypass_unknown_materials(client, auth_headers, db_session, route):
    wo, op, _, _ = material_job(db_session)
    base = '/api/v1/scheduling'
    if route == 'manual':
        response = client.put(
            f'{base}/work-orders/{wo.id}/schedule',
            headers=auth_headers,
            json={'scheduled_start': date.today().isoformat(), 'forward_schedule': True},
        )
    elif route == 'earliest':
        response = client.post(f'{base}/work-orders/{wo.id}/schedule-earliest', headers=auth_headers, json={})
    elif route == 'operation':
        response = client.put(
            f'{base}/operations/{op.id}/schedule',
            headers=auth_headers,
            json={'scheduled_start': date.today().isoformat(), 'scheduled_end': date.today().isoformat()},
        )
    elif route == 'bulk':
        response = client.post(f'{base}/bulk-schedule-earliest', headers=auth_headers, json={'work_order_ids': [wo.id]})
    else:
        response = client.post(f'{base}/run', headers=auth_headers, json={})
    assert response.status_code in (200, 409), response.text
    assert 'Material-ready date is unknown' in response.text
    db_session.expire_all()
    assert op.scheduled_start is None


@pytest.mark.parametrize('action', ['shift', 'earliest'])
def test_future_expiry_blocks_calendar_delayed_or_shifted_start(client, auth_headers, db_session, action):
    wo, op, center, part = material_job(db_session)
    today = datetime.now(ZoneInfo('America/Chicago')).date()
    stock(db_session, part, 10, expiration_date=datetime.combine(today + timedelta(days=1), datetime.min.time()))
    if action == 'shift':
        op.scheduled_start = op.scheduled_end = datetime.combine(today, datetime.min.time())
    else:
        db_session.add(
            WorkingCalendar(
                company_id=1,
                work_center_id=center.id,
                weekly_hours=[8] * 7,
                overrides=[{'date': (today + timedelta(days=i)).isoformat(), 'hours': 0} for i in range(3)],
                version=1,
                updated_by=make_user(db_session).id,
            )
        )
    db_session.commit()
    plan = preview(client, auth_headers, [wo.id], action=action, shift_days=7 if action == 'shift' else 0)
    assert plan['jobs'][0]['materials']['status'] == 'ready'
    assert plan['jobs'][0]['outcome'] == 'blocked'
    assert 'expires before the proposed start' in plan['jobs'][0]['reason']
    assert plan['plan_token'] is None
