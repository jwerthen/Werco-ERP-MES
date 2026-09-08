"""Audited server-run lifecycle; geometry executes only in the fixed worker child."""

import hashlib
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import Session, defer

from app.core.time_utils import to_utc_iso
from app.db.locks import acquire_generator_lock
from app.db.tenant_filter import tenant_query
from app.models.api_token import ApiToken
from app.models.company import Company
from app.models.quote_nesting_draft import QuoteNestingRevision
from app.models.quote_nesting_run import QuoteNestingRun, QuoteNestingRunCheckpoint
from app.models.user import User, UserRole
from app.schemas.quote_nesting_runs import (
    ACTIVE_STATUSES,
    LEASE_SECONDS,
    MAX_CHECKPOINT_BYTES,
    RUN_SETTINGS,
    TERMINAL_STATUSES,
    StartRunRequest,
)
from app.services.audit_service import AuditService
from app.services.nesting_run_protocol import expected_options, validate_option
from app.services.quote_nesting_drafts import canonical_json, parse_estimate, require_access
from app.services.quote_nesting_run_outbox import mark_run_pending

ERROR_MESSAGES = {
    "input_limit": "The saved input exceeded the calculation input budget. No geometry was accepted.",
    "user_cancelled": "Calculation cancelled. Completed checkpoints remain available.",
    "time_limit": "Calculation reached its time limit. Missing work is not proof that parts cannot fit.",
    "work_limit": "Calculation reached its option limit. Missing work is not proof that parts cannot fit.",
    "output_limit": "Calculation exceeded its output budget. Earlier validated checkpoints remain available.",
    "worker_lost": "The worker lease expired. Start a new calculation explicitly to retry.",
    "worker_shutdown": "The worker stopped. Earlier validated checkpoints remain available.",
    "actor_ineligible": "The submitting user or credential no longer has access to this company calculation.",
    "runtime_unavailable": "The pinned nesting runtime is unavailable or mismatched on the worker.",
    "runtime_mismatch": "This calculation requires a different worker build. Start a new calculation explicitly.",
    "invalid_geometry": "The saved geometry did not pass the shared kernel validation. Review the saved inputs.",
    "invalid_protocol": "The worker returned an invalid result. That message was not accepted.",
    "runtime_error": "The worker could not finish this calculation. Earlier validated checkpoints remain available.",
    "input_mismatch": "The saved input hash or immutable calculation profile did not match.",
}
WARNINGS = [
    {
        "code": "unapproved_server_calculation",
        "message": "Server-calculated draft evidence only; material, pricing, CAD authenticity and manufacturing eligibility remain unapproved.",
    },
    {
        "code": "potential_leftovers_zero_credit",
        "message": "Predicted leftovers require review and receive $0 credit. No inventory, reservation or quote records are changed.",
    },
    {
        "code": "bounded_search",
        "message": "Completed means planned option evaluations finished, not optimality or that all parts fit. Missing or unplaced work is not proved infeasible.",
    },
]


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _summary(run: QuoteNestingRun) -> dict:
    fields = (
        "id",
        "company_id",
        "draft_id",
        "revision_id",
        "revision_number",
        "input_sha256",
        "created_by",
        "status",
        "version",
        "cancel_requested",
        "release_identity",
        "solver_version",
        "bundle_sha256",
        "node_version",
        "evaluated_count",
        "completed_count",
        "checkpoint_bytes",
        "error_code",
        "error_message",
    )
    result = {field: getattr(run, field) for field in fields}
    for field in ("created_at", "updated_at", "started_at", "finished_at"):
        value = getattr(run, field)
        result[field] = to_utc_iso(value) if value else None
    return result


def get_run(db: Session, company_id: int, run_id: int, *, locked: bool = False) -> QuoteNestingRun:
    query = tenant_query(db, QuoteNestingRun, company_id).filter(QuoteNestingRun.id == run_id)
    run = query.with_for_update().populate_existing().first() if locked else query.first()
    if run is None:
        raise HTTPException(404, "Nesting calculation not found")
    return run


def _checkpoints(db: Session, run: QuoteNestingRun) -> list[QuoteNestingRunCheckpoint]:
    return (
        tenant_query(db, QuoteNestingRunCheckpoint, run.company_id)
        .filter(QuoteNestingRunCheckpoint.run_id == run.id)
        .order_by(QuoteNestingRunCheckpoint.sequence)
        .all()
    )


def _checkpoint(row: QuoteNestingRunCheckpoint, *, payload: bool = False) -> dict:
    result = row.result_json["result"]
    nest = result["nest"]
    metadata = {
        "sequence": row.sequence,
        "group_id": row.group_id,
        "option_id": row.stock_option_id,
        "content_sha256": row.content_sha256,
        "payload_bytes": row.payload_bytes,
        "created_at": to_utc_iso(row.created_at),
        "complete": result["complete"],
        "sheets": nest["sheets"] if nest else 0,
        "placed": len(nest["placements"]) if nest else 0,
        "unplaced": sum(part["count"] for part in nest["unplaced"]) if nest else row.result_json["requested"],
    }
    if payload:
        metadata.update(schema_version=1, result=row.result_json)
    return metadata


def run_detail(db: Session, run: QuoteNestingRun, checkpoints: list[QuoteNestingRunCheckpoint] | None = None) -> dict:
    return {
        **_summary(run),
        "schema_version": 1,
        "settings": run.settings_json,
        "summary": run.summary_json,
        "warnings": WARNINGS,
        "checkpoints": [_checkpoint(row) for row in (_checkpoints(db, run) if checkpoints is None else checkpoints)],
    }


def list_runs(
    db: Session,
    company_id: int,
    *,
    page: int,
    per_page: int,
    draft_id: int | None = None,
    revision_number: int | None = None,
) -> dict:
    query = tenant_query(db, QuoteNestingRun, company_id).options(
        defer(QuoteNestingRun.settings_json), defer(QuoteNestingRun.summary_json)
    )
    if draft_id is not None:
        query = query.filter(QuoteNestingRun.draft_id == draft_id)
    if revision_number is not None:
        query = query.filter(QuoteNestingRun.revision_number == revision_number)
    total = query.count()
    rows = (
        query.order_by(QuoteNestingRun.created_at.desc(), QuoteNestingRun.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    return {
        "schema_version": 1,
        "items": [_summary(row) for row in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


def get_checkpoint(db: Session, company_id: int, run_id: int, sequence: int) -> dict:
    get_run(db, company_id, run_id)
    row = (
        tenant_query(db, QuoteNestingRunCheckpoint, company_id)
        .filter(QuoteNestingRunCheckpoint.run_id == run_id, QuoteNestingRunCheckpoint.sequence == sequence)
        .first()
    )
    if row is None:
        raise HTTPException(404, "Calculation checkpoint not found")
    return _checkpoint(row, payload=True)


def input_revision(db: Session, run: QuoteNestingRun) -> QuoteNestingRevision:
    revision = (
        tenant_query(db, QuoteNestingRevision, run.company_id)
        .filter(
            QuoteNestingRevision.id == run.revision_id,
            QuoteNestingRevision.draft_id == run.draft_id,
            QuoteNestingRevision.revision_number == run.revision_number,
            QuoteNestingRevision.content_sha256 == run.input_sha256,
        )
        .first()
    )
    if revision is None:
        raise ValueError("input_mismatch")
    return revision


def export_report(db: Session, company_id: int, run_id: int) -> dict:
    run = get_run(db, company_id, run_id)
    revision = input_revision(db, run)
    checkpoints = _checkpoints(db, run)
    # An active run may gain a checkpoint between reads. Snapshot the header and
    # exact currently observed prefix; never claim that later work was included.
    if len(checkpoints) != run.evaluated_count:
        raise HTTPException(409, "Calculation progressed during export. Refresh and export again.")
    for checkpoint in checkpoints:
        if _digest(checkpoint.result_json) != checkpoint.content_sha256:
            raise HTTPException(409, "Stored checkpoint integrity could not be verified")
    result = {
        "schema_version": 1,
        "status": "UNAPPROVED",
        "run": run_detail(db, run, checkpoints),
        "estimate": revision.estimate_json,
        "checkpoints": [_checkpoint(row, payload=True) for row in checkpoints],
    }
    if _digest(revision.estimate_json) != run.input_sha256:
        raise HTTPException(409, "Saved input integrity could not be verified")
    return {**result, "content_sha256": _digest(result)}


def _audit(audit: AuditService, run: QuoteNestingRun, action: str, **values: Any) -> None:
    audit.log_required(
        action,
        "quote_nesting_run",
        resource_id=run.id,
        description="Unapproved nesting calculation lifecycle",
        new_values={
            "draft_id": run.draft_id,
            "revision_number": run.revision_number,
            "input_sha256": run.input_sha256,
            "status": run.status,
            "version": run.version,
            **values,
        },
    )


def start_run(
    db: Session, user: User, company_id: int, audit: AuditService, request: StartRunRequest, runtime: dict | None = None
) -> dict:
    require_access(db, user, company_id, write=True)
    if request.expected_company_id != company_id:
        raise HTTPException(409, "Active company changed. Reopen this company's saved drafts.")
    company = db.query(Company).filter(Company.id == company_id, Company.is_active.is_(True)).first()
    if company is None:
        raise HTTPException(403, "The active company is unavailable")
    key = str(request.request_key)
    request_hash = _digest(
        {
            "protocol": 1,
            "actor": user.id,
            "company": company_id,
            "submitted_api_token_id": getattr(user, "_api_token_id", None),
            "request": request.model_dump(mode="json"),
        }
    )
    acquire_generator_lock(db, "quote_nesting_run", company_id)
    prior = tenant_query(db, QuoteNestingRun, company_id).filter(QuoteNestingRun.request_key == key).first()
    if prior is not None:
        if prior.created_by != user.id or prior.request_hash != request_hash:
            raise HTTPException(409, "This request key already identifies a different calculation")
        if prior.status == "QUEUED":
            mark_run_pending(db, company_id, prior.id)
        return run_detail(db, prior)
    if runtime is None:
        raise HTTPException(503, "The matching nesting worker is unavailable. Refresh runtime status before starting.")
    if tenant_query(db, QuoteNestingRun, company_id).filter(QuoteNestingRun.status.in_(ACTIVE_STATUSES)).first():
        raise HTTPException(409, "This company already has an active nesting calculation. Open or cancel it first.")
    revision = (
        tenant_query(db, QuoteNestingRevision, company_id)
        .filter(
            QuoteNestingRevision.draft_id == request.draft_id,
            QuoteNestingRevision.revision_number == request.revision_number,
        )
        .first()
    )
    if revision is None:
        raise HTTPException(404, "Saved nesting revision not found")
    if revision.content_sha256 != request.input_sha256 or _digest(revision.estimate_json) != request.input_sha256:
        raise HTTPException(409, "The requested input hash does not match that exact saved revision")
    # Re-apply bounded structure, never execute geometry or current-catalog substitutions.
    parse_estimate(canonical_json(revision.estimate_json).encode("utf-8"))
    if not expected_options(revision.estimate_json):
        raise HTTPException(422, "Save at least one part and enabled stock option before starting a calculation")
    now = datetime.utcnow()
    run = QuoteNestingRun(
        company_id=company_id,
        draft_id=revision.draft_id,
        revision_id=revision.id,
        revision_number=revision.revision_number,
        input_sha256=revision.content_sha256,
        request_key=key,
        request_hash=request_hash,
        created_by=user.id,
        submitted_api_token_id=getattr(user, "_api_token_id", None),
        status="QUEUED",
        version=1,
        cancel_requested=False,
        settings_json={**RUN_SETTINGS, "runtime": runtime},
        release_identity=runtime["release"],
        completed_count=0,
        evaluated_count=0,
        checkpoint_bytes=0,
        created_at=now,
        updated_at=now,
    )
    db.add(run)
    db.flush()
    _audit(audit, run, "NESTING_RUN_CREATED")
    # Register only after the last nested audit transaction has finished. The
    # listener dispatches solely on the outer commit, never on a savepoint.
    mark_run_pending(db, company_id, run.id)
    return run_detail(db, run)


def _advance(
    db: Session,
    run: QuoteNestingRun,
    audit: AuditService,
    action: str,
    changes: dict,
    *,
    lease_token: str | None = None,
) -> None:
    query = tenant_query(db, QuoteNestingRun, run.company_id).filter(
        QuoteNestingRun.id == run.id,
        QuoteNestingRun.version == run.version,
        QuoteNestingRun.status.in_(ACTIVE_STATUSES),
    )
    if lease_token is not None:
        query = query.filter(QuoteNestingRun.lease_token == lease_token)
    updates = {**changes, "version": run.version + 1, "updated_at": datetime.utcnow()}
    if query.update(updates, synchronize_session=False) != 1:
        raise HTTPException(409, "Calculation changed. Refresh its current state.")
    db.expire(run)
    db.refresh(run)
    _audit(
        audit,
        run,
        action,
        evaluated_count=run.evaluated_count,
        checkpoint_bytes=run.checkpoint_bytes,
        error_code=run.error_code,
    )


def cancel_run(
    db: Session,
    user: User,
    company_id: int,
    audit: AuditService,
    run_id: int,
    expected_company_id: int,
    expected_version: int,
) -> dict:
    require_access(db, user, company_id, write=True)
    if company_id != expected_company_id:
        raise HTTPException(409, "Active company changed. Refresh saved calculations.")
    run = get_run(db, company_id, run_id, locked=True)
    if run.status in TERMINAL_STATUSES:
        return run_detail(db, run)
    if run.version != expected_version:
        raise HTTPException(409, "Calculation changed. Refresh before cancelling.")
    if run.cancel_requested:
        return run_detail(db, run)
    changes = {
        "cancel_requested": True,
        "error_code": "user_cancelled",
        "error_message": ERROR_MESSAGES["user_cancelled"],
    }
    if run.status == "QUEUED":
        changes.update(status="CANCELLED", finished_at=datetime.utcnow())
    _advance(db, run, audit, "NESTING_RUN_CANCEL_REQUESTED", changes)
    return run_detail(db, run)


def worker_actor(db: Session, run: QuoteNestingRun) -> User:
    # The run is already tenant-resolved; a platform submitter may intentionally
    # have another home company. Ordinary users must still belong to this company.
    user = db.query(User).filter(User.id == run.created_by, User.is_active.is_(True)).first()
    company = db.query(Company).filter(Company.id == run.company_id, Company.is_active.is_(True)).first()
    if user is None or company is None:
        raise ValueError("actor_ineligible")
    platform = user.is_superuser or user.role == UserRole.PLATFORM_ADMIN
    if not platform and user.company_id != run.company_id:
        raise ValueError("actor_ineligible")
    user._active_company_id = run.company_id
    user._read_only_company_context = False
    user._api_token_id = None
    if run.submitted_api_token_id is not None:
        token = (
            tenant_query(db, ApiToken, run.company_id)
            .filter(ApiToken.id == run.submitted_api_token_id, ApiToken.user_id == user.id, ApiToken.revoked.is_(False))
            .first()
        )
        if platform or token is None or (token.expires_at is not None and token.expires_at <= datetime.utcnow()):
            raise ValueError("actor_ineligible")
        user._api_token_id = token.id
        user._api_token_label = token.label
        user._api_token_jti_prefix = token.jti_prefix
    try:
        require_access(db, user, run.company_id, write=True)
    except HTTPException as exc:
        raise ValueError("actor_ineligible") from exc
    return user


def worker_audit(db: Session, run: QuoteNestingRun) -> AuditService:
    # Lifecycle failures must still be audited even after actor revocation.
    user = db.query(User).filter(User.id == run.created_by).first()
    if user is not None:
        # Restore attribution from the immutable submission in every session.
        # This lookup intentionally includes revoked/expired tokens: it grants
        # no execution rights, which worker_actor checks separately.
        user._api_token_id = run.submitted_api_token_id
        user._api_token_label = None
        user._api_token_jti_prefix = None
        if run.submitted_api_token_id is not None:
            token = (
                tenant_query(db, ApiToken, run.company_id)
                .filter(ApiToken.id == run.submitted_api_token_id, ApiToken.user_id == run.created_by)
                .first()
            )
            if token is not None:
                user._api_token_label = token.label
                user._api_token_jti_prefix = token.jti_prefix
    return AuditService(db, user=user, company_id=run.company_id)


def finish_run(
    db: Session, run: QuoteNestingRun, code: str | None, *, summary: dict | None = None, lease_token: str | None = None
) -> None:
    if run.status in TERMINAL_STATUSES:
        return
    if lease_token is not None and run.lease_token != lease_token:
        raise ValueError("worker_lost")
    if run.cancel_requested:
        code = "user_cancelled"
    status = (
        "CANCELLED"
        if code == "user_cancelled"
        else (
            "PARTIAL"
            if code in ("time_limit", "work_limit", "output_limit") and run.evaluated_count
            else "FAILED" if code else "COMPLETED"
        )
    )
    _advance(
        db,
        run,
        worker_audit(db, run),
        "NESTING_RUN_FINISHED",
        {
            "status": status,
            "finished_at": datetime.utcnow(),
            "summary_json": summary,
            "error_code": code,
            "error_message": ERROR_MESSAGES.get(code) if code else None,
        },
        lease_token=lease_token,
    )


def claim_run(db: Session, company_id: int, run_id: int, *, runtime: dict) -> tuple[dict, str] | None:
    run = get_run(db, company_id, run_id, locked=True)
    if run.status != "QUEUED":
        return None
    try:
        actor = worker_actor(db, run)
        revision = input_revision(db, run)
        raw, _, canonical = parse_estimate(canonical_json(revision.estimate_json).encode("utf-8"))
        if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != run.input_sha256:
            raise ValueError("input_mismatch")
        if run.settings_json != {**RUN_SETTINGS, "runtime": runtime}:
            raise ValueError("runtime_mismatch")
    except (ValueError, HTTPException) as exc:
        code = str(exc) if str(exc) in ERROR_MESSAGES else "input_mismatch"
        finish_run(db, run, code)
        return None
    lease = str(uuid4())
    now = datetime.utcnow()
    _advance(
        db,
        run,
        AuditService(db, user=actor, company_id=company_id),
        "NESTING_RUN_STARTED",
        {
            "status": "RUNNING",
            "started_at": now,
            "lease_token": lease,
            "lease_expires_at": now + timedelta(seconds=LEASE_SECONDS),
        },
    )
    return {"protocol": 1, "input_sha256": run.input_sha256, "estimate": raw}, lease


def live_run(db: Session, company_id: int, run_id: int, lease: str) -> QuoteNestingRun:
    run = get_run(db, company_id, run_id, locked=True)
    if run.status != "RUNNING" or run.lease_token != lease or run.lease_expires_at <= datetime.utcnow():
        raise ValueError("worker_lost")
    if run.cancel_requested:
        raise ValueError("user_cancelled")
    worker_actor(db, run)
    return run


def heartbeat(db: Session, company_id: int, run_id: int, lease: str) -> None:
    run = live_run(db, company_id, run_id, lease)
    _advance(
        db,
        run,
        worker_audit(db, run),
        "NESTING_RUN_HEARTBEAT",
        {"lease_expires_at": datetime.utcnow() + timedelta(seconds=LEASE_SECONDS)},
        lease_token=lease,
    )


def accept_hello(db: Session, company_id: int, run_id: int, lease: str, hello: dict) -> None:
    run = live_run(db, company_id, run_id, lease)
    if run.bundle_sha256 is not None:
        raise ValueError("invalid_protocol")
    _advance(
        db,
        run,
        worker_audit(db, run),
        "NESTING_RUN_RUNTIME_BOUND",
        {field: hello[field] for field in ("solver_version", "bundle_sha256", "node_version")},
        lease_token=lease,
    )


def append_checkpoint(db: Session, company_id: int, run_id: int, lease: str, message: dict) -> None:
    run = live_run(db, company_id, run_id, lease)
    if run.bundle_sha256 is None:
        raise ValueError("invalid_protocol")
    revision = input_revision(db, run)
    planned = expected_options(revision.estimate_json)
    if run.evaluated_count >= min(len(planned), 36):
        raise ValueError("invalid_protocol")
    digest, byte_count = validate_option(
        message, run.input_sha256, planned[run.evaluated_count], run.evaluated_count + 1
    )
    if run.checkpoint_bytes + byte_count > MAX_CHECKPOINT_BYTES:
        raise ValueError("output_limit")
    checkpoint = QuoteNestingRunCheckpoint(
        company_id=company_id,
        run_id=run.id,
        lease_token=lease,
        sequence=message["sequence"],
        group_id=message["group_id"],
        stock_option_id=message["option_id"],
        result_json=message,
        content_sha256=digest,
        payload_bytes=byte_count,
        created_at=datetime.utcnow(),
    )
    db.add(checkpoint)
    db.flush()
    _advance(
        db,
        run,
        worker_audit(db, run),
        "NESTING_RUN_CHECKPOINT",
        {
            "evaluated_count": run.evaluated_count + 1,
            "completed_count": run.completed_count + int(message["result"]["complete"]),
            "checkpoint_bytes": run.checkpoint_bytes + byte_count,
        },
        lease_token=lease,
    )


def fail_expired(db: Session, company_id: int, run_id: int) -> bool:
    run = get_run(db, company_id, run_id, locked=True)
    if run.status != "RUNNING" or run.lease_expires_at > datetime.utcnow():
        return False
    finish_run(db, run, "worker_lost", lease_token=run.lease_token)
    return True
