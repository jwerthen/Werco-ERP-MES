"""Participant-scoped handoffs; ordered, approved procedures with retained evidence."""

import hashlib
import io
import os
import warnings
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from PIL import Image
from sqlalchemy import or_

from app.db.locks import acquire_generator_lock
from app.db.tenant_filter import tenant_query
from app.models.document import Document
from app.models.hank_teamwork import HankHandoff, HankRoutine, HankRoutineRun
from app.models.notification import Notification
from app.models.purchasing import PurchaseOrder
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder
from app.schemas.hank_teamwork import (
    HandoffContent,
    HandoffList,
    HandoffResponse,
    RoutineResponse,
    RoutineRunResponse,
)
from app.services.hank_task_service import HankTaskService, _digest, _reference
from app.services.notification_links import hank_handoff
from app.services.storage_service import delete_ref, get_storage, read_ref_bytes, resolve_upload_dir


def now():
    return datetime.now(timezone.utc)


def person_name(user):
    return f'{user.first_name} {user.last_name}'.strip()


class HankTeamworkService:
    def __init__(self, db, user, company_id):
        self.db, self.user, self.company_id = db, user, company_id

    def _interactive(self, write=False):
        return HankTaskService(self.db, self.user, self.company_id).can_watch(write=write)

    def _require(self, write=False):
        if not self._interactive(write):
            raise HTTPException(403, 'Use an interactive account with work-order access in this company.')

    def _company(self, expected):
        if expected != self.company_id:
            raise HTTPException(409, 'Active company changed. Reopen Hank in the intended company.')

    def _job(self, job_id, *, live=False):
        query = tenant_query(self.db, WorkOrder, self.company_id).filter(WorkOrder.id == job_id)
        if live:
            query = query.filter(WorkOrder.is_deleted.is_(False))
        row = query.first()
        if not row:
            raise HTTPException(404, 'Work order not found')
        return row

    def _audit(self, audit, action, resource, row, old=None, new=None):
        audit.log_required(
            action,
            resource,
            resource_id=row.id,
            resource_identifier=f'{resource} {row.id}',
            old_values=jsonable_encoder(old),
            new_values=jsonable_encoder(new),
            extra_data={'source': 'hank_teamwork'},
        )
        self.db.flush()

    def _notice(self, row, recipient_id, title, body):
        recipient = (
            self.db.query(User)
            .filter(
                User.id == recipient_id,
                User.company_id == self.company_id,
                User.is_active.is_(True),
            )
            .first()
        )
        if recipient and 'work_orders:view' in HankTaskService(self.db, recipient, self.company_id)._permissions():
            self.db.add(
                Notification(
                    company_id=self.company_id,
                    user_id=recipient_id,
                    event_key='hank.handoff',
                    severity='info',
                    title=title,
                    body=body,
                    link=hank_handoff(row.id),
                    related_type='hank_handoff',
                    related_id=row.id,
                )
            )

    def people(self, query=''):
        self._require()
        rows = self.db.query(User).filter(
            User.company_id == self.company_id,
            User.is_active.is_(True),
            User.id != self.user.id,
        )
        if query:
            query = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            rows = rows.filter(
                or_(User.first_name.ilike(f'%{query}%', escape='\\'), User.last_name.ilike(f'%{query}%', escape='\\'))
            )
        selected = rows.order_by(User.last_name, User.first_name, User.id).limit(101).all()
        return {
            'people': [
                {'id': row.id, 'name': person_name(row), 'role': row.role.value}
                for row in selected[:100]
                if 'work_orders:view' in HankTaskService(self.db, row, self.company_id)._permissions()
            ],
            'truncated': len(selected) > 100,
        }

    def _handoffs(self):
        return tenant_query(self.db, HankHandoff, self.company_id).filter(
            or_(HankHandoff.sender_id == self.user.id, HankHandoff.recipient_id == self.user.id)
        )

    def get_handoff(self, handoff_id, *, locked=False):
        self._require()
        query = self._handoffs().filter(HankHandoff.id == handoff_id)
        if locked:
            query = query.with_for_update().populate_existing()
        row = query.first()
        if not row:
            raise HTTPException(404, 'Handoff not found')
        self._job(row.work_order_id)
        return row

    def handoff_response(self, row):
        content = HandoffContent.model_validate(row.content_json)
        docs = tenant_query(self.db, Document, self.company_id).filter(Document.id.in_(content.document_ids)).all()
        editable = self._interactive(write=True) and not self._job(row.work_order_id).is_deleted
        return HandoffResponse(
            id=row.id,
            company_id=row.company_id,
            version=row.version,
            status=row.status,
            work_order_id=row.work_order_id,
            work_order_number=row.work_order_number,
            sender={'id': row.sender_id, 'name': row.sender_name},
            recipient={'id': row.recipient_id, 'name': row.recipient_name},
            **content.model_dump(),
            attachments=[
                {
                    'id': item['id'],
                    'filename': item['filename'],
                    'mime_type': item['mime_type'],
                    'url': f'/api/v1/hank/handoffs/{row.id}/attachments/{item["id"]}',
                }
                for item in row.attachments_json
            ],
            document_references=[
                _reference(
                    'document', doc.id, f'{doc.document_number} rev {doc.revision}', f'/documents?document={doc.id}'
                )
                for doc in docs
            ],
            created_at=row.created_at,
            updated_at=row.updated_at,
            acknowledged_at=row.acknowledged_at,
            completed_at=row.completed_at,
            can_acknowledge=editable and self.user.id == row.recipient_id and row.status == 'open',
            can_complete=editable and self.user.id == row.recipient_id and row.status == 'acknowledged',
            can_cancel=editable and self.user.id == row.sender_id and row.status in ('open', 'acknowledged'),
        )

    def list_handoffs(self, direction='all', status=None, limit=20, before_id=None):
        self._require()
        query = self._handoffs()
        if direction == 'sent':
            query = query.filter(HankHandoff.sender_id == self.user.id)
        elif direction == 'received':
            query = query.filter(HankHandoff.recipient_id == self.user.id)
        if status:
            query = query.filter(HankHandoff.status == status)
        if before_id:
            query = query.filter(HankHandoff.id < before_id)
        rows = query.order_by(HankHandoff.id.desc()).limit(limit + 1).all()
        return HandoffList(
            handoffs=[self.handoff_response(row) for row in rows[:limit]],
            has_more=len(rows) > limit,
            next_before_id=rows[limit - 1].id if len(rows) > limit else None,
        )

    def create_handoff(self, command, audit):
        self._company(command.expected_company_id)
        self._require(write=True)
        digest = _digest(command.model_dump(mode='json'))
        acquire_generator_lock(self.db, f'hank_handoff:{command.request_key}', self.company_id)
        old = (
            tenant_query(self.db, HankHandoff, self.company_id)
            .filter(HankHandoff.request_key == command.request_key)
            .first()
        )
        if old:
            if old.sender_id != self.user.id or old.request_hash != digest:
                raise HTTPException(409, 'This request key belongs to another handoff.')
            return self.handoff_response(old)
        job = self._job(command.work_order_id, live=True)
        recipient = (
            self.db.query(User)
            .filter(User.id == command.recipient_id, User.company_id == self.company_id, User.is_active.is_(True))
            .first()
        )
        if (
            not recipient
            or recipient.id == self.user.id
            or 'work_orders:view' not in HankTaskService(self.db, recipient, self.company_id)._permissions()
        ):
            raise HTTPException(422, 'Choose another active employee with work-order access in this company.')
        docs = tenant_query(self.db, Document, self.company_id).filter(Document.id.in_(command.document_ids)).all()
        if len(docs) != len(command.document_ids) or any(
            (doc.work_order_id and doc.work_order_id != job.id) or (doc.part_id and doc.part_id != job.part_id)
            for doc in docs
        ):
            raise HTTPException(422, 'Choose documents for this job or its part.')
        content = HandoffContent.model_validate(command.model_dump(include=set(HandoffContent.model_fields)))
        row = HankHandoff(
            company_id=self.company_id,
            sender_id=self.user.id,
            recipient_id=recipient.id,
            sender_name=person_name(self.user),
            recipient_name=person_name(recipient),
            work_order_id=job.id,
            work_order_number=job.work_order_number,
            request_key=command.request_key,
            request_hash=digest,
            content_json=content.model_dump(mode='json'),
        )
        self.db.add(row)
        self.db.flush()
        self._audit(
            audit,
            'CREATE',
            'hank_handoff',
            row,
            new={
                'sender_id': row.sender_id,
                'recipient_id': row.recipient_id,
                'work_order_id': row.work_order_id,
                'status': row.status,
                'content': row.content_json,
            },
        )
        self._notice(
            row,
            recipient.id,
            f'Handoff for {job.work_order_number}',
            f'{row.sender_name} shared a handoff. Open it to review and acknowledge.',
        )
        return self.handoff_response(row)

    def _handoff_cas(self, row, command, changes, audit):
        if row.version != command.expected_version:
            raise HTTPException(409, 'This handoff changed. Reload it before continuing.')
        old = {'version': row.version, 'status': row.status}
        changes = {**changes, 'version': row.version + 1, 'updated_at': now()}
        count = (
            self._handoffs()
            .filter(HankHandoff.id == row.id, HankHandoff.version == row.version)
            .update(changes, synchronize_session=False)
        )
        if count != 1:
            raise HTTPException(409, 'This handoff changed. Reload it before continuing.')
        self.db.refresh(row)
        self._audit(audit, 'UPDATE', 'hank_handoff', row, old=old, new=changes)

    def transition_handoff(self, handoff_id, command, action, audit):
        self._company(command.expected_company_id)
        self._require(write=True)
        row = self.get_handoff(handoff_id, locked=True)
        self._job(row.work_order_id, live=True)
        target = {'acknowledge': 'acknowledged', 'complete': 'completed', 'cancel': 'cancelled'}[action]
        authorized = self.user.id == (row.sender_id if action == 'cancel' else row.recipient_id)
        if not authorized:
            raise HTTPException(
                403, 'Only the assigned recipient can acknowledge or finish; only the sender can cancel.'
            )
        if row.status == target:
            return self.handoff_response(row)
        allowed = (
            ('open', 'acknowledged')
            if action == 'cancel'
            else (('open',) if action == 'acknowledge' else ('acknowledged',))
        )
        if row.status not in allowed:
            raise HTTPException(409, 'This handoff cannot make that transition. Reload its current state.')
        changes = {'status': target}
        if action == 'acknowledge':
            changes['acknowledged_at'] = now()
        elif action == 'complete':
            changes['completed_at'] = now()
        self._handoff_cas(row, command, changes, audit)
        other = row.recipient_id if action == 'cancel' else row.sender_id
        self._notice(
            row,
            other,
            f'Handoff {target}: {row.work_order_number}',
            f'{person_name(self.user)} marked this handoff {target}.',
        )
        return self.handoff_response(row)

    def add_attachment(self, handoff_id, command, request_key, filename, content, audit):
        self._company(command.expected_company_id)
        self._require(write=True)
        request_key = str(UUID(request_key))
        if not content or len(content) > 10 * 1024 * 1024:
            raise HTTPException(422, 'Choose a PNG or JPEG photo up to 10 MB.')
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(content)) as photo:
                    if photo.format not in ('PNG', 'JPEG') or photo.width * photo.height > 20_000_000:
                        raise ValueError('unsupported photo')
                    image_format = photo.format
                    photo.verify()
        except Exception as exc:
            raise HTTPException(422, 'Choose a valid PNG or JPEG photo up to 20 megapixels.') from exc
        digest = hashlib.sha256(content).hexdigest()
        row = self.get_handoff(handoff_id)
        existing = next((item for item in row.attachments_json if item['id'] == request_key), None)
        if existing:
            if existing['sha256'] != digest or existing['uploaded_by'] != self.user.id:
                raise HTTPException(409, 'This upload key belongs to another photo.')
            return self.handoff_response(row)
        self._job(row.work_order_id, live=True)
        if row.status not in ('open', 'acknowledged') or len(row.attachments_json) >= 10:
            raise HTTPException(409, 'Photos can be added to an active handoff, up to ten per handoff.')
        if row.version != command.expected_version:
            raise HTTPException(409, 'This handoff changed. Reload before adding a photo.')
        # Release all read transactions during blob I/O. Never delete bytes after
        # an uncertain commit; request-key replay recovers an already saved photo.
        self.db.rollback()
        extension = '.png' if image_format == 'PNG' else '.jpg'
        storage = get_storage()
        key = f'{self.company_id}/hank-handoffs/{uuid4()}{extension}'
        if not storage.is_remote:
            key = os.path.join(resolve_upload_dir(), key)
        stored = storage.save(content, key=key)
        try:
            self._require(write=True)
            row = self.get_handoff(handoff_id, locked=True)
            self._job(row.work_order_id, live=True)
            existing = next((item for item in row.attachments_json if item['id'] == request_key), None)
            if existing:
                if existing['sha256'] != digest or existing['uploaded_by'] != self.user.id:
                    raise HTTPException(409, 'This upload key belongs to another photo.')
                response = self.handoff_response(row)
                self.db.rollback()
                delete_ref(stored)
                return response
            if row.status not in ('open', 'acknowledged') or len(row.attachments_json) >= 10:
                raise HTTPException(409, 'Reload this handoff before adding more photos.')
            item = {
                'id': request_key,
                'filename': os.path.basename(filename or f'photo{extension}')[:255],
                'mime_type': 'image/png' if image_format == 'PNG' else 'image/jpeg',
                'storage_ref': stored,
                'sha256': digest,
                'uploaded_by': self.user.id,
                'created_at': now().isoformat(),
            }
            self._handoff_cas(row, command, {'attachments_json': [*row.attachments_json, item]}, audit)
            response = self.handoff_response(row)
        except Exception:
            self.db.rollback()
            delete_ref(stored)
            raise
        try:
            self.db.commit()
        except Exception as exc:
            self.db.rollback()
            raise HTTPException(503, 'Photo save outcome is unknown. Reload the handoff before retrying.') from exc
        return response

    def attachment(self, handoff_id, attachment_id):
        row = self.get_handoff(handoff_id)
        item = next((item for item in row.attachments_json if item['id'] == attachment_id), None)
        if not item:
            raise HTTPException(404, 'Handoff photo not found')
        self.db.rollback()
        try:
            content = read_ref_bytes(item['storage_ref'])
        except Exception as exc:
            raise HTTPException(503, 'Handoff photo is temporarily unavailable') from exc
        if hashlib.sha256(content).hexdigest() != item['sha256']:
            raise HTTPException(409, 'Photo verification failed. The stored evidence was not returned.')
        return content, item['mime_type']

    def can_manage_routines(self):
        return self._interactive(write=True) and (
            self.user.is_superuser
            or self.user.role in (UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.PLATFORM_ADMIN)
        )

    def can_approve_routines(self):
        return self._interactive(write=True) and (
            self.user.is_superuser or self.user.role in (UserRole.ADMIN, UserRole.MANAGER, UserRole.PLATFORM_ADMIN)
        )

    def _manage(self, approve=False):
        if not (self.can_approve_routines() if approve else self.can_manage_routines()):
            raise HTTPException(403, 'Routine approval requires Admin/Manager; editing also permits Supervisor.')

    def routine_response(self, row):
        return RoutineResponse(
            id=row.id,
            company_id=row.company_id,
            version=row.version,
            status=row.status,
            title=row.title,
            description=row.description,
            steps=row.steps_json,
            created_by=row.created_by,
            approved_by=row.approved_by,
            approved_at=row.approved_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
            can_manage=self.can_manage_routines(),
            can_approve=self.can_approve_routines(),
        )

    def get_routine(self, routine_id, locked=False):
        self._require()
        query = tenant_query(self.db, HankRoutine, self.company_id).filter(HankRoutine.id == routine_id)
        if locked:
            query = query.with_for_update().populate_existing()
        row = query.first()
        if not row or (row.status != 'approved' and not self.can_manage_routines()):
            raise HTTPException(404, 'Routine not found')
        return row

    def list_routines(self):
        self._require()
        query = tenant_query(self.db, HankRoutine, self.company_id)
        if not self.can_manage_routines():
            query = query.filter(HankRoutine.status == 'approved')
        rows = query.order_by(HankRoutine.id.desc()).limit(101).all()
        return {
            'routines': [self.routine_response(row) for row in rows[:100]],
            'templates': ROUTINE_TEMPLATES,
            'can_manage': self.can_manage_routines(),
            'can_approve': self.can_approve_routines(),
            'truncated': len(rows) > 100,
        }

    def create_routine(self, command, audit):
        self._company(command.expected_company_id)
        self._manage()
        acquire_generator_lock(self.db, f'hank_routine:{command.request_key}', self.company_id)
        digest = _digest(command.model_dump(mode='json'))
        row = (
            tenant_query(self.db, HankRoutine, self.company_id)
            .filter(HankRoutine.request_key == command.request_key)
            .first()
        )
        if row:
            if row.created_by != self.user.id or row.request_hash != digest:
                raise HTTPException(409, 'This request key belongs to another routine.')
            return self.routine_response(row)
        row = HankRoutine(
            company_id=self.company_id,
            created_by=self.user.id,
            request_key=command.request_key,
            request_hash=digest,
            title=command.title,
            description=command.description,
            steps_json=[step.model_dump() for step in command.steps],
        )
        self.db.add(row)
        self.db.flush()
        self._audit(audit, 'CREATE', 'hank_routine', row, new=self.routine_response(row).model_dump(mode='json'))
        return self.routine_response(row)

    def _routine_cas(self, row, command, changes, audit):
        if row.version != command.expected_version:
            raise HTTPException(409, 'The routine changed. Reload and review its current revision.')
        old = self.routine_response(row).model_dump(mode='json')
        changed = (
            tenant_query(self.db, HankRoutine, self.company_id)
            .filter(HankRoutine.id == row.id, HankRoutine.version == command.expected_version)
            .update({**changes, 'version': row.version + 1, 'updated_at': now()}, synchronize_session=False)
        )
        if changed != 1:
            raise HTTPException(409, 'The routine changed. Reload and review its current revision.')
        self.db.refresh(row)
        self._audit(
            audit, 'UPDATE', 'hank_routine', row, old=old, new=self.routine_response(row).model_dump(mode='json')
        )
        return self.routine_response(row)

    def update_routine(self, routine_id, command, audit):
        self._company(command.expected_company_id)
        self._manage()
        row = self.get_routine(routine_id, locked=True)
        if row.status == 'archived':
            raise HTTPException(409, 'Archived procedures are retained. Create a new routine to replace one.')
        return self._routine_cas(
            row,
            command,
            {
                'title': command.title,
                'description': command.description,
                'steps_json': [step.model_dump() for step in command.steps],
                'status': 'draft',
                'approved_by': None,
                'approved_at': None,
            },
            audit,
        )

    def transition_routine(self, routine_id, command, action, audit):
        self._company(command.expected_company_id)
        self._manage(approve=action == 'approve')
        row = self.get_routine(routine_id, locked=True)
        target = 'approved' if action == 'approve' else 'archived'
        if row.status == target:
            return self.routine_response(row)
        if row.status == 'archived':
            raise HTTPException(409, 'An archived routine cannot be approved again.')
        changes = {'status': target}
        if action == 'approve':
            changes.update(approved_by=self.user.id, approved_at=now())
        return self._routine_cas(row, command, changes, audit)

    def _context(self, context):
        if context.get('work_order_id'):
            self._job(context['work_order_id'])
        if context.get('purchase_order_id'):
            if 'purchasing:view' not in HankTaskService(self.db, self.user, self.company_id)._permissions():
                raise HTTPException(403, 'Purchasing view access is required for this routine context.')
            if (
                not tenant_query(self.db, PurchaseOrder, self.company_id)
                .filter(PurchaseOrder.id == context['purchase_order_id'])
                .first()
            ):
                raise HTTPException(404, 'Purchase order not found')

    def run_response(self, row):
        self._context(row.context_json)
        return RoutineRunResponse(
            id=row.id,
            company_id=row.company_id,
            routine_id=row.routine_id,
            routine_version=row.snapshot_json['version'],
            title=row.snapshot_json['title'],
            status=row.status,
            version=row.version,
            current_step=row.current_step,
            steps=row.snapshot_json['steps'],
            work_order_id=row.context_json.get('work_order_id'),
            purchase_order_id=row.context_json.get('purchase_order_id'),
            results=row.results_json,
            created_at=row.created_at,
            updated_at=row.updated_at,
            completed_at=row.completed_at,
            can_edit=self._interactive(write=True),
        )

    def start_routine(self, routine_id, command, audit):
        self._company(command.expected_company_id)
        self._require(write=True)
        digest = _digest({'routine_id': routine_id, **command.model_dump(mode='json')})
        acquire_generator_lock(self.db, f'hank_routine_run:{command.request_key}', self.company_id)
        existing = (
            tenant_query(self.db, HankRoutineRun, self.company_id)
            .filter(HankRoutineRun.request_key == command.request_key)
            .first()
        )
        if existing:
            if existing.owner_id != self.user.id or existing.request_hash != digest:
                raise HTTPException(409, 'This request key belongs to another routine run.')
            return self.run_response(existing)
        routine = self.get_routine(routine_id, locked=True)
        if routine.status != 'approved' or routine.version != command.expected_version:
            raise HTTPException(409, 'Review the current approved routine before starting it.')
        context = {'work_order_id': command.work_order_id, 'purchase_order_id': command.purchase_order_id}
        self._context(context)
        if context['work_order_id']:
            self._job(context['work_order_id'], live=True)
        if (
            context['purchase_order_id']
            and tenant_query(self.db, PurchaseOrder, self.company_id)
            .filter(PurchaseOrder.id == context['purchase_order_id'], PurchaseOrder.is_deleted.is_(False))
            .first()
            is None
        ):
            raise HTTPException(404, 'Purchase order not found')
        row = HankRoutineRun(
            company_id=self.company_id,
            routine_id=routine.id,
            owner_id=self.user.id,
            request_key=command.request_key,
            request_hash=digest,
            context_json=context,
            snapshot_json={
                'title': routine.title,
                'version': routine.version,
                'steps': routine.steps_json,
                'approved_by': routine.approved_by,
                'approved_at': routine.approved_at.isoformat(),
            },
        )
        self.db.add(row)
        self.db.flush()
        self._audit(
            audit,
            'CREATE',
            'hank_routine_run',
            row,
            new={'routine_id': row.routine_id, 'snapshot': row.snapshot_json, 'context': context},
        )
        return self.run_response(row)

    def get_run(self, run_id, locked=False):
        self._require()
        query = tenant_query(self.db, HankRoutineRun, self.company_id).filter(
            HankRoutineRun.owner_id == self.user.id, HankRoutineRun.id == run_id
        )
        if locked:
            query = query.with_for_update().populate_existing()
        row = query.first()
        if not row:
            raise HTTPException(404, 'Routine run not found')
        self._context(row.context_json)
        return row

    def list_runs(self, limit=20, before_id=None):
        self._require()
        query = tenant_query(self.db, HankRoutineRun, self.company_id).filter(HankRoutineRun.owner_id == self.user.id)
        if before_id:
            query = query.filter(HankRoutineRun.id < before_id)
        rows = query.order_by(HankRoutineRun.id.desc()).limit(limit + 1).all()
        # A lost module grant must not expose saved PO context through a run.
        permissions = HankTaskService(self.db, self.user, self.company_id)._permissions()
        visible = [
            row
            for row in rows[:limit]
            if not row.context_json.get('purchase_order_id') or 'purchasing:view' in permissions
        ]
        return {
            'runs': [self.run_response(row) for row in visible],
            'has_more': len(rows) > limit,
            'next_before_id': rows[limit - 1].id if len(rows) > limit else None,
        }

    def _step_evidence(self, row, command):
        kind = row.snapshot_json['steps'][row.current_step]['kind']
        if sum(value is not None for value in (command.task_id, command.intake_file_id, command.handoff_id)) > 1:
            raise HTTPException(422, 'Link one result for the current step.')
        if kind in ('receive_delivery', 'report_production', 'draft_shipment'):
            if not command.task_id:
                raise HTTPException(422, 'Link the completed Hank task for this step.')
            task = HankTaskService(self.db, self.user, self.company_id).get(command.task_id)
            if task.kind != kind or task.status != 'completed':
                raise HTTPException(422, 'The linked task must be a completed action of the current step type.')
            job_id = task.input_json.get('work_order_id')
            if kind == 'report_production':
                from app.models.work_order import WorkOrderOperation

                operation = (
                    tenant_query(self.db, WorkOrderOperation, self.company_id)
                    .filter(WorkOrderOperation.id == task.input_json.get('operation_id'))
                    .first()
                )
                job_id = operation.work_order_id if operation else None
            if (
                kind != 'receive_delivery'
                and row.context_json.get('work_order_id')
                and row.context_json['work_order_id'] != job_id
            ):
                raise HTTPException(422, 'The completed action belongs to a different job.')
            if (
                kind == 'receive_delivery'
                and row.context_json.get('purchase_order_id')
                and row.context_json['purchase_order_id'] != task.input_json.get('purchase_order_id')
            ):
                raise HTTPException(422, 'The completed receipt belongs to a different purchase order.')
            return [_reference('hank_task', task.id, task.title, f'/?hank_task={task.id}')]
        if kind == 'handoff':
            if not command.handoff_id:
                raise HTTPException(422, 'Link a completed handoff to finish this step.')
            handoff = self.get_handoff(command.handoff_id)
            if handoff.status != 'completed' or (
                row.context_json.get('work_order_id') and row.context_json['work_order_id'] != handoff.work_order_id
            ):
                raise HTTPException(422, 'The handoff must be completed by its recipient for this job.')
            return [
                _reference(
                    'hank_handoff', handoff.id, handoff.work_order_number, f'/?hank_work=handoff&hank_id={handoff.id}'
                )
            ]
        if kind == 'document_intake':
            from pydantic import ValidationError

            from app.models.purchasing import POReceipt, PurchaseOrderLine
            from app.schemas.hank_intake import IntakePlanInput
            from app.services.hank_intake_service import HankIntakeService

            if not command.intake_file_id:
                raise HTTPException(422, 'Link a completed intake file to finish this step.')
            intake = HankIntakeService(self.db, self.user, self.company_id)
            item = intake.file(command.intake_file_id)
            if item.status != 'completed':
                raise HTTPException(422, 'Choose your completed intake file in this company.')
            try:
                plan = IntakePlanInput.model_validate((item.plan_json or {}).get('input'))
            except ValidationError as exc:
                raise HTTPException(422, 'The intake result has no valid saved filing plan.') from exc
            if row.context_json.get('work_order_id') and plan.work_order_id != row.context_json['work_order_id']:
                raise HTTPException(422, 'Choose an intake result filed for this job.')
            purchase_order_id = plan.purchase_order_id
            if row.context_json.get('purchase_order_id') and plan.receipt_id:
                if 'receiving:view' not in intake.permissions():
                    raise HTTPException(403, 'Current receiving view permission is required for receipt evidence.')
                receipt_po = (
                    tenant_query(self.db, PurchaseOrderLine, self.company_id)
                    .join(POReceipt, POReceipt.po_line_id == PurchaseOrderLine.id)
                    .filter(POReceipt.company_id == self.company_id, POReceipt.id == plan.receipt_id)
                    .first()
                )
                if not receipt_po or (purchase_order_id and purchase_order_id != receipt_po.purchase_order_id):
                    raise HTTPException(422, 'The intake receipt does not match its selected purchase order.')
                purchase_order_id = receipt_po.purchase_order_id
            if row.context_json.get('purchase_order_id') and purchase_order_id != row.context_json['purchase_order_id']:
                raise HTTPException(422, 'Choose an intake result filed for this purchase order.')
            return [_reference('hank_intake', item.id, item.filename, f'/?hank_work=intake&hank_id={item.id}')]
        if command.task_id or command.intake_file_id or command.handoff_id or not command.note.strip():
            raise HTTPException(422, 'Record what you reviewed or performed for this checklist step.')
        return []

    def transition_run(self, run_id, command, action, audit):
        self._company(command.expected_company_id)
        self._require(write=True)
        row = self.get_run(run_id, locked=True)
        if row.status != 'active':
            if (action == 'cancel' and row.status == 'cancelled') or (
                action == 'advance' and row.status == 'completed'
            ):
                return self.run_response(row)
            raise HTTPException(409, 'This routine run has ended.')
        if command.expected_version != row.version:
            raise HTTPException(409, 'The routine run changed. Reload before continuing.')
        values = {'version': row.version + 1, 'updated_at': now()}
        if action == 'cancel':
            values['status'] = 'cancelled'
        else:
            evidence = self._step_evidence(row, command)
            values['results_json'] = [
                *row.results_json,
                {
                    'step_index': row.current_step,
                    'note': command.note,
                    'completed_at': now().isoformat(),
                    'evidence': evidence,
                },
            ]
            values['current_step'] = row.current_step + 1
            if values['current_step'] == len(row.snapshot_json['steps']):
                values.update(status='completed', completed_at=now())
        old = {'version': row.version, 'status': row.status, 'current_step': row.current_step}
        changed = (
            tenant_query(self.db, HankRoutineRun, self.company_id)
            .filter(
                HankRoutineRun.id == row.id,
                HankRoutineRun.owner_id == self.user.id,
                HankRoutineRun.version == command.expected_version,
            )
            .update(values, synchronize_session=False)
        )
        if changed != 1:
            raise HTTPException(409, 'The routine run changed. Reload before continuing.')
        self.db.refresh(row)
        self._audit(audit, 'UPDATE', 'hank_routine_run', row, old=old, new=values)
        return self.run_response(row)


ROUTINE_TEMPLATES = [
    {
        'title': 'Prepare a receiving packet',
        'description': 'Review delivery evidence, post the receipt, and confirm labels.',
        'steps': [
            {
                'kind': 'document_intake',
                'title': 'File the delivery document',
                'instruction': 'Read the packing slip or certificate, review its fields and file it against the correct records.',
            },
            {
                'kind': 'receive_delivery',
                'title': 'Record the delivery',
                'instruction': 'Confirm PO lines, physical counts, heat/lot details, location and inspection requirements.',
            },
            {
                'kind': 'checklist',
                'title': 'Check labels and inspection routing',
                'instruction': 'Verify the saved receipts and label delivery. Record any inspection or printing follow-up.',
            },
        ],
    },
    {
        'title': 'Review a job before starting',
        'description': 'Gather readiness evidence and current instructions.',
        'steps': [
            {
                'kind': 'readiness',
                'title': 'Review readiness gaps',
                'instruction': 'Check material, active blockers, instructions and inspection evidence. Record unresolved gaps.',
            },
            {
                'kind': 'knowledge',
                'title': 'Read the current setup guidance',
                'instruction': 'Review released instructions and historical setup notes, checking the source revision.',
            },
        ],
    },
    {
        'title': 'Prepare a shipping packet',
        'description': 'Check the job evidence and prepare the shipment for review.',
        'steps': [
            {
                'kind': 'shipping_packet',
                'title': 'Review the shipping packet',
                'instruction': 'Check available quantities, required documents and quality evidence. Identify missing requirements.',
            },
            {
                'kind': 'draft_shipment',
                'title': 'Prepare the shipment',
                'instruction': 'Review quantities, delivery address, package details and notes before creating the shipment.',
            },
            {
                'kind': 'checklist',
                'title': 'Check the physical packet',
                'instruction': 'Confirm printed paperwork and physical contents. This step does not purchase postage or mark shipped.',
            },
        ],
    },
]
