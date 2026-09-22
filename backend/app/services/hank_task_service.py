"""Employee-reviewed, atomic Hank commands. No model-selected arbitrary ERP writes."""

import hashlib
import json
from datetime import datetime
from enum import Enum

from fastapi import HTTPException
from sqlalchemy import inspect

from app.db.locks import acquire_generator_lock
from app.db.tenant_filter import tenant_query
from app.models.company import Company
from app.models.document import Document
from app.models.hank import HankTask
from app.models.laser_nest import LaserNest, LaserNestPackage
from app.models.part import Part
from app.models.part_number_alias import PartNumberAlias
from app.models.process_sheet import ProcessSheet, ProcessSheetStep, WOOperationStep
from app.models.purchasing import Vendor
from app.models.role_permission import ALL_PERMISSIONS, DEFAULT_ROLE_PERMISSIONS, RolePermission
from app.models.user import UserRole
from app.models.work_order import WorkOrder, WorkOrderOperation
from app.models.work_order_material import WorkOrderMaterialAllocation
from app.schemas.hank_tasks import (
    INPUT_SCHEMAS,
    HankCapabilities,
    HankTaskList,
    HankTaskPreview,
    HankTaskResponse,
    HankTaskResult,
)
from app.schemas.purchasing import POCreate
from app.services import notification_links
from app.services.audit_service import AuditService, AuditWriteError
from app.services.erp_draft_commands import attach_document_command, create_purchase_order_command
from app.services.work_order_duplicate_service import duplicate_work_order

ACTION_ROLES = {
    'repeat_job': {UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR},
    'draft_purchase_order': {UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR},
    'attach_document': {UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY},
    'receive_delivery': {UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR},
    'report_production': {UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.OPERATOR, UserRole.QUALITY},
    'draft_shipment': {UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.SHIPPING},
}
ACTION_PERMISSIONS = {
    'repeat_job': {'work_orders:view', 'work_orders:create'},
    'draft_purchase_order': {'purchasing:view', 'purchasing:create'},
    'attach_document': {'work_orders:view'},
    'receive_delivery': {'receiving:view', 'receiving:create'},
    'report_production': {'work_orders:view', 'work_orders:complete'},
    'draft_shipment': {'shipping:view', 'shipping:create', 'work_orders:view'},
}
PLAN_ROW_LIMIT = 2000


def _default(value):
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return str(value)


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(',', ':'), default=_default).encode()
    ).hexdigest()


def _row_values(row):
    return {column.key: getattr(row, column.key) for column in inspect(type(row)).columns}


def _reference(kind, row_id, label, url):
    return {'type': kind, 'id': row_id, 'label': label, 'url': url}


class _RequiredCreateAudit(AuditService):
    """Require every legacy domain audit used by a Hank action to succeed."""

    def __init__(self, delegate):
        self._delegate = delegate

    def _call(self, method, *args, **kwargs):
        entry = getattr(self._delegate, method)(*args, **kwargs)
        if entry is None:
            raise AuditWriteError('Unable to save required domain audit record')
        return entry

    def log_create(self, *args, **kwargs):
        return self._call('log_create', *args, **kwargs)

    def log_update(self, *args, **kwargs):
        return self._call('log_update', *args, **kwargs)

    def log_status_change(self, *args, **kwargs):
        return self._call('log_status_change', *args, **kwargs)

    def log(self, *args, **kwargs):
        return self._call('log', *args, **kwargs)

    def log_required(self, *args, **kwargs):
        return self._call('log_required', *args, **kwargs)


class HankTaskService:
    def __init__(self, db, user, company_id):
        self.db, self.user, self.company_id = db, user, company_id
        token_id = getattr(user, '_api_token_id', None)
        self.credential_key = f'api:{token_id}' if token_id is not None else 'user'

    def _permissions(self):
        if self.user.is_superuser or self.user.role == UserRole.PLATFORM_ADMIN:
            return set(ALL_PERMISSIONS)
        row = (
            tenant_query(self.db, RolePermission, self.company_id).filter(RolePermission.role == self.user.role).first()
        )
        return set(row.permissions or []) if row else set(DEFAULT_ROLE_PERMISSIONS.get(self.user.role, []))

    def _allowed_kinds(self, *, write=True):
        permissions = self._permissions()
        writable = (not write or not getattr(self.user, '_read_only_company_context', False)) and bool(
            self.user.is_active
        )
        elevated = self.user.is_superuser or self.user.role == UserRole.PLATFORM_ADMIN
        allowed = [
            kind
            for kind, roles in ACTION_ROLES.items()
            if writable
            and (elevated or self.user.role in roles)
            and (
                ACTION_PERMISSIONS[kind]
                if write
                else {permission for permission in ACTION_PERMISSIONS[kind] if permission.endswith(':view')}
            )
            <= permissions
        ]
        if not write and self.can_watch(write=False):
            allowed.append('watch_work_order')
        return allowed

    def can_watch(self, *, write=True):
        elevated = self.user.is_superuser or self.user.role == UserRole.PLATFORM_ADMIN
        company_active = self.db.query(Company.is_active).filter(Company.id == self.company_id).scalar()
        return bool(
            company_active
            and self.user.is_active
            and (self.user.company_id == self.company_id or elevated)
            and getattr(self.user, '_api_token_id', None) is None
            and getattr(self.user, '_token_scope', None) not in ('kiosk', 'api')
            and (not write or not getattr(self.user, '_read_only_company_context', False))
            and 'work_orders:view' in self._permissions()
        )

    def capabilities(self):
        allowed = self._allowed_kinds()
        return HankCapabilities(
            company_id=self.company_id, allowed_kinds=allowed, can_write=bool(allowed), can_watch=self.can_watch()
        )

    def _require_action(self, kind):
        if kind not in self.capabilities().allowed_kinds:
            raise HTTPException(403, 'Your current role or company context cannot perform this Hank action')

    def _company(self, expected):
        if expected != self.company_id:
            raise HTTPException(409, 'Active company changed. Reopen Hank in the intended company.')

    def _query(self):
        return tenant_query(self.db, HankTask, self.company_id).filter(
            HankTask.owner_id == self.user.id, HankTask.credential_key == self.credential_key
        )

    def get(self, task_id, *, locked=False):
        query = self._query().filter(HankTask.id == task_id)
        if locked:
            query = query.with_for_update().populate_existing()
        task = query.first()
        if task is None:
            raise HTTPException(404, 'Hank task not found')
        # Previously stored previews contain ERP data; current permissions still apply.
        if task.kind not in self._allowed_kinds(write=False):
            raise HTTPException(403, 'Your current role cannot view this Hank task')
        return task

    def list(self, *, limit=25, before_id=None, status=None):
        allowed = self._allowed_kinds(write=False)
        query = self._query().filter(HankTask.kind.in_(allowed))
        if status is not None:
            query = query.filter(HankTask.status == status)
        if before_id is not None:
            query = query.filter(HankTask.id < before_id)
        rows = query.order_by(HankTask.id.desc()).limit(limit + 1).all()
        more = len(rows) > limit
        tasks = rows[:limit]
        return HankTaskList(
            tasks=[self.response(task) for task in tasks],
            has_more=more,
            next_before_id=tasks[-1].id if more and tasks else None,
        )

    @staticmethod
    def response(task):
        return HankTaskResponse(
            id=task.id,
            company_id=task.company_id,
            kind=task.kind,
            title=task.title,
            status=task.status,
            version=task.version,
            input=task.input_json,
            preview=HankTaskPreview.model_validate(task.preview_json),
            result=HankTaskResult.model_validate(task.result_json) if task.result_json else None,
            error_message=task.error_message,
            created_at=task.created_at,
            updated_at=task.updated_at,
            completed_at=task.completed_at,
            last_checked_at=task.last_checked_at,
            snoozed_until=task.snoozed_until,
        )

    def _rows(self, model, predicate, *, locked=False):
        query = tenant_query(self.db, model, self.company_id).filter(predicate).order_by(model.id)
        if locked:
            query = query.with_for_update().populate_existing()
        rows = query.limit(PLAN_ROW_LIMIT + 1).all()
        if len(rows) > PLAN_ROW_LIMIT:
            raise HTTPException(409, 'This source plan is too large for Hank. Use the existing ERP workflow.')
        return rows

    def _work_order(self, row_id, *, locked=False):
        rows = self._rows(WorkOrder, WorkOrder.id == row_id, locked=locked)
        if not rows or rows[0].is_deleted:
            raise HTTPException(404, 'Work order not found')
        return rows[0]

    def _repeat_plan(self, source_id, *, locked=False):
        source = self._work_order(source_id, locked=locked)
        operations = self._rows(WorkOrderOperation, WorkOrderOperation.work_order_id == source.id, locked=locked)
        operation_ids = [row.id for row in operations]
        nests = self._rows(LaserNest, LaserNest.work_order_operation_id.in_(operation_ids), locked=locked)
        packages = self._rows(
            LaserNestPackage, LaserNestPackage.id.in_([row.package_id for row in nests]), locked=locked
        )
        allocations = self._rows(
            WorkOrderMaterialAllocation, WorkOrderMaterialAllocation.work_order_id == source.id, locked=locked
        )
        steps = self._rows(WOOperationStep, WOOperationStep.work_order_operation_id.in_(operation_ids), locked=locked)
        original_sheets = self._rows(
            ProcessSheet, ProcessSheet.id.in_([row.source_sheet_id for row in steps]), locked=locked
        )
        sheets = self._rows(
            ProcessSheet, ProcessSheet.sheet_number.in_([row.sheet_number for row in original_sheets]), locked=locked
        )
        sheet_steps = self._rows(
            ProcessSheetStep, ProcessSheetStep.process_sheet_id.in_([row.id for row in sheets]), locked=locked
        )
        part_ids = {
            source.part_id,
            *[row.part_id for row in allocations],
            *[row.component_part_id for row in operations],
        }
        parts = self._rows(Part, Part.id.in_([row_id for row_id in part_ids if row_id is not None]), locked=locked)
        aliases = self._rows(PartNumberAlias, PartNumberAlias.part_id.in_([row.id for row in parts]), locked=locked)
        documents = self._rows(
            Document, Document.id.in_([row.document_id for row in nests if row.document_id]), locked=locked
        )
        snapshot = {
            'source': _row_values(source),
            'operations': [_row_values(row) for row in operations],
            'nests': [_row_values(row) for row in nests],
            'packages': [_row_values(row) for row in packages],
            'allocations': [_row_values(row) for row in allocations],
            'steps': [_row_values(row) for row in steps],
            'sheets': [_row_values(row) for row in sheets],
            'sheet_steps': [_row_values(row) for row in sheet_steps],
            'parts': [_row_values(row) for row in parts],
            'aliases': [_row_values(row) for row in aliases],
            'documents': [_row_values(row) for row in documents],
        }
        return source, operations, nests, allocations, {'plan_sha256': _digest(snapshot)}

    def _preview(self, kind, data, *, locked=False):
        from app.services.hank_operational_actions import KINDS, HankOperationalActions

        if kind in KINDS:
            return HankOperationalActions(self).preview(kind, data, locked=locked)
        if kind == 'repeat_job':
            source, operations, nests, allocations, snapshot = self._repeat_plan(
                data['source_work_order_id'], locked=locked
            )
            live_nests = [row for row in nests if not row.is_deleted]
            quantity = (
                sum(row.planned_runs or 0 for row in live_nests) if live_nests else float(data['quantity_ordered'])
            )
            warnings = [
                'Review the resulting draft before release. Production history, lot pins and dispatch ranks are not copied.',
                'Unavailable operations or material ties may be skipped; the completion receipt lists every omission.',
                'Process-sheet steps are resnapshotted from currently released revisions; no manufacturing approval is granted.',
            ]
            if live_nests:
                warnings.append(
                    f'Laser quantity is derived from copied nest runs: {quantity:g}; requested quantity does not rescale nests.'
                )
            return (
                f'Repeat {source.work_order_number}',
                HankTaskPreview(
                    summary=f'Create a draft repeat of {source.work_order_number}.',
                    changes=[
                        f'New job status: draft. Quantity: {quantity:g}. Due: {data.get("due_date") or "not set"}.',
                        f'Source plan: {len(operations)} operations, {len(live_nests)} live nests, {len(allocations)} material ties.',
                    ],
                    warnings=warnings,
                    references=[
                        _reference(
                            'work_order',
                            source.id,
                            source.work_order_number,
                            notification_links.work_order_detail(source.id),
                        )
                    ],
                ),
                snapshot,
            )
        if kind == 'draft_purchase_order':
            vendors = self._rows(Vendor, Vendor.id == data['vendor_id'], locked=locked)
            if not vendors or vendors[0].is_deleted or not vendors[0].is_active:
                raise HTTPException(404, 'Active vendor not found')
            vendor = vendors[0]
            ids = {line['part_id'] for line in data['lines']}
            parts = self._rows(Part, Part.id.in_(ids), locked=locked)
            if len(parts) != len(ids) or any(row.is_deleted or not row.is_active for row in parts):
                raise HTTPException(404, 'An active purchase-order part was not found')
            by_id = {row.id: row for row in parts}
            total = sum(float(line['quantity_ordered']) * float(line['unit_price']) for line in data['lines'])
            changes = [
                f'Create a draft PO for {vendor.name}; {len(data["lines"])} lines; subtotal {total:.2f}.',
                f'Required: {data.get("required_date") or "not set"}; expected: {data.get("expected_date") or "not set"}.',
            ]
            changes.extend(
                f'{by_id[line["part_id"]].part_number}: {line["quantity_ordered"]} at {line["unit_price"]} each; '
                f'required {line.get("required_date") or data.get("required_date") or "not set"}; '
                f'notes: {line.get("notes") or "none"}.'
                for line in data['lines']
            )
            for key, label in (('ship_to', 'Ship to'), ('shipping_method', 'Shipping method'), ('notes', 'Notes')):
                if data.get(key):
                    changes.append(f'{label}: {data[key]}')
            snapshot = {
                'vendor_sha256': _digest(_row_values(vendor)),
                'parts_sha256': _digest([_row_values(row) for row in parts]),
            }
            return (
                f'Draft PO for {vendor.name}'[:300],
                HankTaskPreview(
                    summary=f'Prepare a purchase order for {vendor.name}.',
                    changes=changes,
                    warnings=['The PO stays draft. It is not approved, sent to the vendor, or received.'],
                ),
                snapshot,
            )
        work_order = self._work_order(data['work_order_id'], locked=locked)
        document_rows = self._rows(Document, Document.id == data['document_id'], locked=locked)
        if not document_rows:
            raise HTTPException(404, 'Document not found')
        document = document_rows[0]
        successors = self._rows(Document, Document.previous_revision_id == document.id, locked=locked)
        if document.mime_type != 'application/pdf' and not (document.file_name or '').lower().endswith('.pdf'):
            raise HTTPException(400, 'Only PDF documents can be attached to a work order')
        if document.work_order_id != work_order.id and (
            document.work_order_id is not None or document.previous_revision_id or successors
        ):
            raise HTTPException(409, 'An existing work-order or revision-history attachment cannot be reassigned')
        snapshot = {
            'document_sha256': _digest(_row_values(document)),
            'successors_sha256': _digest([_row_values(row) for row in successors]),
            'work_order_sha256': _digest(_row_values(work_order)),
        }
        return (
            f'Attach {document.document_number}'[:300],
            HankTaskPreview(
                summary=f'Attach {document.document_number} to {work_order.work_order_number}.',
                changes=[
                    f'PDF: {document.title}; revision {document.revision}; status {document.status}.',
                    f'Job: {work_order.work_order_number}.',
                ],
                warnings=['Attachment does not approve the document or verify its contents.'],
                references=[
                    _reference('document', document.id, document.document_number, f'/documents?document={document.id}'),
                    _reference(
                        'work_order',
                        work_order.id,
                        work_order.work_order_number,
                        notification_links.work_order_detail(work_order.id),
                    ),
                ],
            ),
            snapshot,
        )

    def prepare(self, payload, audit):
        """Flush a durable reviewed proposal; caller commits. Safe for endpoint or chat use."""
        self._company(payload.expected_company_id)
        self._require_action(payload.kind)
        request_hash = _digest(
            {
                'schema': 1,
                'company_id': self.company_id,
                'owner_id': self.user.id,
                'credential_key': self.credential_key,
                'command': payload.model_dump(mode='json'),
            }
        )
        acquire_generator_lock(self.db, 'hank_request:' + payload.request_key, self.company_id)
        prior = (
            tenant_query(self.db, HankTask, self.company_id).filter(HankTask.request_key == payload.request_key).first()
        )
        if prior is not None:
            if (
                prior.owner_id != self.user.id
                or prior.credential_key != self.credential_key
                or prior.request_hash != request_hash
            ):
                raise HTTPException(409, 'This request key already records a different command or credential')
            return prior
        acquire_generator_lock(self.db, f'hank_owner:{self.user.id}', self.company_id)
        pending = self._query().filter(HankTask.status == 'awaiting_review').limit(100).count()
        if pending >= 100:
            raise HTTPException(
                409, 'You have 100 tasks waiting for review. Complete or cancel one before adding another.'
            )
        title, preview, snapshot = self._preview(payload.kind, payload.input)
        now = datetime.utcnow()
        task = HankTask(
            company_id=self.company_id,
            owner_id=self.user.id,
            credential_key=self.credential_key,
            request_key=payload.request_key,
            request_hash=request_hash,
            kind=payload.kind,
            title=title,
            status='awaiting_review',
            version=1,
            input_json=payload.input,
            preview_json=preview.model_dump(mode='json'),
            source_versions_json=snapshot,
            created_at=now,
            updated_at=now,
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
                'preview': task.preview_json,
            },
        )
        return task

    def execute(self, task_id, command, audit):
        self._company(command.expected_company_id)
        acquire_generator_lock(self.db, f'hank_task:{task_id}', self.company_id)
        task = self.get(task_id, locked=True)
        self._require_action(task.kind)
        if task.status == 'completed':
            return task
        if task.status != 'awaiting_review' or task.version != command.expected_version:
            raise HTTPException(409, 'This task changed. Refresh it before executing.')
        data = INPUT_SCHEMAS[task.kind].model_validate(task.input_json).model_dump(mode='json')
        if task.kind == 'report_production':
            acquire_generator_lock(self.db, f'production_receipt:hank:{task.request_key}', self.company_id)
        _, _, snapshot = self._preview(task.kind, data, locked=True)
        if snapshot != task.source_versions_json:
            raise HTTPException(409, 'The source records changed. Prepare and review a fresh task.')
        warnings = []
        if task.kind in {'receive_delivery', 'report_production', 'draft_shipment'}:
            from app.services.hank_operational_actions import HankOperationalActions

            receipt = HankOperationalActions(self).execute(task, data, _RequiredCreateAudit(audit))
        elif task.kind == 'repeat_job':
            parsed = INPUT_SCHEMAS[task.kind].model_validate(data)
            result = duplicate_work_order(
                self.db,
                source=self._work_order(data['source_work_order_id'], locked=True),
                quantity_ordered=float(parsed.quantity_ordered),
                due_date=parsed.due_date,
                company_id=self.company_id,
                user_id=self.user.id,
                audit=_RequiredCreateAudit(audit),
            )
            work_order = result.work_order
            warnings.extend(
                f'Skipped operation {row.operation_number or row.source_operation_id}: {row.reason}.'
                for row in result.skipped_operations
            )
            warnings.extend(
                f'Skipped material tie {row.source_allocation_id} (part {row.part_id}): {row.reason}.'
                for row in result.skipped_allocations
            )
            receipt = HankTaskResult(
                summary=f'Created {work_order.work_order_number} as a draft with quantity {work_order.quantity_ordered:g}.',
                warnings=warnings,
                references=[
                    _reference(
                        'work_order',
                        work_order.id,
                        work_order.work_order_number,
                        notification_links.work_order_detail(work_order.id),
                    )
                ],
            )
        elif task.kind == 'draft_purchase_order':
            po = create_purchase_order_command(
                self.db, POCreate.model_validate(data), self.user, self.company_id, _RequiredCreateAudit(audit)
            )
            receipt = HankTaskResult(
                summary=f'Created {po.po_number} as a draft. Total: {po.total:.2f}.',
                references=[
                    _reference('purchase_order', po.id, po.po_number, notification_links.purchase_order(po.id))
                ],
            )
        else:
            document = attach_document_command(
                self.db, data['document_id'], data['work_order_id'], self.company_id, audit
            )
            work_order = self._work_order(data['work_order_id'])
            receipt = HankTaskResult(
                summary=f'Attached {document.document_number} to {work_order.work_order_number}.',
                references=[
                    _reference('document', document.id, document.document_number, f'/documents?document={document.id}'),
                    _reference(
                        'work_order',
                        work_order.id,
                        work_order.work_order_number,
                        notification_links.work_order_detail(work_order.id),
                    ),
                ],
            )
        now = datetime.utcnow()
        changed = (
            self._query()
            .filter(
                HankTask.id == task.id,
                HankTask.status == 'awaiting_review',
                HankTask.version == command.expected_version,
            )
            .update(
                {
                    'status': 'completed',
                    'version': command.expected_version + 1,
                    'result_json': receipt.model_dump(mode='json'),
                    'updated_at': now,
                    'completed_at': now,
                },
                synchronize_session=False,
            )
        )
        if changed != 1:
            raise HTTPException(409, 'This task changed before completion. Refresh its receipt.')
        audit.log_required(
            'UPDATE',
            'hank_task',
            resource_id=task.id,
            resource_identifier=task.title,
            old_values={'status': 'awaiting_review', 'version': command.expected_version},
            new_values={
                'status': 'completed',
                'version': command.expected_version + 1,
                'result': receipt.model_dump(mode='json'),
            },
        )
        self.db.flush()
        self.db.refresh(task)
        return task

    def cancel(self, task_id, command, audit):
        self._company(command.expected_company_id)
        acquire_generator_lock(self.db, f'hank_task:{task_id}', self.company_id)
        task = self.get(task_id, locked=True)
        self._require_action(task.kind)
        if task.status == 'cancelled':
            return task
        if task.status != 'awaiting_review' or task.version != command.expected_version:
            raise HTTPException(409, 'This task changed. Refresh it before cancelling.')
        now = datetime.utcnow()
        changed = (
            self._query()
            .filter(
                HankTask.id == task.id,
                HankTask.status == 'awaiting_review',
                HankTask.version == command.expected_version,
            )
            .update(
                {
                    'status': 'cancelled',
                    'version': command.expected_version + 1,
                    'updated_at': now,
                    'completed_at': now,
                },
                synchronize_session=False,
            )
        )
        if changed != 1:
            raise HTTPException(409, 'This task changed. Refresh it before cancelling.')
        audit.log_required(
            'UPDATE',
            'hank_task',
            resource_id=task.id,
            resource_identifier=task.title,
            old_values={'status': 'awaiting_review', 'version': command.expected_version},
            new_values={'status': 'cancelled', 'version': command.expected_version + 1},
        )
        self.db.flush()
        self.db.refresh(task)
        return task
