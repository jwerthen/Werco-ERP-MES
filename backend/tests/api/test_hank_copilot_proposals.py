"""Chat can persist reviewed proposals but cannot execute ERP business actions."""

import json

import pytest

from app.api.endpoints import copilot as endpoint
from app.models.hank import HankTask
from app.models.work_order import WorkOrder
from app.services import copilot_service
from app.services.copilot_service import CopilotService

pytestmark = pytest.mark.api


def test_chat_proposal_injects_company_and_changes_no_job(db_session, test_user, test_work_order):
    before = db_session.query(WorkOrder).count()
    service = CopilotService(db_session, company_id=1, user=test_user)
    result = service.execute_tool(
        'prepare_hank_task',
        {
            'kind': 'repeat_job',
            'company_id': 999,
            'input': {'source_work_order_id': test_work_order.id, 'quantity_ordered': 3},
        },
    )
    assert not result.is_error, result.payload
    # No audited transaction remains open during the next external model call.
    assert not db_session.in_transaction()
    db_session.rollback()  # a later chat failure cannot erase this proposal
    task = db_session.query(HankTask).one()
    assert task.company_id == 1 and task.owner_id == test_user.id
    assert task.status == 'awaiting_review'
    assert result.references[0]['url'] == f'/?hank_task={task.id}'
    assert db_session.query(WorkOrder).count() == before
    assert 'execute_hank_task' not in [tool.name for tool in service.tool_specs_for_user()]


def test_chat_rejects_incomplete_proposal(db_session, test_user):
    service = CopilotService(db_session, company_id=1, user=test_user)
    result = service.execute_tool('prepare_hank_task', {'kind': 'repeat_job', 'input': {}})
    assert result.is_error
    assert db_session.query(HankTask).count() == 0


def test_commit_failure_emits_error_without_final_receipt(client, auth_headers, db_session, monkeypatch):
    endpoint._rate_buckets.clear()

    def fake_stream(self, **kwargs):
        yield {'type': 'delta', 'text': 'Review the prepared task.'}
        yield {
            'type': 'final',
            'answer': 'Review the prepared task.',
            'references': [],
            'tool_trace': [],
            'interaction_id': None,
            'rounds': 0,
            'truncated': False,
        }

    def fail_commit():
        raise RuntimeError('simulated transaction failure')

    monkeypatch.setattr(copilot_service.CopilotService, 'stream_chat', fake_stream)
    monkeypatch.setattr(db_session, 'commit', fail_commit)
    response = client.post(
        '/api/v1/copilot/chat',
        headers=auth_headers,
        json={'messages': [{'role': 'user', 'content': 'Prepare a repeat job'}]},
    )
    frames = [json.loads(chunk[6:]) for chunk in response.text.split('\n\n') if chunk.startswith('data: ')]
    assert any(frame['type'] == 'error' for frame in frames)
    assert not any(frame['type'] == 'final' for frame in frames)
    endpoint._rate_buckets.clear()
