"""Reviewed imports with atomic per-record receipts and resumable input identities.

Legacy importers own all domain validation. A savepoint-joined Session lets their
commits release only a savepoint; the outer transaction commits business data,
audit/outbox records and the import receipt together. No business row is inferred
from a similar name after a timeout. A retry reads the durable row receipt.
"""

import csv
import hashlib
import io
import json
from collections import defaultdict
from datetime import datetime
from uuid import uuid4

from fastapi import HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import event, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import require_role
from app.db.tenant_filter import tenant_query
from app.models.import_batch import ImportBatch, ImportBatchRow
from app.models.user import UserRole
from app.services.audit_service import AuditService
from app.services.export_safety import sanitize_csv_row
from app.services.import_service import ImportFileError, parse_import_file

ENTITIES = ('users', 'parts', 'materials', 'customers', 'vendors', 'work-centers', 'work-orders', 'purchase-orders')
SENSITIVE_COLUMNS = {'password', 'default_password', 'hashed_password'}
ROW_PAGE = 200
COMMIT_GROUP_LIMIT = 25


def assert_import_role(user, entity):
    if entity not in ENTITIES:
        raise HTTPException(422, 'Unknown import entity')
    roles = [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]
    if entity == 'users':
        roles = [UserRole.ADMIN]
    elif entity in ('customers', 'vendors', 'work-centers', 'purchase-orders'):
        roles = [UserRole.ADMIN, UserRole.MANAGER]
    require_role(roles)(user)


def visible_entities(user):
    entities = []
    for entity in ENTITIES:
        try:
            assert_import_role(user, entity)
            entities.append(entity)
        except HTTPException:
            pass
    return entities


def _parse(filename, content):
    try:
        return parse_import_file(filename, content)
    except ImportFileError as exc:
        raise HTTPException(400, str(exc)) from exc


def _clean(data):
    return {
        key: value for key, value in data.items() if key not in SENSITIVE_COLUMNS and not key.startswith('_import_')
    }


def _encode(headers, rows):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=headers, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode('utf-8')


def _connection(db):
    connection = db.connection()
    # sqlite3's legacy transaction mode does not BEGIN on SELECT; a released
    # SAVEPOINT would otherwise become a real commit outside the parent.
    if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN')
    return connection


async def _invoke(db, user, company_id, request, entity, headers, rows, dry_run, default_password=None):
    from app.api.endpoints import customers, materials, parts, purchasing, users, work_centers, work_orders

    handlers = {
        'users': users.import_users_csv,
        'parts': parts.import_parts_csv,
        'materials': materials.import_materials_csv,
        'customers': customers.import_customers_csv,
        'vendors': purchasing.import_vendors_csv,
        'work-centers': work_centers.import_work_centers_csv,
        'work-orders': work_orders.import_open_work_orders_endpoint,
        'purchase-orders': purchasing.import_open_purchase_orders_endpoint,
    }
    pending_events = []
    with Session(bind=_connection(db), join_transaction_mode='create_savepoint') as inner:
        # The global outbox listener runs after every Session commit. Move its
        # pending IDs before it runs; only the enclosing durable commit dispatches.
        def forward_outbox(session):
            pending = session.info.pop('pending_notification_event_ids', [])
            if pending:
                pending_events.extend(pending)

        event.listen(inner, 'before_commit', forward_outbox)
        kwargs = dict(
            file=UploadFile(filename='review.csv', file=io.BytesIO(_encode(headers, rows))),
            dry_run=dry_run,
            db=inner,
            current_user=user,
            company_id=company_id,
        )
        if entity in ('work-orders', 'purchase-orders'):
            kwargs['audit'] = AuditService(inner, user, request)
        else:
            kwargs['request'] = request
        if entity == 'users':
            kwargs['default_password'] = default_password
        result = await handlers[entity](**kwargs)
        outcome = result.model_dump(mode='json') if hasattr(result, 'model_dump') else result
        outcome['_pending_events'] = pending_events
        return outcome


def load_batch(db, company_id, user, batch_id, lock=False):
    query = tenant_query(db, ImportBatch, company_id).filter(ImportBatch.id == batch_id)
    if lock:
        query = query.populate_existing().with_for_update(of=ImportBatch)
    batch = query.first()
    if not batch:
        raise HTTPException(404, 'Import batch not found')
    assert_import_role(user, batch.entity)
    return batch


def batch_response(db, company_id, user, batch, row_offset=0, include_rows=True):
    query = tenant_query(db, ImportBatchRow, company_id).filter(ImportBatchRow.batch_id == batch.id)
    counts = dict(query.with_entities(ImportBatchRow.status, func.count()).group_by(ImportBatchRow.status).all())
    rows = query.order_by(ImportBatchRow.source_row).offset(row_offset).limit(ROW_PAGE).all() if include_rows else []
    created_records = (
        query.filter(ImportBatchRow.status == 'created').with_entities(ImportBatchRow.group_key).distinct().count()
    )
    return dict(
        id=batch.id,
        entity=batch.entity,
        filename=batch.filename,
        version=batch.version,
        created_at=batch.created_at,
        updated_at=batch.updated_at,
        total_rows=sum(counts.values()),
        counts=counts,
        created_records=created_records,
        rows=rows,
        row_offset=row_offset,
        has_more_rows=row_offset + len(rows) < sum(counts.values()),
        requires_credentials=batch.entity == 'users',
    )


def _set_review(rows, outcome):
    errors = {error['row']: error['reason'] for error in outcome.get('errors', [])}
    previews = {}
    for result in outcome.get('results', []):
        for row_number in result.get('rows', [result.get('row')]):
            previews[row_number] = result
    for index, row in enumerate(rows, 2):
        row.status = 'invalid' if index in errors else 'ready'
        row.error = errors.get(index)
        row.result = previews.get(index)
    failed_groups = {row.group_key for row in rows if row.status == 'invalid'}
    for row in rows:
        if row.group_key in failed_groups and row.status == 'ready':
            row.status = 'invalid'
            row.error = 'Another line in this purchase order failed validation. Correct every line together.'


async def prepare_batch(db, user, company_id, request, entity, filename, content, request_key, default_password=None):
    assert_import_role(user, entity)
    table = await run_in_threadpool(_parse, filename, content)
    if not table.rows:
        raise HTTPException(422, 'The file has no import rows')
    clean_rows = [_clean(row) for _, row in table.rows]
    headers = [
        header for header in table.headers if header not in SENSITIVE_COLUMNS and not header.startswith('_import_')
    ]
    source_hash = hashlib.sha256(json.dumps(clean_rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    same_key = tenant_query(db, ImportBatch, company_id).filter(ImportBatch.request_key == request_key).first()
    if same_key and (same_key.entity != entity or same_key.source_hash != source_hash):
        raise HTTPException(409, 'This import request key was already used for a different file')
    existing = (
        same_key
        or tenant_query(db, ImportBatch, company_id)
        .filter(ImportBatch.entity == entity, ImportBatch.source_hash == source_hash)
        .first()
    )
    if existing:
        return batch_response(db, company_id, user, existing)
    # Re-encode once, so validation row numbers match ledger order consistently
    # even when the source workbook contains blank/guidance rows.
    outcome = await _invoke(
        db, user, company_id, request, entity, table.headers, [row for _, row in table.rows], True, default_password
    )
    batch = ImportBatch(
        company_id=company_id,
        entity=entity,
        filename=(filename or 'import.csv')[:255],
        source_hash=source_hash,
        request_key=request_key,
        headers=headers,
        created_by=user.id,
        version=1,
    )
    db.add(batch)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = (
            tenant_query(db, ImportBatch, company_id)
            .filter(ImportBatch.entity == entity, ImportBatch.source_hash == source_hash)
            .first()
        )
        if existing:
            return batch_response(db, company_id, user, existing)
        raise HTTPException(409, 'Import request was used concurrently; reload batch history')
    group_keys = {}
    records = []
    for (number, _), data in zip(table.rows, clean_rows):
        group = (data.get('po_number') or '').upper() if entity == 'purchase-orders' else ''
        group_key = group_keys.setdefault(group, str(uuid4())) if group else str(uuid4())
        row = ImportBatchRow(
            company_id=company_id,
            batch_id=batch.id,
            row_key=str(uuid4()),
            group_key=group_key,
            source_row=number,
            data=data,
            status='ready',
        )
        db.add(row)
        records.append(row)
    _set_review(records, outcome)
    AuditService(db, user, request).log_create(
        'import_batch',
        batch.id,
        batch.filename,
        extra_data={'entity': entity, 'rows': len(records), 'source_hash': source_hash},
    )
    db.commit()
    return batch_response(db, company_id, user, batch)


def _check_version(batch, version):
    if batch.version != version:
        raise HTTPException(409, 'Batch changed. Reload its receipt before continuing.')


def _credential_rows(table):
    return {row.get('_import_row_id') or str(number): row for number, row in table.rows} if table else {}


def _inputs(rows, entity, credentials):
    result = []
    for row in rows:
        data = dict(row.data)
        if entity == 'users':
            supplied = credentials.get(row.row_key) or credentials.get(str(row.source_row))
            if supplied:
                if _clean(supplied) != row.data:
                    raise HTTPException(409, 'Employee file differs from this reviewed batch; upload corrections first')
                if supplied.get('password'):
                    data['password'] = supplied['password']
        result.append(data)
    return result


async def commit_batch(
    db, user, company_id, request, batch_id, expected_version, credentials=None, default_password=None
):
    batch = load_batch(db, company_id, user, batch_id, lock=True)
    _check_version(batch, expected_version)
    groups = (
        tenant_query(db, ImportBatchRow, company_id)
        .filter(ImportBatchRow.batch_id == batch_id, ImportBatchRow.status == 'ready')
        .with_entities(ImportBatchRow.group_key, func.min(ImportBatchRow.source_row))
        .group_by(ImportBatchRow.group_key)
        .order_by(func.min(ImportBatchRow.source_row))
        .limit(COMMIT_GROUP_LIMIT)
        .all()
    )
    for group_key, _ in groups:
        batch = load_batch(db, company_id, user, batch_id, lock=True)
        _check_version(batch, expected_version)
        rows = (
            tenant_query(db, ImportBatchRow, company_id)
            .filter(ImportBatchRow.batch_id == batch_id, ImportBatchRow.group_key == group_key)
            .order_by(ImportBatchRow.source_row)
            .populate_existing()
            .with_for_update(of=ImportBatchRow)
            .all()
        )
        if not all(row.status == 'ready' for row in rows):
            continue
        payload = _inputs(rows, batch.entity, credentials or {})
        headers = list(batch.headers) + (['password'] if batch.entity == 'users' else [])
        _connection(db)
        unit = db.begin_nested()
        try:
            outcome = await _invoke(
                db, user, company_id, request, batch.entity, headers, payload, False, default_password
            )
            ids = outcome.get('created_ids', [])
            if outcome.get('errors') or len(ids) != 1:
                unit.rollback()
                errors = outcome.get('errors', [])
                reason = (
                    errors[0]['reason'] if errors else 'No record was created. Correct and validate this row again.'
                )
                for row in rows:
                    row.status, row.error = 'failed', reason
            else:
                result = (outcome.get('results') or [{}])[0]
                result = {**result, 'record_id': ids[0], 'entity': batch.entity}
                for row in rows:
                    row.status, row.error, row.result = 'created', None, result
                unit.commit()
                db.info.setdefault('pending_notification_event_ids', []).extend(outcome.get('_pending_events', []))
        except Exception:
            if unit.is_active:
                unit.rollback()
            raise
        now = datetime.utcnow()
        for row in rows:
            row.updated_at = now
        batch.version += 1
        batch.updated_at = now
        AuditService(db, user, request).log_update(
            'import_batch',
            batch.id,
            batch.filename,
            old_values={'version': expected_version},
            new_values={'version': batch.version},
            extra_data={'row_keys': [row.row_key for row in rows], 'status': rows[0].status},
        )
        expected_version = batch.version
        # If this commit's response is lost, record and receipt still agree.
        # Do not turn a commit exception into a guessed failure or resend.
        db.commit()
    return batch_response(db, company_id, user, batch)


def failed_rows_csv(db, company_id, batch):
    rows = (
        tenant_query(db, ImportBatchRow, company_id)
        .filter(ImportBatchRow.batch_id == batch.id, ImportBatchRow.status.in_(['invalid', 'failed']))
        .order_by(ImportBatchRow.source_row)
        .all()
    )
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(sanitize_csv_row(['_import_row_id', *batch.headers, '_import_error']))
    for row in rows:
        writer.writerow(
            sanitize_csv_row([row.row_key, *[row.data.get(key, '') for key in batch.headers], row.error or ''])
        )
    return output.getvalue()


async def correct_batch(
    db, user, company_id, request, batch_id, expected_version, filename, content, default_password=None
):
    table = await run_in_threadpool(_parse, filename, content)
    batch = load_batch(db, company_id, user, batch_id, lock=True)
    _check_version(batch, expected_version)
    if '_import_row_id' not in table.headers:
        raise HTTPException(422, 'Use the failed-row CSV; keep its _import_row_id column unchanged')
    rows = tenant_query(db, ImportBatchRow, company_id).filter(ImportBatchRow.batch_id == batch.id).all()
    lookup = {row.row_key: row for row in rows}
    changes = {}
    for _, data in table.rows:
        key = data.get('_import_row_id')
        row = lookup.get(key)
        if not row or key in changes or row.status not in ('invalid', 'failed'):
            raise HTTPException(
                409, 'Corrections must contain distinct failed rows from this batch; created rows cannot change'
            )
        updated = {header: data.get(header, '') for header in batch.headers}
        for header, value in updated.items():
            original = row.data.get(header, '')
            if value == sanitize_csv_row([original])[0]:
                updated[header] = original
        changes[key] = updated
    if not changes:
        raise HTTPException(422, 'No failed rows were supplied')
    groups = defaultdict(list)
    for row in rows:
        if row.row_key in changes:
            groups[row.group_key].append(row)
    for key, group in groups.items():
        if any(row.group_key == key and row.row_key not in changes for row in rows):
            raise HTTPException(422, 'Correct every line of the failed purchase order together')
        before = {row.row_key: row.data for row in group}
        for row in group:
            row.data = changes[row.row_key]
        inputs = _inputs(group, batch.entity, _credential_rows(table))
        outcome = await _invoke(
            db,
            user,
            company_id,
            request,
            batch.entity,
            list(batch.headers) + (['password'] if batch.entity == 'users' else []),
            inputs,
            True,
            default_password,
        )
        _set_review(group, outcome)
        AuditService(db, user, request).log_update(
            'import_batch',
            batch.id,
            batch.filename,
            old_values=before,
            new_values={row.row_key: row.data for row in group},
            extra_data={'action': 'correct_failed_rows', 'row_keys': [row.row_key for row in group]},
        )
    batch.version += 1
    batch.updated_at = datetime.utcnow()
    db.commit()
    return batch_response(db, company_id, user, batch)
