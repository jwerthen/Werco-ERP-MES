"""Private, recoverable PDF intake with extraction outside database transactions."""

import base64
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException

from app.core.queue import enqueue_job_best_effort
from app.db.locks import acquire_generator_lock
from app.db.session import SessionLocal
from app.db.tenant_filter import tenant_query
from app.models.company import Company
from app.models.document import Document, DocumentType
from app.models.hank_intake import HankIntakeBatch, HankIntakeFile
from app.models.part import Part
from app.models.purchasing import POReceipt, PurchaseOrder, PurchaseOrderLine, Vendor
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder
from app.schemas.hank_intake import (
    IntakeAnalysis,
    IntakeBatchList,
    IntakeBatchResponse,
    IntakeExtraction,
    IntakeFileResponse,
    IntakeMatch,
    IntakePlanInput,
    IntakePlanResponse,
    IntakeReceipt,
)
from app.services.audit_service import AuditService
from app.services.document_numbering import generate_document_number
from app.services.hank_task_service import HankTaskService, _digest, _row_values
from app.services.llm_client import LLMEgressDisabledError, LLMNotConfiguredError, run_llm_task
from app.services.llm_model_router import LLMTaskContext
from app.services.prompts.hank_intake import HANK_INTAKE_PROMPT
from app.services.receiving_delivery_service import validate_certificate
from app.services.storage_service import backend_for_ref, get_storage, resolve_upload_dir

logger = logging.getLogger(__name__)
MAX_FILES = 5
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_BATCH_BYTES = 25 * 1024 * 1024
MAX_PAGES = 25
STALE_AFTER = timedelta(minutes=15)
WRITE_ROLES = {UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY}
LINKS = {
    'part': (Part, 'parts:view', 'part_number', '/parts/{id}'),
    'work_order': (WorkOrder, 'work_orders:view', 'work_order_number', '/work-orders/{id}'),
    'vendor': (Vendor, 'purchasing:view', 'name', '/purchasing'),
    'purchase_order': (PurchaseOrder, 'purchasing:view', 'po_number', '/purchasing?po={id}'),
    'receipt': (POReceipt, 'receiving:view', 'receipt_number', '/receiving'),
}


class IntakeExtractionIncompleteError(ValueError):
    """The provider reached its output budget before returning all extraction data."""


def enqueue_intake(file_id, version):
    return enqueue_job_best_effort(
        'process_hank_intake_file_job', file_id, _job_id=f'hank-intake:{file_id}:{version}', fast_fail=True
    )


def _filename(value):
    value = os.path.basename((value or 'document.pdf').replace('\\', '/'))
    return re.sub(r'[\x00-\x1f\x7f]', '', value)[:255] or 'document.pdf'


def read_verified_source(ref, expected_hash, expected_size):
    """Read at most the accepted file bound; never present changed source evidence."""
    chunks, size = [], 0
    try:
        for chunk in backend_for_ref(ref).open_stream(ref):
            size += len(chunk)
            if size > MAX_FILE_BYTES or size > expected_size:
                raise HTTPException(409, 'The stored source no longer matches the saved file.')
            chunks.append(chunk)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, 'The saved source is temporarily unavailable. Retry after refreshing intake.') from exc
    content = b''.join(chunks)
    if size != expected_size or hashlib.sha256(content).hexdigest() != expected_hash:
        raise HTTPException(409, 'The stored source no longer matches the saved file.')
    return content


def _pdf_pages(content):
    if not content.startswith(b'%PDF-'):
        raise ValueError('This file is not a PDF')
    # A page/byte bound alone does not bound decompression. Isolate untrusted
    # parsing with OS memory/CPU limits and a wall-clock deadline.
    parsed = subprocess.run(
        [sys.executable, '-m', 'app.services.hank_pdf_reader'],
        input=content,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=True,
        timeout=20,
    )
    pages = json.loads(parsed.stdout)
    if (
        not isinstance(pages, list)
        or not 1 <= len(pages) <= MAX_PAGES
        or any(not isinstance(page, str) or len(page) > 16000 for page in pages)
    ):
        raise ValueError('The PDF could not be safely read')
    return pages


def extract_intake(content):
    pages = _pdf_pages(content)
    return pages


def analyze_pdf(content, company_id):
    pages = extract_intake(content)
    result = run_llm_task(
        LLMTaskContext(
            task='hank_document_intake',
            input_chars=min(sum(map(len, pages)), 12000),
            has_pdf_document=True,
            is_ocr=not any(text.strip() for text in pages),
            max_output_tokens=6000,
        ),
        system=[{'type': 'text', 'text': HANK_INTAKE_PROMPT.text, 'cache_control': {'type': 'ephemeral'}}],
        tools=[
            {
                'name': 'record_intake',
                'description': 'Record evidence-bearing PDF extraction for employee review.',
                'input_schema': IntakeExtraction.model_json_schema(),
            }
        ],
        tool_choice={'type': 'tool', 'name': 'record_intake'},
        messages=[
            {
                'role': 'user',
                'content': [
                    {
                        'type': 'document',
                        'source': {
                            'type': 'base64',
                            'media_type': 'application/pdf',
                            'data': base64.b64encode(content).decode('ascii'),
                        },
                    },
                    {
                        'type': 'text',
                        'text': f'Extract the {len(pages)} PDF pages. Cite page evidence; retain uncertainty.',
                    },
                ],
            }
        ],
        company_id=company_id,
        feature='hank_document_intake',
        prompt_version=HANK_INTAKE_PROMPT.version,
        max_tokens=6000,
        timeout=90,
        max_retries=0,
    )
    if getattr(result.raw_response, 'stop_reason', None) == 'max_tokens':
        raise IntakeExtractionIncompleteError(
            'The PDF extraction was incomplete. Split the PDF into smaller documents and upload them again.'
        )
    blocks = [
        block
        for block in result.raw_response.content
        if getattr(block, 'type', None) == 'tool_use' and getattr(block, 'name', None) == 'record_intake'
    ]
    if len(blocks) != 1:
        raise ValueError('The extraction did not return a valid structured result')
    extraction = IntakeExtraction.model_validate(blocks[0].input)
    uncertain = False
    for item in [extraction, *extraction.fields, *extraction.lines]:
        valid = []
        for evidence in item.evidence:
            if evidence.page > len(pages):
                uncertain = True
                continue
            valid.append(evidence)
            if (
                ' '.join(evidence.excerpt.split()).casefold()
                not in ' '.join(pages[evidence.page - 1].split()).casefold()
            ):
                uncertain = True
                item.confidence = 'low'
        item.evidence = valid
        if not valid:
            item.confidence = 'unknown'
            uncertain = True
        if (
            valid
            and getattr(item, 'value', None)
            and not any(
                ' '.join(item.value.split()).casefold() in ' '.join(proof.excerpt.split()).casefold() for proof in valid
            )
        ):
            item.confidence = 'low'
            uncertain = True
        if item in extraction.lines and valid:
            # A real excerpt alone does not substantiate a model-invented
            # quantity or heat. Require every populated receipt identifier/value
            # to occur as a whole token in its cited evidence before trusting it.
            excerpt = ' '.join(' '.join(proof.excerpt.split()).casefold() for proof in valid)
            for name in ('part_number', 'quantity', 'lot_number', 'heat_number', 'unit_of_measure'):
                value = getattr(item, name, None)
                if value and not re.search(
                    r'(?<!\w)' + re.escape(' '.join(value.split()).casefold()) + r'(?!\w)', excerpt
                ):
                    item.confidence = 'low'
                    uncertain = True
    extraction.warnings = [str(value)[:500] for value in extraction.warnings[:7]]
    if uncertain:
        extraction.warnings.append(
            'Some evidence cannot be verified against native PDF text. Review the cited pages, especially scans.'
        )
    extraction.warnings.extend(
        [
            'Extraction is a suggestion. Review identifiers, quantities and source pages before filing.',
            'This does not verify certificate contents or authorize production, receipt acceptance or a drawing revision.',
        ]
    )
    return extraction, len(pages)


class HankIntakeService:
    def __init__(self, db, user, company_id):
        self.db, self.user, self.company_id = db, user, company_id

    def authority(self, *, write=False):
        elevated = self.user.is_superuser or self.user.role == UserRole.PLATFORM_ADMIN
        company = self.db.query(Company).filter(Company.id == self.company_id).first()
        if not (
            company
            and company.is_active
            and self.user.is_active
            and (self.user.company_id == self.company_id or elevated)
            and (self.user.role in WRITE_ROLES or elevated)
            and getattr(self.user, '_api_token_id', None) is None
            and getattr(self.user, '_token_scope', None) not in ('kiosk', 'api')
        ):
            raise HTTPException(403, 'Document intake requires an interactive Admin, Manager or Quality account.')
        if write and getattr(self.user, '_read_only_company_context', False):
            raise HTTPException(403, 'Read-only company context cannot change document intake.')

    def permissions(self):
        return HankTaskService(self.db, self.user, self.company_id)._permissions()

    def batches(self):
        return tenant_query(self.db, HankIntakeBatch, self.company_id).filter(
            HankIntakeBatch.owner_id == self.user.id, HankIntakeBatch.credential_key == 'user'
        )

    def batch(self, batch_id):
        self.authority()
        row = self.batches().filter(HankIntakeBatch.id == batch_id).first()
        if row is None:
            raise HTTPException(404, 'Intake batch not found')
        return row

    def file(self, file_id, *, locked=False):
        self.authority()
        query = (
            tenant_query(self.db, HankIntakeFile, self.company_id)
            .join(HankIntakeBatch, HankIntakeBatch.id == HankIntakeFile.batch_id)
            .filter(
                HankIntakeFile.id == file_id,
                HankIntakeBatch.company_id == self.company_id,
                HankIntakeBatch.owner_id == self.user.id,
                HankIntakeBatch.credential_key == 'user',
            )
        )
        if locked:
            query = query.with_for_update(of=HankIntakeFile).populate_existing()
        row = query.first()
        if row is None:
            raise HTTPException(404, 'Intake file not found')
        return row

    def _command(self, file_id, command):
        self.authority(write=True)
        if command.expected_company_id != self.company_id:
            raise HTTPException(409, 'Active company changed. Reopen intake in the intended company.')
        row = self.file(file_id, locked=True)
        if row.version != command.expected_version:
            raise HTTPException(409, 'This intake file changed. Refresh it before continuing.')
        return row

    def _duplicates(self, row):
        candidates = (
            tenant_query(self.db, HankIntakeFile, self.company_id)
            .filter(
                HankIntakeFile.content_sha256 == row.content_sha256,
                HankIntakeFile.id != row.id,
                HankIntakeFile.status != 'cancelled',
            )
            .order_by(HankIntakeFile.id)
            .limit(21)
            .all()
        )
        own_ids = {
            batch.id
            for batch in self.batches().filter(HankIntakeBatch.id.in_([item.batch_id for item in candidates])).all()
        }
        file_ids = [item.id for item in candidates if item.batch_id in own_ids][:20]
        document_ids = [item.result_json['document_id'] for item in candidates if item.result_json][:20]
        return file_ids, document_ids, bool(candidates)

    def _matches(self, analysis):
        values = {
            item.name: item.value.strip() for item in analysis.fields if item.value and item.confidence != 'unknown'
        }
        searches = {
            'part': [
                values.get('part_number'),
                *[line.part_number for line in analysis.lines if line.part_number and line.confidence != 'unknown'],
            ],
            'work_order': [values.get('work_order_number')],
            'vendor': [values.get('vendor_name')],
            'purchase_order': [values.get('po_number')],
            'receipt': [values.get('receipt_number')],
        }
        permissions, matches = self.permissions(), []
        for kind, terms in searches.items():
            model, permission, label, href = LINKS[kind]
            terms = sorted({term for term in terms if term})[:50]
            if not terms or permission not in permissions:
                continue
            query = tenant_query(self.db, model, self.company_id).filter(getattr(model, label).in_(terms))
            if hasattr(model, 'is_deleted'):
                query = query.filter(model.is_deleted.is_(False))
            for record in query.order_by(model.id).limit(5).all():
                matches.append(
                    IntakeMatch(
                        kind=kind,
                        id=record.id,
                        label=str(getattr(record, label)),
                        href=href.format(id=record.id),
                        reason='Exact extracted identifier; employee confirmation required.',
                    )
                )
        return matches

    def _completed_duplicate_count(self, row):
        # Count the whole tenant/hash cohort so a bounded display window cannot
        # miss another completion among older queued submissions.
        return (
            tenant_query(self.db, HankIntakeFile, self.company_id)
            .filter(
                HankIntakeFile.content_sha256 == row.content_sha256,
                HankIntakeFile.id != row.id,
                HankIntakeFile.status == 'completed',
            )
            .count()
        )

    def response_file(self, row):
        analysis = None
        if row.analysis_json:
            extracted = IntakeExtraction.model_validate(row.analysis_json)
            own, documents, duplicate = self._duplicates(row)
            analysis = IntakeAnalysis(
                **extracted.model_dump(),
                matches=self._matches(extracted),
                has_duplicates=duplicate,
                duplicate_file_ids=own,
                duplicate_document_ids=documents,
            )
            if duplicate:
                analysis.warnings = analysis.warnings[:9]
                analysis.warnings.append(
                    'An identical file was already submitted through intake; review before filing another copy.'
                )
        plan = None
        result = None
        if row.plan_json:
            # Recheck view gates before returning saved record names and links.
            selected = IntakePlanInput.model_validate(row.plan_json['input'])
            permissions = self.permissions()
            permitted = all(
                getattr(selected, kind + '_id') is None or permission in permissions
                for kind, (_, permission, _, _) in LINKS.items()
            )
            if permitted:
                plan = IntakePlanResponse.model_validate(
                    {key: value for key, value in row.plan_json.items() if not key.startswith('_')}
                )
        if row.result_json:
            result = IntakeReceipt.model_validate(row.result_json)
            result.references = [ref for ref in result.references if LINKS[ref.kind][1] in self.permissions()]
        return IntakeFileResponse(
            id=row.id,
            batch_id=row.batch_id,
            company_id=row.company_id,
            filename=row.filename,
            file_size=row.file_size,
            content_sha256=row.content_sha256,
            page_count=row.page_count,
            status=row.status,
            version=row.version,
            source_url=f'/api/v1/hank/intake/files/{row.id}/source',
            analysis=analysis,
            plan=plan,
            result=result,
            error_message=row.error_message,
            created_at=row.created_at,
            updated_at=row.updated_at,
            completed_at=row.completed_at,
        )

    def response_batch(self, batch):
        files = (
            tenant_query(self.db, HankIntakeFile, self.company_id)
            .filter(HankIntakeFile.batch_id == batch.id)
            .order_by(HankIntakeFile.ordinal)
            .all()
        )
        return IntakeBatchResponse(
            id=batch.id,
            company_id=batch.company_id,
            request_key=batch.request_key,
            created_at=batch.created_at,
            files=[self.response_file(row) for row in files],
        )

    def list(self, limit=20, before_id=None):
        self.authority()
        query = self.batches()
        if before_id:
            query = query.filter(HankIntakeBatch.id < before_id)
        rows = query.order_by(HankIntakeBatch.id.desc()).limit(limit + 1).all()
        return IntakeBatchList(
            batches=[self.response_batch(row) for row in rows[:limit]],
            has_more=len(rows) > limit,
            next_before_id=rows[limit - 1].id if len(rows) > limit else None,
        )

    def upload(self, expected_company_id, request_key, files, audit):
        self.authority(write=True)
        if expected_company_id != self.company_id:
            raise HTTPException(409, 'Active company changed. Reopen intake in the intended company.')
        try:
            request_key = str(UUID(request_key))
        except ValueError as exc:
            raise HTTPException(422, 'request_key must be a UUID') from exc
        if not 1 <= len(files) <= MAX_FILES or sum(len(content) for _, content in files) > MAX_BATCH_BYTES:
            raise HTTPException(413, 'Use 1–5 PDFs and at most 25 MB per batch.')
        prepared = []
        for filename, content in files:
            if not 0 < len(content) <= MAX_FILE_BYTES or not content.startswith(b'%PDF-'):
                raise HTTPException(422, 'Each file must be a PDF no larger than 10 MB.')
            prepared.append((_filename(filename), content, hashlib.sha256(content).hexdigest()))
        identity = {'owner_id': self.user.id, 'files': [(name, digest) for name, _, digest in prepared]}
        digest = _digest(identity)
        prior = (
            tenant_query(self.db, HankIntakeBatch, self.company_id)
            .filter(HankIntakeBatch.request_key == request_key)
            .first()
        )
        if prior:
            if prior.owner_id != self.user.id or prior.request_hash != digest or prior.credential_key != 'user':
                raise HTTPException(409, 'This upload key belongs to another batch or input.')
            return self.response_batch(prior)
        actor_id, company_id = self.user.id, self.company_id
        self.db.rollback()  # Release request/auth reads before storage I/O.
        storage, refs = get_storage(), []
        commit_attempted = False
        try:
            for _, content, _ in prepared:
                key = f'{company_id}/hank-intake/{uuid4()}.pdf'
                if not storage.is_remote:
                    key = os.path.join(resolve_upload_dir(), key)
                refs.append(storage.save(content, key=key))
            self.user = self.db.query(User).filter(User.id == actor_id).one()
            self.authority(write=True)
            acquire_generator_lock(self.db, 'hank_intake:' + request_key, company_id)
            prior = (
                tenant_query(self.db, HankIntakeBatch, company_id)
                .filter(HankIntakeBatch.request_key == request_key)
                .first()
            )
            if prior:
                if prior.owner_id != actor_id or prior.request_hash != digest or prior.credential_key != 'user':
                    raise HTTPException(409, 'This upload key belongs to another batch or input.')
                response = self.response_batch(prior)
                self.db.rollback()
                for ref in refs:
                    storage.delete(ref)
                refs.clear()
                return response
            batch = HankIntakeBatch(
                company_id=company_id,
                owner_id=actor_id,
                credential_key='user',
                request_key=request_key,
                request_hash=digest,
            )
            self.db.add(batch)
            self.db.flush()
            for index, ((name, content, sha), ref) in enumerate(zip(prepared, refs)):
                self.db.add(
                    HankIntakeFile(
                        company_id=company_id,
                        batch_id=batch.id,
                        ordinal=index,
                        filename=name,
                        file_size=len(content),
                        content_sha256=sha,
                        storage_ref=ref,
                    )
                )
            self.db.flush()
            audit.log_required(
                'CREATE',
                'hank_intake_batch',
                resource_id=batch.id,
                new_values={
                    'file_count': len(refs),
                    'request_key': request_key,
                    'file_hashes': [sha for _, _, sha in prepared],
                },
            )
            response = self.response_batch(batch)
            commit_attempted = True
            self.db.commit()
        except Exception:
            self.db.rollback()
            if not commit_attempted:
                for ref in refs:
                    try:
                        storage.delete(ref)
                    except Exception:
                        logger.exception('Unable to remove unpublished intake bytes')
            # Never delete bytes after an uncertain commit: same-key retry recovers.
            raise
        for row in response.files:
            enqueue_intake(row.id, row.version)
        return response

    def _links(self, plan, *, locked=False):
        permissions, records, refs = self.permissions(), {}, []
        for kind, (model, permission, label, href) in LINKS.items():
            row_id = getattr(plan, kind + '_id')
            if row_id is None:
                continue
            if permission not in permissions:
                raise HTTPException(403, f'Current {permission} permission is required for this filing plan.')
            query = tenant_query(self.db, model, self.company_id).filter(model.id == row_id)
            if hasattr(model, 'is_deleted') and not (
                kind == 'vendor' and plan.filing_mode == 'release_receipt_certificate'
            ):
                query = query.filter(model.is_deleted.is_(False))
            if locked:
                query = query.with_for_update().populate_existing()
            record = query.first()
            if record is None:
                raise HTTPException(404, 'A selected filing record is unavailable.')
            records[kind] = record
            refs.append(
                IntakeMatch(
                    kind=kind,
                    id=record.id,
                    label=str(getattr(record, label)),
                    href=href.format(id=record.id),
                    reason='Explicit employee selection.',
                )
            )
        if plan.filing_mode == 'release_receipt_certificate':
            if not {'receiving:view', 'receiving:create'} <= permissions:
                raise HTTPException(403, 'Receipt certificate filing requires receiving view and create permission.')
            receipt = records.get('receipt')
            if receipt is None or plan.document_type not in (DocumentType.CERTIFICATE, DocumentType.MATERIAL_CERT):
                raise HTTPException(422, 'Select a receipt and certificate document type for release and attachment.')
            if receipt.certificate_document_id:
                raise HTTPException(409, 'This receipt already has a certificate. Intake cannot replace it.')
            query = tenant_query(self.db, PurchaseOrderLine, self.company_id).filter(
                PurchaseOrderLine.id == receipt.po_line_id
            )
            line = query.with_for_update().first() if locked else query.first()
            po_query = (
                tenant_query(self.db, PurchaseOrder, self.company_id).filter(
                    PurchaseOrder.id == line.purchase_order_id, PurchaseOrder.is_deleted.is_(False)
                )
                if line
                else None
            )
            po = (
                (po_query.with_for_update().populate_existing().first() if locked else po_query.first())
                if po_query is not None
                else None
            )
            if (
                not po
                or plan.part_id != line.part_id
                or plan.vendor_id != po.vendor_id
                or (plan.purchase_order_id is not None and plan.purchase_order_id != po.id)
            ):
                raise HTTPException(
                    422, 'The certificate part, supplier and purchase order must match this exact receipt.'
                )
            records['receipt_line'] = line
            records['receipt_po'] = po
        return records, refs

    def prepare(self, file_id, command, audit):
        row = self._command(file_id, command)
        if row.status not in ('awaiting_review', 'planned'):
            raise HTTPException(409, 'Wait for extraction, then review the filing plan.')
        plan = command.plan
        if not plan.title.strip() or not plan.revision.strip():
            raise HTTPException(422, 'Title and revision cannot be blank.')
        records, refs = self._links(plan)
        _, _, duplicate = self._duplicates(row)
        if duplicate and not plan.acknowledge_duplicate:
            raise HTTPException(
                409, 'Review the identical-file warning and explicitly acknowledge filing another copy.'
            )
        changes = ['Create a draft document; no release or approval is implied.']
        if plan.filing_mode == 'release_receipt_certificate':
            changes = [
                f'File and release the certificate; attach it to receipt {records["receipt"].receipt_number}.',
                'Record certificate attachment only; inspection status and accepted quantities remain unchanged.',
            ]
        if plan.part_id or plan.work_order_id or plan.vendor_id:
            changes.append('Link the document to the explicitly selected part, job and/or supplier.')
        if plan.purchase_order_id or (plan.receipt_id and plan.filing_mode == 'draft'):
            changes.append(
                'Keep the selected PO/receipt as intake evidence references; no PO source file or receipt certificate is replaced.'
            )
        preview = IntakePlanResponse(
            input=plan,
            changes=changes,
            references=refs,
            warnings=['Employee review does not verify PDF contents, material compliance or production authorization.'],
        )
        row.plan_json = {
            **preview.model_dump(mode='json'),
            '_source_hash': _digest({key: _row_values(value) for key, value in records.items()}),
            '_duplicate_count': self._completed_duplicate_count(row),
        }
        self._transition(row, 'planned', audit, extra={'plan': preview.model_dump(mode='json')})
        return row

    def _transition(self, row, status, audit, *, extra=None):
        previous = {'status': row.status, 'version': row.version}
        # PostgreSQL row locks serialize the normal path. The explicit CAS is
        # also load-bearing on SQLite and protects a stale/late worker result.
        with self.db.no_autoflush:
            changed = (
                tenant_query(self.db, HankIntakeFile, self.company_id)
                .filter(
                    HankIntakeFile.id == row.id,
                    HankIntakeFile.version == row.version,
                    HankIntakeFile.status == row.status,
                )
                .update({'version': row.version + 1, 'status': status}, synchronize_session=False)
            )
        if changed != 1:
            raise HTTPException(409, 'This intake file changed. Refresh before continuing.')
        row.status, row.version, row.updated_at = status, row.version + 1, datetime.utcnow()
        if status in ('completed', 'cancelled'):
            row.completed_at = row.updated_at
        audit.log_required(
            'UPDATE',
            'hank_intake_file',
            resource_id=row.id,
            old_values=previous,
            new_values={'status': status, 'version': row.version, **(extra or {})},
        )
        self.db.flush()

    def execute(self, file_id, command, audit):
        self.authority(write=True)
        row = self.file(file_id)
        if command.expected_company_id != self.company_id:
            raise HTTPException(409, 'Active company changed.')
        if row.status == 'completed':
            return row
        # Storage read/check happens before acquiring any task/source/audit lock.
        ref, sha, size = row.storage_ref, row.content_sha256, row.file_size
        self.db.rollback()
        read_verified_source(ref, sha, size)
        row = self._command(file_id, command)
        if row.status != 'planned' or not row.plan_json:
            raise HTTPException(409, 'Prepare and review a filing plan before executing.')
        plan = IntakePlanInput.model_validate(row.plan_json['input'])
        acquire_generator_lock(self.db, 'hank_intake_hash:' + sha, self.company_id)
        records, refs = self._links(plan, locked=True)
        if _digest({key: _row_values(value) for key, value in records.items()}) != row.plan_json['_source_hash']:
            raise HTTPException(409, 'A selected source changed. Prepare a fresh filing plan.')
        if self._completed_duplicate_count(row) != row.plan_json['_duplicate_count']:
            raise HTTPException(
                409, 'Another identical file was filed. Review a fresh plan before creating another copy.'
            )
        released = plan.filing_mode == 'release_receipt_certificate'
        document = Document(
            company_id=self.company_id,
            document_number=generate_document_number(self.db, plan.document_type.value),
            title=plan.title.strip(),
            revision=plan.revision.strip(),
            document_type=plan.document_type,
            description=plan.description,
            part_id=plan.part_id,
            work_order_id=plan.work_order_id,
            vendor_id=plan.vendor_id,
            file_path=ref,
            file_name=row.filename,
            file_size=size,
            mime_type='application/pdf',
            status='released' if released else 'draft',
            created_by=self.user.id,
            released_by=self.user.id if released else None,
            released_at=datetime.utcnow() if released else None,
        )
        self.db.add(document)
        self.db.flush()
        audit.log_required(
            'CREATE',
            'document',
            resource_id=document.id,
            resource_identifier=document.document_number,
            new_values={
                'status': document.status,
                'part_id': document.part_id,
                'work_order_id': document.work_order_id,
                'vendor_id': document.vendor_id,
            },
            extra_data={'source': 'hank_intake', 'intake_file_id': row.id, 'content_sha256': sha},
        )
        if released:
            validate_certificate(self.db, self.company_id, records['receipt_line'], document.id, check_storage=False)
            receipt = records['receipt']
            prior = {'certificate_document_id': receipt.certificate_document_id, 'coc_attached': receipt.coc_attached}
            receipt.certificate_document_id, receipt.coc_attached = document.id, True
            audit.log_required(
                'UPDATE',
                'po_receipt',
                resource_id=receipt.id,
                resource_identifier=receipt.receipt_number,
                old_values=prior,
                new_values={'certificate_document_id': document.id, 'coc_attached': True},
                extra_data={'source': 'hank_intake', 'intake_file_id': row.id},
            )
        result = IntakeReceipt(
            document_id=document.id,
            document_number=document.document_number,
            href=f'/documents?document={document.id}',
            references=refs,
            summary=f'{document.document_number} filed as {document.status}'
            + (' and attached to the reviewed receipt.' if released else '.'),
            warnings=['PDF contents and manufacturing approval were not verified by Hank.'],
        )
        row.result_json = result.model_dump(mode='json')
        self._transition(row, 'completed', audit, extra={'result': row.result_json})
        return row

    def retry(self, file_id, command, audit):
        row = self._command(file_id, command)
        started = row.processing_started_at
        if started is not None:
            started = started.replace(tzinfo=None)
        if row.status not in ('queued', 'failed') and not (
            row.status == 'analyzing' and started and started < datetime.utcnow() - STALE_AFTER
        ):
            raise HTTPException(409, 'This file is not eligible for an analysis retry yet.')
        row.error_code = row.error_message = None
        row.processing_started_at = None
        self._transition(row, 'queued', audit)
        return row

    def cancel(self, file_id, command, audit):
        row = self._command(file_id, command)
        if row.status in ('completed', 'cancelled'):
            raise HTTPException(409, 'This intake file is already finished.')
        self._transition(row, 'cancelled', audit)
        return row


def process_intake_file(file_id):
    """Claim/commit, perform bounded external work with no session, then reauthorize/persist."""
    db = SessionLocal()
    version = None
    try:
        row = db.query(HankIntakeFile).filter(HankIntakeFile.id == file_id).with_for_update().first()
        if row is None or row.status != 'queued':
            return {'status': 'skipped'}
        batch = tenant_query(db, HankIntakeBatch, row.company_id).filter(HankIntakeBatch.id == row.batch_id).one()
        user = db.query(User).filter(User.id == batch.owner_id).one()
        service = HankIntakeService(db, user, row.company_id)
        service.authority(write=True)
        if batch.credential_key != 'user':
            raise HTTPException(403, 'The intake credential is unavailable.')
        row.processing_started_at = datetime.utcnow()
        service._transition(row, 'analyzing', AuditService(db, user=user, company_id=row.company_id))
        version, company_id, owner_id, ref, expected_hash, expected_size = (
            row.version,
            row.company_id,
            user.id,
            row.storage_ref,
            row.content_sha256,
            row.file_size,
        )
        db.commit()
    finally:
        db.close()
    try:
        content = read_verified_source(ref, expected_hash, expected_size)
        extraction, page_count = analyze_pdf(content, company_id)
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == owner_id).one()
            service = HankIntakeService(db, user, company_id)
            service.authority(write=True)
            row = service.file(file_id, locked=True)
            if row.status != 'analyzing' or row.version != version:
                return {'status': 'superseded'}
            row.analysis_json, row.page_count = extraction.model_dump(mode='json'), page_count
            row.error_code = row.error_message = None
            service._transition(
                row,
                'awaiting_review',
                AuditService(db, user=user, company_id=company_id),
                extra={'page_count': page_count, 'classification': extraction.classification},
            )
            db.commit()
            return {'status': 'awaiting_review'}
        finally:
            db.close()
    except Exception as exc:
        db = SessionLocal()
        try:
            row = (
                tenant_query(db, HankIntakeFile, company_id)
                .filter(HankIntakeFile.id == file_id)
                .with_for_update()
                .first()
            )
            if row and row.status == 'analyzing' and row.version == version:
                row.error_code = (
                    'AI_EGRESS_DISABLED' if isinstance(exc, LLMEgressDisabledError) else 'EXTRACTION_FAILED'
                )
                row.error_message = (
                    'Company AI access is disabled. Enable it before retrying analysis.'
                    if isinstance(exc, LLMEgressDisabledError)
                    else 'AI extraction is unavailable or the PDF could not be safely read. Review the source and retry.'
                )
                if isinstance(exc, LLMNotConfiguredError):
                    row.error_message = 'AI extraction is not configured. Contact an administrator before retrying.'
                if isinstance(exc, IntakeExtractionIncompleteError):
                    row.error_code = 'EXTRACTION_INCOMPLETE'
                    row.error_message = str(exc)
                service = HankIntakeService(db, None, company_id)
                service._transition(
                    row, 'failed', AuditService(db, company_id=company_id), extra={'error_code': row.error_code}
                )
                db.commit()
        finally:
            db.close()
        logger.warning('Hank intake analysis failed for file %s (%s)', file_id, type(exc).__name__)
        return {'status': 'failed'}
