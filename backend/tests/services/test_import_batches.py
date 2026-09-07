"""Durable import receipts stay atomic with records, even if responses are lost."""

import csv
import io
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.models.customer import Customer
from app.models.import_batch import ImportBatch, ImportBatchRow
from app.services import import_batch_service as batches
from app.services.audit_service import AuditService

pytestmark = [pytest.mark.requires_db, pytest.mark.asyncio]
REQUEST = Request(
    {'type': 'http', 'method': 'POST', 'path': '/api/v1/import/batches', 'headers': [], 'client': ('test', 123)}
)
CONTENT = b'name,email\nGood Account,good@example.com\nFix Account,invalid-email\n'


async def prepare(db, user, content=CONTENT, entity='customers'):
    return await batches.prepare_batch(db, user, 1, REQUEST, entity, 'review.csv', content, str(uuid4()))


def corrected_csv(content):
    rows = list(csv.reader(io.StringIO(content)))
    rows[1][rows[0].index('email')] = 'fixed@example.com'
    result = io.StringIO()
    csv.writer(result).writerows(rows)
    return result.getvalue().encode()


async def test_partial_commit_correction_and_replay_create_each_row_once(db_session, admin_user):
    review = await prepare(db_session, admin_user)
    assert review['counts'] == {'invalid': 1, 'ready': 1}
    assert db_session.query(Customer).count() == 0
    receipt = await batches.commit_batch(db_session, admin_user, 1, REQUEST, review['id'], review['version'])
    assert receipt['counts'] == {'created': 1, 'invalid': 1}
    assert receipt['created_records'] == 1
    with pytest.raises(HTTPException) as changed:
        await batches.commit_batch(db_session, admin_user, 1, REQUEST, review['id'], review['version'])
    assert changed.value.status_code == 409
    assert (await prepare(db_session, admin_user))['id'] == receipt['id']
    batch = batches.load_batch(db_session, 1, admin_user, receipt['id'])
    correction = corrected_csv(batches.failed_rows_csv(db_session, 1, batch))
    fixed = await batches.correct_batch(
        db_session, admin_user, 1, REQUEST, batch.id, batch.version, 'fix.csv', correction
    )
    assert fixed['counts'] == {'created': 1, 'ready': 1}
    completed = await batches.commit_batch(db_session, admin_user, 1, REQUEST, batch.id, fixed['version'])
    assert completed['created_records'] == 2
    assert db_session.query(Customer).count() == 2
    await batches.commit_batch(db_session, admin_user, 1, REQUEST, batch.id, completed['version'])
    assert db_session.query(Customer).count() == 2


async def test_business_write_rolls_back_if_receipt_cannot_be_saved(db_session, admin_user, monkeypatch):
    review = await prepare(db_session, admin_user, b'name\nAtomic Account\n')

    def fail(*args, **kwargs):
        raise RuntimeError('receipt unavailable')

    monkeypatch.setattr(AuditService, 'log_update', fail)
    with pytest.raises(RuntimeError):
        await batches.commit_batch(db_session, admin_user, 1, REQUEST, review['id'], review['version'])
    db_session.rollback()
    assert db_session.query(Customer).count() == 0
    row = db_session.query(ImportBatchRow).one()
    assert row.status == 'ready'
    assert row.result is None


async def test_lost_commit_response_reconciles_to_original_receipt(db_session, admin_user, monkeypatch):
    review = await prepare(db_session, admin_user, b'name\nLost Response Account\n')
    commit = db_session.commit

    def lost_response():
        commit()
        raise RuntimeError('response lost after durable commit')

    monkeypatch.setattr(db_session, 'commit', lost_response)
    with pytest.raises(RuntimeError):
        await batches.commit_batch(db_session, admin_user, 1, REQUEST, review['id'], review['version'])
    monkeypatch.setattr(db_session, 'commit', commit)
    db_session.rollback()
    batch = batches.load_batch(db_session, 1, admin_user, review['id'])
    assert batches.batch_response(db_session, 1, admin_user, batch)['counts'] == {'created': 1}
    await batches.commit_batch(db_session, admin_user, 1, REQUEST, batch.id, batch.version)
    assert db_session.query(Customer).count() == 1


async def test_created_rows_cannot_be_corrected_and_cross_tenant_reads_refused(db_session, admin_user):
    review = await prepare(db_session, admin_user, b'name\nProtected Account\n')
    receipt = await batches.commit_batch(db_session, admin_user, 1, REQUEST, review['id'], review['version'])
    row = receipt['rows'][0]
    with pytest.raises(HTTPException) as refused:
        await batches.correct_batch(
            db_session,
            admin_user,
            1,
            REQUEST,
            review['id'],
            receipt['version'],
            'fix.csv',
            f'_import_row_id,name\n{row.row_key},Changed\n'.encode(),
        )
    assert refused.value.status_code == 409
    with pytest.raises(HTTPException) as missing:
        batches.load_batch(db_session, 2, admin_user, review['id'])
    assert missing.value.status_code == 404


async def test_user_passwords_never_persist_or_appear_in_receipt(db_session, admin_user):
    secret = 'Ak9#Xz4!Ve7$Tr5@'
    content = f'employee_id,first_name,last_name,email,role,password\nIMP-1,Import,Person,imp1@example.test,manager,{secret}\n'.encode()
    review = await prepare(db_session, admin_user, content, 'users')
    assert review['counts'] == {'ready': 1}
    batch = db_session.query(ImportBatch).one()
    row = db_session.query(ImportBatchRow).one()
    assert 'password' not in batch.headers
    assert 'password' not in row.data
    credentials = batches._credential_rows(batches._parse('users.csv', content))
    receipt = await batches.commit_batch(db_session, admin_user, 1, REQUEST, batch.id, batch.version, credentials)
    assert receipt['counts'] == {'created': 1}
    assert secret not in str(row.data) + str(row.result) + str(row.error)


async def test_permissions_match_each_legacy_import(operator_user, supervisor_user):
    for entity in batches.ENTITIES:
        with pytest.raises(HTTPException):
            batches.assert_import_role(operator_user, entity)
    assert batches.visible_entities(supervisor_user) == ['parts', 'materials', 'work-orders']


async def test_purchase_order_lines_remain_one_atomic_group(db_session, admin_user, test_part):
    from app.models.purchasing import PurchaseOrder, PurchaseOrderLine, Vendor

    vendor = Vendor(company_id=1, code='IMP-V', name='Import Supplier')
    db_session.add(vendor)
    db_session.commit()
    content = f'po_number,vendor_code,part_number,quantity,unit_price\nIMP-PO,IMP-V,{test_part.part_number},2,3\nIMP-PO,IMP-V,{test_part.part_number},4,5\n'.encode()
    review = await prepare(db_session, admin_user, content, 'purchase-orders')
    assert review['counts'] == {'ready': 2}
    assert db_session.query(PurchaseOrder).count() == 0
    receipt = await batches.commit_batch(db_session, admin_user, 1, REQUEST, review['id'], review['version'])
    assert receipt['counts'] == {'created': 2}
    assert receipt['created_records'] == 1
    assert db_session.query(PurchaseOrder).one().total == 26
    assert db_session.query(PurchaseOrderLine).count() == 2
    assert len({row.result['record_id'] for row in receipt['rows']}) == 1


async def test_generated_work_order_numbers_do_not_duplicate_on_resume(db_session, admin_user):
    from app.models.part import Part
    from app.models.routing import Routing, RoutingOperation
    from app.models.work_center import WorkCenter
    from app.models.work_order import WorkOrder

    part = Part(company_id=1, part_number='IMP-GENERATED', name='Import Part', part_type='manufactured')
    center = WorkCenter(company_id=1, code='IMP-C', name='Import Center', work_center_type='machining')
    db_session.add_all([part, center])
    db_session.flush()
    routing = Routing(company_id=1, part_id=part.id, status='released', is_active=True)
    db_session.add(routing)
    db_session.flush()
    db_session.add(
        RoutingOperation(
            company_id=1,
            routing_id=routing.id,
            sequence=10,
            operation_number='10',
            name='Cut',
            work_center_id=center.id,
            run_hours_per_unit=0.1,
        )
    )
    db_session.commit()
    content = b'part_number,quantity\nIMP-GENERATED,2\n'
    review = await prepare(db_session, admin_user, content, 'work-orders')
    assert review['counts'] == {'ready': 1}
    receipt = await batches.commit_batch(db_session, admin_user, 1, REQUEST, review['id'], review['version'])
    assert receipt['counts'] == {'created': 1}
    number = db_session.query(WorkOrder).one().work_order_number
    assert (await prepare(db_session, admin_user, content, 'work-orders'))['id'] == review['id']
    await batches.commit_batch(db_session, admin_user, 1, REQUEST, review['id'], receipt['version'])
    assert db_session.query(WorkOrder).one().work_order_number == number


@pytest.mark.parametrize('fail_receipt', [False, True])
async def test_outbox_waits_for_receipt_commit_and_is_dropped_on_rollback(
    db_session, admin_user, monkeypatch, fail_receipt
):
    import app.services.notification_outbox as outbox
    from app.api.endpoints import customers
    from app.services.operational_event_service import OperationalEventService

    review = await prepare(db_session, admin_user, b'name\nOutbox Account\n')
    enqueued = []
    monkeypatch.setattr(outbox, '_enqueue_dispatch', enqueued.append)
    original_import = customers.import_customers_csv

    async def emit_during_import(**kwargs):
        result = await original_import(**kwargs)
        OperationalEventService(kwargs['db']).emit(
            company_id=1, event_type='work_order_released', source_module='test', entity_type='work_order', entity_id=42
        )
        kwargs['db'].commit()
        assert enqueued == []
        return result

    monkeypatch.setattr(customers, 'import_customers_csv', emit_during_import)
    original_audit = AuditService.log_update

    def receipt_audit(*args, **kwargs):
        assert enqueued == []
        if fail_receipt:
            raise RuntimeError('Receipt unavailable')
        return original_audit(*args, **kwargs)

    monkeypatch.setattr(AuditService, 'log_update', receipt_audit)
    if fail_receipt:
        with pytest.raises(RuntimeError, match='Receipt unavailable'):
            await batches.commit_batch(db_session, admin_user, 1, REQUEST, review['id'], review['version'])
        db_session.rollback()
        assert enqueued == []
        assert db_session.query(Customer).count() == 0
        assert db_session.query(ImportBatchRow).one().status == 'ready'
        return
    receipt = await batches.commit_batch(db_session, admin_user, 1, REQUEST, review['id'], review['version'])
    assert receipt['counts'] == {'created': 1}
    assert len(enqueued) == 1


async def test_partial_group_validation_marks_all_po_lines_correctable():
    rows = [ImportBatchRow(group_key='same-po', row_key=str(index)) for index in range(2)]
    batches._set_review(rows, {'errors': [{'row': 2, 'reason': 'Invalid quantity'}]})
    assert [row.status for row in rows] == ['invalid', 'invalid']
    assert rows[1].error


async def test_failed_po_group_exports_all_lines_and_can_be_corrected_once(db_session, admin_user, test_part):
    from app.models.purchasing import PurchaseOrder, PurchaseOrderLine, Vendor

    db_session.add(Vendor(company_id=1, code='FIX-V', name='Correction Supplier'))
    db_session.commit()
    content = f'po_number,vendor_code,part_number,quantity,unit_price\nFIX-PO,FIX-V,{test_part.part_number},bad,3\nFIX-PO,FIX-V,{test_part.part_number},4,5\n'.encode()
    review = await prepare(db_session, admin_user, content, 'purchase-orders')
    assert review['counts'] == {'invalid': 2}
    batch = batches.load_batch(db_session, 1, admin_user, review['id'])
    exported = batches.failed_rows_csv(db_session, 1, batch)
    rows = list(csv.DictReader(io.StringIO(exported)))
    assert len(rows) == 2
    assert len({row['_import_row_id'] for row in rows}) == 2
    fixed = await batches.correct_batch(
        db_session,
        admin_user,
        1,
        REQUEST,
        batch.id,
        batch.version,
        'correction.csv',
        exported.replace(',bad,', ',2,').encode(),
    )
    assert fixed['counts'] == {'ready': 2}
    done = await batches.commit_batch(db_session, admin_user, 1, REQUEST, batch.id, fixed['version'])
    assert done['created_records'] == 1
    assert db_session.query(PurchaseOrder).one().total == 26
    assert db_session.query(PurchaseOrderLine).count() == 2
    await batches.commit_batch(db_session, admin_user, 1, REQUEST, batch.id, done['version'])
    assert db_session.query(PurchaseOrder).count() == 1
