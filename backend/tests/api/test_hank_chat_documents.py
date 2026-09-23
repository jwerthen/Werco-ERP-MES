"""Uploaded evidence remains private and is reused without another PDF/model call."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.endpoints import copilot as endpoint
from app.models.hank_intake import HankIntakeBatch, HankIntakeFile
from app.schemas.copilot import CopilotChatRequest
from app.schemas.hank_intake import IntakeExtraction
from app.services import copilot_service
from app.services.copilot_service import CopilotService
from app.services.hank_chat_documents import attachment_manifest, document_evidence


@pytest.fixture
def source(db_session, test_user):
    batch = HankIntakeBatch(
        company_id=1, owner_id=test_user.id, credential_key='user', request_key=str(uuid4()), request_hash='a' * 64
    )
    db_session.add(batch)
    db_session.flush()
    row = HankIntakeFile(
        company_id=1,
        batch_id=batch.id,
        ordinal=0,
        filename='delivery.pdf',
        content_sha256='b' * 64,
        storage_ref='private/source.pdf',
        file_size=100,
        status='awaiting_review',
        version=2,
        page_count=1,
        analysis_json=IntakeExtraction(
            classification='packing_slip',
            confidence='high',
            summary='Delivered material from PO-123.',
            evidence=[{'page': 1, 'excerpt': 'PO-123'}],
            fields=[
                {
                    'name': 'po_number',
                    'value': 'PO-123',
                    'confidence': 'high',
                    'evidence': [{'page': 1, 'excerpt': 'PO-123'}],
                }
            ],
            lines=[
                {
                    'part_number': f'P-{i}',
                    'quantity': '2',
                    'confidence': 'high',
                    'evidence': [{'page': 1, 'excerpt': f'P-{i} 2 EA'}],
                }
                for i in range(12)
            ],
        ).model_dump(),
    )
    db_session.add(row)
    db_session.commit()
    return row


def test_manifest_contains_metadata_not_source_bytes_or_all_lines(db_session, test_user, source):
    manifest, refs = attachment_manifest(db_session, 1, test_user, [source.id, source.id])
    assert len(manifest) == len(refs) == 1
    assert manifest[0]['line_count'] == 12
    assert 'lines' not in manifest[0] and 'storage_ref' not in manifest[0]
    assert refs[0]['url'] == f'/?hank_work=intake&hank_id={source.id}'


def test_evidence_pagination_preserves_complete_source_lines(db_session, test_user, source):
    first = document_evidence(db=db_session, company_id=1, user=test_user, file_id=source.id)
    assert len(first['data']['items']) == 5 and first['data']['next_offset'] == 5
    second = document_evidence(db=db_session, company_id=1, user=test_user, file_id=source.id, offset=5)
    assert [line['index'] for line in second['data']['items']] == list(range(5, 10))
    last = document_evidence(db=db_session, company_id=1, user=test_user, file_id=source.id, offset=10)
    assert len(last['data']['items']) == 2 and last['data']['next_offset'] is None
    assert first['data']['items'][0]['evidence'] == [{'page': 1, 'excerpt': 'P-0 2 EA'}]


@pytest.mark.parametrize('changes', [{'offset': -1}, {'limit': 50}, {'section': 'storage_ref'}, {'file_id': True}])
def test_evidence_rejects_invalid_tool_arguments(db_session, test_user, source, changes):
    args = {'db': db_session, 'company_id': 1, 'user': test_user, 'file_id': source.id, **changes}
    assert document_evidence(**args)['is_error']


@pytest.mark.parametrize('change', ['owner', 'company', 'credential', 'cancelled', 'queued'])
def test_manifest_rechecks_source_authority_and_state(db_session, test_user, source, change):
    batch = db_session.get(HankIntakeBatch, source.batch_id)
    if change == 'owner':
        batch.owner_id = test_user.id + 100
    elif change == 'company':
        source.company_id = 2
    elif change == 'credential':
        batch.credential_key = 'api:1'
    else:
        source.status = change
    db_session.flush()
    with pytest.raises(HTTPException) as error:
        attachment_manifest(db_session, 1, test_user, [source.id])
    assert error.value.status_code in (404, 409)


def test_attachment_validation_runs_before_ai_call(client, auth_headers, source, db_session, monkeypatch):
    source.status = 'queued'
    db_session.commit()

    def forbidden(*args, **kwargs):
        pytest.fail('Invalid document attachment reached the model')

    monkeypatch.setattr(copilot_service, 'run_llm_task', forbidden)
    endpoint._rate_buckets.clear()
    response = client.post(
        '/api/v1/copilot/chat',
        headers=auth_headers,
        json={
            'messages': [{'role': 'user', 'content': 'Receive this material'}],
            'intake_file_ids': [source.id],
        },
    )
    assert response.status_code == 409
    endpoint._rate_buckets.clear()


def test_chat_attaches_saved_manifest_and_returns_pdf_link_without_reextracting(
    client, auth_headers, source, monkeypatch
):
    calls = []

    def model(ctx, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            model='claude-sonnet-4-6',
            raw_response=SimpleNamespace(
                content=[SimpleNamespace(type='text', text='Review the delivery PDF.')], stop_reason='end_turn'
            ),
        )

    def no_extract(*args, **kwargs):
        pytest.fail('Chat must reuse saved PDF extraction')

    monkeypatch.setattr(copilot_service, 'run_llm_task', model)
    monkeypatch.setattr('app.services.hank_intake_service.analyze_pdf', no_extract)
    endpoint._rate_buckets.clear()
    response = client.post(
        '/api/v1/copilot/chat?stream=false',
        headers=auth_headers,
        json={
            'messages': [{'role': 'user', 'content': 'What is in this PDF?'}],
            'intake_file_ids': [source.id],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()['references'][0]['id'] == source.id
    assert len(calls) == 1 and calls[0]['cache_conversation'] is True
    contents = calls[0]['messages'][0]['content']
    assert all(block['type'] == 'text' for block in contents)
    assert str(source.id) in contents[0]['text'] and 'attached_pdf_evidence' in contents[0]['text']
    assert 'storage_ref' not in json.dumps(contents)
    endpoint._rate_buckets.clear()


def test_evidence_tool_preserves_tenant_injection(db_session, test_user, source):
    service = CopilotService(db_session, company_id=1, user=test_user)
    result = service.execute_tool(
        'hank_document_evidence', {'file_id': source.id, 'company_id': 999, 'section': 'fields'}
    )
    assert not result.is_error and result.payload['items'][0]['value'] == 'PO-123'


@pytest.mark.parametrize('attach', [True, False])
def test_receiving_from_pdf_cannot_omit_source_provenance(db_session, test_user, source, attach):
    service = CopilotService(db_session, company_id=1, user=test_user)
    if attach:
        service.attach_documents([source.id])
    else:
        service.execute_tool('hank_document_evidence', {'file_id': source.id})
    result = service.execute_tool('prepare_hank_task', {'kind': 'receive_delivery', 'input': {'purchase_order_id': 1}})
    assert result.is_error and 'source_intake_file_id' in result.payload['error']


def test_receiving_document_tool_limits_chat_payload_without_reextracting(db_session, test_user, source, monkeypatch):
    from app.schemas.hank_intake import IntakeReceivingDraft, IntakeReceivingLine
    from app.services.hank_chat_documents import receiving_document
    from app.services.hank_intake_receiving_service import HankIntakeReceivingService

    draft = IntakeReceivingDraft(
        file_id=source.id,
        file_version=source.version,
        company_id=1,
        filename=source.filename,
        lines=[IntakeReceivingLine(source_line_index=i, part_number=f'P-{i}') for i in range(12)],
    )
    monkeypatch.setattr(HankIntakeReceivingService, 'draft', lambda *args: draft)
    result = receiving_document(db=db_session, company_id=1, user=test_user, file_id=source.id, offset=5)
    assert result['data']['line_count'] == 12 and len(result['data']['lines']) == 5
    assert result['data']['next_offset'] == 10
    assert result['data']['source_intake_file_id'] == source.id
    assert result['data']['source_intake_version'] == source.version


@pytest.mark.parametrize('ids', [[0], [-1], list(range(1, 7))])
def test_attachment_request_bounds(ids):
    with pytest.raises(ValueError):
        CopilotChatRequest(messages=[{'role': 'user', 'content': 'Read this'}], intake_file_ids=ids)
