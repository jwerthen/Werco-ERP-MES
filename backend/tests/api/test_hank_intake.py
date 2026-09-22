"""Durable source retention, isolated extraction and employee-reviewed filing."""

import io
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from reportlab.pdfgen import canvas
from sqlalchemy.orm import sessionmaker

from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.document import Document, DocumentType
from app.models.hank_intake import HankIntakeBatch, HankIntakeFile
from app.models.part import Part, PartType
from app.models.purchasing import POReceipt, PurchaseOrder, PurchaseOrderLine, ReceiptStatus, Vendor
from app.models.role_permission import RolePermission
from app.models.user import UserRole
from app.schemas.hank_intake import IntakeCommand, IntakeExtraction, IntakePlanCommand, IntakePlanInput
from app.services import hank_intake_service as intake
from app.services.audit_service import AuditService, AuditWriteError
from app.services.llm_client import LLMEgressDisabledError

BASE = '/api/v1/hank/intake'


def pdf(text='Purchase order PO-123', pages=1):
    stream = io.BytesIO()
    document = canvas.Canvas(stream, invariant=1)
    for _ in range(pages):
        document.drawString(30, 700, text)
        document.showPage()
    document.save()
    return stream.getvalue()


@pytest.fixture
def storage(monkeypatch, db_session):
    class MemoryStorage:
        is_remote = True

        def __init__(self):
            self.items = {}

        def save(self, content, *, key):
            assert not db_session.in_transaction(), 'Storage save held the request transaction'
            self.items[key] = content
            return key

        def delete(self, ref):
            assert not db_session.in_transaction()
            self.items.pop(ref, None)

        def read_bytes(self, ref):
            assert not db_session.in_transaction(), 'Storage read held the request transaction'
            return self.items[ref]

        def open_stream(self, ref):
            yield self.read_bytes(ref)

    backend = MemoryStorage()
    monkeypatch.setattr(intake, 'get_storage', lambda: backend)
    monkeypatch.setattr(intake, 'backend_for_ref', lambda ref: backend)
    monkeypatch.setattr(intake, 'enqueue_intake', lambda *args: False)
    monkeypatch.setattr('app.api.endpoints.hank_intake.enqueue_intake', lambda *args: False)
    return backend


@pytest.fixture
def service(db_session, test_user, storage):
    return intake.HankIntakeService(db_session, test_user, 1)


def audit(service):
    return AuditService(service.db, user=service.user, company_id=service.company_id)


def uploaded(service, content=None, key=None):
    return service.upload(1, key or str(uuid4()), [('source.pdf', content or pdf())], audit(service)).files[0]


def reviewed(service, *, plan=None):
    result = uploaded(service)
    row = service.file(result.id)
    row.status = 'awaiting_review'
    row.analysis_json = IntakeExtraction(
        classification='material_certificate', confidence='unknown', summary='Review'
    ).model_dump()
    service.db.commit()
    command = IntakePlanCommand(
        expected_company_id=1,
        expected_version=row.version,
        plan=plan or IntakePlanInput(title='Reviewed source', document_type=DocumentType.OTHER),
    )
    row = service.prepare(row.id, command, audit(service))
    response = service.response_file(row)
    service.db.commit()
    return response


def command(row):
    return IntakeCommand(expected_company_id=1, expected_version=row.version)


@pytest.fixture
def receipt(db_session, test_user, test_part):
    vendor = Vendor(company_id=1, code='INTAKE', name='Intake supplier')
    db_session.add(vendor)
    db_session.flush()
    po = PurchaseOrder(company_id=1, po_number='INTAKE-PO', vendor_id=vendor.id)
    db_session.add(po)
    db_session.flush()
    line = PurchaseOrderLine(
        company_id=1, purchase_order_id=po.id, line_number=1, part_id=test_part.id, quantity_ordered=5, unit_price=1
    )
    db_session.add(line)
    db_session.flush()
    row = POReceipt(
        company_id=1,
        receipt_number='INTAKE-R',
        po_line_id=line.id,
        quantity_received=5,
        lot_number='LOT',
        status=ReceiptStatus.PENDING_INSPECTION,
        received_by=test_user.id,
    )
    db_session.add(row)
    db_session.commit()
    return row, line, po, vendor


def certificate_plan(receipt):
    row, line, po, vendor = receipt
    return IntakePlanInput(
        filing_mode='release_receipt_certificate',
        title='Reviewed mill certificate',
        document_type=DocumentType.MATERIAL_CERT,
        receipt_id=row.id,
        part_id=line.part_id,
        vendor_id=vendor.id,
        purchase_order_id=po.id,
    )


def test_api_upload_replay_stream_and_owner_scope(client, db_session, auth_headers, admin_headers, storage):
    key, content = str(uuid4()), pdf()

    def upload(data=content):
        return client.post(
            BASE,
            headers=auth_headers,
            data={'expected_company_id': 1, 'request_key': key},
            files=[('files', ('source.pdf', data, 'application/pdf'))],
        )

    first = upload()
    assert first.status_code == 200, first.text
    assert upload().json() == first.json()
    assert len(storage.items) == 1
    assert upload(pdf('different')).status_code == 409
    batch = first.json()
    source = client.get(batch['files'][0]['source_url'], headers=auth_headers)
    assert source.status_code == 200 and source.content == content
    assert source.headers['cache-control'] == 'private, no-store'
    assert client.get(f'{BASE}/{batch["id"]}', headers=admin_headers).status_code == 404
    exact = f'{BASE}/files/{batch["files"][0]["id"]}'
    assert client.get(exact, headers=auth_headers).json() == batch['files'][0]
    assert client.get(exact, headers=admin_headers).status_code == 404
    assert client.get(batch['files'][0]['source_url'], headers=admin_headers).status_code == 404
    assert client.get(BASE, headers=admin_headers).json()['batches'] == []
    assert db_session.query(AuditLog).filter_by(resource_type='hank_intake_batch').count() == 1


def test_upload_does_not_erase_bytes_after_unknown_commit(service, storage, monkeypatch):
    key, content = str(uuid4()), pdf()
    original = service.db.commit

    def uncertain():
        original()
        raise RuntimeError('connection lost after commit')

    monkeypatch.setattr(service.db, 'commit', uncertain)
    with pytest.raises(RuntimeError):
        uploaded(service, content, key)
    assert len(storage.items) == 1
    monkeypatch.setattr(service.db, 'commit', original)
    recovered = uploaded(service, content, key)
    assert recovered.status == 'queued'
    assert service.db.query(HankIntakeBatch).count() == 1
    assert len(storage.items) == 1


def test_source_preview_rejects_changed_hash_size_and_oversize(client, auth_headers, service, storage):
    source = uploaded(service)
    ref = service.file(source.id).storage_ref
    original = storage.items[ref]
    for corrupt in (b'x' * len(original), original[:-1], original + b'x', b'x' * (intake.MAX_FILE_BYTES + 1)):
        storage.items[ref] = corrupt
        response = client.get(source.source_url, headers=auth_headers)
        assert response.status_code == 409
        assert response.headers['content-type'].startswith('application/json')


def test_filed_intake_document_metadata_and_source_are_retained(client, auth_headers, service, storage, monkeypatch):
    ready = reviewed(service)
    result = service.response_file(service.execute(ready.id, command(ready), audit(service)))
    service.db.commit()
    before = dict(storage.items)
    monkeypatch.setattr(
        'app.api.endpoints.documents.delete_ref', lambda ref: pytest.fail('Retained intake source must not be deleted')
    )
    response = client.delete(f'/api/v1/documents/{result.result.document_id}', headers=auth_headers)
    assert response.status_code == 409 and 'retained intake evidence' in response.text
    assert service.db.get(Document, result.result.document_id) is not None
    assert storage.items == before
    service.db.rollback()
    assert client.get(result.source_url, headers=auth_headers).status_code == 200


def test_upload_required_audit_failure_cleans_unpublished_bytes(service, storage, monkeypatch):
    monkeypatch.setattr(
        AuditService, 'log_required', lambda *args, **kwargs: (_ for _ in ()).throw(AuditWriteError('no'))
    )
    with pytest.raises(AuditWriteError):
        uploaded(service)
    assert storage.items == {}
    assert service.db.query(HankIntakeBatch).count() == 0


@pytest.mark.parametrize(
    'files',
    [[('bad.pdf', b'not PDF')], [('large.pdf', b'%PDF-' + b'a' * intake.MAX_FILE_BYTES)], [('a.pdf', b'%PDF-')] * 6],
)
def test_upload_bounds_do_not_write_storage(service, storage, files):
    with pytest.raises(HTTPException):
        service.upload(1, str(uuid4()), files, audit(service))
    assert storage.items == {}


def test_draft_plan_is_inert_execute_is_idempotent(service, storage):
    ready = reviewed(service)
    assert service.db.query(Document).count() == 0
    done = service.execute(ready.id, command(ready), audit(service))
    response = service.response_file(done)
    service.db.commit()
    assert response.status == 'completed'
    record = service.db.query(Document).one()
    assert record.status == 'draft' and record.released_at is None
    record_id = record.id
    replay = service.response_file(service.execute(ready.id, command(ready), audit(service)))
    assert replay == response
    assert service.db.query(Document).count() == 1
    assert service.db.query(AuditLog).filter_by(resource_type='document', resource_id=record_id).count() == 1


def test_receipt_certificate_three_atomic_audits_and_completed_replay(service, receipt, monkeypatch):
    monkeypatch.setattr(
        'app.services.receiving_delivery_service.ref_exists',
        lambda ref: pytest.fail('Certificate storage I/O occurred inside the filing transaction'),
    )
    plan = certificate_plan(receipt)
    receipt_id = receipt[0].id
    ready = reviewed(service, plan=plan)
    assert any('release' in change and 'INTAKE-R' in change for change in ready.plan.changes)
    done = service.response_file(service.execute(ready.id, command(ready), audit(service)))
    service.db.commit()
    record = service.db.query(Document).one()
    actual = service.db.get(POReceipt, receipt_id)
    assert record.status == 'released' and record.released_by == service.user.id
    assert actual.certificate_document_id == record.id and actual.coc_attached
    assert actual.status == ReceiptStatus.PENDING_INSPECTION and actual.quantity_accepted == 0
    assert service.db.query(AuditLog).filter_by(resource_type='po_receipt', resource_id=receipt_id).count() == 1
    assert service.response_file(service.execute(ready.id, command(ready), audit(service))) == done


@pytest.mark.parametrize('resource_type', ['document', 'po_receipt', 'hank_intake_file'])
def test_filing_rolls_back_when_any_required_audit_refuses(service, receipt, monkeypatch, resource_type):
    plan = certificate_plan(receipt)
    ready = reviewed(service, plan=plan)
    original = AuditService.log_required

    def refusing(self, action, resource, **kwargs):
        if resource == resource_type:
            raise AuditWriteError('refused')
        return original(self, action, resource, **kwargs)

    monkeypatch.setattr(AuditService, 'log_required', refusing)
    with pytest.raises(AuditWriteError):
        service.execute(ready.id, command(ready), audit(service))
    service.db.rollback()
    assert service.db.query(Document).count() == 0
    assert service.db.get(POReceipt, plan.receipt_id).certificate_document_id is None
    assert service.file(ready.id).status == 'planned'
    assert service.db.query(AuditLog).filter(AuditLog.resource_type.in_(['document', 'po_receipt'])).count() == 0


def test_receipt_requires_exact_part_supplier_and_never_replaces_certificate(service, receipt, test_part):
    plan = certificate_plan(receipt)
    plan.vendor_id = None
    with pytest.raises(HTTPException) as failure:
        reviewed(service, plan=plan)
    assert failure.value.status_code == 422
    service.db.rollback()
    record = Document(
        company_id=1, document_number='EXISTING', title='Existing', document_type=DocumentType.MATERIAL_CERT
    )
    service.db.add(record)
    service.db.flush()
    receipt[0].certificate_document_id = record.id
    service.db.commit()
    with pytest.raises(HTTPException) as failure:
        service._links(certificate_plan(receipt))
    assert failure.value.status_code == 409


def test_source_record_and_bytes_drift_rejects_stale_review(service, receipt, storage):
    plan = certificate_plan(receipt)
    ready = reviewed(service, plan=plan)
    receipt[0].lot_number = 'CHANGED'
    service.db.commit()
    with pytest.raises(HTTPException) as failure:
        service.execute(ready.id, command(ready), audit(service))
    assert failure.value.status_code == 409
    service.db.rollback()
    row = service.file(ready.id)
    ref = row.storage_ref
    service.db.rollback()
    storage.items[ref] = b'%PDF-different'
    with pytest.raises(HTTPException) as failure:
        service.execute(ready.id, command(ready), audit(service))
    assert failure.value.status_code == 409
    assert service.db.query(Document).count() == 0


def test_duplicate_hash_requires_ack_and_concurrent_completed_duplicate_invalidates_plan(service):
    first = reviewed(service)
    second = uploaded(service, pdf())
    row = service.file(second.id)
    row.status = 'awaiting_review'
    service.db.commit()
    payload = IntakePlanCommand(
        expected_company_id=1,
        expected_version=row.version,
        plan=IntakePlanInput(title='Second', document_type=DocumentType.OTHER),
    )
    with pytest.raises(HTTPException) as failure:
        service.prepare(row.id, payload, audit(service))
    assert failure.value.status_code == 409
    payload.plan.acknowledge_duplicate = True
    second = service.response_file(service.prepare(row.id, payload, audit(service)))
    service.db.commit()
    service.execute(first.id, command(first), audit(service))
    service.db.commit()
    with pytest.raises(HTTPException) as failure:
        service.execute(second.id, command(second), audit(service))
    assert failure.value.status_code == 409


def test_private_pending_duplicate_exposes_ack_flag_without_other_owner_ids(client, auth_headers, service, admin_user):
    source = uploaded(service)
    row = service.file(source.id)
    row.status = 'awaiting_review'
    row.analysis_json = IntakeExtraction(
        classification='material_certificate', confidence='unknown', summary='Review'
    ).model_dump()
    service.db.commit()
    endpoint = f'{BASE}/files/{source.id}'
    initial = client.get(endpoint, headers=auth_headers).json()
    assert initial['analysis']['has_duplicates'] is False

    other_service = intake.HankIntakeService(service.db, admin_user, 1)
    private_source = uploaded(other_service)
    assert private_source.status == 'queued'
    visible = client.get(endpoint, headers=auth_headers).json()
    assert visible['analysis']['has_duplicates'] is True
    assert visible['analysis']['duplicate_file_ids'] == []
    assert visible['analysis']['duplicate_document_ids'] == []
    assert any('identical file' in warning for warning in visible['analysis']['warnings'])
    assert client.get(f'{BASE}/files/{private_source.id}', headers=auth_headers).status_code == 404

    payload = {
        'expected_company_id': 1,
        'expected_version': visible['version'],
        'plan': {'title': 'Reviewed duplicate', 'document_type': 'other'},
    }
    refused = client.post(endpoint + '/plan', headers=auth_headers, json=payload)
    assert refused.status_code == 409
    payload['plan']['acknowledge_duplicate'] = True
    accepted = client.post(endpoint + '/plan', headers=auth_headers, json=payload)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()['status'] == 'planned'
    assert accepted.json()['plan']['input']['acknowledge_duplicate'] is True
    assert accepted.json()['analysis']['duplicate_file_ids'] == []
    assert service.db.query(Document).count() == 0


def test_current_permissions_filter_matches_and_saved_plan(service, test_part):
    ready = reviewed(
        service, plan=IntakePlanInput(title='Part print', document_type=DocumentType.DRAWING, part_id=test_part.id)
    )
    row = service.file(ready.id)
    extraction = IntakeExtraction(
        classification='drawing',
        confidence='high',
        summary='Print',
        fields=[{'name': 'part_number', 'value': test_part.part_number, 'confidence': 'high'}],
    )
    row.analysis_json = extraction.model_dump()
    service.db.add(RolePermission(company_id=1, role=service.user.role, permissions=['work_orders:view']))
    service.db.commit()
    result = service.response_file(service.file(ready.id))
    assert result.plan is None and result.analysis.matches == []
    with pytest.raises(HTTPException) as failure:
        service.execute(ready.id, command(ready), audit(service))
    assert failure.value.status_code == 403


@pytest.mark.parametrize(
    'attribute,value',
    [
        ('_read_only_company_context', True),
        ('_api_token_id', 12),
        ('_token_scope', 'kiosk'),
        ('role', UserRole.OPERATOR),
    ],
)
def test_write_authority_fences(service, attribute, value):
    setattr(service.user, attribute, value)
    with pytest.raises(HTTPException) as failure:
        uploaded(service)
    assert failure.value.status_code == 403


def test_selected_foreign_tenant_records_cannot_be_filed(service):
    service.db.add(Company(id=2, name='Other', slug='other', is_active=True))
    service.db.flush()
    part = Part(company_id=2, part_number='SECRET', name='Secret part', part_type=PartType.PURCHASED)
    service.db.add(part)
    service.db.commit()
    with pytest.raises(HTTPException) as failure:
        reviewed(service, plan=IntakePlanInput(title='Bad link', document_type=DocumentType.DRAWING, part_id=part.id))
    assert failure.value.status_code == 404


def test_worker_has_no_transaction_during_ai_and_no_filing(service, monkeypatch, storage):
    pending = uploaded(service)
    sessions = []
    factory = sessionmaker(bind=service.db.get_bind())

    def session():
        db = factory()
        sessions.append(db)
        return db

    def analyze(content, company_id):
        assert company_id == 1 and content.startswith(b'%PDF-')
        assert all(not db.in_transaction() for db in sessions)
        return IntakeExtraction(classification='vendor_quote', confidence='unknown', summary='Review quote'), 1

    monkeypatch.setattr(intake, 'SessionLocal', session)
    monkeypatch.setattr(intake, 'analyze_pdf', analyze)
    assert intake.process_intake_file(pending.id) == {'status': 'awaiting_review'}
    service.db.expire_all()
    result = service.response_file(service.file(pending.id))
    assert result.status == 'awaiting_review' and result.page_count == 1 and result.version == 3
    assert service.db.query(Document).count() == 0
    service.db.rollback()
    assert intake.process_intake_file(pending.id) == {'status': 'skipped'}


def test_worker_egress_refusal_is_recoverable_and_cancel_wins_late_result(service, monkeypatch):
    pending = uploaded(service)
    factory = sessionmaker(bind=service.db.get_bind())
    monkeypatch.setattr(intake, 'SessionLocal', factory)
    monkeypatch.setattr(intake, 'analyze_pdf', lambda *args: (_ for _ in ()).throw(LLMEgressDisabledError(1)))
    assert intake.process_intake_file(pending.id) == {'status': 'failed'}
    row = service.file(pending.id)
    assert row.error_code == 'AI_EGRESS_DISABLED'
    retried = service.response_file(service.retry(row.id, command(row), audit(service)))
    service.db.commit()

    def cancel_then_return(*args):
        row = service.file(retried.id)
        service.cancel(row.id, command(row), audit(service))
        service.db.commit()
        return IntakeExtraction(classification='other', confidence='unknown', summary='Late'), 1

    monkeypatch.setattr(intake, 'analyze_pdf', cancel_then_return)
    assert intake.process_intake_file(pending.id) == {'status': 'superseded'}
    assert service.file(pending.id).status == 'cancelled'


def test_worker_rechecks_actor_before_storing_extraction(service, monkeypatch):
    pending = uploaded(service)
    factory = sessionmaker(bind=service.db.get_bind())
    monkeypatch.setattr(intake, 'SessionLocal', factory)

    def revoke_during_analysis(*args):
        service.user.is_active = False
        service.db.commit()
        return IntakeExtraction(classification='other', confidence='unknown', summary='Not authorized'), 1

    monkeypatch.setattr(intake, 'analyze_pdf', revoke_during_analysis)
    assert intake.process_intake_file(pending.id) == {'status': 'failed'}
    row = service.db.get(HankIntakeFile, pending.id)
    assert row.status == 'failed' and row.analysis_json == {}
    assert service.db.query(Document).count() == 0


def test_route_audit_refusal_returns_503_and_preserves_review(client, auth_headers, service, monkeypatch):
    ready = reviewed(service)
    original = AuditService.log_required

    def reject_document(self, action, resource, **kwargs):
        if resource == 'document':
            raise AuditWriteError('unavailable')
        return original(self, action, resource, **kwargs)

    monkeypatch.setattr(AuditService, 'log_required', reject_document)
    response = client.post(f'{BASE}/files/{ready.id}/execute', headers=auth_headers, json=command(ready).model_dump())
    assert response.status_code == 503
    assert service.db.query(Document).count() == 0
    assert service.file(ready.id).status == 'planned'


def test_compare_and_swap_refuses_transition_on_stale_row(service):
    pending = uploaded(service)
    row = service.file(pending.id)
    service.db.query(HankIntakeFile).filter_by(id=row.id).update({'version': 2}, synchronize_session=False)
    with pytest.raises(HTTPException) as failure:
        service._transition(row, 'cancelled', audit(service))
    assert failure.value.status_code == 409
    service.db.rollback()
    assert service.file(pending.id).status == 'queued'


def test_receipt_release_requires_current_receiving_write_permission(service, receipt):
    plan = certificate_plan(receipt)
    ready = reviewed(service, plan=plan)
    service.db.add(
        RolePermission(
            company_id=1, role=service.user.role, permissions=['parts:view', 'purchasing:view', 'receiving:view']
        )
    )
    service.db.commit()
    with pytest.raises(HTTPException) as failure:
        service.execute(ready.id, command(ready), audit(service))
    assert failure.value.status_code == 403
    assert service.db.query(Document).count() == 0


def test_retry_only_reclaims_stale_analysis_and_checks_version(service):
    pending = uploaded(service)
    row = service.file(pending.id)
    row.status, row.processing_started_at = 'analyzing', datetime.utcnow()
    service.db.commit()
    with pytest.raises(HTTPException):
        service.retry(row.id, command(row), audit(service))
    row.processing_started_at = datetime.utcnow() - timedelta(minutes=16)
    service.db.commit()
    retried = service.retry(row.id, command(row), audit(service))
    assert retried.status == 'queued' and retried.processing_started_at is None
    with pytest.raises(HTTPException):
        service.cancel(row.id, IntakeCommand(expected_company_id=1, expected_version=1), audit(service))


def test_pdf_native_evidence_validation_and_structured_call(monkeypatch):
    content = pdf('PO-123')
    calls = []

    def model(context, **kwargs):
        calls.append((context, kwargs))
        return SimpleNamespace(
            raw_response=SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type='tool_use',
                        name='record_intake',
                        input={
                            'classification': 'purchase_order',
                            'confidence': 'high',
                            'summary': 'Order',
                            'evidence': [{'page': 1, 'excerpt': 'PO-123'}],
                            'fields': [
                                {
                                    'name': 'po_number',
                                    'value': 'PO-123',
                                    'confidence': 'high',
                                    'evidence': [{'page': 1, 'excerpt': 'PO-123'}],
                                },
                                {
                                    'name': 'total',
                                    'value': '900',
                                    'confidence': 'high',
                                    'evidence': [{'page': 2, 'excerpt': '900'}],
                                },
                                {
                                    'name': 'vendor_name',
                                    'value': 'Invented',
                                    'confidence': 'high',
                                    'evidence': [{'page': 1, 'excerpt': 'PO-123'}],
                                },
                            ],
                        },
                    )
                ]
            )
        )

    monkeypatch.setattr(intake, 'run_llm_task', model)
    extraction, pages = intake.analyze_pdf(content, 1)
    assert pages == 1
    assert [field.confidence for field in extraction.fields] == ['high', 'unknown', 'low']
    _, args = calls[0]
    assert args['company_id'] == 1 and args['max_retries'] == 0 and args['timeout'] == 90
    assert args['system'][0]['cache_control'] == {'type': 'ephemeral'}
    assert args['messages'][0]['content'][0]['type'] == 'document'
    assert args['tool_choice']['name'] == 'record_intake'
    assert len(extraction.warnings) <= 10


def test_scanned_pdf_remains_uncertain_and_invalid_pdf_rejected(monkeypatch):
    monkeypatch.setattr(
        intake,
        'run_llm_task',
        lambda *args, **kwargs: SimpleNamespace(
            raw_response=SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type='tool_use',
                        name='record_intake',
                        input={
                            'classification': 'material_certificate',
                            'confidence': 'high',
                            'summary': 'Scan',
                            'evidence': [{'page': 1, 'excerpt': 'Heat H123'}],
                        },
                    )
                ]
            )
        ),
    )
    extraction, _ = intake.analyze_pdf(pdf(''), 1)
    assert extraction.confidence == 'low'
    with pytest.raises(ValueError):
        intake.extract_intake(b'not a PDF')
    with pytest.raises(Exception):
        intake.extract_intake(pdf(pages=26))


@pytest.mark.asyncio
async def test_worker_delegate_uses_thread_offload(monkeypatch):
    import threading

    from app.jobs.hank_intake_jobs import process_hank_intake_file_task

    main_thread = threading.get_ident()

    def work(file_id):
        assert threading.get_ident() != main_thread
        return {'id': file_id}

    monkeypatch.setattr(intake, 'process_intake_file', work)
    assert await process_hank_intake_file_task(42) == {'id': 42}
