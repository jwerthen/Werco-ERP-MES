"""Hank's ``change_due_date`` and ``add_note`` task kinds, and the fall-through guard.

Owner decisions pinned here (2026-11-16):

* ``add_note`` posts a WORK-ORDER COMMENT through the comment service -- never ``WorkOrder.notes``.
  The submitter is the author, the create audit row carries ``via: hank`` and is REQUIRED (503,
  nothing written, when it cannot be saved), mentions are validated when the note is prepared and
  again when it is submitted, and the note text never reaches a notification.
* Comment writes are PEOPLE-ONLY: ANY API token (full, read-only, AI-external) is refused 403 at
  prepare, a token's capabilities never offer ``add_note``, and a token cannot execute one.
* ``change_due_date`` moves ``WorkOrder.due_date`` ONLY, never ``must_ship_by``. It shares the
  PUT's terminal-status rule (one implementation, the exact text), refuses a no-op, needs a
  reason, writes a REQUIRED audit row carrying the reason and ``via: hank``, and emits
  ``work_order_updated`` in the PUT's shape with no reason in it. Its snapshot is NARROW, so a
  dispatch-plan rank write between prepare and execute does not refuse it. A FULL API token may
  run it (parity with the update_work_order agent door); read-only and AI-external tokens are
  refused by ``get_current_user`` like every other write.
* Every kind in ``INPUT_SCHEMAS`` has its OWN preview and execute branch: nothing falls through
  to ``attach_document`` any more, and an unregistered branch is a clean 400.
* ``PUT /work-orders/{id}`` keeps its behavior, now judging the due date through the shared helper,
  and a soft-deleted work order is a 404 there.
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Dict, List, Set, Tuple
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

import app.services.notification_dispatch as dispatch
from app.core.credentials import API_TOKEN_COMMENT_WRITE, READ_ONLY_ROUTE_WRITES_DETAIL
from app.models.audit_log import AuditLog
from app.models.comment import Comment, CommentMention, EntityWatcher
from app.models.hank import HankTask
from app.models.notification import Notification, NotificationLog
from app.models.operational_event import OperationalEvent
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.schemas.hank_tasks import INPUT_SCHEMAS, AttachDocumentInput, HankTaskCommand, HankTaskPreview
from app.services import hank_task_service
from app.services.audit_service import AuditService
from app.services.dispatch_plan_service import work_order_rank_update_statements
from app.services.hank_task_service import (
    ACTION_PERMISSIONS,
    ACTION_ROLES,
    PEOPLE_ONLY_KINDS,
    UNSUPPORTED_KIND_DETAIL,
    HankTaskService,
)
from app.services.notification_catalog import CATALOG, CHANNEL_EMAIL
from app.services.work_order_due_date import due_date_change_refusal
from tests.api.export_control_helpers import bearer, issue_token
from tests.api.kiosk_test_helpers import COMPANY_B, make_user, user_headers

pytestmark = pytest.mark.api

BASE = '/api/v1/hank'
SECRET = 'Customer called: the 0.375 bore is undersize, hold the rework until Friday'
MISSING = 987654
TERMINAL_TEXT = (
    "cannot change the due date of a work order in terminal status 'complete': its due date is the promise "
    "date its delivery performance was scored against."
)


def propose(client, headers, kind, input_data, key=None, company=1):
    return client.post(
        BASE + '/tasks',
        headers=headers,
        json={'expected_company_id': company, 'request_key': key or str(uuid4()), 'kind': kind, 'input': input_data},
    )


def execute(client, headers, task, version=None):
    return client.post(
        f'{BASE}/tasks/{task["id"]}/execute',
        headers=headers,
        json={'expected_company_id': 1, 'expected_version': task['version'] if version is None else version},
    )


def due_input(work_order, new_due=None, reason='Customer moved the delivery slot'):
    new_due = new_due or (work_order.due_date + timedelta(days=7))
    return {'work_order_id': work_order.id, 'new_due_date': new_due.isoformat(), 'reason': reason}


def note_input(work_order, body=SECRET, mentions=None):
    return {'work_order_id': work_order.id, 'body': body, 'mentioned_user_ids': mentions or []}


def events(db: Session, event_type: str) -> List[OperationalEvent]:
    db.expire_all()
    return db.query(OperationalEvent).filter_by(event_type=event_type).order_by(OperationalEvent.id).all()


@pytest.fixture
def crew(db_session: Session) -> Dict[str, User]:
    return {
        'mentioned': make_user(db_session, role=UserRole.SUPERVISOR, first_name='Blake', last_name='Marrow'),
        'watcher': make_user(db_session, role=UserRole.VIEWER, first_name='Casey', last_name='Fernhill'),
        'viewer': make_user(db_session, role=UserRole.VIEWER, first_name='Dana', last_name='Holloway'),
        'inactive': make_user(
            db_session, role=UserRole.OPERATOR, first_name='Gray', last_name='Dormant', is_active=False
        ),
        'foreign': make_user(db_session, company_id=COMPANY_B, first_name='Harper', last_name='Elsewhere'),
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_both_kinds_are_registered_in_every_map(self):
        from typing import get_args

        from app.schemas.hank_tasks import HankActionKind, HankTaskKind

        for kind in ('change_due_date', 'add_note'):
            assert kind in ACTION_ROLES and kind in ACTION_PERMISSIONS and kind in INPUT_SCHEMAS
            assert kind in get_args(HankActionKind) and kind in get_args(HankTaskKind)
        # _allowed_kinds indexes ACTION_PERMISSIONS[kind] for every ACTION_ROLES key: a gap is a 500.
        assert set(ACTION_ROLES) == set(ACTION_PERMISSIONS) == set(INPUT_SCHEMAS)
        assert ACTION_ROLES['change_due_date'] == {UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR}
        assert ACTION_PERMISSIONS['change_due_date'] == {'work_orders:view', 'work_orders:edit'}
        assert ACTION_ROLES['add_note'] == {
            UserRole.ADMIN,
            UserRole.MANAGER,
            UserRole.SUPERVISOR,
            UserRole.OPERATOR,
            UserRole.QUALITY,
            UserRole.SHIPPING,
            UserRole.VIEWER,
        }
        assert ACTION_PERMISSIONS['add_note'] == {'work_orders:view'}
        assert PEOPLE_ONLY_KINDS == {'add_note'}

    @pytest.mark.parametrize('role', [role for role in UserRole if role != UserRole.PLATFORM_ADMIN])
    def test_capabilities_per_role(self, client, db_session, role):
        user = make_user(db_session, role=role)
        capabilities = client.get(BASE + '/capabilities', headers=user_headers(user)).json()
        assert 'add_note' in capabilities['allowed_kinds']
        assert capabilities['can_write'] is True
        assert ('change_due_date' in capabilities['allowed_kinds']) == (
            role in (UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR)
        )

    def test_a_viewers_first_hank_write_is_a_note(self, client, db_session, test_work_order, crew):
        headers = user_headers(crew['viewer'])
        capabilities = client.get(BASE + '/capabilities', headers=headers).json()
        assert capabilities['allowed_kinds'] == ['add_note'] and capabilities['can_write'] is True
        assert propose(client, headers, 'change_due_date', due_input(test_work_order)).status_code == 403
        task = propose(client, headers, 'add_note', note_input(test_work_order, body='Seen on the floor.'))
        assert task.status_code == 200, task.text
        done = execute(client, headers, task.json())
        assert done.status_code == 200, done.text
        assert db_session.query(Comment).one().author_id == crew['viewer'].id

    def test_inputs_are_strict(self, client, auth_headers, test_work_order):
        bad = [
            ('change_due_date', {**due_input(test_work_order), 'must_ship_by': '2027-01-01'}),
            ('change_due_date', {**due_input(test_work_order), 'new_due_date': None}),
            ('change_due_date', {k: v for k, v in due_input(test_work_order).items() if k != 'reason'}),
            ('change_due_date', due_input(test_work_order, reason='   ')),
            ('change_due_date', due_input(test_work_order, reason='x' * 501)),
            ('change_due_date', {**due_input(test_work_order), 'work_order_id': 0}),
            ('add_note', {**note_input(test_work_order), 'notes': 'x'}),
            ('add_note', note_input(test_work_order, body='   ')),
            ('add_note', note_input(test_work_order, body='x' * 5001)),
            ('add_note', note_input(test_work_order, mentions=[1, 1])),
            ('add_note', note_input(test_work_order, mentions=list(range(1, 22)))),
            ('add_note', {**note_input(test_work_order), 'mentioned_user_ids': [True]}),
        ]
        for kind, data in bad:
            assert propose(client, auth_headers, kind, data).status_code == 422, (kind, data)
        stored = propose(client, auth_headers, 'change_due_date', due_input(test_work_order, reason='  Slot moved  '))
        assert stored.json()['input']['reason'] == 'Slot moved'


# ---------------------------------------------------------------------------
# The fall-through trap
# ---------------------------------------------------------------------------

SAMPLE_INPUTS = {
    'repeat_job': {'source_work_order_id': MISSING, 'quantity_ordered': 1},
    'draft_purchase_order': {
        'vendor_id': MISSING,
        'lines': [{'part_id': MISSING, 'quantity_ordered': 1, 'unit_price': 1}],
    },
    'attach_document': {'document_id': MISSING, 'work_order_id': MISSING},
    'receive_delivery': {
        'purchase_order_id': MISSING,
        'lines': [{'po_line_id': MISSING, 'quantity_received': 1, 'requires_inspection': False}],
    },
    'report_production': {'operation_id': MISSING, 'quantity_complete_delta': 1},
    'draft_shipment': {'work_order_id': MISSING, 'quantity_shipped': 1},
    'change_due_date': {'work_order_id': MISSING, 'new_due_date': '2026-12-01', 'reason': 'Slot moved'},
    'add_note': {'work_order_id': MISSING, 'body': 'Check the fixture.'},
}


class _FellThrough(Exception):
    pass


class TestEveryKindHasItsOwnBranch:
    """attach_document used to be the IMPLICIT last branch of both _preview and execute, so a kind
    registered without a branch ran the attach path and 500ed on data['document_id']."""

    def test_every_registered_kind_has_a_sample(self):
        # A new kind fails here until it is added to SAMPLE_INPUTS -- and so to the guards below.
        assert set(SAMPLE_INPUTS) == set(INPUT_SCHEMAS)
        for kind, data in SAMPLE_INPUTS.items():
            INPUT_SCHEMAS[kind].model_validate(data)

    def test_preview_never_falls_through_to_attach_document(self, db_session, test_user, monkeypatch):
        def attach_preview(self, data, *, locked=False):
            raise _FellThrough('attach_document preview')

        monkeypatch.setattr(HankTaskService, '_attach_document_preview', attach_preview)
        service = HankTaskService(db_session, test_user, 1)
        for kind in INPUT_SCHEMAS:
            data = INPUT_SCHEMAS[kind].model_validate(SAMPLE_INPUTS[kind]).model_dump(mode='json')
            reached_attach = False
            try:
                service._preview(kind, data)
            except _FellThrough:
                reached_attach = True
            except HTTPException as exc:
                assert exc.detail != UNSUPPORTED_KIND_DETAIL, kind
            db_session.rollback()
            assert reached_attach == (kind == 'attach_document'), kind

    def test_execute_never_falls_through_to_attach_document(self, db_session, test_user, monkeypatch):
        def attach_command(*args, **kwargs):
            raise _FellThrough('attach_document execute')

        monkeypatch.setattr(hank_task_service, 'attach_document_command', attach_command)
        # Reach the branch chain for every kind: the gates and the source check are not under test.
        monkeypatch.setattr(HankTaskService, '_allowed_kinds', lambda self, write=True: list(INPUT_SCHEMAS))
        monkeypatch.setattr(
            HankTaskService, '_preview', lambda self, kind, data, locked=False: ('t', HankTaskPreview(summary='s'), {})
        )
        for kind in INPUT_SCHEMAS:
            task = _task_row(db_session, test_user, kind, SAMPLE_INPUTS[kind])
            service = HankTaskService(db_session, test_user, 1)
            reached_attach = False
            try:
                service.execute(
                    task.id, HankTaskCommand(expected_company_id=1, expected_version=1), _audit(db_session, test_user)
                )
            except _FellThrough:
                reached_attach = True
            except HTTPException as exc:
                assert exc.detail != UNSUPPORTED_KIND_DETAIL, kind
            db_session.rollback()
            assert reached_attach == (kind == 'attach_document'), kind

    def test_a_kind_registered_without_a_branch_is_a_clean_400_in_both_places(self, db_session, test_user, monkeypatch):
        monkeypatch.setitem(hank_task_service.INPUT_SCHEMAS, 'unbranched_kind', AttachDocumentInput)
        service = HankTaskService(db_session, test_user, 1)
        with pytest.raises(HTTPException) as preview_refusal:
            service._preview('unbranched_kind', {'document_id': MISSING, 'work_order_id': MISSING})
        assert (preview_refusal.value.status_code, preview_refusal.value.detail) == (400, UNSUPPORTED_KIND_DETAIL)

        # HankCapabilities' Literal rejects an unregistered kind, so skip the capability gate itself.
        monkeypatch.setattr(HankTaskService, '_allowed_kinds', lambda self, write=True: ['unbranched_kind'])
        monkeypatch.setattr(HankTaskService, '_require_action', lambda self, kind: None)
        monkeypatch.setattr(
            HankTaskService, '_preview', lambda self, kind, data, locked=False: ('t', HankTaskPreview(summary='s'), {})
        )
        task = _task_row(db_session, test_user, 'unbranched_kind', {'document_id': MISSING, 'work_order_id': MISSING})
        with pytest.raises(HTTPException) as execute_refusal:
            service.execute(
                task.id, HankTaskCommand(expected_company_id=1, expected_version=1), _audit(db_session, test_user)
            )
        assert (execute_refusal.value.status_code, execute_refusal.value.detail) == (400, UNSUPPORTED_KIND_DETAIL)


def _audit(db, user):
    return AuditService(db, user=user, company_id=1)


def _task_row(db, owner, kind, input_json, *, credential_key='user'):
    from datetime import datetime

    row = HankTask(
        company_id=1,
        owner_id=owner.id,
        credential_key=credential_key,
        request_key=str(uuid4()),
        request_hash='a' * 64,
        kind=kind,
        title=f'Guard {kind}',
        status='awaiting_review',
        version=1,
        input_json=input_json,
        preview_json={'summary': 's', 'changes': [], 'warnings': [], 'references': []},
        source_versions_json={},
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(row)
    db.commit()
    return row


def _foreign_work_order(db) -> WorkOrder:
    from app.models.part import Part
    from tests.api.kiosk_test_helpers import ensure_company

    ensure_company(db, COMPANY_B)
    part = Part(
        company_id=COMPANY_B,
        part_number='FOREIGN-HANK-P',
        name='Other shop bracket',
        part_type='manufactured',
        unit_of_measure='each',
        is_active=True,
    )
    db.add(part)
    db.flush()
    work_order = WorkOrder(
        company_id=COMPANY_B,
        part_id=part.id,
        work_order_number='WO-FOREIGN-HANK',
        quantity_ordered=2,
        status='draft',
        due_date=date.today() + timedelta(days=20),
    )
    db.add(work_order)
    db.commit()
    return work_order


# ---------------------------------------------------------------------------
# change_due_date
# ---------------------------------------------------------------------------


class TestChangeDueDate:
    def test_preview_names_old_and_new_dates_the_reason_and_the_unmoved_promise_date(
        self, client, db_session, auth_headers, test_work_order
    ):
        old_due = test_work_order.due_date
        test_work_order.must_ship_by = old_due + timedelta(days=3)
        db_session.commit()
        new_due = old_due + timedelta(days=10)
        response = propose(client, auth_headers, 'change_due_date', due_input(test_work_order, new_due))
        assert response.status_code == 200, response.text
        task = response.json()
        number = test_work_order.work_order_number
        assert task['title'] == f'Move due date of {number}'
        assert task['preview']['changes'] == [
            f'Due date: {old_due.isoformat()} → {new_due.isoformat()}.',
            'Reason: Customer moved the delivery slot',
        ]
        warnings = ' '.join(task['preview']['warnings'])
        assert f'must ship by {test_work_order.must_ship_by.isoformat()}' in warnings
        assert 'does not move' in warnings and 'precedence' in warnings
        assert task['preview']['references'][0]['url'] == f'/work-orders/{test_work_order.id}'
        # Prepared, not applied.
        db_session.refresh(test_work_order)
        assert test_work_order.due_date == old_due

    def test_an_unset_due_date_previews_as_not_set_and_no_promise_warning_without_must_ship_by(
        self, client, db_session, auth_headers, test_work_order
    ):
        test_work_order.due_date = None
        db_session.commit()
        task = propose(
            client, auth_headers, 'change_due_date', due_input(test_work_order, date.today() + timedelta(days=5))
        ).json()
        assert task['preview']['changes'][0].startswith('Due date: not set → ')
        assert 'promise date' not in ' '.join(task['preview']['warnings'])

    def test_a_no_op_is_refused_409_naming_the_job_and_date(self, client, db_session, auth_headers, test_work_order):
        response = propose(
            client, auth_headers, 'change_due_date', due_input(test_work_order, test_work_order.due_date)
        )
        assert response.status_code == 409
        assert test_work_order.work_order_number in response.json()['detail']
        assert test_work_order.due_date.isoformat() in response.json()['detail']
        assert db_session.query(HankTask).count() == 0

    def test_terminal_status_is_refused_with_the_puts_exact_text_at_prepare(
        self, client, db_session, auth_headers, test_work_order
    ):
        test_work_order.status = WorkOrderStatus.COMPLETE
        db_session.commit()
        response = propose(client, auth_headers, 'change_due_date', due_input(test_work_order))
        assert response.status_code == 409
        assert response.json()['detail'] == TERMINAL_TEXT
        assert db_session.query(HankTask).count() == 0

    def test_terminal_status_is_refused_with_the_exact_text_at_execute(
        self, client, db_session, auth_headers, test_work_order
    ):
        task = propose(client, auth_headers, 'change_due_date', due_input(test_work_order)).json()
        old_due = test_work_order.due_date
        test_work_order.status = WorkOrderStatus.COMPLETE
        db_session.commit()
        response = execute(client, auth_headers, task)
        assert response.status_code == 409
        assert response.json()['detail'] == TERMINAL_TEXT
        db_session.refresh(test_work_order)
        assert test_work_order.due_date == old_due
        assert db_session.get(HankTask, task['id']).status == 'awaiting_review'

    def test_a_deleted_or_foreign_work_order_is_404_at_prepare_and_execute(
        self, client, db_session, auth_headers, test_work_order
    ):
        foreign = _foreign_work_order(db_session)
        assert propose(client, auth_headers, 'change_due_date', due_input(foreign)).status_code == 404
        assert db_session.get(WorkOrder, foreign.id).due_date == foreign.due_date
        task = propose(client, auth_headers, 'change_due_date', due_input(test_work_order)).json()
        test_work_order.soft_delete(None)
        db_session.commit()
        refused = execute(client, auth_headers, task)
        assert refused.status_code == 404 and refused.json()['detail'] == 'Work order not found'
        again = propose(client, auth_headers, 'change_due_date', due_input(test_work_order))
        assert again.status_code == 404

    def test_execute_moves_due_date_only_with_required_audit_and_a_reasonless_event(
        self, client, db_session, auth_headers, test_work_order, test_user
    ):
        test_work_order.must_ship_by = test_work_order.due_date + timedelta(days=2)
        test_work_order.notes = 'Planner notes'
        db_session.commit()
        db_session.refresh(test_work_order)
        before = {c.key: getattr(test_work_order, c.key) for c in WorkOrder.__table__.columns}
        new_due = test_work_order.due_date + timedelta(days=12)
        task = propose(client, auth_headers, 'change_due_date', due_input(test_work_order, new_due)).json()
        response = execute(client, auth_headers, task)
        assert response.status_code == 200, response.text
        result = response.json()
        number = test_work_order.work_order_number
        assert result['status'] == 'completed'
        assert result['result']['summary'] == (
            f'Moved the due date of {number} from {before["due_date"].isoformat()} to {new_due.isoformat()}.'
        )
        assert result['result']['references'][0]['url'] == f'/work-orders/{test_work_order.id}'

        db_session.expire_all()
        after = {
            c.key: getattr(db_session.get(WorkOrder, test_work_order.id), c.key) for c in WorkOrder.__table__.columns
        }
        assert after['due_date'] == new_due
        assert after['must_ship_by'] == before['must_ship_by']
        assert after['version'] == before['version'] + 1
        changed = {key for key in before if before[key] != after[key]}
        assert changed <= {'due_date', 'version', 'updated_at'}

        (row,) = db_session.query(AuditLog).filter_by(resource_type='work_order', resource_id=test_work_order.id).all()
        assert row.action == 'UPDATE' and row.user_id == test_user.id
        assert row.old_values == {'due_date': before['due_date'].isoformat()}
        assert row.new_values == {'due_date': new_due.isoformat()}
        assert row.extra_data['reason'] == 'Customer moved the delivery slot'
        assert row.extra_data['via'] == 'hank'

        (event,) = events(db_session, 'work_order_updated')
        assert event.work_order_id == test_work_order.id and event.user_id == test_user.id
        assert event.event_payload == {
            'work_order_number': number,
            'status': 'draft',
            'updated_fields': ['due_date'],
            'operations_returned_to_pending': 0,
        }
        assert 'Customer moved' not in json.dumps(event.event_payload)
        assert set(event.event_payload).isdisjoint({key for key, _ in dispatch._DETAIL_KEYS} - {'status'})
        # The completed receipt replays without moving anything again.
        assert execute(client, auth_headers, task).json() == result
        assert len(events(db_session, 'work_order_updated')) == 1

    def test_the_event_has_the_puts_shape(self, client, db_session, auth_headers, test_work_order, test_part):
        other = WorkOrder(
            work_order_number='WO-HANK-PUT',
            part_id=test_part.id,
            quantity_ordered=3,
            status='draft',
            due_date=date.today() + timedelta(days=9),
            company_id=1,
        )
        db_session.add(other)
        db_session.commit()
        put = client.put(
            f'/api/v1/work-orders/{other.id}',
            headers=auth_headers,
            json={'version': other.version, 'due_date': (other.due_date + timedelta(days=1)).isoformat()},
        )
        assert put.status_code == 200, put.text
        task = propose(client, auth_headers, 'change_due_date', due_input(test_work_order)).json()
        assert execute(client, auth_headers, task).status_code == 200
        put_event, hank_event = events(db_session, 'work_order_updated')
        assert put_event.work_order_id == other.id and hank_event.work_order_id == test_work_order.id
        assert set(put_event.event_payload) == set(hank_event.event_payload)
        for key in ('status', 'updated_fields', 'operations_returned_to_pending'):
            assert put_event.event_payload[key] == hank_event.event_payload[key]
        assert (put_event.source_module, put_event.entity_type) == (hank_event.source_module, hank_event.entity_type)

    def test_a_lost_required_audit_is_503_and_the_date_does_not_move(
        self, client, db_session, auth_headers, test_work_order, monkeypatch
    ):
        task = propose(client, auth_headers, 'change_due_date', due_input(test_work_order)).json()
        old_due, old_version = test_work_order.due_date, test_work_order.version
        original = AuditService.log_update

        def lose_work_order_row(self, resource_type, *args, **kwargs):
            if resource_type == 'work_order':
                return None
            return original(self, resource_type, *args, **kwargs)

        monkeypatch.setattr(AuditService, 'log_update', lose_work_order_row)
        response = execute(client, auth_headers, task)
        assert response.status_code == 503, response.text
        db_session.expire_all()
        work_order = db_session.get(WorkOrder, test_work_order.id)
        assert (work_order.due_date, work_order.version) == (old_due, old_version)
        assert db_session.get(HankTask, task['id']).status == 'awaiting_review'
        assert events(db_session, 'work_order_updated') == []
        assert db_session.query(AuditLog).filter_by(resource_type='work_order').count() == 0

    def test_the_event_is_written_before_the_required_audit_row(
        self, client, db_session, auth_headers, test_work_order, monkeypatch
    ):
        """Lock order: the audit row takes the global audit-chain lock (held to commit), and the
        event insert's foreign-key checks take KEY SHARE on the company, user and work-order rows,
        so the event must be written first -- the same order as the comment service and the PUT."""
        task = propose(client, auth_headers, 'change_due_date', due_input(test_work_order)).json()
        calls: List[str] = []
        original_emit = hank_task_service.emit_due_date_changed_event
        original_log_update = AuditService.log_update

        def recording_emit(*args, **kwargs):
            calls.append('event')
            return original_emit(*args, **kwargs)

        def recording_log_update(self, resource_type, *args, **kwargs):
            if resource_type == 'work_order':
                calls.append('audit')
            return original_log_update(self, resource_type, *args, **kwargs)

        monkeypatch.setattr(hank_task_service, 'emit_due_date_changed_event', recording_emit)
        monkeypatch.setattr(AuditService, 'log_update', recording_log_update)
        response = execute(client, auth_headers, task)
        assert response.status_code == 200, response.text
        assert calls == ['event', 'audit']
        assert len(events(db_session, 'work_order_updated')) == 1
        assert db_session.query(AuditLog).filter_by(resource_type='work_order').count() == 1

    def test_the_snapshot_ignores_a_dispatch_rank_write_but_not_a_due_date_change(
        self, client, db_session, auth_headers, test_work_order
    ):
        task = propose(client, auth_headers, 'change_due_date', due_input(test_work_order)).json()
        # The dispatch plan's Core UPDATE bumps nothing (no version, no updated_at move).
        for statement in work_order_rank_update_statements(1, {test_work_order.id: 1}, []):
            db_session.execute(statement)
        db_session.commit()
        response = execute(client, auth_headers, task)
        assert response.status_code == 200, response.text
        db_session.expire_all()
        assert db_session.get(WorkOrder, test_work_order.id).dispatch_rank == 1

        second = propose(
            client, auth_headers, 'change_due_date', due_input(db_session.get(WorkOrder, test_work_order.id))
        )
        assert second.status_code == 200, second.text
        # A due-date write that bumps nothing either -- the digest still sees the date itself.
        db_session.execute(
            WorkOrder.__table__.update()
            .where(WorkOrder.id == test_work_order.id)
            .values(due_date=date.today() + timedelta(days=200))
        )
        db_session.commit()
        stale = execute(client, auth_headers, second.json())
        assert stale.status_code == 409
        assert stale.json()['detail'] == 'The source records changed. Prepare and review a fresh task.'

    def test_an_orm_edit_of_the_work_order_refuses_execute(self, client, db_session, auth_headers, test_work_order):
        task = propose(client, auth_headers, 'change_due_date', due_input(test_work_order)).json()
        test_work_order.priority = 1  # any ORM edit bumps version
        db_session.commit()
        assert execute(client, auth_headers, task).status_code == 409

    def test_an_operator_cannot_prepare_it(self, client, db_session, operator_headers, test_work_order):
        response = propose(client, operator_headers, 'change_due_date', due_input(test_work_order))
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# add_note
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self) -> None:
        self.keys: Dict[str, str] = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True


@pytest.fixture
def redis(monkeypatch) -> _FakeRedis:
    fake = _FakeRedis()

    async def pool():
        return fake

    monkeypatch.setattr(dispatch, 'get_redis_pool', pool)
    monkeypatch.setattr(dispatch, 'enqueue_job', AsyncMock())
    return fake


class TestAddNote:
    def test_preview_and_execute_post_exactly_one_comment_by_the_submitter(
        self, client, db_session, auth_headers, test_work_order, test_user, crew
    ):
        notes_before = test_work_order.notes
        response = propose(
            client, auth_headers, 'add_note', note_input(test_work_order, mentions=[crew['mentioned'].id])
        )
        assert response.status_code == 200, response.text
        task = response.json()
        number = test_work_order.work_order_number
        assert task['title'] == f'Add a note to {number}'
        assert task['preview']['changes'] == [f'Note: {SECRET}', 'Mentions: Blake Marrow.']
        assert (
            'Mentioned people are notified in the app and by email; the email does not include the note text.'
            in task['preview']['warnings']
        )
        # Prepared, nothing posted, nobody watching yet.
        assert db_session.query(Comment).count() == 0
        assert db_session.query(EntityWatcher).count() == 0

        done = execute(client, auth_headers, task)
        assert done.status_code == 200, done.text
        assert done.json()['result']['summary'] == f'Added a note to {number}.'
        assert done.json()['result']['references'][0]['url'] == f'/work-orders/{test_work_order.id}'
        db_session.expire_all()
        (comment,) = db_session.query(Comment).all()
        assert (comment.entity_type, comment.entity_id) == ('work_order', test_work_order.id)
        assert comment.author_id == test_user.id and comment.body == SECRET
        assert [row.user_id for row in db_session.query(CommentMention).all()] == [crew['mentioned'].id]
        # Never WorkOrder.notes.
        assert db_session.get(WorkOrder, test_work_order.id).notes == notes_before
        (audit_row,) = db_session.query(AuditLog).filter_by(resource_type='comment').all()
        assert audit_row.action == 'CREATE' and audit_row.user_id == test_user.id
        assert audit_row.extra_data['via'] == 'hank'
        assert audit_row.extra_data['entity_id'] == test_work_order.id
        # Replay returns the receipt without a second comment.
        assert execute(client, auth_headers, task).json() == done.json()
        assert db_session.query(Comment).count() == 1

    def test_mentions_are_validated_at_prepare(self, client, db_session, auth_headers, test_work_order, crew):
        for refused in (crew['inactive'], crew['foreign']):
            response = propose(client, auth_headers, 'add_note', note_input(test_work_order, mentions=[refused.id]))
            assert response.status_code == 422
            assert f'user {refused.id}' in response.json()['detail']
            assert refused.last_name not in response.json()['detail']
        assert db_session.query(HankTask).count() == 0

    def test_mentions_are_validated_again_at_execute(self, client, db_session, auth_headers, test_work_order, crew):
        task = propose(
            client, auth_headers, 'add_note', note_input(test_work_order, mentions=[crew['mentioned'].id])
        ).json()
        crew['mentioned'].is_active = False
        db_session.commit()
        response = execute(client, auth_headers, task)
        assert response.status_code == 422
        assert db_session.query(Comment).count() == 0
        assert db_session.get(HankTask, task['id']).status == 'awaiting_review'

    def test_a_deleted_or_foreign_work_order_is_404(self, client, db_session, auth_headers, test_work_order):
        foreign = _foreign_work_order(db_session)
        refused = propose(client, auth_headers, 'add_note', note_input(foreign))
        assert refused.status_code == 404 and refused.json()['detail'] == 'Work order not found'
        task = propose(client, auth_headers, 'add_note', note_input(test_work_order)).json()
        test_work_order.soft_delete(None)
        db_session.commit()
        assert execute(client, auth_headers, task).status_code == 404
        assert propose(client, auth_headers, 'add_note', note_input(test_work_order)).status_code == 404
        assert db_session.query(Comment).count() == 0

    def test_notifications_fire_without_the_note_text(
        self, client, db_session, auth_headers, test_work_order, crew, redis
    ):
        from app.services import comment_service

        comment_service.watch(
            db_session,
            company_id=1,
            actor=crew['watcher'],
            entity_type='work_order',
            entity_id=test_work_order.id,
            audit=AuditService(db_session, crew['watcher']),
        )
        db_session.commit()
        task = propose(
            client, auth_headers, 'add_note', note_input(test_work_order, mentions=[crew['mentioned'].id])
        ).json()
        assert execute(client, auth_headers, task).status_code == 200
        for event_type, event_key, recipient in (
            ('comment_mentioned', 'comment.mention', crew['mentioned']),
            ('comment_added', 'comment.added', crew['watcher']),
        ):
            (event,) = events(db_session, event_type)
            assert SECRET not in json.dumps(event.event_payload)
            assert 'source' not in event.event_payload and 'reason' not in event.event_payload
            asyncio.run(dispatch.dispatch_for_event(db_session, event))
            db_session.commit()
            db_session.expire_all()
            (notice,) = db_session.query(Notification).filter_by(user_id=recipient.id, event_key=event_key).all()
            assert SECRET not in notice.title and SECRET not in (notice.body or '')
            assert notice.title == f'{CATALOG[event_key].label}: {test_work_order.work_order_number}'
        (mail,) = (
            db_session.query(NotificationLog)
            .filter_by(user_id=crew['mentioned'].id, event_type='comment.mention', channel=CHANNEL_EMAIL)
            .all()
        )
        assert SECRET not in (mail.subject or '') and SECRET not in (mail.body or '')
        assert 'bore' not in (mail.body or '')

    def test_a_lost_required_comment_audit_is_503_and_nothing_is_written(
        self, client, db_session, auth_headers, test_work_order, crew, monkeypatch
    ):
        task = propose(
            client, auth_headers, 'add_note', note_input(test_work_order, mentions=[crew['mentioned'].id])
        ).json()
        original = AuditService.log_create

        def lose_comment_row(self, resource_type, *args, **kwargs):
            if resource_type == 'comment':
                return None
            return original(self, resource_type, *args, **kwargs)

        monkeypatch.setattr(AuditService, 'log_create', lose_comment_row)
        response = execute(client, auth_headers, task)
        assert response.status_code == 503, response.text
        db_session.expire_all()
        assert db_session.query(Comment).count() == 0
        assert db_session.query(CommentMention).count() == 0
        assert db_session.query(EntityWatcher).count() == 0
        assert events(db_session, 'comment_mentioned') == [] and events(db_session, 'comment_added') == []
        assert db_session.get(HankTask, task['id']).status == 'awaiting_review'


class TestChatPreparesOnly:
    def test_chat_can_prepare_both_kinds_and_executes_neither(self, db_session, test_user, test_work_order):
        from app.services.copilot_service import CopilotService

        old_due = test_work_order.due_date
        service = CopilotService(db_session, company_id=1, user=test_user)
        for kind, data in (('change_due_date', due_input(test_work_order)), ('add_note', note_input(test_work_order))):
            result = service.execute_tool('prepare_hank_task', {'kind': kind, 'input': data})
            assert not result.is_error, result.payload
            assert result.payload['requires_review'] is True
        db_session.expire_all()
        assert {row.kind for row in db_session.query(HankTask).all()} == {'change_due_date', 'add_note'}
        assert {row.status for row in db_session.query(HankTask).all()} == {'awaiting_review'}
        assert db_session.get(WorkOrder, test_work_order.id).due_date == old_due
        assert db_session.query(Comment).count() == 0


# ---------------------------------------------------------------------------
# Credentials: API tokens
# ---------------------------------------------------------------------------


@pytest.fixture
def manager(db_session: Session) -> User:
    return make_user(db_session, role=UserRole.MANAGER, first_name='Morgan', last_name='Tallis')


class TestApiTokens:
    def test_a_full_token_can_prepare_and_execute_a_due_date_change(
        self, client, db_session, admin_headers, manager, test_work_order
    ):
        token_id, token = issue_token(client, admin_headers, manager)
        headers = bearer(token)
        capabilities = client.get(BASE + '/capabilities', headers=headers).json()
        assert 'change_due_date' in capabilities['allowed_kinds']
        assert 'add_note' not in capabilities['allowed_kinds']
        new_due = test_work_order.due_date + timedelta(days=4)
        task = propose(client, headers, 'change_due_date', due_input(test_work_order, new_due))
        assert task.status_code == 200, task.text
        done = execute(client, headers, task.json())
        assert done.status_code == 200, done.text
        db_session.expire_all()
        assert db_session.get(WorkOrder, test_work_order.id).due_date == new_due
        (row,) = db_session.query(AuditLog).filter_by(resource_type='work_order').all()
        assert row.user_id == manager.id and row.extra_data['via'] == 'hank'
        assert row.extra_data['credential']['api_token_id'] == token_id

    @pytest.mark.parametrize('ai_external', [True, False], ids=['ai_external', 'read_only'])
    def test_narrowed_tokens_cannot_prepare_a_due_date_change(
        self, client, db_session, admin_headers, manager, test_work_order, ai_external
    ):
        _id, token = issue_token(client, admin_headers, manager, scope='read_only', ai_external=ai_external)
        response = propose(client, bearer(token), 'change_due_date', due_input(test_work_order))
        assert response.status_code == 403
        if not ai_external:
            assert response.json()['detail'] == READ_ONLY_ROUTE_WRITES_DETAIL
        assert db_session.query(HankTask).count() == 0

    @pytest.mark.parametrize(
        'mint',
        [{}, {'scope': 'read_only', 'ai_external': False}, {'scope': 'read_only'}],
        ids=['full', 'read_only', 'ai_external'],
    )
    def test_every_token_is_refused_a_note_at_prepare(
        self, client, db_session, admin_headers, manager, test_work_order, mint
    ):
        _id, token = issue_token(client, admin_headers, manager, **mint)
        response = propose(client, bearer(token), 'add_note', note_input(test_work_order))
        assert response.status_code == 403
        if not mint:
            # The comment routes' own text, not the generic Hank refusal.
            assert response.json()['detail'] == API_TOKEN_COMMENT_WRITE
        assert db_session.query(HankTask).count() == 0
        assert db_session.query(Comment).count() == 0
        assert db_session.query(AuditLog).filter_by(resource_type='hank_task').count() == 0

    def test_a_token_cannot_execute_a_persons_note(self, client, db_session, admin_headers, manager, test_work_order):
        task = propose(client, user_headers(manager), 'add_note', note_input(test_work_order)).json()
        _id, token = issue_token(client, admin_headers, manager)
        # Tasks are bound to the credential that prepared them: the token cannot see the person's.
        response = execute(client, bearer(token), task)
        assert response.status_code == 404
        assert db_session.query(Comment).count() == 0

    def test_the_specific_refusal_wins_at_prepare_and_execute(self, db_session, manager, test_work_order):
        """Service level: a token-marked person on an add_note task is refused with the comment
        text before the generic capability refusal (and before the company check at prepare)."""
        from app.schemas.hank_tasks import HankTaskCreate

        manager._api_token_id = 4242
        try:
            service = HankTaskService(db_session, manager, 1)
            payload = HankTaskCreate(
                expected_company_id=2, request_key=str(uuid4()), kind='add_note', input=note_input(test_work_order)
            )
            with pytest.raises(HTTPException) as prepare_refusal:
                service.prepare(payload, AuditService(db_session, manager))
            assert (prepare_refusal.value.status_code, prepare_refusal.value.detail) == (403, API_TOKEN_COMMENT_WRITE)
            with pytest.raises(HTTPException) as preview_refusal:
                service._preview('add_note', note_input(test_work_order))
            assert preview_refusal.value.detail == API_TOKEN_COMMENT_WRITE
            task = _task_row(db_session, manager, 'add_note', note_input(test_work_order), credential_key='api:4242')
            with pytest.raises(HTTPException) as execute_refusal:
                service.execute(
                    task.id, HankTaskCommand(expected_company_id=1, expected_version=1), _audit(db_session, manager)
                )
            assert (execute_refusal.value.status_code, execute_refusal.value.detail) == (403, API_TOKEN_COMMENT_WRITE)
        finally:
            manager._api_token_id = None
        db_session.rollback()
        assert db_session.query(Comment).count() == 0


# ---------------------------------------------------------------------------
# PUT /work-orders/{id}: the shared rule and the deleted-row fix
# ---------------------------------------------------------------------------


class TestPutSharesTheRule:
    def test_the_put_answers_the_helpers_exact_text(self, client, db_session, auth_headers, test_work_order):
        test_work_order.status = WorkOrderStatus.COMPLETE
        db_session.commit()
        new_due = test_work_order.due_date + timedelta(days=1)
        response = client.put(
            f'/api/v1/work-orders/{test_work_order.id}',
            headers=auth_headers,
            json={'version': test_work_order.version, 'due_date': new_due.isoformat()},
        )
        assert response.status_code == 409
        assert response.json()['detail'] == TERMINAL_TEXT == due_date_change_refusal(test_work_order, new_due)

    def test_the_put_still_clears_and_sets_a_due_date_once(self, client, db_session, auth_headers, test_work_order):
        response = client.put(
            f'/api/v1/work-orders/{test_work_order.id}',
            headers=auth_headers,
            json={'version': test_work_order.version, 'due_date': None},
        )
        assert response.status_code == 200, response.text
        assert response.json()['due_date'] is None
        (row,) = db_session.query(AuditLog).filter_by(resource_type='work_order', action='UPDATE').all()
        assert 'due_date' in row.extra_data['changes']

    def test_a_soft_deleted_work_order_is_404_on_the_put(self, client, db_session, auth_headers, test_work_order):
        test_work_order.soft_delete(None)
        db_session.commit()
        version = test_work_order.version
        response = client.put(
            f'/api/v1/work-orders/{test_work_order.id}',
            headers=auth_headers,
            json={'version': version, 'notes': 'edited after delete'},
        )
        assert response.status_code == 404
        assert response.json()['detail'] == 'Work order not found'
        db_session.expire_all()
        assert db_session.get(WorkOrder, test_work_order.id).notes != 'edited after delete'
        assert db_session.query(AuditLog).filter_by(resource_type='work_order').count() == 0


# ---------------------------------------------------------------------------
# Row-lock strength: the weakest lock each kind's write needs
# ---------------------------------------------------------------------------

# The trailing row-lock clause of a compiled Postgres SELECT. The alternation order does not matter
# for correctness ("FOR UPDATE" cannot match inside "FOR NO KEY UPDATE", nor "FOR SHARE" inside
# "FOR KEY SHARE"), but the longer forms go first for the reader.
_LOCK_CLAUSE = re.compile(r'\bFOR (NO KEY UPDATE|KEY SHARE|UPDATE|SHARE)\b')


@contextmanager
def locked_reads(db: Session):
    """Every row-locking ORM read issued on ``db`` while open, as ``(tables, Postgres lock clause)``.

    SQLite takes no row locks and renders none, so the locking SELECT is compiled with the Postgres
    dialect -- the one place the lock mode is visible (CLAUDE.md: assert engine-specific behavior by
    dialect-compiling, never by executing on SQLite and trusting the result).
    """
    seen: List[Tuple[Set[str], str]] = []

    def capture(state):
        if not state.is_select or getattr(state.statement, '_for_update_arg', None) is None:
            return
        sql = str(state.statement.compile(dialect=postgresql.dialect()))
        match = _LOCK_CLAUSE.search(sql)
        assert match is not None, sql
        tables = {getattr(source, 'name', str(source)) for source in state.statement.get_final_froms()}
        seen.append((tables, match.group(0)))

    event.listen(db, 'do_orm_execute', capture)
    try:
        yield seen
    finally:
        event.remove(db, 'do_orm_execute', capture)


def _work_order_locks(seen) -> List[str]:
    return [clause for tables, clause in seen if 'work_orders' in tables]


def _locked_preview(db, user, kind, data):
    """Run ``kind``'s preview exactly as execute does (``locked=True``) and return its locked reads.

    A refusal after the reads (a missing document, vendor) is fine: the locks were already issued.
    """
    service = HankTaskService(db, user, 1)
    parsed = INPUT_SCHEMAS[kind].model_validate(data).model_dump(mode='json')
    with locked_reads(db) as seen:
        try:
            service._preview(kind, parsed, locked=True)
        except HTTPException:
            pass
    db.rollback()
    return seen


class TestLockStrength:
    """Execute re-runs the preview under row locks, then takes the global audit-chain lock. A plain
    FOR UPDATE on the work order conflicts with the FOR KEY SHARE every concurrent foreign-key insert
    on it takes (events, time entries, comments), widening the deadlock window against writers that
    take the chain lock first -- so the two kinds that only guard the work order take the weakest
    lock their own write needs, and every other kind keeps FOR UPDATE."""

    def test_a_due_date_change_locks_the_work_order_for_no_key_update(self, db_session, test_user, test_work_order):
        seen = _locked_preview(db_session, test_user, 'change_due_date', due_input(test_work_order))
        # Exactly the lock its own non-key UPDATE takes: no upgrade mid-transaction, and a soft
        # delete or version-bumping write still waits.
        assert seen == [({'work_orders'}, 'FOR NO KEY UPDATE')]

    def test_a_note_locks_the_work_order_for_share(self, db_session, test_user, test_work_order):
        seen = _locked_preview(db_session, test_user, 'add_note', note_input(test_work_order))
        locks = _work_order_locks(seen)
        assert locks and locks[0] == 'FOR SHARE', seen
        # Nothing on the note path locks the work order for an update of any kind.
        assert not {'FOR UPDATE', 'FOR NO KEY UPDATE'} & set(locks), seen

    @pytest.mark.parametrize(
        'kind, table',
        [('repeat_job', 'work_orders'), ('attach_document', 'work_orders'), ('draft_purchase_order', 'vendors')],
    )
    def test_every_other_kind_still_locks_for_update(self, db_session, test_user, test_work_order, kind, table):
        data = dict(SAMPLE_INPUTS[kind])
        if 'source_work_order_id' in data:
            data['source_work_order_id'] = test_work_order.id
        if 'work_order_id' in data:
            data['work_order_id'] = test_work_order.id
        seen = _locked_preview(db_session, test_user, kind, data)
        assert any(table in tables for tables, _ in seen), seen
        assert {clause for _, clause in seen} == {'FOR UPDATE'}, seen

    def test_the_whole_due_date_execute_never_locks_the_work_order_harder(
        self, client, db_session, auth_headers, test_work_order
    ):
        task = propose(client, auth_headers, 'change_due_date', due_input(test_work_order)).json()
        with locked_reads(db_session) as seen:
            response = execute(client, auth_headers, task)
        assert response.status_code == 200, response.text
        assert set(_work_order_locks(seen)) == {'FOR NO KEY UPDATE'}, seen
        # The task row itself keeps its FOR UPDATE: two executes of one task must serialize.
        assert ({'hank_tasks'}, 'FOR UPDATE') in seen

    def test_the_whole_note_execute_never_locks_the_work_order_for_an_update(
        self, client, db_session, auth_headers, test_work_order
    ):
        task = propose(client, auth_headers, 'add_note', note_input(test_work_order)).json()
        with locked_reads(db_session) as seen:
            response = execute(client, auth_headers, task)
        assert response.status_code == 200, response.text
        locks = _work_order_locks(seen)
        assert locks and locks[0] == 'FOR SHARE', seen
        # The comment service may read the record FOR KEY SHARE too; neither conflicts with an
        # FK insert, and neither is an update lock.
        assert set(locks) <= {'FOR SHARE', 'FOR KEY SHARE'}, seen
        assert db_session.query(Comment).count() == 1
