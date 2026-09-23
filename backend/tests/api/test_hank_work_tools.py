"""New chat tools expose actual scoped evidence and can only prepare operational writes."""

import json

import pytest

from app.models.hank import HankTask
from app.models.purchasing import POReceipt
from app.models.role_permission import RolePermission
from app.models.work_order import WorkOrder
from app.services.copilot_service import CopilotService
from app.services.hank_copilot_tools import TASK_INPUT_SCHEMA
from app.services.prompts import COPILOT_CHAT_PROMPT, HANK_INTAKE_PROMPT, PROMPT_REGISTRY

from .test_hank_operations import delivery_input, po, running
from .test_hank_teamwork import create, handoff_body

pytestmark = [pytest.mark.api]


def test_all_six_reviewed_schemas_are_deterministic_and_no_execute_tool_exists(db_session, test_user):
    service = CopilotService(db_session, company_id=1, user=test_user)
    assert set(TASK_INPUT_SCHEMA['properties']['kind']['enum']) == {
        'repeat_job',
        'draft_purchase_order',
        'attach_document',
        'receive_delivery',
        'report_production',
        'draft_shipment',
    }
    assert len(TASK_INPUT_SCHEMA['properties']['input']['anyOf']) == 6
    assert 'expected_company_id' not in json.dumps(TASK_INPUT_SCHEMA)
    assert 'request_key' not in json.dumps(TASK_INPUT_SCHEMA)
    assert {tool.name for tool in service.tool_specs_for_user()} >= {
        'hank_operational_report',
        'hank_action_context',
        'hank_saved_work',
        'prepare_hank_task',
    }
    assert not any(tool.name.startswith('execute') for tool in service.tool_specs_for_user())
    assert COPILOT_CHAT_PROMPT.version == '1.7.0'
    assert PROMPT_REGISTRY[HANK_INTAKE_PROMPT.id] is HANK_INTAKE_PROMPT


def test_chat_reads_exact_po_lines_and_prepares_without_receiving_stock(db_session, test_user, po):
    service = CopilotService(db_session, company_id=1, user=test_user)
    context = service.execute_tool('hank_action_context', {'kind': 'receiving', 'purchase_order_id': po[0].id})
    assert not context.is_error, context.payload
    assert context.payload['lines'][0]['po_line_id'] == po[1].id
    assert context.payload['lines'][0]['quantity_ordered'] == 10
    result = service.execute_tool(
        'prepare_hank_task', {'kind': 'receive_delivery', 'input': delivery_input(po), 'company_id': 900}
    )
    assert not result.is_error, result.payload
    assert not db_session.in_transaction()
    task = db_session.query(HankTask).one()
    assert task.status == 'awaiting_review' and task.company_id == 1 and task.kind == 'receive_delivery'
    assert db_session.query(POReceipt).count() == 0


def test_active_job_tool_only_returns_own_open_clock_and_honors_permission(db_session, test_user, running):
    service = CopilotService(db_session, company_id=1, user=test_user)
    result = service.execute_tool('hank_action_context', {'kind': 'active_job'})
    assert not result.is_error, result.payload
    assert result.payload['active_jobs'][0]['operation_id'] == running[0].id
    assert 'deltas' in result.payload['instruction']
    db_session.add(RolePermission(company_id=1, role=test_user.role, permissions=['inventory:view']))
    db_session.commit()
    assert service.execute_tool('hank_action_context', {'kind': 'active_job'}).is_error


def test_live_report_has_source_evidence_without_creating_work(db_session, test_user, test_work_order):
    before = db_session.query(WorkOrder).count()
    service = CopilotService(db_session, company_id=1, user=test_user)
    result = service.execute_tool('hank_operational_report', {'report': 'readiness', 'record_id': test_work_order.id})
    assert not result.is_error, result.payload
    assert result.references and result.payload['coverage_notes']
    assert db_session.query(WorkOrder).count() == before and db_session.query(HankTask).count() == 0
    assert service.execute_tool('hank_operational_report', {'report': 'readiness', 'record_id': 'bad'}).is_error


def test_saved_work_tool_keeps_participant_handoff_private(
    client, auth_headers, db_session, test_user, operator_user, admin_user, test_work_order
):
    handoff = create(client, auth_headers, handoff_body(test_work_order, operator_user))
    own = CopilotService(db_session, company_id=1, user=test_user)
    found = own.execute_tool('hank_saved_work', {'kind': 'handoff', 'record_id': handoff['id']})
    assert not found.is_error and found.payload['status'] == 'open'
    other = CopilotService(db_session, company_id=1, user=admin_user)
    assert other.execute_tool('hank_saved_work', {'kind': 'handoff', 'record_id': handoff['id']}).is_error
