"""Hank ``change_due_date`` / ``add_note`` -- the QA matrix the first-pass suite left unpinned.

``tests/api/test_hank_due_date_and_note.py`` pins each verb's contract with a fixed cast. This
file pins the rules ACROSS the axes that cast does not span:

* **The change_due_date snapshot names four fields** (``due_date``, ``must_ship_by``, ``status``,
  ``version``) and each one refuses execute when it changes between prepare and execute -- even
  through a Core ``UPDATE`` that bumps nothing (the first-pass suite pinned ``due_date`` and an
  ORM ``version`` bump only, so dropping ``must_ship_by`` or ``status`` from the digest failed
  nothing). A note's snapshot is deliberately narrow: a header edit or a status change between
  prepare and execute does not refuse it.
* **must_ship_by never moves on any successful path** -- a person, a full API token, a task the
  chat prepared -- whether it was set or unset, and even when the new due date lands AFTER the
  promise date (no clamping, no "helpful" sync). The preview and the receipt say so.
* **The terminal rule over every status**: complete / closed / cancelled refused with the exact
  status text, every other status allowed; a past date is a warning, never a refusal; the pure
  helper's idempotent and clearing cases.
* **A Platform Admin switched into another company**: both kinds act in the ACTIVE company (task,
  work order, comment, mention, audit rows), a home-company person cannot be mentioned there, the
  admin is not auto-watched there (a home-company note does auto-watch them), the home company's
  work order is unreachable, and the note's notifications are delivered to that company only --
  rendered email included, never the note text.
* **The chat door for an API token**: ``prepare_hank_task`` with ``add_note`` answers the comment
  routes' text and saves nothing (the third door to ``prepare``; the first-pass suite pinned the
  HTTP door and the MCP door).
* **Narrowed tokens at execute**: read-only and AI-external tokens are refused 403 at the execute
  route for both kinds before any task is resolved, and change nothing.
* The note body is stored verbatim (markup, ampersands, inner line breaks), only trimmed.
* ``PUT /work-orders/{id}``'s broadcasts are unchanged by the refactor: a due-date change still
  broadcasts after commit, a refused or deleted-row PUT broadcasts nothing.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Dict, List
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

import app.api.endpoints.work_orders as work_order_endpoints
import app.services.notification_dispatch as dispatch
from app.core.config import settings
from app.core.credentials import API_TOKEN_COMMENT_WRITE, READ_ONLY_ROUTE_WRITES_DETAIL
from app.models.audit_log import AuditLog
from app.models.comment import Comment, CommentMention, EntityWatcher
from app.models.hank import HankTask
from app.models.notification import Notification
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.services.email_service import EmailService
from app.services.work_order_due_date import due_date_change_refusal
from tests.api.export_control_helpers import bearer, issue_token
from tests.api.kiosk_test_helpers import COMPANY_A, COMPANY_B, ensure_company, make_user, user_headers
from tests.api.test_hank_due_date_and_note import (
    BASE,
    SECRET,
    TERMINAL_TEXT,
    due_input,
    events,
    execute,
    note_input,
    propose,
)

pytestmark = pytest.mark.api

STALE = 'The source records changed. Prepare and review a fresh task.'
BASE_URL = 'https://erp.example.test'


def _wo(db: Session, *, company_id: int = COMPANY_A, status: str = 'released', **extra) -> WorkOrder:
    ensure_company(db, company_id)
    suffix = uuid4().hex[:8]
    part = Part(
        company_id=company_id,
        part_number=f'QA-HANK-{suffix}',
        name='Matrix bracket',
        part_type='manufactured',
        unit_of_measure='each',
        is_active=True,
    )
    db.add(part)
    db.flush()
    work_order = WorkOrder(
        company_id=company_id,
        part_id=part.id,
        work_order_number=f'WO-QA-{suffix}',
        quantity_ordered=4,
        status=status,
        due_date=extra.pop('due_date', date.today() + timedelta(days=21)),
        **extra,
    )
    db.add(work_order)
    db.commit()
    db.refresh(work_order)
    return work_order


def _execute_in(client, headers, task, company_id):
    return client.post(
        f'{BASE}/tasks/{task["id"]}/execute',
        headers=headers,
        json={'expected_company_id': company_id, 'expected_version': task['version']},
    )


def _fresh(db: Session, work_order: WorkOrder) -> WorkOrder:
    db.expire_all()
    return db.get(WorkOrder, work_order.id)


# ---------------------------------------------------------------------------
# The snapshot: every judged field refuses execute; the note's snapshot is narrow
# ---------------------------------------------------------------------------


class TestSnapshotFields:
    @pytest.mark.parametrize(
        'column, value',
        [
            ('must_ship_by', date.today() + timedelta(days=400)),
            ('status', WorkOrderStatus.RELEASED),
            ('version', 99),
        ],
        ids=['must_ship_by', 'status', 'version'],
    )
    def test_a_core_update_of_any_judged_field_refuses_execute(
        self, client, db_session, auth_headers, test_work_order, column, value
    ):
        """A write that bumps NOTHING else (no version, no updated_at) to one of the four fields
        the digest names still refuses: the reviewed preview no longer describes the record."""
        old_due = test_work_order.due_date
        task = propose(client, auth_headers, 'change_due_date', due_input(test_work_order)).json()
        db_session.execute(
            WorkOrder.__table__.update().where(WorkOrder.id == test_work_order.id).values({column: value})
        )
        db_session.commit()

        response = execute(client, auth_headers, task)

        assert response.status_code == 409, response.text
        assert response.json()['detail'] == STALE
        assert _fresh(db_session, test_work_order).due_date == old_due
        assert db_session.get(HankTask, task['id']).status == 'awaiting_review'
        assert db_session.query(AuditLog).filter_by(resource_type='work_order').count() == 0
        assert events(db_session, 'work_order_updated') == []

    def test_a_column_outside_the_digest_does_not_refuse(self, client, db_session, auth_headers, test_work_order):
        """The digest is NARROW on purpose: a Core write to a column it does not name (a
        scheduled date, the customer PO) leaves the reviewed change valid."""
        new_due = test_work_order.due_date + timedelta(days=6)
        task = propose(client, auth_headers, 'change_due_date', due_input(test_work_order, new_due)).json()
        db_session.execute(
            WorkOrder.__table__.update()
            .where(WorkOrder.id == test_work_order.id)
            .values(scheduled_end=None, customer_po='PO-CHANGED-UNDER-REVIEW')
        )
        db_session.commit()

        response = execute(client, auth_headers, task)

        assert response.status_code == 200, response.text
        assert _fresh(db_session, test_work_order).due_date == new_due

    def test_a_note_survives_a_header_edit_and_a_status_change(self, client, db_session, auth_headers, test_work_order):
        """A note never judged the header, so editing the job (an ORM write that bumps version)
        or finishing it between prepare and execute does not make the reviewed note stale.
        Comments are open at any status; only a delete (404) or a mention that no longer
        qualifies (422) stops it."""
        task = propose(client, auth_headers, 'add_note', note_input(test_work_order, body='After review')).json()
        test_work_order.priority = 1
        test_work_order.due_date = test_work_order.due_date + timedelta(days=2)
        test_work_order.status = WorkOrderStatus.COMPLETE
        db_session.commit()

        response = execute(client, auth_headers, task)

        assert response.status_code == 200, response.text
        (comment,) = db_session.query(Comment).all()
        assert comment.body == 'After review' and comment.entity_id == test_work_order.id


# ---------------------------------------------------------------------------
# must_ship_by never moves -- on every successful path
# ---------------------------------------------------------------------------


@pytest.fixture
def manager(db_session: Session) -> User:
    return make_user(db_session, role=UserRole.MANAGER, first_name='Morgan', last_name='Tallis')


def _run_person(client, db, work_order, data, *, manager, admin_headers):
    headers = user_headers(manager)
    task = propose(client, headers, 'change_due_date', data)
    assert task.status_code == 200, task.text
    return execute(client, headers, task.json())


def _run_full_token(client, db, work_order, data, *, manager, admin_headers):
    _id, token = issue_token(client, admin_headers, manager)
    task = propose(client, bearer(token), 'change_due_date', data)
    assert task.status_code == 200, task.text
    return execute(client, bearer(token), task.json())


def _run_chat_prepared(client, db, work_order, data, *, manager, admin_headers):
    from app.services.copilot_service import CopilotService

    prepared = CopilotService(db, company_id=1, user=manager).execute_tool(
        'prepare_hank_task', {'kind': 'change_due_date', 'input': data}
    )
    assert not prepared.is_error, prepared.payload
    db.expire_all()
    task = client.get(f'{BASE}/tasks/{prepared.payload["task_id"]}', headers=user_headers(manager))
    assert task.status_code == 200, task.text
    return execute(client, user_headers(manager), task.json())


_PATHS = {'person': _run_person, 'full_token': _run_full_token, 'chat_prepared': _run_chat_prepared}


class TestMustShipByNeverMoves:
    @pytest.mark.parametrize('path', list(_PATHS))
    @pytest.mark.parametrize(
        'promise_offset, new_due_offset',
        [(None, 10), (5, 10), (40, 10), (5, 3)],
        ids=['unset', 'new_due_after_promise', 'new_due_before_promise', 'promise_after_new_due'],
    )
    def test_only_due_date_moves(
        self, client, db_session, admin_headers, manager, test_work_order, path, promise_offset, new_due_offset
    ):
        base = test_work_order.due_date
        promise = base + timedelta(days=promise_offset) if promise_offset is not None else None
        test_work_order.must_ship_by = promise
        db_session.commit()
        new_due = base + timedelta(days=new_due_offset)

        response = _PATHS[path](
            client,
            db_session,
            test_work_order,
            due_input(test_work_order, new_due),
            manager=manager,
            admin_headers=admin_headers,
        )

        assert response.status_code == 200, response.text
        after = _fresh(db_session, test_work_order)
        assert after.due_date == new_due
        # Never cleared, never synced to the new due date, never clamped to it.
        assert after.must_ship_by == promise
        (row,) = db_session.query(AuditLog).filter_by(resource_type='work_order', resource_id=after.id).all()
        assert set(row.old_values) == set(row.new_values) == {'due_date'}
        receipt = response.json()['result']
        if promise is None:
            assert receipt['warnings'] == []
        else:
            assert receipt['warnings'] == [f'Must ship by {promise.isoformat()} was not changed.']


# ---------------------------------------------------------------------------
# The terminal rule over every status; past dates warn, never refuse
# ---------------------------------------------------------------------------

_TERMINAL = [WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED]
_OPEN = [status for status in WorkOrderStatus if status not in _TERMINAL]


class TestTerminalRuleAcrossStatuses:
    @pytest.mark.parametrize('status', _TERMINAL, ids=lambda status: status.value)
    def test_every_terminal_status_is_refused_with_its_own_name_at_prepare(
        self, client, db_session, auth_headers, test_work_order, status
    ):
        old_due = test_work_order.due_date
        test_work_order.status = status
        db_session.commit()

        response = propose(client, auth_headers, 'change_due_date', due_input(test_work_order))

        assert response.status_code == 409, response.text
        assert response.json()['detail'] == TERMINAL_TEXT.replace("'complete'", f"'{status.value}'")
        assert db_session.query(HankTask).count() == 0
        assert _fresh(db_session, test_work_order).due_date == old_due

    @pytest.mark.parametrize('status', _OPEN, ids=lambda status: status.value)
    def test_every_open_status_moves_the_date(self, client, db_session, auth_headers, test_work_order, status):
        test_work_order.status = status
        db_session.commit()
        new_due = test_work_order.due_date + timedelta(days=3)

        task = propose(client, auth_headers, 'change_due_date', due_input(test_work_order, new_due))
        assert task.status_code == 200, task.text
        done = execute(client, auth_headers, task.json())

        assert done.status_code == 200, done.text
        after = _fresh(db_session, test_work_order)
        assert after.due_date == new_due and after.status == status

    @pytest.mark.parametrize('status', _TERMINAL, ids=lambda status: status.value)
    def test_the_pure_rule_refuses_only_an_actual_change(self, db_session, test_work_order, status):
        test_work_order.status = status
        stored = test_work_order.due_date
        expected = TERMINAL_TEXT.replace("'complete'", f"'{status.value}'")
        # Re-sending the stored value is idempotent (the PUT echoes whole records).
        assert due_date_change_refusal(test_work_order, stored) is None
        # Moving it, or clearing it, is refused.
        assert due_date_change_refusal(test_work_order, stored + timedelta(days=1)) == expected
        assert due_date_change_refusal(test_work_order, None) == expected
        # A terminal job that never had a due date cannot be given one after the fact either.
        test_work_order.due_date = None
        assert due_date_change_refusal(test_work_order, None) is None
        assert due_date_change_refusal(test_work_order, stored) == expected

    @pytest.mark.parametrize('status', _OPEN, ids=lambda status: status.value)
    def test_the_pure_rule_allows_any_change_on_an_open_job(self, db_session, test_work_order, status):
        test_work_order.status = status
        assert due_date_change_refusal(test_work_order, None) is None
        assert due_date_change_refusal(test_work_order, test_work_order.due_date + timedelta(days=9)) is None

    def test_a_past_date_is_a_warning_and_still_executes(self, client, db_session, auth_headers, test_work_order):
        past = date(2020, 1, 15)
        task = propose(client, auth_headers, 'change_due_date', due_input(test_work_order, past))
        assert task.status_code == 200, task.text
        assert 'The new due date is already in the past.' in task.json()['preview']['warnings']
        assert task.json()['preview']['warnings'][0] == (
            'Only the due date changes. Scheduled dates, priority and the dispatch plan are not edited.'
        )
        assert execute(client, auth_headers, task.json()).status_code == 200
        assert _fresh(db_session, test_work_order).due_date == past

    def test_a_future_date_carries_no_past_warning(self, client, auth_headers, test_work_order):
        task = propose(client, auth_headers, 'change_due_date', due_input(test_work_order))
        assert 'The new due date is already in the past.' not in task.json()['preview']['warnings']

    def test_a_no_op_on_a_terminal_job_is_the_no_op_refusal(self, client, db_session, auth_headers, test_work_order):
        """Hank refuses every no-op (409, nothing to change) -- on a terminal job too -- where the
        PUT answers a re-sent value with 200. Neither writes."""
        test_work_order.status = WorkOrderStatus.CLOSED
        db_session.commit()
        response = propose(
            client, auth_headers, 'change_due_date', due_input(test_work_order, test_work_order.due_date)
        )
        assert response.status_code == 409
        assert 'There is nothing to change.' in response.json()['detail']
        assert db_session.query(HankTask).count() == 0


# ---------------------------------------------------------------------------
# A Platform Admin switched into another company
# ---------------------------------------------------------------------------


@pytest.fixture
def platform_admin(db_session: Session) -> User:
    return make_user(
        db_session, role=UserRole.PLATFORM_ADMIN, company_id=COMPANY_A, first_name='Plato', last_name='Oversight'
    )


class _FakeRedis:
    def __init__(self) -> None:
        self.keys: Dict[str, str] = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True


@pytest.fixture
def mail(monkeypatch) -> AsyncMock:
    fake = _FakeRedis()

    async def pool():
        return fake

    enqueue = AsyncMock()
    monkeypatch.setattr(dispatch, 'get_redis_pool', pool)
    monkeypatch.setattr(dispatch, 'enqueue_job', enqueue)
    monkeypatch.setattr(settings, 'FRONTEND_BASE_URL', BASE_URL)
    return enqueue


def _deliver_comment_events(db: Session) -> None:
    db.expire_all()
    for event in (
        db.query(OperationalEvent)
        .filter(OperationalEvent.source_module == 'comments')
        .order_by(OperationalEvent.id)
        .all()
    ):
        asyncio.run(dispatch.dispatch_for_event(db, event))
        db.commit()


def _email_jobs(mail: AsyncMock) -> List[dict]:
    return [call.kwargs for call in mail.await_args_list if call.args and call.args[0] == 'send_email_job']


def _watchers(db: Session, work_order: WorkOrder) -> List[int]:
    db.expire_all()
    return sorted(
        row.user_id
        for row in db.query(EntityWatcher).filter_by(entity_type='work_order', entity_id=work_order.id).all()
    )


class TestPlatformAdminInAnotherCompany:
    def test_capabilities_in_the_viewed_company_offer_both_kinds(self, client, db_session, platform_admin):
        ensure_company(db_session, COMPANY_B)
        viewing_b = user_headers(platform_admin, active_company_id=COMPANY_B)
        capabilities = client.get(BASE + '/capabilities', headers=viewing_b).json()
        assert capabilities['company_id'] == COMPANY_B
        assert {'change_due_date', 'add_note'} <= set(capabilities['allowed_kinds'])

    def test_a_note_is_posted_in_the_active_company_and_notifies_only_it(
        self, client, db_session, platform_admin, mail
    ):
        b_wo = _wo(db_session, company_id=COMPANY_B)
        a_wo = _wo(db_session, company_id=COMPANY_A)
        b_mentioned = make_user(db_session, company_id=COMPANY_B, first_name='Imogen', last_name='Bayliss')
        b_watcher = make_user(
            db_session, company_id=COMPANY_B, role=UserRole.VIEWER, first_name='Gale', last_name='Ward'
        )
        a_person = make_user(db_session, company_id=COMPANY_A, first_name='Jasper', last_name='Homefield')
        watched = client.post(
            '/api/v1/watchers',
            json={'entity_type': 'work_order', 'entity_id': b_wo.id},
            headers=user_headers(b_watcher),
        )
        assert watched.status_code == 200, watched.text
        viewing_b = user_headers(platform_admin, active_company_id=COMPANY_B)

        # A home-company person is not mentionable in the viewed company, and their name never
        # reaches the refusal; the home company's work order is not reachable from here.
        refused = propose(client, viewing_b, 'add_note', note_input(b_wo, mentions=[a_person.id]), company=COMPANY_B)
        assert refused.status_code == 422, refused.text
        assert 'Jasper' not in refused.json()['detail'] and 'Homefield' not in refused.json()['detail']
        assert propose(client, viewing_b, 'add_note', note_input(a_wo), company=COMPANY_B).status_code == 404
        # The expected company is the ACTIVE one, never the platform admin's home company.
        assert propose(client, viewing_b, 'add_note', note_input(b_wo), company=COMPANY_A).status_code == 409
        assert db_session.query(HankTask).count() == 0

        prepared = propose(
            client, viewing_b, 'add_note', note_input(b_wo, mentions=[b_mentioned.id]), company=COMPANY_B
        )
        assert prepared.status_code == 200, prepared.text
        assert prepared.json()['company_id'] == COMPANY_B
        done = _execute_in(client, viewing_b, prepared.json(), COMPANY_B)
        assert done.status_code == 200, done.text

        db_session.expire_all()
        (task,) = db_session.query(HankTask).all()
        assert task.company_id == COMPANY_B and task.owner_id == platform_admin.id
        (comment,) = db_session.query(Comment).all()
        assert (comment.company_id, comment.author_id, comment.entity_id) == (COMPANY_B, platform_admin.id, b_wo.id)
        assert [(m.company_id, m.user_id) for m in db_session.query(CommentMention).all()] == [
            (COMPANY_B, b_mentioned.id)
        ]
        # The non-member author is NOT auto-watched in the viewed company; the mentioned member is.
        assert _watchers(db_session, b_wo) == sorted([b_watcher.id, b_mentioned.id])
        assert {row.company_id for row in db_session.query(EntityWatcher).all()} == {COMPANY_B}
        (create_audit,) = db_session.query(AuditLog).filter_by(resource_type='comment').all()
        assert create_audit.company_id == COMPANY_B and create_audit.user_id == platform_admin.id
        assert create_audit.extra_data['via'] == 'hank'
        assert create_audit.extra_data['auto_watched_user_ids'] == [b_mentioned.id]
        # Nothing landed in the home company.
        assert db_session.query(Comment).filter_by(company_id=COMPANY_A).count() == 0

        _deliver_comment_events(db_session)
        notices = db_session.query(Notification).order_by(Notification.id).all()
        assert {row.company_id for row in notices} == {COMPANY_B}
        assert {(row.user_id, row.event_key) for row in notices} == {
            (b_mentioned.id, 'comment.mention'),
            (b_watcher.id, 'comment.added'),
        }
        for row in notices:
            assert SECRET not in f'{row.title}\n{row.body}'
            assert row.body.endswith('By: Plato Oversight')
        # The ONE email is the mention's, to the company-B person, and the RENDERED message
        # (the last hop the worker sends) carries neither the note text nor a fragment of it.
        (job,) = _email_jobs(mail)
        assert job['to'] == b_mentioned.email and job['company_id'] == COMPANY_B
        assert SECRET not in repr(job)
        html = EmailService()._render_template(job['template'], job['context'])
        plain = EmailService()._html_to_plain(html)
        for text in (html, plain):
            assert b_wo.work_order_number in text
            assert SECRET not in text and 'undersize' not in text and '0.375' not in text
        assert inbox_count(db_session, platform_admin) == 0

    def test_at_home_a_hank_note_auto_watches_the_platform_admin(self, client, db_session, platform_admin):
        a_wo = _wo(db_session, company_id=COMPANY_A)
        at_home = user_headers(platform_admin)
        task = propose(client, at_home, 'add_note', note_input(a_wo, body='Home note'))
        assert task.status_code == 200, task.text
        assert execute(client, at_home, task.json()).status_code == 200
        assert _watchers(db_session, a_wo) == [platform_admin.id]

    def test_a_due_date_change_moves_the_active_companys_work_order(self, client, db_session, platform_admin):
        b_wo = _wo(db_session, company_id=COMPANY_B, must_ship_by=date.today() + timedelta(days=30))
        a_wo = _wo(db_session, company_id=COMPANY_A)
        viewing_b = user_headers(platform_admin, active_company_id=COMPANY_B)
        assert propose(client, viewing_b, 'change_due_date', due_input(a_wo), company=COMPANY_B).status_code == 404
        new_due = b_wo.due_date + timedelta(days=5)
        task = propose(client, viewing_b, 'change_due_date', due_input(b_wo, new_due), company=COMPANY_B)
        assert task.status_code == 200, task.text

        done = _execute_in(client, viewing_b, task.json(), COMPANY_B)

        assert done.status_code == 200, done.text
        after = _fresh(db_session, b_wo)
        assert after.due_date == new_due and after.must_ship_by == b_wo.must_ship_by
        assert _fresh(db_session, a_wo).due_date == a_wo.due_date
        (row,) = db_session.query(AuditLog).filter_by(resource_type='work_order').all()
        assert row.company_id == COMPANY_B and row.user_id == platform_admin.id
        (event,) = events(db_session, 'work_order_updated')
        assert event.company_id == COMPANY_B and event.work_order_id == b_wo.id

    def test_a_read_only_switched_context_is_offered_nothing_and_writes_nothing(
        self, client, db_session, platform_admin
    ):
        from app.core.security import create_access_token

        b_wo = _wo(db_session, company_id=COMPANY_B)
        read_only = bearer(create_access_token(subject=platform_admin.id, company_id=COMPANY_B, read_only=True))
        capabilities = client.get(BASE + '/capabilities', headers=read_only).json()
        assert capabilities['allowed_kinds'] == [] and capabilities['can_write'] is False
        for kind, data in (('change_due_date', due_input(b_wo)), ('add_note', note_input(b_wo))):
            response = propose(client, read_only, kind, data, company=COMPANY_B)
            assert response.status_code == 403, (kind, response.text)
        assert db_session.query(HankTask).count() == 0
        assert db_session.query(Comment).count() == 0


def inbox_count(db: Session, user: User) -> int:
    db.expire_all()
    return db.query(Notification).filter(Notification.user_id == user.id).count()


# ---------------------------------------------------------------------------
# Credentials: the chat door, and narrowed tokens at execute
# ---------------------------------------------------------------------------


class TestCredentials:
    def test_the_chat_door_refuses_a_token_a_note_and_saves_nothing(self, db_session, manager, test_work_order):
        """Hank's chat reaches ``prepare`` too. A token-authenticated chat gets the comment routes'
        own refusal for a note -- and nothing is saved, so there is nothing to execute later."""
        from app.services.copilot_service import CopilotService

        manager._api_token_id = 4242
        try:
            service = CopilotService(db_session, company_id=1, user=manager)
            refused = service.execute_tool(
                'prepare_hank_task', {'kind': 'add_note', 'input': note_input(test_work_order)}
            )
            assert refused.is_error is True
            assert refused.payload == {'error': API_TOKEN_COMMENT_WRITE}
            # The same token's chat may still prepare a due-date change (a full token's agent door).
            allowed = service.execute_tool(
                'prepare_hank_task', {'kind': 'change_due_date', 'input': due_input(test_work_order)}
            )
            assert not allowed.is_error, allowed.payload
        finally:
            manager._api_token_id = None
        db_session.expire_all()
        assert [row.kind for row in db_session.query(HankTask).all()] == ['change_due_date']
        assert db_session.query(Comment).count() == 0

    @pytest.mark.parametrize('kind', ['change_due_date', 'add_note'])
    @pytest.mark.parametrize(
        'mint',
        [{'scope': 'read_only', 'ai_external': False}, {'scope': 'read_only'}],
        ids=['read_only', 'ai_external'],
    )
    def test_narrowed_tokens_are_refused_at_execute_before_any_task_is_read(
        self, client, db_session, admin_headers, manager, test_work_order, kind, mint
    ):
        data = due_input(test_work_order) if kind == 'change_due_date' else note_input(test_work_order)
        task = propose(client, user_headers(manager), kind, data).json()
        old_due = test_work_order.due_date
        _id, token = issue_token(client, admin_headers, manager, **mint)

        response = execute(client, bearer(token), task)

        # 403 at the route (a POST), never the 404 a task lookup would answer.
        assert response.status_code == 403, response.text
        if mint.get('ai_external') is False:
            assert response.json()['detail'] == READ_ONLY_ROUTE_WRITES_DETAIL
        db_session.expire_all()
        assert db_session.get(HankTask, task['id']).status == 'awaiting_review'
        assert db_session.get(WorkOrder, test_work_order.id).due_date == old_due
        assert db_session.query(Comment).count() == 0

    def test_a_full_token_can_cancel_but_never_see_a_persons_note(
        self, client, db_session, admin_headers, manager, test_work_order
    ):
        """Tasks are bound to the credential that prepared them, so a person's note is invisible
        to the same person's token on every verb -- read, execute and cancel alike."""
        task = propose(client, user_headers(manager), 'add_note', note_input(test_work_order)).json()
        _id, token = issue_token(client, admin_headers, manager)
        headers = bearer(token)
        assert client.get(f'{BASE}/tasks/{task["id"]}', headers=headers).status_code == 404
        cancelled = client.post(
            f'{BASE}/tasks/{task["id"]}/cancel',
            headers=headers,
            json={'expected_company_id': 1, 'expected_version': task['version']},
        )
        assert cancelled.status_code == 404, cancelled.text
        listed = client.get(BASE + '/tasks', headers=headers)
        assert listed.status_code == 200, listed.text
        assert all(item['id'] != task['id'] for item in listed.json()['tasks'])
        db_session.expire_all()
        assert db_session.get(HankTask, task['id']).status == 'awaiting_review'

    def test_a_token_marked_person_cannot_execute_a_note_through_the_service_even_if_it_owned_one(
        self, db_session, manager, test_work_order
    ):
        """Defense in depth below the credential binding: the execute branch itself refuses."""
        from app.services.audit_service import AuditService
        from app.services.hank_task_service import HankTaskService

        service = HankTaskService(db_session, manager, 1)
        manager._api_token_id = 77
        try:
            with pytest.raises(HTTPException) as refusal:
                service._execute_add_note(note_input(test_work_order), AuditService(db_session, manager))
        finally:
            manager._api_token_id = None
        assert (refusal.value.status_code, refusal.value.detail) == (403, API_TOKEN_COMMENT_WRITE)
        assert db_session.query(Comment).count() == 0


# ---------------------------------------------------------------------------
# The note body is stored verbatim (only trimmed)
# ---------------------------------------------------------------------------


def test_a_note_body_is_stored_and_previewed_verbatim(client, db_session, auth_headers, test_work_order):
    body = 'Bore <REF> Ø0.375 & "flatness" <TYP>\n\n  second line kept  '
    task = propose(client, auth_headers, 'add_note', note_input(test_work_order, body=f'  \n{body}\n  '))
    assert task.status_code == 200, task.text
    expected = body.strip()
    assert task.json()['preview']['changes'][0] == f'Note: {expected}'
    assert execute(client, auth_headers, task.json()).status_code == 200
    db_session.expire_all()
    (comment,) = db_session.query(Comment).all()
    assert comment.body == expected


# ---------------------------------------------------------------------------
# PUT /work-orders/{id}: broadcasts unchanged by the refactor
# ---------------------------------------------------------------------------


class TestPutBroadcasts:
    @pytest.fixture
    def broadcasts(self, monkeypatch) -> List[tuple]:
        seen: List[tuple] = []

        def record(fn, *args, **kwargs):
            seen.append((fn.__name__, args, kwargs))

        monkeypatch.setattr(work_order_endpoints, 'safe_broadcast', record)
        return seen

    def _put(self, client, headers, work_order, **body):
        return client.put(
            f'/api/v1/work-orders/{work_order.id}', headers=headers, json={'version': work_order.version, **body}
        )

    def test_a_due_date_change_still_broadcasts_after_commit(self, client, auth_headers, test_work_order, broadcasts):
        new_due = test_work_order.due_date + timedelta(days=2)
        response = self._put(client, auth_headers, test_work_order, due_date=new_due.isoformat())
        assert response.status_code == 200, response.text
        assert [name for name, _, _ in broadcasts] == ['broadcast_work_order_update', 'broadcast_dashboard_update']
        assert all(kwargs == {'company_id': 1} for _, _, kwargs in broadcasts)
        assert broadcasts[0][1] == (test_work_order.id, {'event': 'work_order_updated', 'status': 'draft'})

    def test_a_refused_due_date_change_broadcasts_nothing(
        self, client, db_session, auth_headers, test_work_order, broadcasts
    ):
        test_work_order.status = WorkOrderStatus.CANCELLED
        db_session.commit()
        response = self._put(
            client, auth_headers, test_work_order, due_date=(test_work_order.due_date + timedelta(days=2)).isoformat()
        )
        assert response.status_code == 409
        assert response.json()['detail'] == TERMINAL_TEXT.replace("'complete'", "'cancelled'")
        assert broadcasts == []
        assert events(db_session, 'work_order_updated') == []

    def test_a_soft_deleted_work_order_is_404_even_for_a_due_date_on_a_terminal_row(
        self, client, db_session, auth_headers, test_work_order, broadcasts
    ):
        """The tombstone answers first: 404 like an absent row, never the terminal 409 that would
        confirm the deleted job's status."""
        test_work_order.status = WorkOrderStatus.COMPLETE
        db_session.commit()
        test_work_order.soft_delete(None)
        db_session.commit()
        old_due = test_work_order.due_date
        response = self._put(client, auth_headers, test_work_order, due_date=(old_due + timedelta(days=2)).isoformat())
        assert response.status_code == 404
        assert response.json()['detail'] == 'Work order not found'
        assert broadcasts == []
        assert _fresh(db_session, test_work_order).due_date == old_due
