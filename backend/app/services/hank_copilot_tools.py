"""Hank's chat can gather evidence and save proposals; execution stays explicit."""

from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError

from app.schemas.hank_tasks import INPUT_SCHEMAS, HankTaskCreate
from app.services.audit_service import AuditService
from app.services.hank_briefing_service import HankBriefingService


def shift_briefing(*, db, company_id, user):
    briefing = HankBriefingService(db, user, company_id).briefing()
    return {
        'data': briefing.model_dump(mode='json'),
        'summary': 'checked your role-specific shift priorities',
        'references': [
            {'type': item.source_kind, 'id': item.source_id, 'label': item.title, 'url': item.href}
            for section in briefing.sections
            for item in section.items
        ],
    }


def prepare_task(*, db, company_id, user, kind, input):
    # Imported lazily to keep the deterministic read tool independent of task storage.
    from app.services.hank_task_service import HankTaskService

    try:
        payload = HankTaskCreate(expected_company_id=company_id, request_key=str(uuid4()), kind=kind, input=input)
        # This only prepares; no domain command executes in chat. Complete the
        # short audited transaction before another model round: the audit-chain
        # advisory lock must never remain held during external LLM I/O.
        with db.begin_nested():
            task = HankTaskService(db, user, company_id).prepare(
                payload, AuditService(db, user=user, company_id=company_id)
            )
        # Materialize while locked so expire-on-commit cannot start another read.
        task_id, task_title = task.id, task.title
        task_status, preview = task.status, task.preview_json
        db.commit()
    except ValidationError:
        return {
            'data': {'error': 'The proposal details are incomplete or invalid. Ask for the missing details.'},
            'is_error': True,
        }
    except HTTPException as exc:
        db.rollback()
        return {'data': {'error': exc.detail}, 'is_error': True}
    except Exception:
        db.rollback()
        raise
    return {
        'data': {'task_id': task_id, 'status': task_status, 'preview': preview, 'requires_review': True},
        'summary': 'prepared a task for your review; no ERP record changed',
        'references': [
            {'type': 'hank_task', 'id': task_id, 'label': f'Review: {task_title}', 'url': f'/?hank_task={task_id}'}
        ],
    }


def _proposal_schema():
    # Anthropic requires an object at the tool schema root and rejects root
    # oneOf/anyOf/allOf. Keep the alternatives on the input property; the
    # kind-specific binding is enforced by HankTaskCreate before saving a task.
    definitions = {}
    variants = []
    for kind, model in INPUT_SCHEMAS.items():
        schema = model.model_json_schema(ref_template='#/$defs/{model}')
        definitions.update(schema.pop('$defs', {}))
        definitions[model.__name__] = schema
        variants.append({'$ref': f'#/$defs/{model.__name__}', 'description': f'Fields for kind={kind}.'})
    return {
        'type': 'object',
        'properties': {
            'kind': {'type': 'string', 'enum': list(INPUT_SCHEMAS)},
            'input': {
                'type': 'object',
                'description': 'Use the variant for the selected kind. Ask for missing facts.',
                'anyOf': variants,
            },
        },
        'required': ['kind', 'input'],
        'additionalProperties': False,
        '$defs': definitions,
    }


TASK_INPUT_SCHEMA = _proposal_schema()


def operational_report(*, db, company_id, user, report, record_id=None, trace_value=None):
    from app.services.hank_operations_service import HankOperationsService

    service = HankOperationsService(db, user, company_id)
    if report in ('lot', 'serial'):
        if not isinstance(trace_value, str) or not 1 <= len(trace_value.strip()) <= 100:
            return {'data': {'error': 'Provide the exact lot or serial identifier.'}, 'is_error': True}
        result = service.trace(report, trace_value.strip())
    else:
        if type(record_id) is not int or record_id <= 0:
            return {'data': {'error': 'Look up the exact work order or PO first.'}, 'is_error': True}
        methods = {
            'readiness': service.readiness,
            'knowledge': service.knowledge,
            'shipping_packet': service.shipping_packet,
            'purchasing_impact': service.impact,
        }
        if report not in methods:
            return {'data': {'error': 'Unsupported operational report.'}, 'is_error': True}
        result = methods[report](record_id)
    return {
        'data': result.model_dump(mode='json'),
        'summary': result.summary,
        'references': [ref.model_dump() for check in result.checks for ref in check.references],
    }


def action_context(*, db, company_id, user, kind, purchase_order_id=None):
    from app.db.tenant_filter import tenant_query
    from app.models.part import Part
    from app.models.purchasing import PurchaseOrder, PurchaseOrderLine
    from app.models.time_entry import TimeEntry
    from app.models.work_order import WorkOrder, WorkOrderOperation
    from app.services.hank_operations_service import HankOperationsService

    service = HankOperationsService(db, user, company_id)
    if kind == 'active_job':
        service.require('work_orders:view')
        rows = (
            tenant_query(db, TimeEntry, company_id)
            .join(WorkOrderOperation, WorkOrderOperation.id == TimeEntry.operation_id)
            .join(WorkOrder, WorkOrder.id == WorkOrderOperation.work_order_id)
            .filter(
                TimeEntry.user_id == user.id,
                TimeEntry.clock_out.is_(None),
                WorkOrderOperation.company_id == company_id,
                WorkOrder.company_id == company_id,
                WorkOrder.is_deleted.is_(False),
            )
            .order_by(TimeEntry.id.desc())
            .limit(11)
            .all()
        )
        entries = []
        for entry in rows[:10]:
            op = entry.operation
            job = op.work_order
            entries.append(
                {
                    'time_entry_id': entry.id,
                    'operation_id': op.id,
                    'work_order_id': job.id,
                    'work_order_number': job.work_order_number,
                    'operation_name': op.name,
                    'status': getattr(op.status, 'value', op.status),
                }
            )
        return {
            'data': {
                'active_jobs': entries,
                'has_more': len(rows) > 10,
                'instruction': 'Ask for the good/scrap deltas and inspection/hold choices; do not infer them.',
            },
            'summary': 'checked your own open job clocks',
            'references': [
                {
                    'type': 'work_order',
                    'id': item['work_order_id'],
                    'label': item['work_order_number'],
                    'url': f'/work-orders/{item["work_order_id"]}',
                }
                for item in entries
            ],
        }
    if kind != 'receiving' or type(purchase_order_id) is not int or purchase_order_id <= 0:
        return {'data': {'error': 'Choose active_job or receiving with an exact PO id.'}, 'is_error': True}
    service.require('purchasing:view', 'receiving:view')
    po = (
        tenant_query(db, PurchaseOrder, company_id)
        .filter(PurchaseOrder.id == purchase_order_id, PurchaseOrder.is_deleted.is_(False))
        .first()
    )
    if not po:
        raise HTTPException(404, 'Purchase order not found')
    rows = (
        tenant_query(db, PurchaseOrderLine, company_id)
        .filter(PurchaseOrderLine.purchase_order_id == po.id)
        .order_by(PurchaseOrderLine.id)
        .limit(51)
        .all()
    )
    parts = {
        part.id: part
        for part in tenant_query(db, Part, company_id).filter(Part.id.in_([row.part_id for row in rows])).all()
    }
    lines = [
        {
            'po_line_id': row.id,
            'part_id': row.part_id,
            'part_number': parts[row.part_id].part_number if row.part_id in parts else None,
            'quantity_ordered': row.quantity_ordered,
            'quantity_received': row.quantity_received,
            'notes': (row.notes or '')[:1000],
        }
        for row in rows[:50]
    ]
    return {
        'data': {
            'purchase_order_id': po.id,
            'po_number': po.po_number,
            'lines': lines,
            'has_more': len(rows) > 50,
            'instruction': 'Ask for actual delivery quantities, locations, lot/heat and whether inspection is required for EACH line. Do not infer received quantities from ordered quantities.',
        },
        'summary': 'checked PO lines for receiving',
        'references': [
            {'type': 'purchase_order', 'id': po.id, 'label': po.po_number, 'url': f'/purchasing?po={po.id}'}
        ],
    }


def saved_work(*, db, company_id, user, kind='queue', record_id=None):
    from app.services.hank_intake_service import HankIntakeService
    from app.services.hank_task_service import HankTaskService
    from app.services.hank_teamwork_service import HankTeamworkService
    from app.services.hank_work_queue import work_queue

    if kind == 'queue':
        result = work_queue(db, user, company_id)
        return {
            'data': result.model_dump(mode='json'),
            'summary': 'checked your saved work queue',
            'references': [
                {'type': item.kind, 'id': item.id, 'label': item.title, 'url': item.url} for item in result.items
            ],
        }
    if type(record_id) is not int or record_id <= 0:
        return {
            'data': {'error': 'Provide the exact saved task, intake file, handoff or routine-run id.'},
            'is_error': True,
        }
    teamwork = HankTeamworkService(db, user, company_id)
    if kind == 'task':
        tasks = HankTaskService(db, user, company_id)
        result = tasks.response(tasks.get(record_id))
        url = f'/?hank_task={record_id}'
    elif kind == 'intake':
        intake = HankIntakeService(db, user, company_id)
        result = intake.response_file(intake.file(record_id))
        url = f'/?hank_work=intake&hank_id={record_id}'
    elif kind == 'handoff':
        result = teamwork.handoff_response(teamwork.get_handoff(record_id))
        url = f'/?hank_work=handoff&hank_id={record_id}'
    elif kind == 'routine':
        result = teamwork.run_response(teamwork.get_run(record_id))
        url = f'/?hank_work=routine&hank_id={record_id}'
    else:
        return {'data': {'error': 'Unsupported saved-work type.'}, 'is_error': True}
    return {
        'data': result.model_dump(mode='json'),
        'summary': 'checked the saved record and current status',
        'references': [{'type': kind, 'id': record_id, 'label': f'Open {kind} {record_id}', 'url': url}],
    }
