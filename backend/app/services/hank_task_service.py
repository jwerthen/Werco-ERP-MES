"""Employee-reviewed, atomic Hank commands. No model-selected arbitrary ERP writes."""

import hashlib
import json
from datetime import datetime
from enum import Enum

from fastapi import HTTPException
from sqlalchemy import inspect
from sqlalchemy.exc import DBAPIError

from app.core.credentials import API_TOKEN_COMMENT_WRITE, PEOPLE_ONLY_HANK_TASK_KINDS, is_api_token_credential
from app.core.time_utils import central_today
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
    AddNoteInput,
    ChangeDueDateInput,
    HankCapabilities,
    HankTaskList,
    HankTaskPreview,
    HankTaskResponse,
    HankTaskResult,
)
from app.schemas.purchasing import POCreate
from app.services import comment_service, notification_links
from app.services.audit_service import AuditService, AuditWriteError
from app.services.erp_draft_commands import attach_document_command, create_purchase_order_command
from app.services.supplier_approval_service import assert_vendor_not_disqualified
from app.services.work_order_due_date import (
    apply_due_date_change,
    assert_due_date_change_allowed,
    emit_due_date_changed_event,
)
from app.services.work_order_duplicate_service import duplicate_work_order

ACTION_ROLES = {
    'repeat_job': {UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR},
    'draft_purchase_order': {UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR},
    'attach_document': {UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY},
    'receive_delivery': {UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR},
    'report_production': {UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.OPERATOR, UserRole.QUALITY},
    'draft_shipment': {UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.SHIPPING},
    # The roles that hold work_orders:edit by default -- the role set of PUT /work-orders/{id}, the
    # other door that moves a due date.
    'change_due_date': {UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR},
    # Commenting is open to everyone who can read the work order (all seven tenant roles), so a
    # VIEWER's first Hank write is a note. It is a person's own words: see PEOPLE_ONLY_KINDS.
    'add_note': {
        UserRole.ADMIN,
        UserRole.MANAGER,
        UserRole.SUPERVISOR,
        UserRole.OPERATOR,
        UserRole.QUALITY,
        UserRole.SHIPPING,
        UserRole.VIEWER,
    },
}
ACTION_PERMISSIONS = {
    'repeat_job': {'work_orders:view', 'work_orders:create'},
    'draft_purchase_order': {'purchasing:view', 'purchasing:create'},
    'attach_document': {'work_orders:view'},
    'receive_delivery': {'receiving:view', 'receiving:create'},
    'report_production': {'work_orders:view', 'work_orders:complete'},
    'draft_shipment': {'shipping:view', 'shipping:create', 'work_orders:view'},
    'change_due_date': {'work_orders:view', 'work_orders:edit'},
    'add_note': {'work_orders:view'},
}
# Kinds only a PERSON may run. A note is a work-order comment, and a comment is a person's own words
# (owner decision for work-order comments, 2026-11-16): ANY API token, whatever its scope or role,
# is refused 403 with the comment routes' own text at prepare and at execute, and the kind is left
# out of a token's capabilities. A crew-station badge token is not an API token, but its path fence
# keeps it off /hank entirely. The set lives in app.core.credentials because the MCP package reads
# it too: an agent on a person's own JWT is refused these kinds there (app/mcp/people_only.py).
PEOPLE_ONLY_KINDS = PEOPLE_ONLY_HANK_TASK_KINDS
# A kind with no branch of its own. HankTaskCreate's Literal admits only registered kinds, so this
# is a programming error (a kind added to the maps without its preview / execute branch), answered
# as a clean 400 rather than by running some other kind's code.
UNSUPPORTED_KIND_DETAIL = 'Hank cannot run this kind of task.'
PLAN_ROW_LIMIT = 2000


class RowLock(str, Enum):
    """How strongly a ``locked`` read in :meth:`HankTaskService._rows` locks the rows it reads.

    Postgres row-lock modes (SQLite takes no row locks, so tests compile the SQL with the Postgres
    dialect to see them). ``UPDATE`` is the default and what every kind takes unless its preview
    names a weaker mode. A plain FOR UPDATE conflicts with the FOR KEY SHARE that every concurrent
    foreign-key insert referencing the row takes (an operational event, a time entry, a comment's
    events), and an execute goes on to take the global audit-chain lock -- so a lock stronger than
    the kind's own write needs only widens the window for a deadlock against a writer that took the
    chain lock first. A kind that only has to keep the row from changing under it takes the weakest
    mode that does.
    """

    UPDATE = 'update'  # FOR UPDATE: conflicts with every other row lock, KEY SHARE included
    NO_KEY_UPDATE = 'no_key_update'  # FOR NO KEY UPDATE: the lock a non-key UPDATE takes anyway
    SHARE = 'share'  # FOR SHARE: blocks any UPDATE / DELETE of the row, never a KEY SHARE


# ``with_for_update`` arguments per mode. ``key_share=True`` ALONE renders FOR NO KEY UPDATE;
# ``read=True`` alone renders FOR SHARE (both together would be FOR KEY SHARE, which no kind uses).
_ROW_LOCK_ARGS = {
    RowLock.UPDATE: {},
    RowLock.NO_KEY_UPDATE: {'key_share': True},
    RowLock.SHARE: {'read': True},
}


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
        # A database error ``AuditService.log`` re-raises (it aborted the transaction) is
        # a lost required audit too: same AuditWriteError, so the same rollback and 503.
        try:
            entry = getattr(self._delegate, method)(*args, **kwargs)
        except DBAPIError as exc:
            raise AuditWriteError('Unable to save required domain audit record') from exc
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
        api_token = is_api_token_credential(self.user)
        allowed = [
            kind
            for kind, roles in ACTION_ROLES.items()
            if writable
            # Capabilities stay honest: a token is never offered a person-only kind. Reads of a
            # token's own saved tasks (write=False) are unaffected -- a token can never own one.
            and not (write and api_token and kind in PEOPLE_ONLY_KINDS)
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

    def _refuse_api_token_for_people_only(self, kind):
        """403 with the comment routes' text for an API token on a person-only kind.

        Runs BEFORE ``_require_action`` at prepare and at execute, so the specific refusal wins over
        the generic one; ``_preview`` repeats it as defense in depth.
        """
        if kind in PEOPLE_ONLY_KINDS and is_api_token_credential(self.user):
            raise HTTPException(403, API_TOKEN_COMMENT_WRITE)

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

    def _rows(self, model, predicate, *, locked=False, strength=RowLock.UPDATE):
        """``strength`` applies only when ``locked``; see :class:`RowLock` for when to weaken it."""
        query = tenant_query(self.db, model, self.company_id).filter(predicate).order_by(model.id)
        if locked:
            query = query.with_for_update(**_ROW_LOCK_ARGS[strength]).populate_existing()
        rows = query.limit(PLAN_ROW_LIMIT + 1).all()
        if len(rows) > PLAN_ROW_LIMIT:
            raise HTTPException(409, 'This source plan is too large for Hank. Use the existing ERP workflow.')
        return rows

    def _work_order(self, row_id, *, locked=False, strength=RowLock.UPDATE):
        rows = self._rows(WorkOrder, WorkOrder.id == row_id, locked=locked, strength=strength)
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
            if locked:
                # All Hank PO commands take the generator lock before row locks.
                # Otherwise a manual draft holding a vendor could deadlock with
                # a source import holding the number lock for the same vendor.
                acquire_generator_lock(self.db, 'po_number', self.company_id)
            source, source_snapshot = None, {}
            if data.get("source_intake_file_id") is not None:
                from app.services.hank_intake_purchase_order_service import HankIntakePurchaseOrderService

                source, source_snapshot = HankIntakePurchaseOrderService(
                    self.db, self.user, self.company_id
                ).validate_import(data, locked=locked)
            vendors = self._rows(Vendor, Vendor.id == data['vendor_id'], locked=locked)
            if not vendors or vendors[0].is_deleted or not vendors[0].is_active:
                raise HTTPException(404, 'Active vendor not found')
            vendor = vendors[0]
            # Refuse at prepare as well as at execute (create_purchase_order_command), so Hank
            # never offers a plan the ASL's Disqualified gate would refuse.
            assert_vendor_not_disqualified(vendor)
            ids = {line['part_id'] for line in data['lines']}
            parts = self._rows(Part, Part.id.in_(ids), locked=locked)
            if len(parts) != len(ids) or any(row.is_deleted or not row.is_active for row in parts):
                raise HTTPException(404, 'An active purchase-order part was not found')
            by_id = {row.id: row for row in parts}
            if source:
                from app.services.hank_intake_receiving_service import _unit

                for line in data["lines"]:
                    if _unit(line.get("unit_of_measure")) != _unit(by_id[line["part_id"]].unit_of_measure):
                        raise HTTPException(409, "Review each imported quantity in the selected part’s stocking units.")
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
                    changes.append(f"{label}: {data[key]}")
            snapshot = {
                'vendor_sha256': _digest(_row_values(vendor)),
                'parts_sha256': _digest([_row_values(row) for row in parts]),
            }
            if source:
                snapshot.update(source_snapshot)
                ready = data.get("ready_for_receiving", False)
                changes[0] = (
                    f'Create PO {data["po_number"]} for {vendor.name}; {len(data["lines"])} lines; subtotal {total:.2f}.'
                )
                changes.append(f'Original order date: {data.get("order_date") or "not recorded"}.')
                changes.append(
                    "Record the existing PO as sent and add its open lines to Receiving."
                    if ready
                    else "Keep the imported PO as a draft; it will not appear in Receiving yet."
                )
                changes.extend(
                    f'Source line {line["source_line_index"] + 1}: {line["quantity_ordered"]} '
                    f'{line["unit_of_measure"]} of {by_id[line["part_id"]].part_number}.'
                    for line in data["lines"]
                )
                return (
                    f'Import PO {data["po_number"]}'[:300],
                    HankTaskPreview(
                        summary=f'Import purchase order {data["po_number"]} for {vendor.name}.',
                        changes=changes,
                        warnings=[
                            "Review every line, supplier, number, date and price against the uploaded original.",
                            "This does not send email, record received material, or change inventory.",
                        ]
                        + HankIntakePurchaseOrderService(self.db, self.user, self.company_id).review_warnings(
                            source, data
                        ),
                        references=[
                            _reference(
                                "intake_file", source.id, source.filename, f"/?hank_work=intake&hank_id={source.id}"
                            )
                        ],
                    ),
                    snapshot,
                )
            return (
                f"Draft PO for {vendor.name}"[:300],
                HankTaskPreview(
                    summary=f"Prepare a purchase order for {vendor.name}.",
                    changes=changes,
                    warnings=['The PO stays draft. It is not approved, sent to the vendor, or received.'],
                ),
                snapshot,
            )
        # Every kind has its OWN branch. Nothing falls through to attach_document: a new kind
        # without a branch is refused below rather than run as someone else's preview.
        if kind == 'change_due_date':
            return self._due_date_preview(data, locked=locked)
        if kind == 'add_note':
            return self._note_preview(data, locked=locked)
        if kind == 'attach_document':
            return self._attach_document_preview(data, locked=locked)
        raise HTTPException(400, UNSUPPORTED_KIND_DETAIL)

    def _attach_document_preview(self, data, *, locked=False):
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

    @staticmethod
    def _work_order_reference(work_order):
        return _reference(
            'work_order',
            work_order.id,
            work_order.work_order_number,
            notification_links.work_order_detail(work_order.id),
        )

    def _due_date_preview(self, data, *, locked=False):
        """Move ``due_date`` only -- never ``must_ship_by``. Refusals (no-op, terminal status) run at
        prepare and again under the row lock at execute."""
        parsed = ChangeDueDateInput.model_validate(data)
        # FOR NO KEY UPDATE, not FOR UPDATE: the execute's own write is a non-key UPDATE of this row,
        # which takes exactly this lock anyway (so no upgrade happens mid-transaction), and it still
        # blocks a concurrent soft delete or version-bumping write between the snapshot check and
        # the write. Unlike FOR UPDATE it does not block the FOR KEY SHARE a concurrent foreign-key
        # insert on this work order takes (events, time entries, comments) -- see RowLock.
        work_order = self._work_order(parsed.work_order_id, locked=locked, strength=RowLock.NO_KEY_UPDATE)
        number = work_order.work_order_number
        new_due_date = parsed.new_due_date.isoformat()
        if work_order.due_date == parsed.new_due_date:
            raise HTTPException(409, f'{number} is already due {new_due_date}. There is nothing to change.')
        # The PUT's own rule (services/work_order_due_date.py), never a copy of it.
        assert_due_date_change_allowed(work_order, parsed.new_due_date)
        old_due_date = work_order.due_date.isoformat() if work_order.due_date else 'not set'
        warnings = ['Only the due date changes. Scheduled dates, priority and the dispatch plan are not edited.']
        if work_order.must_ship_by is not None:
            warnings.append(
                f'The promise date does not move: must ship by {work_order.must_ship_by.isoformat()} stays as it '
                'is, and it takes precedence over the due date for on-time delivery.'
            )
        if parsed.new_due_date < central_today():
            warnings.append('The new due date is already in the past.')
        # NARROW on purpose: a full-row digest would refuse execute after any column change,
        # including dispatch-plan rank writes that deliberately bump nothing. These four are what
        # the review judged; ``version`` catches every ORM edit of the work order.
        snapshot = {
            'due_date_source_sha256': _digest(
                {
                    'due_date': work_order.due_date,
                    'must_ship_by': work_order.must_ship_by,
                    'status': work_order.status,
                    'version': work_order.version,
                }
            )
        }
        return (
            f'Move due date of {number}'[:300],
            HankTaskPreview(
                summary=f'Change the due date of {number} to {new_due_date}.',
                changes=[f'Due date: {old_due_date} → {new_due_date}.', f'Reason: {parsed.reason}'],
                warnings=warnings,
                references=[self._work_order_reference(work_order)],
            ),
            snapshot,
        )

    def _note_preview(self, data, *, locked=False):
        """A work-order COMMENT (never ``WorkOrder.notes``), judged by the comment service's own rules
        without writing anything."""
        self._refuse_api_token_for_people_only('add_note')
        parsed = AddNoteInput.model_validate(data)
        # FOR SHARE, not FOR UPDATE: a note never writes the work-order row, it only needs the row
        # to stay live and unchanged until the comment is in. FOR SHARE blocks a concurrent soft
        # delete or header write and does not conflict with FOR KEY SHARE -- the lock the comment
        # service itself takes on this row and every concurrent foreign-key insert takes -- see RowLock.
        # If the note path ever UPDATEs the work order, this must become NO_KEY_UPDATE: two notes
        # holding FOR SHARE that both upgrade to an update lock deadlock on each other.
        work_order = self._work_order(parsed.work_order_id, locked=locked, strength=RowLock.SHARE)
        draft = comment_service.validate_comment(
            self.db,
            company_id=self.company_id,
            actor=self.user,
            entity_type='work_order',
            entity_id=work_order.id,
            body=parsed.body,
            mentioned_user_ids=parsed.mentioned_user_ids,
        )
        number = work_order.work_order_number
        changes = [f'Note: {draft.body}']
        warnings = ['Everyone who can see this job can read the note. It is posted under your name.']
        if draft.mentioned:
            changes.append('Mentions: ' + ', '.join(user.full_name for user in draft.mentioned) + '.')
            warnings.append(
                'Mentioned people are notified in the app and by email; the email does not include the note text.'
            )
        warnings.append('People watching this job get an in-app notice that does not include the note text.')
        snapshot = {
            'note_target_sha256': _digest(
                {
                    'work_order_id': work_order.id,
                    'is_deleted': bool(work_order.is_deleted),
                    'mentioned_user_ids': draft.mention_ids,
                }
            )
        }
        return (
            f'Add a note to {number}'[:300],
            HankTaskPreview(
                summary=f'Add a note to {number}.',
                changes=changes,
                warnings=warnings,
                references=[self._work_order_reference(work_order)],
            ),
            snapshot,
        )

    def _execute_due_date_change(self, data, audit):
        parsed = ChangeDueDateInput.model_validate(data)
        # The row _preview just locked and re-read under the matching snapshot.
        work_order = self._work_order(parsed.work_order_id)
        old_due_date = work_order.due_date
        apply_due_date_change(self.db, work_order, parsed.new_due_date)
        number = work_order.work_order_number
        old_text = old_due_date.isoformat() if old_due_date else 'not set'
        new_text = parsed.new_due_date.isoformat()
        # REQUIRED: a lost row is AuditWriteError -> rollback -> 503, and the date never moves.
        _RequiredCreateAudit(audit).log_update(
            resource_type='work_order',
            resource_id=work_order.id,
            resource_identifier=number,
            old_values={'due_date': old_due_date},
            new_values={'due_date': work_order.due_date},
            description=f'Due date of {number} moved from {old_text} to {new_text} through Hank',
            extra_data={'reason': parsed.reason, 'via': 'hank'},
        )
        # Best-effort telemetry in the PUT's shape. The reason stays on the audit row only:
        # ``reason`` is an email detail key, so it never goes into the event payload.
        emit_due_date_changed_event(self.db, company_id=self.company_id, actor=self.user, work_order=work_order)
        # No realtime broadcast, deliberately: Hank's endpoint commits once with no post-commit hook,
        # a broadcast must never precede the commit, and realtime ships dark
        # (REALTIME_BROADCAST_ENABLED=False). Open views pick the date up on their next read.
        warnings = []
        if work_order.must_ship_by is not None:
            warnings.append(f'Must ship by {work_order.must_ship_by.isoformat()} was not changed.')
        return HankTaskResult(
            summary=f'Moved the due date of {number} from {old_text} to {new_text}.',
            warnings=warnings,
            references=[self._work_order_reference(work_order)],
        )

    def _execute_add_note(self, data, audit):
        self._refuse_api_token_for_people_only('add_note')
        parsed = AddNoteInput.model_validate(data)
        # The comment service owns every rule and the one REQUIRED create audit (the wrapper makes
        # its single log_create fail closed); the submitter is the author.
        comment_service.create_comment(
            self.db,
            company_id=self.company_id,
            actor=self.user,
            entity_type='work_order',
            entity_id=parsed.work_order_id,
            body=parsed.body,
            mentioned_user_ids=parsed.mentioned_user_ids,
            audit=_RequiredCreateAudit(audit),
            audit_extra={'via': 'hank'},
        )
        work_order = self._work_order(parsed.work_order_id)
        return HankTaskResult(
            summary=f'Added a note to {work_order.work_order_number}.',
            references=[self._work_order_reference(work_order)],
        )

    def prepare(self, payload, audit):
        """Flush a durable reviewed proposal; caller commits. Safe for endpoint or chat use."""
        self._refuse_api_token_for_people_only(payload.kind)
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
        self._refuse_api_token_for_people_only(task.kind)
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
            imported = {}
            source = None
            if data.get("source_intake_file_id") is not None:
                from app.services.hank_intake_purchase_order_service import HankIntakePurchaseOrderService

                source = HankIntakePurchaseOrderService(self.db, self.user, self.company_id).source(
                    data["source_intake_file_id"], version=data["source_intake_version"], locked=True
                )
                parsed = INPUT_SCHEMAS[task.kind].model_validate(data)
                imported = {
                    "imported_po_number": parsed.po_number,
                    "imported_order_date": parsed.order_date,
                    "source_document_path": source.storage_ref,
                    "ready_for_receiving": parsed.ready_for_receiving,
                }
            po = create_purchase_order_command(
                self.db,
                POCreate.model_validate(data),
                self.user,
                self.company_id,
                _RequiredCreateAudit(audit),
                **imported,
            )
            ready = bool(data.get("ready_for_receiving"))
            receipt = HankTaskResult(
                summary=(
                    f"Imported {po.po_number} and added its open lines to Receiving. Total: {po.total:.2f}."
                    if ready
                    else f"Created {po.po_number} as a draft. Total: {po.total:.2f}."
                ),
                warnings=(["No email was sent and no material was received."] if source else []),
                references=[_reference('purchase_order', po.id, po.po_number, notification_links.purchase_order(po.id))]
                + (
                    [_reference("receiving", po.id, f"Receive {po.po_number}", f"/receiving?po={po.id}")]
                    if ready
                    else []
                )
                + (
                    [_reference("intake_file", source.id, source.filename, f"/?hank_work=intake&hank_id={source.id}")]
                    if source
                    else []
                ),
            )
        elif task.kind == 'change_due_date':
            receipt = self._execute_due_date_change(data, audit)
        elif task.kind == 'add_note':
            receipt = self._execute_add_note(data, audit)
        elif task.kind == 'attach_document':
            # The raw ``audit`` is safe here only because attach_document_command writes its own
            # document audit through log_required.
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
        else:
            raise HTTPException(400, UNSUPPORTED_KIND_DETAIL)
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
