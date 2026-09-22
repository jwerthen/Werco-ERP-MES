"""Bounded, read-only view of the employee's actual saved Hank work."""

from datetime import datetime, timezone

from fastapi import HTTPException

from app.db.tenant_filter import tenant_query
from app.models.hank import HankTask
from app.models.hank_intake import HankIntakeBatch, HankIntakeFile
from app.models.hank_teamwork import HankHandoff, HankRoutineRun
from app.schemas.hank_teamwork import WorkQueue, WorkQueueItem
from app.services.hank_intake_service import HankIntakeService
from app.services.hank_task_service import HankTaskService
from app.services.hank_teamwork_service import HankTeamworkService

LIMIT = 50


def work_queue(db, user, company_id, state=None):
    """Filter each source before its cap; counts are not represented as exhaustive."""
    teamwork = HankTeamworkService(db, user, company_id)
    teamwork._require()
    items, truncated = [], False

    def append(query, mapper):
        nonlocal truncated
        rows = query.limit(LIMIT + 1).all()
        truncated = truncated or len(rows) > LIMIT
        for row in rows[:LIMIT]:
            entry = mapper(row)
            if entry and (state is None or entry['state'] == state):
                items.append(WorkQueueItem.model_validate(entry))

    tasks = HankTaskService(db, user, company_id)
    task_states = {
        'working': [],
        'waiting_on_you': ['awaiting_review', 'needs_attention'],
        'waiting_on_other': ['watching', 'snoozed'],
        'finished': ['completed', 'cancelled'],
    }
    query = tasks._query().filter(HankTask.kind.in_(tasks._allowed_kinds(write=False)))
    if state:
        query = query.filter(HankTask.status.in_(task_states[state]))
    append(
        query.order_by(HankTask.updated_at.desc(), HankTask.id.desc()),
        lambda row: {
            'key': f'task:{row.id}',
            'kind': 'task',
            'id': row.id,
            'title': row.title,
            'state': next(key for key, statuses in task_states.items() if row.status in statuses),
            'status': row.status,
            'url': f'/?hank_task={row.id}',
            'updated_at': row.updated_at,
        },
    )
    query = teamwork._handoffs()
    if state == 'working':
        query = query.filter(HankHandoff.id < 0)
    elif state == 'finished':
        query = query.filter(HankHandoff.status.in_(['completed', 'cancelled']))
    elif state in ('waiting_on_you', 'waiting_on_other'):
        query = query.filter(HankHandoff.status.in_(['open', 'acknowledged']))
        query = query.filter(
            HankHandoff.recipient_id == user.id if state == 'waiting_on_you' else HankHandoff.sender_id == user.id
        )
    append(
        query.order_by(HankHandoff.updated_at.desc(), HankHandoff.id.desc()),
        lambda row: {
            'key': f'handoff:{row.id}',
            'kind': 'handoff',
            'id': row.id,
            'title': f'Handoff: {row.work_order_number}',
            'state': (
                'finished'
                if row.status in ('completed', 'cancelled')
                else ('waiting_on_you' if row.recipient_id == user.id else 'waiting_on_other')
            ),
            'status': row.status,
            'url': f'/?hank_work=handoff&hank_id={row.id}',
            'updated_at': row.updated_at,
        },
    )
    query = tenant_query(db, HankRoutineRun, company_id).filter(HankRoutineRun.owner_id == user.id)
    if 'purchasing:view' not in tasks._permissions():
        # JSON path compilation is supported by both our production Postgres and SQLite test backend.
        query = query.filter(HankRoutineRun.context_json['purchase_order_id'].as_integer().is_(None))
    if state == 'finished':
        query = query.filter(HankRoutineRun.status.in_(['completed', 'cancelled']))
    elif state == 'waiting_on_you':
        query = query.filter(HankRoutineRun.status == 'active')
    elif state:
        query = query.filter(HankRoutineRun.id < 0)
    append(
        query.order_by(HankRoutineRun.updated_at.desc(), HankRoutineRun.id.desc()),
        lambda row: {
            'key': f'routine:{row.id}',
            'kind': 'routine',
            'id': row.id,
            'title': row.snapshot_json['title'],
            'state': 'waiting_on_you' if row.status == 'active' else 'finished',
            'status': row.status,
            'url': f'/?hank_work=routine&hank_id={row.id}',
            'updated_at': row.updated_at,
        },
    )
    intake = HankIntakeService(db, user, company_id)
    try:
        intake.authority()
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
    else:
        intake_states = {
            'working': ['queued', 'analyzing'],
            'waiting_on_you': ['awaiting_review', 'planned', 'failed'],
            'waiting_on_other': [],
            'finished': ['completed', 'cancelled'],
        }
        query = (
            tenant_query(db, HankIntakeFile, company_id)
            .join(HankIntakeBatch, HankIntakeFile.batch_id == HankIntakeBatch.id)
            .filter(
                HankIntakeBatch.company_id == company_id,
                HankIntakeBatch.owner_id == user.id,
                HankIntakeBatch.credential_key == 'user',
            )
        )
        if state:
            query = query.filter(HankIntakeFile.status.in_(intake_states[state]))
        append(
            query.order_by(HankIntakeFile.updated_at.desc(), HankIntakeFile.id.desc()),
            lambda row: {
                'key': f'intake:{row.id}',
                'kind': 'intake',
                'id': row.id,
                'title': row.filename,
                'state': next(key for key, statuses in intake_states.items() if row.status in statuses),
                'status': row.status,
                'url': f'/?hank_work=intake&hank_id={row.id}',
                'updated_at': row.updated_at,
            },
        )
    items.sort(
        key=lambda item: (
            item.updated_at.replace(tzinfo=timezone.utc) if item.updated_at.tzinfo is None else item.updated_at,
            item.key,
        ),
        reverse=True,
    )
    return WorkQueue(checked_at=datetime.now(timezone.utc), items=items, truncated=truncated)
