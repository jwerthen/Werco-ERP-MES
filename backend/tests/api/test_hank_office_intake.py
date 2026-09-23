"""Office sources use the existing private, durable intake and reviewed filing flow."""

import io
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from openpyxl import load_workbook
from sqlalchemy.orm import sessionmaker

from app.models.document import Document, DocumentType
from app.models.hank_intake import HankIntakeBatch
from app.schemas.hank_intake import IntakeCommand, IntakePlanCommand, IntakePlanInput
from app.services import hank_intake_service as intake
from app.services.hank_office_reader import MIME_BY_FORMAT, OfficeReadError
from tests.api.test_hank_intake import audit, service, storage  # noqa: F401
from tests.services.test_hank_office_reader import excel_document, legacy_excel_document, word_document

BASE = '/api/v1/hank/intake'


def upload_office(service, content=None, filename='PO.docx'):
    return service.upload(1, str(uuid4()), [(filename, content or word_document())], audit(service)).files[0]


def model_result(*, locator='Document table 1 row 2', more=False, quantity='12', excerpt=None):
    excerpt = excerpt or 'MAT-001 | 12 | EA | 4.25'
    return SimpleNamespace(
        raw_response=SimpleNamespace(
            stop_reason='tool_use',
            content=[
                SimpleNamespace(
                    type='tool_use',
                    name='record_intake',
                    input={
                        'classification': 'purchase_order',
                        'confidence': 'high',
                        'summary': 'Order PO-WORD-123',
                        'source_format': 'pdf',
                        'source_labels': ['Model-invented label'],
                        'has_more_lines': more,
                        'evidence': [
                            {'page': 1, 'excerpt': 'Purchase Order PO-WORD-123', 'locator': 'Document paragraph 1'}
                        ],
                        'fields': [
                            {
                                'name': 'po_number',
                                'value': 'PO-WORD-123',
                                'confidence': 'high',
                                'evidence': [
                                    {
                                        'page': 1,
                                        'excerpt': 'Purchase Order PO-WORD-123',
                                        'locator': 'Document paragraph 1',
                                    }
                                ],
                            }
                        ],
                        'lines': [
                            {
                                'description': 'Material',
                                'part_number': 'MAT-001',
                                'quantity': quantity,
                                'unit_of_measure': 'EA',
                                'unit_price': '4.25',
                                'confidence': 'high',
                                'evidence': [{'page': 1, 'excerpt': excerpt, 'locator': locator}],
                            }
                        ],
                    },
                )
            ],
        )
    )


@pytest.mark.parametrize(
    'factory,extension', [(word_document, 'docx'), (excel_document, 'xlsx'), (legacy_excel_document, 'xls')]
)
def test_office_upload_download_preview_are_owned_hash_verified_and_do_not_call_ai(
    client, auth_headers, admin_headers, service, storage, monkeypatch, factory, extension
):
    monkeypatch.setattr(
        intake, 'run_llm_task', lambda *args, **kwargs: pytest.fail('Preview/upload must not call Claude')
    )
    content, key = factory(), str(uuid4())

    def upload():
        return client.post(
            BASE,
            headers=auth_headers,
            data={'expected_company_id': 1, 'request_key': key},
            files=[('files', (f'PO.{extension}', content, 'application/octet-stream'))],
        )

    response = upload()
    assert response.status_code == 200, response.text
    assert upload().json() == response.json()
    file = response.json()['files'][0]
    assert file['source_format'] == extension
    assert len(storage.items) == 1
    assert next(iter(storage.items)).endswith('.' + extension)
    source = client.get(file['source_url'], headers=auth_headers)
    assert source.status_code == 200 and source.content == content
    assert source.headers['content-type'] == MIME_BY_FORMAT[extension]
    assert source.headers['content-disposition'].startswith('attachment;')
    preview_url = f'{BASE}/files/{file["id"]}/source-preview'
    preview = client.get(preview_url, headers=auth_headers)
    assert preview.status_code == 200, preview.text
    assert preview.headers['cache-control'] == 'private, no-store'
    assert preview.headers['content-type'].startswith('application/json')
    assert preview.json()['format'] == extension
    assert 'MAT-001' in '\n'.join(preview.json()['units'])
    assert client.get(preview_url, headers=admin_headers).status_code == 404
    assert client.get(file['source_url'], headers=admin_headers).status_code == 404
    ref = service.file(file['id']).storage_ref
    service.db.rollback()
    storage.items[ref] = b'x' * len(content)
    assert client.get(preview_url, headers=auth_headers).status_code == 409


def test_disguised_or_encrypted_upload_stores_nothing(client, auth_headers, service, storage):
    for filename, content in [
        ('PO.docx', excel_document()),
        ('PO.xlsx', word_document()),
        ('PO.xls', legacy_excel_document(encrypted=True)),
        ('PO.doc', b'old binary word'),
    ]:
        response = client.post(
            BASE,
            headers=auth_headers,
            data={'expected_company_id': 1, 'request_key': str(uuid4())},
            files=[('files', (filename, content, MIME_BY_FORMAT['docx']))],
        )
        assert response.status_code == 422, response.text
    assert storage.items == {}
    assert service.db.query(HankIntakeBatch).count() == 0


def test_office_worker_extracts_text_once_without_database_transaction_and_stores_canonical_source(
    service, monkeypatch
):
    pending = upload_office(service)
    sessions, calls = [], []
    factory = sessionmaker(bind=service.db.get_bind())

    def session():
        db = factory()
        sessions.append(db)
        return db

    def model(context, **kwargs):
        assert all(not db.in_transaction() for db in sessions)
        calls.append((context, kwargs))
        return model_result()

    monkeypatch.setattr(intake, 'SessionLocal', session)
    monkeypatch.setattr(intake, 'run_llm_task', model)
    assert intake.process_intake_file(pending.id) == {'status': 'awaiting_review'}
    service.db.expire_all()
    saved = service.response_file(service.file(pending.id))
    assert saved.source_format == saved.analysis.source_format == 'docx'
    assert saved.source_labels == ['Document — section 1']
    assert saved.analysis.lines[0].confidence == 'high'
    assert saved.analysis.lines[0].evidence[0].locator == 'Document table 1 row 2'
    context, request = calls[0]
    assert context.has_pdf_document is False and request['max_tokens'] == 8192
    assert all(block['type'] == 'text' for block in request['messages'][0]['content'])
    assert 'MAT-001 | 12 | EA | 4.25' in request['messages'][0]['content'][0]['text']
    assert service.db.query(Document).count() == 0
    service.db.rollback()
    assert intake.process_intake_file(pending.id) == {'status': 'skipped'}
    assert len(calls) == 1


@pytest.mark.parametrize(
    'locator,quantity',
    [('Document table 1 row 1', '12'), ('Invented paragraph', '12'), ('Document table 1 row 2', '99')],
)
def test_office_unsupported_values_and_wrong_locations_are_not_high_confidence(monkeypatch, locator, quantity):
    monkeypatch.setattr(
        intake, 'run_llm_task', lambda *args, **kwargs: model_result(locator=locator, quantity=quantity)
    )
    extraction, count = intake.analyze_office(word_document(), 1, 'PO.docx')
    assert count == 1
    assert extraction.lines[0].confidence == 'low'
    if locator != 'Document table 1 row 2':
        assert extraction.lines[0].evidence[0].locator is None


def test_office_formula_evidence_cannot_be_treated_as_confirmed_value(monkeypatch):
    monkeypatch.setattr(
        intake,
        'run_llm_task',
        lambda *args, **kwargs: model_result(
            locator='Material PO!row 3', excerpt='A3=MAT-001 | B3=UNTRUSTED formula: no cached value | C3=EA | D3=4.25'
        ),
    )
    extraction, _ = intake.analyze_office(excel_document(formula=True), 1, 'PO.xlsx')
    assert extraction.lines[0].confidence == 'low'
    assert any('not evaluated' in warning for warning in extraction.warnings)


def test_unrelated_excel_formula_total_does_not_taint_supported_order_line(monkeypatch):
    workbook = load_workbook(io.BytesIO(excel_document()))
    workbook.active.append(['TOTAL', '=B3*D3'])
    stream = io.BytesIO()
    workbook.save(stream)
    monkeypatch.setattr(
        intake,
        'run_llm_task',
        lambda *args, **kwargs: model_result(
            locator='Material PO!row 3', excerpt='A3=MAT-001 | B3=12 | C3=EA | D3=4.25'
        ),
    )
    extraction, _ = intake.analyze_office(stream.getvalue(), 1, 'PO.xlsx')
    assert extraction.lines[0].confidence == 'high'
    assert any('not evaluated' in warning for warning in extraction.warnings)


def test_office_incomplete_line_set_is_rejected_instead_of_saved(monkeypatch):
    monkeypatch.setattr(intake, 'run_llm_task', lambda *args, **kwargs: model_result(more=True))
    with pytest.raises(intake.IntakeExtractionIncompleteError, match='50 relevant lines'):
        intake.analyze_office(word_document(), 1, 'PO.docx')


def test_office_parser_timeout_and_process_crash_are_controlled(monkeypatch):
    content = word_document()

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('reader', 20)

    monkeypatch.setattr(intake.subprocess, 'run', timeout)
    with pytest.raises(OfficeReadError, match='safe parsing limits'):
        intake.read_intake_document(content, 'PO.docx')
    monkeypatch.setattr(intake.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=-9, stdout=b''))
    with pytest.raises(OfficeReadError):
        intake.read_intake_document(content, 'PO.docx')


def test_office_filing_retains_native_mime_and_certificate_release_requires_pdf(service, monkeypatch):
    pending = upload_office(service)
    monkeypatch.setattr(intake, 'run_llm_task', lambda *args, **kwargs: model_result())
    extraction, _ = intake.analyze_office(word_document(), 1, 'PO.docx')
    row = service.file(pending.id)
    row.analysis_json = extraction.model_dump(mode='json')
    row.status = 'awaiting_review'
    service.db.commit()
    command = IntakePlanCommand(
        expected_company_id=1,
        expected_version=row.version,
        plan=IntakePlanInput(title='Purchase order source', document_type=DocumentType.OTHER),
    )
    row = service.prepare(row.id, command, audit(service))
    service.db.commit()
    result = service.execute(row.id, IntakeCommand(expected_company_id=1, expected_version=row.version), audit(service))
    service.db.commit()
    document = service.db.get(Document, result.result_json['document_id'])
    assert document.mime_type == MIME_BY_FORMAT['docx']
    assert document.file_name == 'PO.docx' and document.file_path.endswith('.docx')
    pending = upload_office(service)
    row = service.file(pending.id)
    row.status = 'awaiting_review'
    service.db.commit()
    command = IntakePlanCommand(
        expected_company_id=1,
        expected_version=row.version,
        plan=IntakePlanInput(
            filing_mode='release_receipt_certificate',
            title='Unsafe certificate',
            document_type=DocumentType.MATERIAL_CERT,
        ),
    )
    with pytest.raises(HTTPException, match='Receipt certificates must'):
        service.prepare(row.id, command, audit(service))
