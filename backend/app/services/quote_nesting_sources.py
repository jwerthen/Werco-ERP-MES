"""Audited original-byte attachments across independent DB/storage phases."""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from fastapi import HTTPException
from sqlalchemy import func, tuple_
from sqlalchemy.orm import Session

from app.core.time_utils import to_utc_iso
from app.db.database import atomic_transaction
from app.db.locks import acquire_generator_lock
from app.db.tenant_filter import tenant_query
from app.models.quote_nesting_draft import QuoteNestingRevision
from app.models.quote_nesting_source import QuoteNestingSourceAttempt as Attempt
from app.models.quote_nesting_source import QuoteNestingSourceBinding as Binding
from app.models.quote_nesting_source import QuoteNestingSourceIntent as Intent
from app.models.quote_nesting_source import QuoteNestingSourceReceipt as Receipt
from app.models.user import User
from app.schemas.quote_nesting_sources import (
    MAX_ATTEMPTS,
    MAX_SOURCE_BYTES,
    MAX_TARGET_SNAPSHOT_BYTES,
    CreateSourceIntent,
)
from app.services import quote_nesting_source_storage as storage
from app.services.audit_service import AuditService
from app.services.quote_nesting_drafts import canonical_json, require_access
from app.services.remnant_planning import require_saved_evidence_access


def digest(value) -> str:
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class SourceContext:
    db: Session
    company_id: int
    draft_id: int
    number: int
    authorize: Callable[[], User]
    audit: Callable[[User], AuditService]


def _actor(ctx: SourceContext, *, write: bool) -> User:
    user = ctx.authorize()
    if getattr(user, '_active_company_id', None) != ctx.company_id:
        raise HTTPException(409, 'Active company changed; reopen the saved revision')
    require_access(ctx.db, user, ctx.company_id, write=write)
    require_saved_evidence_access(ctx.db, user, ctx.company_id, _revision(ctx).estimate_json)
    return user


def _revision(ctx: SourceContext) -> QuoteNestingRevision:
    row = (
        tenant_query(ctx.db, QuoteNestingRevision, ctx.company_id)
        .filter(QuoteNestingRevision.draft_id == ctx.draft_id, QuoteNestingRevision.revision_number == ctx.number)
        .first()
    )
    if row is None:
        raise HTTPException(404, 'Saved nesting revision not found')
    return row


def _intent(ctx: SourceContext, intent_id: int, *, lock: bool = False) -> Intent:
    query = tenant_query(ctx.db, Intent, ctx.company_id).filter(
        Intent.id == intent_id, Intent.draft_id == ctx.draft_id, Intent.revision_number == ctx.number
    )
    row = query.with_for_update().populate_existing().first() if lock else query.first()
    if row is None:
        raise HTTPException(404, 'Original source intent not found')
    return row


def _same_actor(intent: Intent, user: User) -> bool:
    return intent.created_by == user.id and intent.submitted_api_token_id == getattr(user, '_api_token_id', None)


def _require_owner(intent: Intent, user: User) -> None:
    if not _same_actor(intent, user):
        raise HTTPException(403, 'Only the original actor and credential can resume this source upload')


def _receipt(ctx: SourceContext, intent_id: int) -> Receipt | None:
    return tenant_query(ctx.db, Receipt, ctx.company_id).filter(Receipt.intent_id == intent_id).first()


def _response(intent: Intent, receipt: Receipt | None, count: int, user: User, can_write: bool) -> dict:
    return {
        'id': intent.id,
        'company_id': intent.company_id,
        'draft_id': intent.draft_id,
        'revision_id': intent.revision_id,
        'revision_number': intent.revision_number,
        'input_sha256': intent.input_sha256,
        'source_sha256': intent.source_sha256,
        'byte_count': intent.byte_count,
        'source_name': intent.source_name,
        'mime_type': intent.mime_type,
        'targets': intent.targets_json,
        'targets_sha256': intent.targets_sha256,
        'target_count': intent.target_count,
        'request_key': intent.request_key,
        'created_by': intent.created_by,
        'submitted_api_token_id': intent.submitted_api_token_id,
        'created_at': to_utc_iso(intent.created_at),
        'state': 'ATTACHED' if receipt else 'PENDING',
        'attempt_count': count,
        'can_resume': can_write and _same_actor(intent, user) and receipt is None,
        'receipt': (
            {
                'id': receipt.id,
                'source_sha256': receipt.source_sha256,
                'byte_count': receipt.byte_count,
                'verified_at': to_utc_iso(receipt.verified_at),
                'created_by': receipt.created_by,
                'submitted_api_token_id': receipt.submitted_api_token_id,
                'claim': 'server_hash_verified_unapproved',
            }
            if receipt
            else None
        ),
    }


def _detail(ctx: SourceContext, intent: Intent, user: User) -> dict:
    count = tenant_query(ctx.db, Attempt, ctx.company_id).filter(Attempt.intent_id == intent.id).count()
    return _response(intent, _receipt(ctx, intent.id), count, user, True)


def _audit(ctx: SourceContext, user: User, resource: str, row_id: int, values: dict) -> None:
    ctx.audit(user).log_required(
        action='CREATE',
        resource_type=resource,
        resource_id=row_id,
        company_id=ctx.company_id,
        description='Recorded unapproved original CAD byte evidence',
        new_values=values,
    )


def _conflicts(ctx: SourceContext, revision_id: int, targets: list[dict]) -> bool:
    return (
        tenant_query(ctx.db, Binding, ctx.company_id)
        .filter(
            Binding.revision_id == revision_id,
            tuple_(Binding.group_id, Binding.part_id).in_([(t['group_id'], t['part_id']) for t in targets]),
        )
        .first()
        is not None
    )


def create_intent(ctx: SourceContext, command: CreateSourceIntent) -> dict:
    with atomic_transaction(ctx.db):
        user = _actor(ctx, write=True)
        if command.expected_company_id != ctx.company_id:
            raise HTTPException(409, 'Active company changed; reopen the saved revision')
        request = command.model_dump(mode='json')
        request['targets'] = sorted(request['targets'], key=lambda t: (t['group_id'], t['part_id']))
        request_hash = digest(
            {
                'version': 1,
                'company_id': ctx.company_id,
                'actor_id': user.id,
                'api_token_id': getattr(user, '_api_token_id', None),
                'draft_id': ctx.draft_id,
                'revision_number': ctx.number,
                'command': request,
            }
        )
        acquire_generator_lock(ctx.db, 'quote_nesting_source:' + command.request_key, ctx.company_id)
        prior = tenant_query(ctx.db, Intent, ctx.company_id).filter(Intent.request_key == command.request_key).first()
        if prior is not None:
            if not _same_actor(prior, user) or prior.request_hash != request_hash:
                raise HTTPException(409, 'This request key belongs to a different source command or credential')
            return _detail(ctx, prior, user)
        revision = _revision(ctx)
        if revision.content_sha256 != command.expected_input_sha256:
            raise HTTPException(409, 'The selected saved revision hash does not match')
        parts = {
            (group['id'], part['id']): part
            for group in revision.estimate_json['groups']
            for part in group['quote']['parts']
        }
        targets = []
        for target in request['targets']:
            part = parts.get((target['group_id'], target['part_id']))
            provenance = part.get('provenance') if part else None
            if not isinstance(provenance, dict) or provenance.get('sourceHashBasis') != 'original-bytes':
                raise HTTPException(409, 'Every target must have original-byte provenance in this saved revision')
            if provenance.get('sourceSha256') != command.source_sha256:
                raise HTTPException(409, 'Original byte hash does not match every selected saved part')
            snapshot = dict(provenance)
            if 'revision' in part:
                snapshot['reportedRevision'] = part['revision']
            targets.append({**target, 'provenance': snapshot})
        if len(canonical_json(targets).encode('utf-8')) > MAX_TARGET_SNAPSHOT_BYTES:
            raise HTTPException(413, 'Selected source metadata exceeds 512 KiB; select fewer saved profiles')
        if _conflicts(ctx, revision.id, targets):
            raise HTTPException(409, 'A selected saved part already has an immutable source attachment')
        row = Intent(
            company_id=ctx.company_id,
            draft_id=ctx.draft_id,
            revision_id=revision.id,
            revision_number=ctx.number,
            input_sha256=revision.content_sha256,
            source_sha256=command.source_sha256,
            byte_count=command.byte_count,
            source_name=command.source_name,
            mime_type=command.mime_type,
            targets_json=targets,
            targets_sha256=digest(targets),
            target_count=len(targets),
            request_key=command.request_key,
            request_hash=request_hash,
            created_by=user.id,
            submitted_api_token_id=getattr(user, '_api_token_id', None),
            created_at=datetime.utcnow(),
        )
        ctx.db.add(row)
        ctx.db.flush()
        _audit(
            ctx,
            user,
            'quote_nesting_source_intent',
            row.id,
            {
                'revision_id': revision.id,
                'input_sha256': row.input_sha256,
                'source_sha256': row.source_sha256,
                'byte_count': row.byte_count,
                'targets_sha256': row.targets_sha256,
                'target_count': row.target_count,
            },
        )
        return _response(row, None, 0, user, True)


def list_sources(ctx: SourceContext, page: int, per_page: int) -> dict:
    user = _actor(ctx, write=False)
    revision = _revision(ctx)
    try:
        require_access(ctx.db, user, ctx.company_id, write=True)
        can_write = True
    except HTTPException:
        can_write = False
    query = tenant_query(ctx.db, Intent, ctx.company_id).filter(Intent.revision_id == revision.id)
    total = query.count()
    rows = query.order_by(Intent.id.desc()).offset((page - 1) * per_page).limit(per_page).all()
    ids = [row.id for row in rows]
    receipts = (
        {
            row.intent_id: row
            for row in tenant_query(ctx.db, Receipt, ctx.company_id).filter(Receipt.intent_id.in_(ids)).all()
        }
        if ids
        else {}
    )
    counts = (
        dict(
            tenant_query(ctx.db, Attempt, ctx.company_id)
            .with_entities(Attempt.intent_id, func.count(Attempt.id))
            .filter(Attempt.intent_id.in_(ids))
            .group_by(Attempt.intent_id)
            .all()
        )
        if ids
        else {}
    )
    return {
        'company_id': ctx.company_id,
        'draft_id': ctx.draft_id,
        'revision_number': ctx.number,
        'input_sha256': revision.content_sha256,
        'can_attach': can_write,
        'total': total,
        'page': page,
        'per_page': per_page,
        'items': [_response(row, receipts.get(row.id), counts.get(row.id, 0), user, can_write) for row in rows],
    }


def _load_attempts(ctx: SourceContext, intent_id: int, *, write: bool) -> tuple[dict, list[dict]]:
    try:
        user = _actor(ctx, write=write)
        intent = _intent(ctx, intent_id)
        if write:
            _require_owner(intent, user)
        response = _detail(ctx, intent, user)
        attempts = [
            {
                name: getattr(row, name)
                for name in ('id', 'object_key', 'storage_ref', 'provider_json', 'provider_sha256')
            }
            for row in tenant_query(ctx.db, Attempt, ctx.company_id)
            .filter(Attempt.intent_id == intent_id)
            .order_by(Attempt.ordinal)
            .limit(MAX_ATTEMPTS)
            .all()
        ]
        if not write:
            receipt = _receipt(ctx, intent_id)
            if receipt is None:
                raise HTTPException(409, 'Original source bytes have not been attached')
            attempts = [a for a in attempts if a['id'] == receipt.attempt_id]
        return response, attempts
    finally:
        # All values returned above are scalar/JSON snapshots. Release the
        # request's read transaction and connection before any blob access.
        ctx.db.rollback()


def _read_attempt(
    ctx: SourceContext, intent_id: int, attempt: dict, response: dict, budget: storage.SourceIOBudget
) -> bytes:
    budget.check()
    plan = storage.resolve_object(
        ctx.company_id,
        intent_id,
        attempt['object_key'],
        attempt['storage_ref'],
        attempt['provider_json'],
        attempt['provider_sha256'],
    )
    try:
        return storage.verified_bytes(plan, response['byte_count'], response['source_sha256'], budget)
    finally:
        plan.close()


def _complete(ctx: SourceContext, intent_id: int, attempt_id: int) -> dict:
    with atomic_transaction(ctx.db):
        user = _actor(ctx, write=True)
        intent = _intent(ctx, intent_id, lock=True)
        _require_owner(intent, user)
        if _receipt(ctx, intent_id) is not None:
            return _detail(ctx, intent, user)
        attempt = (
            tenant_query(ctx.db, Attempt, ctx.company_id)
            .filter(Attempt.intent_id == intent_id, Attempt.id == attempt_id)
            .first()
        )
        if attempt is None:
            raise HTTPException(409, 'Source verification attempt no longer matches this intent')
        if _conflicts(ctx, intent.revision_id, intent.targets_json):
            raise HTTPException(409, 'A selected saved part already has an immutable source attachment')
        receipt = Receipt(
            company_id=ctx.company_id,
            intent_id=intent.id,
            attempt_id=attempt_id,
            source_sha256=intent.source_sha256,
            byte_count=intent.byte_count,
            created_by=user.id,
            submitted_api_token_id=getattr(user, '_api_token_id', None),
            verified_at=datetime.utcnow(),
        )
        ctx.db.add(receipt)
        ctx.db.flush()
        for target in intent.targets_json:
            ctx.db.add(
                Binding(
                    company_id=ctx.company_id,
                    receipt_id=receipt.id,
                    intent_id=intent.id,
                    revision_id=intent.revision_id,
                    group_id=target['group_id'],
                    part_id=target['part_id'],
                    provenance_json=target['provenance'],
                )
            )
        ctx.db.flush()
        _audit(
            ctx,
            user,
            'quote_nesting_source_receipt',
            receipt.id,
            {
                'intent_id': intent.id,
                'attempt_id': attempt_id,
                'source_sha256': receipt.source_sha256,
                'byte_count': receipt.byte_count,
                'targets_sha256': intent.targets_sha256,
            },
        )
        return _detail(ctx, intent, user)


def _check_io_budget(budget: storage.SourceIOBudget) -> None:
    try:
        budget.check()
    except storage.SourceStorageError as exc:
        raise HTTPException(
            503, 'Source storage recovery budget expired. Existing attempts remain available for explicit recovery.'
        ) from exc


def _recover(ctx: SourceContext, intent_id: int, budget: storage.SourceIOBudget) -> dict | None:
    response, attempts = _load_attempts(ctx, intent_id, write=True)
    if response['receipt'] is not None:
        return response
    for attempt in attempts:
        _check_io_budget(budget)
        try:
            _read_attempt(ctx, intent_id, attempt, response, budget)
        except storage.SourceStorageError:
            _check_io_budget(budget)
            continue
        return _complete(ctx, intent_id, attempt['id'])
    _check_io_budget(budget)
    return None


def finalize_source(ctx: SourceContext, intent_id: int) -> dict:
    result = _recover(ctx, intent_id, storage.SourceIOBudget.start())
    if result is None:
        raise HTTPException(
            503, 'Original bytes are not verified yet. Retry recovery or explicitly resend the original file.'
        )
    return result


def upload_source(ctx: SourceContext, intent_id: int, content: bytes) -> dict:
    budget = storage.SourceIOBudget.start()
    if not content or len(content) > MAX_SOURCE_BYTES:
        raise HTTPException(413, 'Original DXF must contain fewer than 5,000,000 bytes')
    response, _ = _load_attempts(ctx, intent_id, write=True)
    if len(content) != response['byte_count'] or hashlib.sha256(content).hexdigest() != response['source_sha256']:
        raise HTTPException(409, 'Uploaded original bytes do not match this immutable intent')
    result = _recover(ctx, intent_id, budget)
    if result is not None:
        return result
    # Provider planning may touch local directories/client initialization, so
    # it too runs after all read transactions have been released.
    try:
        budget.check()
        plan = storage.plan_object(ctx.company_id, intent_id)
    except storage.SourceStorageError as exc:
        raise HTTPException(503, 'Source storage is unavailable; the intent remains pending') from exc
    try:
        with atomic_transaction(ctx.db):
            _check_io_budget(budget)
            user = _actor(ctx, write=True)
            intent = _intent(ctx, intent_id, lock=True)
            _require_owner(intent, user)
            if _receipt(ctx, intent_id) is not None:
                return _detail(ctx, intent, user)
            attempts = tenant_query(ctx.db, Attempt, ctx.company_id).filter(Attempt.intent_id == intent_id).count()
            if attempts >= MAX_ATTEMPTS:
                raise HTTPException(
                    409, 'This intent has reached eight tracked attempts. Existing attempts may still be finalized.'
                )
            _check_io_budget(budget)
            attempt = Attempt(
                company_id=ctx.company_id,
                intent_id=intent_id,
                ordinal=attempts + 1,
                object_key=plan.object_key,
                storage_ref=plan.storage_ref,
                provider_json=plan.provider,
                provider_sha256=storage.digest(plan.provider),
                created_by=user.id,
                submitted_api_token_id=getattr(user, '_api_token_id', None),
                created_at=datetime.utcnow(),
            )
            ctx.db.add(attempt)
            ctx.db.flush()
            attempt_id = attempt.id
            _audit(
                ctx,
                user,
                'quote_nesting_source_attempt',
                attempt_id,
                {
                    'intent_id': intent_id,
                    'ordinal': attempt.ordinal,
                    'provider_sha256': attempt.provider_sha256,
                    'source_sha256': intent.source_sha256,
                    'byte_count': intent.byte_count,
                },
            )
        try:
            storage.save_once(plan, content, budget)
        except storage.SourceStorageError as exc:
            if exc.code == 'invalid_storage_reference':
                raise HTTPException(
                    503, 'Storage returned an unexpected reference; the source remains unconfirmed'
                ) from exc
            # A timed-out write may have completed. Read back the same recorded
            # object; never call save again for this attempt.
            pass
        try:
            storage.verified_bytes(plan, response['byte_count'], response['source_sha256'], budget)
        except storage.SourceStorageError as exc:
            raise HTTPException(
                503, 'Source write is unconfirmed. Retry recovery before explicitly resending the file.'
            ) from exc
        return _complete(ctx, intent_id, attempt_id)
    finally:
        plan.close()


def download_source(ctx: SourceContext, intent_id: int) -> tuple[dict, bytes]:
    budget = storage.SourceIOBudget.start()
    response, attempts = _load_attempts(ctx, intent_id, write=False)
    if len(attempts) != 1:
        raise HTTPException(503, 'Recorded source bytes are unavailable')
    try:
        return response, _read_attempt(ctx, intent_id, attempts[0], response, budget)
    except storage.SourceStorageError as exc:
        raise HTTPException(
            503, 'Recorded source bytes are missing, changed or unavailable; no download was released'
        ) from exc
