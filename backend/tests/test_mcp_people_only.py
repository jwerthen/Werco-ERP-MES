"""An agent never posts a Hank note over MCP, even on a person's own access JWT.

Owner decision for work-order comments (2026-11-16): a comment is a person's own words. The comment
write routes are not MCP tools at all, and Hank's ``add_note`` (a work-order comment) travels through
the generated ``prepare_task`` / ``execute_task`` tools, so ``app.mcp.people_only`` refuses it there
before dispatch. Pinned end to end over the in-memory transport into the real routers, with a
PERSON's JWT (the case the API-token refusals cannot see):

* ``prepare_task`` with kind ``add_note`` -> 403, nothing dispatched, no task row;
* ``execute_task`` on an ``add_note`` task the person prepared in the app -> 403 after ONE read of
  the task, the execute never dispatched, no comment;
* a task the caller cannot read (absent, someone else's) -> the route's own 404, unchanged;
* a read that cannot complete -> 503, the execute never dispatched (fail closed, no information);
* ``change_due_date`` is prepared and executed through MCP as before;
* the routes themselves still serve the person in the app;
* matching is by route, the kind set is the Hank service's, and the catalog is unchanged.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from app.core.credentials import AGENT_PEOPLE_ONLY_TASK_DETAIL, PEOPLE_ONLY_HANK_TASK_KINDS
from app.main import app
from app.mcp.catalog import build_catalog
from app.mcp.convenience import CONVENIENCE_TOOL_NAMES, CONVENIENCE_TOOL_ROUTES, SHADOWED_OPERATIONS
from app.mcp.people_only import AGENT_TASK_UNVERIFIED_DETAIL, HANK_EXECUTE_ROUTE, HANK_PREPARE_ROUTE, PEOPLE_ONLY_ROUTES
from app.mcp.results import ExecResult
from app.mcp.server import DEFAULT_INSTRUCTIONS
from app.models.comment import Comment
from app.models.hank import HankTask
from app.models.work_order import WorkOrder
from app.schemas.hank_tasks import INPUT_SCHEMAS
from app.services.hank_task_service import PEOPLE_ONLY_KINDS
from tests.test_mcp_server import RecordingExecutor, call, make_server

pytestmark = pytest.mark.api

TASKS = '/api/v1/hank/tasks'
NOTE = 'Bore checked twice; second-shift lead agrees.'


@pytest.fixture(scope='module')
def catalog():
    return build_catalog(app.openapi(), shadowed=SHADOWED_OPERATIONS, reserved_names=CONVENIENCE_TOOL_NAMES)


def prepare_arguments(kind, task_input):
    return {'expected_company_id': 1, 'request_key': str(uuid4()), 'kind': kind, 'input': task_input}


def note_input(work_order):
    return {'work_order_id': work_order.id, 'body': NOTE, 'mentioned_user_ids': []}


def due_input(work_order):
    return {
        'work_order_id': work_order.id,
        'new_due_date': (work_order.due_date + timedelta(days=6)).isoformat(),
        'reason': 'Customer moved the slot',
    }


def prepare_in_app(client, headers, kind, task_input):
    response = client.post(TASKS, headers=headers, json=prepare_arguments(kind, task_input))
    assert response.status_code == 200, response.text
    return response.json()


class TestPrepare:
    async def test_an_agent_on_a_persons_jwt_cannot_prepare_a_note(
        self, client, db_session, auth_headers, test_work_order, catalog
    ):
        server, executor = make_server(catalog, headers=auth_headers)
        result = await call(server, 'prepare_task', prepare_arguments('add_note', note_input(test_work_order)))
        assert result.is_error
        assert result.structured_content['status'] == 403
        assert result.structured_content['detail'] == AGENT_PEOPLE_ONLY_TASK_DETAIL
        assert executor.calls == [], 'nothing may be dispatched for a people-only kind'
        assert db_session.query(HankTask).count() == 0

    async def test_change_due_date_is_still_prepared_and_executed_through_mcp(
        self, client, db_session, auth_headers, test_work_order, catalog
    ):
        server, executor = make_server(catalog, headers=auth_headers)
        data = due_input(test_work_order)
        prepared = await call(server, 'prepare_task', prepare_arguments('change_due_date', data))
        assert not prepared.is_error, prepared.structured_content
        task = prepared.structured_content
        assert task['kind'] == 'change_due_date' and task['status'] == 'awaiting_review'
        executed = await call(
            server,
            'execute_task',
            {'task_id': task['id'], 'expected_company_id': 1, 'expected_version': task['version']},
        )
        assert not executed.is_error, executed.structured_content
        assert executed.structured_content['status'] == 'completed'
        assert executor.calls == [
            ('POST', TASKS),
            ('GET', f'{TASKS}/{task["id"]}'),
            ('POST', f'{TASKS}/{task["id"]}/execute'),
        ]
        db_session.expire_all()
        assert db_session.get(WorkOrder, test_work_order.id).due_date.isoformat() == data['new_due_date']


class TestExecute:
    async def test_an_agent_cannot_execute_a_note_the_person_prepared_in_the_app(
        self, client, db_session, auth_headers, test_work_order, catalog
    ):
        task = prepare_in_app(client, auth_headers, 'add_note', note_input(test_work_order))
        server, executor = make_server(catalog, headers=auth_headers)
        result = await call(
            server, 'execute_task', {'task_id': task['id'], 'expected_company_id': 1, 'expected_version': 1}
        )
        assert result.is_error
        assert result.structured_content['status'] == 403
        assert result.structured_content['detail'] == AGENT_PEOPLE_ONLY_TASK_DETAIL
        assert executor.calls == [('GET', f'{TASKS}/{task["id"]}')], 'the execute is never dispatched'
        db_session.expire_all()
        assert db_session.query(Comment).count() == 0
        assert db_session.get(HankTask, task['id']).status == 'awaiting_review'

        # The route itself is unchanged: the person submits the note in the app.
        done = client.post(
            f'{TASKS}/{task["id"]}/execute',
            headers=auth_headers,
            json={'expected_company_id': 1, 'expected_version': 1},
        )
        assert done.status_code == 200, done.text
        assert db_session.query(Comment).count() == 1

    async def test_a_task_the_caller_cannot_read_gets_the_routes_own_answer(
        self, client, db_session, auth_headers, admin_headers, test_work_order, catalog
    ):
        someone_elses = prepare_in_app(client, admin_headers, 'add_note', note_input(test_work_order))
        server, executor = make_server(catalog, headers=auth_headers)
        for task_id in (someone_elses['id'], 987654):
            result = await call(
                server, 'execute_task', {'task_id': task_id, 'expected_company_id': 1, 'expected_version': 1}
            )
            direct = client.post(
                f'{TASKS}/{task_id}/execute',
                headers=auth_headers,
                json={'expected_company_id': 1, 'expected_version': 1},
            )
            assert result.is_error and direct.status_code == 404
            assert result.structured_content['status'] == 404
            assert result.structured_content['detail'] == direct.json()['detail']
        assert executor.calls == [
            ('GET', f'{TASKS}/{someone_elses["id"]}'),
            ('POST', f'{TASKS}/{someone_elses["id"]}/execute'),
            ('GET', f'{TASKS}/987654'),
            ('POST', f'{TASKS}/987654/execute'),
        ]
        assert db_session.query(Comment).count() == 0

    @pytest.mark.parametrize('failure', ['server_error', 'rate_limited', 'transport', 'no_kind'])
    async def test_a_read_that_cannot_complete_fails_closed(
        self, client, db_session, auth_headers, test_work_order, catalog, failure
    ):
        task = prepare_in_app(client, auth_headers, 'add_note', note_input(test_work_order))
        read_path = f'{TASKS}/{task["id"]}'

        def refuse(method, path):
            if (method, path) != ('GET', read_path):
                return None
            if failure == 'transport':
                raise ConnectionError('simulated transport failure')
            if failure == 'no_kind':
                return ExecResult(status=200, content=b'{"id": 1}', content_type='application/json')
            status = 500 if failure == 'server_error' else 429
            return ExecResult(status=status, content=b'{"detail": "x"}', content_type='application/json')

        server, executor = make_server(catalog, headers=auth_headers)
        assert isinstance(executor, RecordingExecutor)
        executor.refuse = refuse
        result = await call(
            server, 'execute_task', {'task_id': task['id'], 'expected_company_id': 1, 'expected_version': 1}
        )
        assert result.is_error
        assert result.structured_content['status'] == 503
        assert result.structured_content['detail'] == AGENT_TASK_UNVERIFIED_DETAIL
        assert ('POST', f'{read_path}/execute') not in executor.calls
        db_session.expire_all()
        assert db_session.query(Comment).count() == 0
        assert db_session.get(HankTask, task['id']).status == 'awaiting_review'


class TestWiring:
    def test_the_kind_set_is_the_hank_services_and_every_kind_is_registered(self):
        assert PEOPLE_ONLY_HANK_TASK_KINDS is PEOPLE_ONLY_KINDS
        assert PEOPLE_ONLY_HANK_TASK_KINDS == {'add_note'}
        assert PEOPLE_ONLY_HANK_TASK_KINDS <= set(INPUT_SCHEMAS)
        assert 'change_due_date' not in PEOPLE_ONLY_HANK_TASK_KINDS

    def test_the_guarded_routes_are_the_live_prepare_and_execute_tools(self, catalog):
        by_route = {(tool.method, tool.path): tool.name for tool in catalog}
        assert by_route[HANK_PREPARE_ROUTE] == 'prepare_task'
        assert by_route[HANK_EXECUTE_ROUTE] == 'execute_task'
        # No convenience tool reaches either route around the check (it guards generated tools).
        assert all(not (routes & PEOPLE_ONLY_ROUTES) for routes in CONVENIENCE_TOOL_ROUTES.values())

    def test_the_catalog_is_unchanged_and_the_instructions_disclose_the_rule(self, catalog):
        names = {tool.name for tool in catalog}
        assert {'prepare_task', 'execute_task', 'get_task', 'list_tasks', 'cancel_task'} <= names
        assert "kind 'add_note'" in DEFAULT_INSTRUCTIONS and 'execute_task' in DEFAULT_INSTRUCTIONS
