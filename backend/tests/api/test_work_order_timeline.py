"""Job readers get bounded, tenant-scoped facts without global audit secrets."""

from datetime import datetime, timedelta

from app.models.audit_log import AuditLog
from app.models.inventory import InventoryTransaction, TransactionType
from app.models.production_receipt import ProductionReceipt
from app.models.time_entry import TimeEntry
from app.models.user import UserRole
from app.models.work_order_blocker import WorkOrderBlocker
from app.services.audit_service import AuditService
from tests.api.kiosk_test_helpers import make_user, user_headers
from tests.api.test_scheduling_impact import fixture_job


def timeline(client, wo, headers, query=''):
    return client.get(f'/api/v1/work-orders/{wo.id}/timeline{query}', headers=headers)


def test_authorized_operator_gets_all_three_ledger_shapes_without_costs_or_foreign_actors(client, db_session):
    operator = make_user(db_session)
    foreign = make_user(db_session, company_id=2)
    wo, op, _ = fixture_job(db_session)
    other, _, _ = fixture_job(db_session)
    when = datetime(2026, 9, 7, 15)
    for ref, key, qty in [
        ('work_order', wo.id, -1),
        ('work_order_backflush', wo.id, -2),
        ('work_order_operation', op.id, -3),
    ]:
        db_session.add(
            InventoryTransaction(
                company_id=1,
                part_id=wo.part_id,
                reference_type=ref,
                reference_id=key,
                transaction_type=TransactionType.ISSUE,
                quantity=qty,
                created_by=foreign.id,
                created_at=when,
                unit_cost=98765,
                notes='SECRET_PRIVATE_NOTE',
            )
        )
    db_session.add(
        InventoryTransaction(
            company_id=2,
            part_id=wo.part_id,
            reference_type='work_order',
            reference_id=wo.id,
            transaction_type=TransactionType.ADJUST,
            quantity=555,
            created_by=foreign.id,
            created_at=when,
        )
    )
    db_session.add(
        InventoryTransaction(
            company_id=1,
            part_id=wo.part_id,
            reference_type='work_order',
            reference_id=other.id,
            transaction_type=TransactionType.ADJUST,
            quantity=666,
            created_by=operator.id,
            created_at=when,
        )
    )
    db_session.commit()
    response = timeline(client, wo, user_headers(operator), '?category=material')
    assert response.status_code == 200, response.text
    rows = response.json()['items']
    assert len(rows) == 3 and all(row['actor_id'] is None and row['actor_name'] is None for row in rows)
    assert all('work_order_id=' + str(wo.id) in row['source_url'] for row in rows)
    assert all(
        secret not in response.text for secret in ['98765', 'SECRET_PRIVATE_NOTE', '555', '666', foreign.full_name]
    )


def test_cross_tenant_deleted_unauthenticated_and_invalid_filters(client, db_session):
    viewer = make_user(db_session)
    foreign, _, _ = fixture_job(db_session, company_id=2)
    assert timeline(client, foreign, user_headers(viewer)).status_code == 404
    own, _, _ = fixture_job(db_session)
    assert timeline(client, own, {}).status_code == 401
    assert timeline(client, own, user_headers(viewer), '?limit=101').status_code == 422
    assert timeline(client, own, user_headers(viewer), '?cursor=broken').status_code == 422
    assert timeline(client, own, user_headers(viewer), '?category=secrets').status_code == 422
    own.is_deleted = True
    db_session.commit()
    assert timeline(client, own, user_headers(viewer)).status_code == 404


def test_stable_cursor_no_duplicates_across_identical_timestamps(client, db_session):
    user = make_user(db_session)
    wo, op, _ = fixture_job(db_session)
    stamp = datetime(2026, 9, 7, 15)
    wo.created_at = stamp
    for index in range(8):
        db_session.add(
            TimeEntry(
                company_id=1,
                user_id=user.id,
                work_order_id=wo.id,
                operation_id=op.id,
                clock_in=stamp,
                clock_out=stamp,
                duration_hours=1,
            )
        )
    db_session.add(
        WorkOrderBlocker(
            company_id=1, work_order_id=wo.id, title='Fixture hold', reported_at=stamp, reported_by=user.id
        )
    )
    db_session.commit()
    whole = timeline(client, wo, user_headers(user), '?limit=100').json()['items']
    found = []
    query = '?limit=3'
    for _ in range(10):
        response = timeline(client, wo, user_headers(user), query)
        assert response.status_code == 200, response.text
        page = response.json()
        assert len(page['items']) <= 3
        found.extend(row['id'] for row in page['items'])
        if not page['next_cursor']:
            break
        query = '?limit=3&cursor=' + page['next_cursor']
    assert found == [row['id'] for row in whole]
    assert len(found) == len(set(found)) == 18


def test_actor_date_filters_and_private_audit_payloads(client, db_session):
    user = make_user(db_session, role=UserRole.MANAGER)
    wo, _, _ = fixture_job(db_session)
    audit = AuditService(db_session, user=user, company_id=1)
    audit.log_update(
        resource_type='work_order',
        resource_id=wo.id,
        resource_identifier=wo.work_order_number,
        description='PRIVATE_COST',
        old_values={'actual_cost': 777777},
        new_values={'due_date': '2026-10-01', 'actual_cost': 888888},
        extra_data={'secret': 'DO_NOT_EXPOSE'},
    )
    db_session.commit()
    row = db_session.query(AuditLog).filter(AuditLog.resource_id == wo.id).one()
    response = timeline(
        client,
        wo,
        user_headers(user),
        f'?category=audit&actor_id={user.id}&start_at={(row.timestamp-timedelta(seconds=1)).isoformat()}Z',
    )
    assert response.status_code == 200, response.text
    body = response.json()['items']
    assert len(body) == 1 and body[0]['evidence'] == 'audit'
    assert body[0]['actor_name'] == user.full_name
    assert body[0]['detail'] == 'Updated due date'
    assert all(text not in response.text for text in ['PRIVATE_COST', '777777', '888888', 'DO_NOT_EXPOSE'])
    assert (
        timeline(
            client,
            wo,
            user_headers(user),
            f'?category=audit&start_at={(row.timestamp+timedelta(seconds=1)).isoformat()}Z',
        ).json()['items']
        == []
    )


def test_production_receipt_shows_recorded_totals_and_read_is_pure(client, db_session):
    user = make_user(db_session)
    wo, op, _ = fixture_job(db_session)
    entry = TimeEntry(
        company_id=1, user_id=user.id, work_order_id=wo.id, operation_id=op.id, clock_in=datetime.utcnow()
    )
    db_session.add(entry)
    db_session.flush()
    db_session.add(
        ProductionReceipt(
            company_id=1,
            request_id='fixture',
            request_hash='a' * 64,
            operator_id=user.id,
            operation_id=op.id,
            time_entry_id=entry.id,
            response={'operation': {'quantity_complete': 4, 'quantity_scrapped': 2}},
        )
    )
    db_session.commit()
    before = (db_session.query(AuditLog).count(), db_session.query(InventoryTransaction).count())
    response = timeline(client, wo, user_headers(user), '?category=production')
    assert response.status_code == 200, response.text
    assert 'Recorded totals: good 4, scrap 2' in response.text
    assert (db_session.query(AuditLog).count(), db_session.query(InventoryTransaction).count()) == before


def test_supplemental_quality_telemetry_scopes_parents_and_never_exposes_payload(client, db_session):
    from app.models.operational_event import OperationalEvent
    from app.models.quality import NonConformanceReport

    user = make_user(db_session)
    foreign_actor = make_user(db_session, company_id=2)
    wo, _, _ = fixture_job(db_session)
    ncrs = [
        NonConformanceReport(
            company_id=company,
            work_order_id=wo.id,
            ncr_number=f'TIMELINE-{company}-{deleted}',
            title='Telemetry fixture',
            source='in_process',
            description='Private quality description',
            is_deleted=deleted,
        )
        for company, deleted in [(1, False), (1, True), (2, False)]
    ]
    db_session.add_all(ncrs)
    db_session.flush()
    for company, ncr in [(1, ncrs[0]), (1, ncrs[1]), (1, ncrs[2]), (2, ncrs[0])]:
        db_session.add(
            OperationalEvent(
                company_id=company,
                event_type='ncr_updated',
                source_module='quality',
                entity_type='ncr',
                entity_id=ncr.id,
                work_order_id=wo.id,
                user_id=foreign_actor.id,
                event_payload={'private_cost': 987654, 'notes': 'PRIVATE_TELEMETRY_PAYLOAD'},
            )
        )
    db_session.commit()
    response = timeline(client, wo, user_headers(user), '?category=quality')
    assert response.status_code == 200, response.text
    telemetry = [row for row in response.json()['items'] if row['evidence'] == 'telemetry']
    assert len(telemetry) == 1
    assert telemetry[0]['source_url'] == f'/quality?ncr={ncrs[0].id}'
    assert telemetry[0]['actor_id'] is None and telemetry[0]['actor_name'] is None
    assert 'Supplemental event' in telemetry[0]['detail']
    assert all(
        secret not in response.text for secret in ['PRIVATE_TELEMETRY_PAYLOAD', '987654', foreign_actor.full_name]
    )
