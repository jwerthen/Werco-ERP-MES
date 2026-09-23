"""Anthropic tool-schema contract and server-side action binding regression checks."""

from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator
from jsonschema import ValidationError as JSONSchemaValidationError
from pydantic import ValidationError

from app.schemas.hank_tasks import HankTaskCreate
from app.services.copilot_service import TOOL_REGISTRY, anthropic_tool_definitions
from app.services.hank_copilot_tools import TASK_INPUT_SCHEMA, _proposal_schema, prepare_task

pytestmark = pytest.mark.unit


def test_all_chat_tools_have_anthropic_compatible_schema_roots():
    # This request boundary was previously only exercised by permissive mocks.
    # Even an unused tool with a root combinator rejects the entire chat call.
    for tool in anthropic_tool_definitions(TOOL_REGISTRY):
        schema = tool['input_schema']
        assert schema['type'] == 'object', tool['name']
        assert not {'oneOf', 'anyOf', 'allOf'} & schema.keys(), tool['name']
        Draft202012Validator.check_schema(schema)


def test_proposal_schema_preserves_valid_action_inputs_and_is_stable():
    assert TASK_INPUT_SCHEMA == _proposal_schema()
    validator = Draft202012Validator(TASK_INPUT_SCHEMA)
    validator.validate({'kind': 'repeat_job', 'input': {'source_work_order_id': 1, 'quantity_ordered': 2}})
    validator.validate({'kind': 'attach_document', 'input': {'document_id': 1, 'work_order_id': 2}})
    with pytest.raises(JSONSchemaValidationError):
        validator.validate({'kind': 'repeat_job', 'input': {}})


def test_selected_kind_is_still_validated_server_side_before_persisting():
    # The nested API schema exposes a union of input shapes. A shape belonging
    # to another kind must still be refused by the authoritative task schema.
    mismatched = {'kind': 'repeat_job', 'input': {'document_id': 1, 'work_order_id': 2}}
    Draft202012Validator(TASK_INPUT_SCHEMA).validate(mismatched)
    with pytest.raises(ValidationError):
        HankTaskCreate(expected_company_id=1, request_key=str(uuid4()), **mismatched)
    # No DB or user is needed: invalid input exits before task storage/access.
    result = prepare_task(db=None, company_id=1, user=None, **mismatched)
    assert result['is_error'] is True
