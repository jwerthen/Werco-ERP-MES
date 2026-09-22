"""One-shot private follow-ups over live evidence; never changes ERP business records."""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import case, func, or_

from app.db.locks import acquire_generator_lock
from app.db.session import SessionLocal
from app.db.tenant_filter import tenant_query
from app.models.document import Document
from app.models.hank import HankTask
from app.models.notification import Notification
from app.models.user import User
from app.models.work_order import WorkOrder
from app.models.work_order_blocker import WorkOrderBlocker
from app.schemas.hank_tasks import HankTaskPreview, HankTaskResult
from app.schemas.hank_watches import HankWatchInput
from app.services import notification_links
from app.services.audit_service import AuditService
from app.services.hank_preference_service import get_hank_preference_values
from app.services.hank_task_service import HankTaskService, _digest, _reference

logger = logging.getLogger(__name__)
ACTIVE_STATUSES = ('watching', 'snoozed')
WATCH_LIMIT = 50
BASELINE_LIMIT = 500
CHECK_INTERVAL = timedelta(minutes=5)
FAILURE_BACKOFF = timedelta(minutes=30)
CHECK_FAILED = 'WATCH_CHECK_FAILED'


def _naive_utc(value):
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _now(value=None):
    return _naive_utc(value or datetime.now(timezone.utc))


def _work_order(db, company_id, work_order_id):
    return (
        tenant_query(db, WorkOrder, company_id)
        .filter(WorkOrder.id == work_order_id, WorkOrder.is_deleted.is_(False))
        .first()
    )


def _open_blockers(db, company_id, work_order_id):
    return tenant_query(db, WorkOrderBlocker, company_id).filter(
        WorkOrderBlocker.work_order_id == work_order_id,
        WorkOrderBlocker.status.in_(('open', 'acknowledged')),
    )


def _documents(db, company_id, payload):
    query = tenant_query(db, Document, company_id).filter(
        Document.work_order_id == payload.work_order_id,
        or_(Document.mime_type == 'application/pdf', func.lower(Document.file_name).like('%.pdf')),
    )
    if payload.document_type is not None:
        query = query.filter(Document.document_type == payload.document_type)
    return query


def _transition(db, task, status, audit, now, **fields):
    """CAS the owner-bound header and required evidence in the same transaction."""
    prior_status, prior_version = task.status, task.version
    values = {'status': status, 'version': prior_version + 1, 'updated_at': now, **fields}
    count = (
        tenant_query(db, HankTask, task.company_id)
        .filter(
            HankTask.id == task.id,
            HankTask.owner_id == task.owner_id,
            HankTask.credential_key == task.credential_key,
            HankTask.version == prior_version,
            HankTask.status == prior_status,
        )
        .update(values, synchronize_session=False)
    )
    if count != 1:
        raise HTTPException(409, 'This watch changed. Refresh it before continuing.')
    audit.log_required(
        'UPDATE',
        'hank_task',
        resource_id=task.id,
        resource_identifier=task.title,
        old_values={'status': prior_status, 'version': prior_version},
        new_values={
            'status': status,
            'version': prior_version + 1,
            **{
                key: (value.isoformat() + 'Z' if isinstance(value, datetime) else value)
                for key, value in fields.items()
                if key not in ('last_checked_at',)
            },
        },
        extra_data={'source': 'hank_follow_up', 'owner_id': task.owner_id},
    )
    db.flush()
    db.refresh(task)
    return task


def _unavailable(db, task, audit, now):
    return _transition(
        db,
        task,
        'needs_attention',
        audit,
        now,
        last_checked_at=now,
        snoozed_until=None,
        error_code='WATCH_ACCESS_OR_SOURCE_UNAVAILABLE',
        error_message='Hank stopped checking because access or the source record is no longer available.',
    )


def _evaluate_locked(db, task, owner, audit, now):
    """Caller holds task row lock; notification and terminal receipt are inseparable."""
    authority = HankTaskService(db, owner, task.company_id) if owner is not None else None
    if task.credential_key != 'user' or authority is None or not authority.can_watch():
        return _unavailable(db, task, audit, now)
    payload = HankWatchInput.model_validate(task.input_json)
    work_order = _work_order(db, task.company_id, payload.work_order_id)
    if work_order is None:
        return _unavailable(db, task, audit, now)
    references = [
        _reference(
            'work_order',
            work_order.id,
            work_order.work_order_number,
            notification_links.work_order_detail(work_order.id),
        )
    ]
    receipt = None
    if payload.condition == 'blockers_cleared':
        if _open_blockers(db, task.company_id, work_order.id).first() is None:
            receipt = HankTaskResult(
                summary=f'At this check, {work_order.work_order_number} has no open or acknowledged blockers.',
                warnings=['This does not establish that the job is ready or authorized for production.'],
                references=references,
            )
    else:
        baseline = task.source_versions_json.get('document_ids', [])
        arrived = (
            _documents(db, task.company_id, payload).filter(Document.id.notin_(baseline)).order_by(Document.id).first()
        )
        if arrived is not None:
            references.insert(
                0, _reference('document', arrived.id, arrived.document_number, f'/documents?document={arrived.id}')
            )
            receipt = HankTaskResult(
                summary=f'{arrived.document_number} is now attached to {work_order.work_order_number}; '
                f'revision {arrived.revision}, status {arrived.status}.',
                warnings=[
                    'This is PDF attachment metadata; Hank has not verified the contents or approved a certificate.'
                ],
                references=references,
            )
    if receipt is None:
        # Last-check telemetry is intentionally quiet: unchanged observations do not
        # create notices, audit noise, or a new employee command version.
        task.last_checked_at = now
        if task.error_code == CHECK_FAILED:
            task.error_code = None
            task.error_message = None
            task.updated_at = now
        db.flush()
        return task
    alerts_enabled = get_hank_preference_values(db, task.company_id, task.owner_id).follow_up_alerts
    if not alerts_enabled:
        receipt.warnings.append('In-app alert was muted by your Hank preferences; the result is saved here.')
    _transition(
        db,
        task,
        'completed',
        audit,
        now,
        result_json=receipt.model_dump(mode='json'),
        completed_at=now,
        last_checked_at=now,
        snoozed_until=None,
        error_code=None,
        error_message=None,
    )
    if alerts_enabled:
        db.add(
            Notification(
                company_id=task.company_id,
                user_id=task.owner_id,
                event_key='hank.follow_up',
                severity='info',
                title='Hank: your follow-up is complete',
                body=receipt.summary,
                link=notification_links.hank_task(task.id),
                related_type='hank_task',
                related_id=task.id,
            )
        )
    db.flush()
    return task


class HankWatchService:
    def __init__(self, db, user, company_id, *, now=None):
        self.db, self.user, self.company_id = db, user, company_id
        self.now = _now(now)
        self.tasks = HankTaskService(db, user, company_id)

    def _authority(self, expected_company_id):
        self.tasks._company(expected_company_id)
        if not self.tasks.can_watch():
            raise HTTPException(403, 'Start or manage watches from an interactive account with work-order view access.')

    def prepare(self, command, audit):
        self._authority(command.expected_company_id)
        payload = HankWatchInput.model_validate(
            command.model_dump(include={'work_order_id', 'condition', 'document_type'})
        )
        request_hash = _digest(
            {
                'schema': 1,
                'company_id': self.company_id,
                'owner_id': self.user.id,
                'credential_key': 'user',
                'kind': 'watch_work_order',
                'command': command.model_dump(mode='json'),
            }
        )
        acquire_generator_lock(self.db, 'hank_request:' + command.request_key, self.company_id)
        prior = (
            tenant_query(self.db, HankTask, self.company_id).filter(HankTask.request_key == command.request_key).first()
        )
        if prior is not None:
            if (
                prior.owner_id != self.user.id
                or prior.credential_key != 'user'
                or prior.kind != 'watch_work_order'
                or prior.request_hash != request_hash
            ):
                raise HTTPException(409, 'This request key already records a different command or credential')
            return prior
        acquire_generator_lock(self.db, f'hank_owner:{self.user.id}', self.company_id)
        active = (
            self.tasks._query()
            .filter(HankTask.kind == 'watch_work_order', HankTask.status.in_(ACTIVE_STATUSES))
            .limit(WATCH_LIMIT)
            .count()
        )
        if active >= WATCH_LIMIT:
            raise HTTPException(409, 'You have 50 active watches. Cancel one before starting another.')
        work_order = _work_order(self.db, self.company_id, payload.work_order_id)
        if work_order is None:
            raise HTTPException(404, 'Work order not found')
        if payload.condition == 'blockers_cleared':
            ids = [
                row_id
                for (row_id,) in _open_blockers(self.db, self.company_id, work_order.id)
                .with_entities(WorkOrderBlocker.id)
                .order_by(WorkOrderBlocker.id)
                .limit(BASELINE_LIMIT + 1)
                .all()
            ]
            if not ids:
                raise HTTPException(409, 'This job already has no open or acknowledged blockers.')
            baseline = {'blocker_ids': ids}
            condition = 'its open or acknowledged blockers are cleared'
            warning = 'Cleared blockers do not establish that the job is ready or authorized for production.'
        else:
            ids = [
                row_id
                for (row_id,) in _documents(self.db, self.company_id, payload)
                .with_entities(Document.id)
                .order_by(Document.id)
                .limit(BASELINE_LIMIT + 1)
                .all()
            ]
            baseline = {'document_ids': ids}
            label = payload.document_type.value.replace('_', ' ') if payload.document_type else 'matching'
            condition = f'a new {label} PDF attachment appears'
            warning = 'Hank checks attachment metadata; it does not verify PDF contents or approve certificates.'
        if len(ids) > BASELINE_LIMIT:
            raise HTTPException(
                409, 'This job exceeds the 500-record watch baseline limit. Use its ERP records directly.'
            )
        preview = HankTaskPreview(
            summary=f'Watch {work_order.work_order_number} until {condition}.',
            changes=[
                'Scheduled checks run when follow-ups are enabled for the shop. Use Check now for a fresh result.',
                'Notify only you in the ERP when this condition is observed, then finish the watch.',
            ],
            warnings=[warning],
            references=[
                _reference(
                    'work_order',
                    work_order.id,
                    work_order.work_order_number,
                    notification_links.work_order_detail(work_order.id),
                )
            ],
        )
        task = HankTask(
            company_id=self.company_id,
            owner_id=self.user.id,
            credential_key='user',
            request_key=command.request_key,
            request_hash=request_hash,
            kind='watch_work_order',
            title=f'Watch {work_order.work_order_number}'[:300],
            status='watching',
            version=1,
            input_json=payload.model_dump(mode='json'),
            preview_json=preview.model_dump(mode='json'),
            source_versions_json=baseline,
            created_at=self.now,
            updated_at=self.now,
            last_checked_at=None,
        )
        self.db.add(task)
        self.db.flush()
        audit.log_required(
            'CREATE',
            'hank_task',
            resource_id=task.id,
            resource_identifier=task.title,
            new_values={
                'kind': task.kind,
                'status': task.status,
                'version': task.version,
                'input': task.input_json,
                'baseline': baseline,
            },
            extra_data={'source': 'hank_follow_up', 'owner_id': task.owner_id},
        )
        return task

    def command(self, task_id, verb, command, audit):
        self._authority(command.expected_company_id)
        acquire_generator_lock(self.db, f'hank_task:{task_id}', self.company_id)
        task = self.tasks.get(task_id, locked=True)
        if task.kind != 'watch_work_order':
            raise HTTPException(404, 'Hank watch not found')
        if task.status == 'completed' and verb == 'check':
            return task
        if task.status == 'cancelled' and verb == 'cancel':
            return task
        replay_status = {'snooze': 'snoozed', 'resume': 'watching'}.get(verb)
        if replay_status == task.status and task.version == command.expected_version + 1:
            return task
        if task.version != command.expected_version:
            raise HTTPException(409, 'This watch changed. Refresh it before continuing.')
        if verb == 'cancel' and task.status in (*ACTIVE_STATUSES, 'needs_attention'):
            return _transition(self.db, task, 'cancelled', audit, self.now, completed_at=self.now, snoozed_until=None)
        if verb == 'snooze' and task.status == 'watching':
            return _transition(self.db, task, 'snoozed', audit, self.now, snoozed_until=self.now + timedelta(hours=1))
        if verb == 'resume' and task.status in ('snoozed', 'needs_attention'):
            if task.status == 'needs_attention':
                acquire_generator_lock(self.db, f'hank_owner:{self.user.id}', self.company_id)
                active = (
                    self.tasks._query()
                    .filter(HankTask.kind == 'watch_work_order', HankTask.status.in_(ACTIVE_STATUSES))
                    .limit(WATCH_LIMIT)
                    .count()
                )
                if active >= WATCH_LIMIT:
                    raise HTTPException(409, 'You have 50 active watches. Cancel one before resuming another.')
            payload = HankWatchInput.model_validate(task.input_json)
            if _work_order(self.db, self.company_id, payload.work_order_id) is None:
                raise HTTPException(404, 'Work order not found')
            return _transition(
                self.db,
                task,
                'watching',
                audit,
                self.now,
                snoozed_until=None,
                last_checked_at=None,
                error_code=None,
                error_message=None,
            )
        if verb == 'check' and task.status == 'watching':
            return _evaluate_locked(self.db, task, self.user, audit, self.now)
        raise HTTPException(409, 'This watch cannot perform that action in its current state.')


def _retry_ready(instant):
    return or_(
        HankTask.error_code.is_(None),
        HankTask.error_code != CHECK_FAILED,
        HankTask.updated_at <= instant - FAILURE_BACKOFF,
    )


def _record_failed_check(task_id, version, status, last_checked_at, instant):
    """Store failed-attempt telemetry after rollback, never a successful check or transition.

    Re-lock and compare the failed snapshot so a concurrent manual completion,
    cancellation, snooze or successful check always wins over this diagnostic.
    """
    db = SessionLocal()
    try:
        task = (
            db.query(HankTask)
            .filter(
                HankTask.id == task_id,
                HankTask.kind == 'watch_work_order',
                HankTask.status == status,
                HankTask.status.in_(ACTIVE_STATUSES),
                HankTask.version == version,
                HankTask.last_checked_at == last_checked_at,
            )
            .with_for_update(skip_locked=True)
            .first()
        )
        if task is not None:
            task.error_code = CHECK_FAILED
            task.error_message = 'The last scheduled check failed. Hank will retry; you can also use Check now.'
            task.updated_at = instant
            db.commit()
    except Exception:
        db.rollback()
        logger.exception('Unable to record failed-check telemetry for Hank task %s', task_id)
    finally:
        db.close()


def _due_query(db, instant):
    return db.query(HankTask).filter(
        HankTask.kind == 'watch_work_order',
        HankTask.status.in_(ACTIVE_STATUSES),
        _retry_ready(instant),
        or_(HankTask.snoozed_until.is_(None), HankTask.snoozed_until <= instant),
        or_(HankTask.last_checked_at.is_(None), HankTask.last_checked_at <= instant - CHECK_INTERVAL),
    )


def _claim_query(db, task_id, instant):
    return _due_query(db, instant).filter(HankTask.id == task_id).with_for_update(skip_locked=True)


def process_hank_watches(*, limit=100, now=None):
    """Bounded worker pass; each claim revalidates authority and commits independently."""
    limit = min(max(int(limit), 1), 100)
    instant = _now(now)
    db = SessionLocal()
    try:
        candidates = (
            _due_query(db, instant)
            .with_entities(HankTask.id)
            .order_by(
                case(
                    (HankTask.error_code == CHECK_FAILED, HankTask.updated_at),
                    else_=func.coalesce(HankTask.last_checked_at, HankTask.created_at),
                ),
                HankTask.id,
            )
            .limit(limit)
            .all()
        )
        candidate_ids = [row_id for (row_id,) in candidates]
    finally:
        db.close()
    result = {'checked': 0, 'completed': 0, 'needs_attention': 0, 'failed': 0}
    for task_id in candidate_ids:
        db = SessionLocal()
        failed_snapshot = None
        try:
            task = _claim_query(db, task_id, instant).first()
            if task is None:
                continue
            failed_snapshot = (task.version, task.status, task.last_checked_at)
            owner = db.query(User).filter(User.id == task.owner_id).first()
            audit = AuditService(db, company_id=task.company_id)
            if (
                task.status == 'snoozed'
                and owner is not None
                and task.credential_key == 'user'
                and HankTaskService(db, owner, task.company_id).can_watch()
            ):
                # Expiration resumes the same explicit watch and original baseline.
                _transition(db, task, 'watching', audit, instant, snoozed_until=None)
            _evaluate_locked(db, task, owner, audit, instant)
            db.commit()
            result['checked'] += 1
            if task.status in ('completed', 'needs_attention'):
                result[task.status] += 1
        except Exception:
            db.rollback()
            result['failed'] += 1
            if failed_snapshot is not None:
                _record_failed_check(task_id, *failed_snapshot, instant)
            logger.exception('Hank watch check failed for task %s; it remains eligible for recovery', task_id)
        finally:
            db.close()
    return result
