"""Company-owned immutable quote-spacing decisions with exact decimal resolution."""

import hashlib
import json
from datetime import datetime
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.orm import Session, defer

from app.core.time_utils import to_utc_iso
from app.db.locks import acquire_generator_lock
from app.db.tenant_filter import tenant_query
from app.models.company import Company
from app.models.quote_nesting_spacing_policy import QuoteNestingSpacingEvent as Event
from app.models.quote_nesting_spacing_policy import QuoteNestingSpacingPolicy as Policy
from app.models.quote_nesting_spacing_policy import QuoteNestingSpacingRevision as Revision
from app.models.user import User, UserRole
from app.schemas.quote_nesting_spacing import (
    MAX_POLICY_BYTES,
    CreateSpacingRevision,
    PolicyCommand,
    PublishSpacingRevision,
    SpacingContent,
    SpacingPolicySnapshot,
    WithdrawSpacingPublication,
    naive_utc,
    normalize_thickness,
    resolve_band,
)
from app.services.audit_service import AuditService


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def policy_lock(db: Session, company_id: int) -> None:
    acquire_generator_lock(db, "quote_nesting_spacing_policy", company_id)


def require_policy_write(db: Session, user: User, company_id: int) -> None:
    from app.services.quote_nesting_drafts import require_access

    require_access(db, user, company_id, write=True)
    if not (user.is_superuser or user.role in (UserRole.ADMIN, UserRole.PLATFORM_ADMIN)):
        raise HTTPException(403, "Spacing policy changes require an Admin")
    if db.query(Company).filter(Company.id == company_id, Company.is_active.is_(True)).first() is None:
        raise HTTPException(403, "The active company is unavailable")


def _header(db: Session, company_id: int, *, locked: bool = False):
    query = tenant_query(db, Policy, company_id)
    return query.with_for_update().populate_existing().first() if locked else query.first()


def _revision(db: Session, company_id: int, number: int) -> Revision:
    row = tenant_query(db, Revision, company_id).filter(Revision.revision_number == number).first()
    if row is None:
        raise HTTPException(404, "Spacing policy revision not found")
    return row


def _publication(db: Session, company_id: int, publication_id: int) -> Event:
    row = tenant_query(db, Event, company_id).filter(Event.id == publication_id, Event.kind == "PUBLISHED").first()
    if row is None:
        raise HTTPException(404, "Spacing policy publication not found")
    return row


def _current(db: Session, company_id: int, now: datetime) -> Event | None:
    # Select before checking withdrawal; never reactivate an earlier approval.
    return (
        tenant_query(db, Event, company_id)
        .filter(Event.kind == "PUBLISHED", Event.effective_at <= now)
        .order_by(Event.effective_at.desc(), Event.id.desc())
        .first()
    )


def _withdrawal(db: Session, company_id: int, publication_id: int) -> Event | None:
    return (
        tenant_query(db, Event, company_id)
        .filter(Event.kind == "WITHDRAWN", Event.publication_id == publication_id)
        .first()
    )


def revision_summary(row: Revision) -> dict:
    fields = (
        "id",
        "company_id",
        "policy_id",
        "revision_number",
        "name",
        "content_sha256",
        "payload_schema_version",
        "payload_bytes",
        "created_by",
    )
    return {**{field: getattr(row, field) for field in fields}, "created_at": to_utc_iso(row.created_at)}


def publication_response(db: Session, row: Event, now: datetime, current: Event | None) -> dict:
    withdrawal = _withdrawal(db, row.company_id, row.id)
    status = (
        "withdrawn"
        if withdrawal
        else (
            "scheduled"
            if row.effective_at > now
            else "current" if current is not None and current.id == row.id else "superseded"
        )
    )
    fields = (
        "id",
        "company_id",
        "policy_id",
        "policy_version",
        "revision_id",
        "revision_number",
        "content_sha256",
        "created_by",
        "reason",
    )
    return {
        **{field: getattr(row, field) for field in fields},
        "effective_at": to_utc_iso(row.effective_at),
        "created_at": to_utc_iso(row.created_at),
        "status": status,
        "withdrawal": (
            None
            if withdrawal is None
            else {
                "id": withdrawal.id,
                "created_by": withdrawal.created_by,
                "created_at": to_utc_iso(withdrawal.created_at),
                "reason": withdrawal.reason,
            }
        ),
    }


def state(db: Session, company_id: int, *, page: int, per_page: int) -> dict:
    now = datetime.utcnow()
    header = _header(db, company_id)
    current = _current(db, company_id, now)
    revisions = tenant_query(db, Revision, company_id).options(defer(Revision.content_json))
    publications = tenant_query(db, Event, company_id).filter(Event.kind == "PUBLISHED")
    offset = (page - 1) * per_page
    fields = ("id", "company_id", "version", "latest_revision_number", "created_by")
    return {
        "schema_version": 1,
        "policy": (
            None
            if header is None
            else {
                **{field: getattr(header, field) for field in fields},
                "created_at": to_utc_iso(header.created_at),
                "updated_at": to_utc_iso(header.updated_at),
            }
        ),
        "current_publication": publication_response(db, current, now, current) if current is not None else None,
        "revisions": [
            revision_summary(row)
            for row in revisions.order_by(Revision.revision_number.desc()).offset(offset).limit(per_page)
        ],
        "publications": [
            publication_response(db, row, now, current)
            for row in publications.order_by(Event.effective_at.desc(), Event.id.desc()).offset(offset).limit(per_page)
        ],
        "total_revisions": revisions.count(),
        "total_publications": publications.count(),
        "page": page,
        "per_page": per_page,
    }


def get_revision(db: Session, company_id: int, number: int) -> dict:
    row = _revision(db, company_id, number)
    return {**revision_summary(row), "schema_version": 1, "content": row.content_json}


def command_response(db: Session, event: Event) -> dict:
    revision = _revision(db, event.company_id, event.revision_number)
    publication = (
        event
        if event.kind == "PUBLISHED"
        else (_publication(db, event.company_id, event.publication_id) if event.kind == "WITHDRAWN" else None)
    )
    now = datetime.utcnow()
    return {
        "schema_version": 1,
        "policy_version": event.policy_version,
        "event_id": event.id,
        "revision": revision_summary(revision),
        "publication": (
            publication_response(db, publication, now, _current(db, event.company_id, now)) if publication else None
        ),
    }


def _begin(
    db: Session, user: User, company_id: int, request: PolicyCommand, kind: str, publication_id: int | None = None
):
    require_policy_write(db, user, company_id)
    if request.expected_company_id != company_id:
        raise HTTPException(409, "Active company changed. Reopen spacing policies.")
    request_hash = digest(
        {
            "schema_version": 1,
            "company_id": company_id,
            "actor": user.id,
            "submitted_api_token_id": getattr(user, "_api_token_id", None),
            "kind": kind,
            "publication_id": publication_id,
            "request": request.model_dump(mode="json"),
        }
    )
    policy_lock(db, company_id)
    prior = tenant_query(db, Event, company_id).filter(Event.request_key == str(request.request_key)).first()
    if prior is not None:
        if prior.created_by != user.id or prior.request_hash != request_hash:
            raise HTTPException(409, "This request key already identifies a different policy command")
        return None, prior, request_hash
    header = _header(db, company_id, locked=True)
    if (header.version if header is not None else 0) != request.expected_version:
        raise HTTPException(409, "The policy history changed. Refresh before submitting a new command.")
    if header is None:
        if kind != "REVISION_CREATED":
            raise HTTPException(404, "Create a spacing policy revision first")
        header = Policy(company_id=company_id, created_by=user.id, version=0, latest_revision_number=0)
        db.add(header)
        db.flush()
    return header, None, request_hash


def _record(
    db: Session,
    user: User,
    header: Policy,
    revision: Revision,
    request: PolicyCommand,
    kind: str,
    request_hash: str,
    audit: AuditService,
    *,
    publication_id: int | None = None,
    effective_at: datetime | None = None,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.utcnow()
    version = header.version + 1
    latest = revision.revision_number if kind == "REVISION_CREATED" else header.latest_revision_number
    changed = (
        tenant_query(db, Policy, header.company_id)
        .filter(Policy.id == header.id, Policy.version == header.version)
        .update(
            {Policy.version: version, Policy.latest_revision_number: latest, Policy.updated_at: now},
            synchronize_session=False,
        )
    )
    if changed != 1:
        raise HTTPException(409, "Policy changed during this command. Refresh its history.")
    if kind == "REVISION_CREATED":
        db.add(revision)
        db.flush()
    event = Event(
        company_id=header.company_id,
        policy_id=header.id,
        policy_version=version,
        kind=kind,
        revision_id=revision.id,
        revision_number=revision.revision_number,
        content_sha256=revision.content_sha256,
        publication_id=publication_id,
        effective_at=effective_at,
        reason=request.reason,
        created_by=user.id,
        submitted_api_token_id=getattr(user, "_api_token_id", None),
        created_at=now,
        request_key=str(request.request_key),
        request_hash=request_hash,
    )
    db.add(event)
    db.flush()
    audit.log_required(
        action="NESTING_SPACING_" + kind,
        resource_type="quote_nesting_spacing_event",
        resource_id=event.id,
        company_id=header.company_id,
        description="Explicit quoting-policy decision; no quote or manufacturing approval",
        new_values={
            "policy_id": header.id,
            "policy_version": version,
            "revision_number": revision.revision_number,
            "content_sha256": revision.content_sha256,
            "publication_id": publication_id,
            "effective_at": to_utc_iso(effective_at),
            "reason": request.reason,
        },
    )
    db.expire(header)
    return command_response(db, event)


def create_revision(
    db: Session, user: User, company_id: int, audit: AuditService, request: CreateSpacingRevision
) -> dict:
    header, prior, request_hash = _begin(db, user, company_id, request, "REVISION_CREATED")
    if prior is not None:
        return command_response(db, prior)
    content = request.content.model_dump(mode="json")
    payload = canonical(content).encode("utf-8")
    if len(payload) > MAX_POLICY_BYTES:
        raise HTTPException(422, "Spacing policy content exceeds 64 KiB")
    row = Revision(
        company_id=company_id,
        policy_id=header.id,
        revision_number=header.latest_revision_number + 1,
        name=request.content.name,
        content_json=content,
        content_sha256=hashlib.sha256(payload).hexdigest(),
        payload_schema_version=1,
        payload_bytes=len(payload),
        created_by=user.id,
    )
    return _record(db, user, header, row, request, "REVISION_CREATED", request_hash, audit)


def publish(db: Session, user: User, company_id: int, audit: AuditService, request: PublishSpacingRevision) -> dict:
    header, prior, request_hash = _begin(db, user, company_id, request, "PUBLISHED")
    if prior is not None:
        return command_response(db, prior)
    revision = _revision(db, company_id, request.revision_number)
    if revision.content_sha256 != request.content_sha256 or digest(revision.content_json) != request.content_sha256:
        raise HTTPException(409, "Publication must bind the exact saved policy revision hash")
    SpacingContent.model_validate(revision.content_json)
    now = datetime.utcnow()
    effective = naive_utc(request.effective_at) if request.effective_at is not None else now
    if effective < now:
        raise HTTPException(422, "A policy publication cannot be retroactive; choose immediate or a future time")
    if tenant_query(db, Event, company_id).filter(Event.kind == "PUBLISHED", Event.effective_at == effective).first():
        raise HTTPException(409, "A policy is already scheduled for this exact effective time")
    return _record(
        db, user, header, revision, request, "PUBLISHED", request_hash, audit, effective_at=effective, now=now
    )


def withdraw(
    db: Session,
    user: User,
    company_id: int,
    audit: AuditService,
    publication_id: int,
    request: WithdrawSpacingPublication,
) -> dict:
    header, prior, request_hash = _begin(db, user, company_id, request, "WITHDRAWN", publication_id)
    if prior is not None:
        return command_response(db, prior)
    publication = _publication(db, company_id, publication_id)
    if _withdrawal(db, company_id, publication.id) is not None:
        raise HTTPException(409, "This policy publication has already been withdrawn")
    return _record(
        db,
        user,
        header,
        _revision(db, company_id, publication.revision_number),
        request,
        "WITHDRAWN",
        request_hash,
        audit,
        publication_id=publication.id,
    )


def resolve(db: Session, company_id: int, material: str, thickness_in: str) -> dict:
    now = datetime.utcnow()
    thickness = normalize_thickness(thickness_in)
    current = _current(db, company_id, now)
    if current is None or _withdrawal(db, company_id, current.id) is not None:
        return {
            "schema_version": 1,
            "status": "unavailable",
            "policy": None,
            "explanation": "No currently effective, unwithdrawn company policy is available. Starting allowances remain unapproved.",
        }
    revision = _revision(db, company_id, current.revision_number)
    content = SpacingContent.model_validate(revision.content_json)
    if digest(revision.content_json) != current.content_sha256:
        raise HTTPException(409, "The immutable policy content hash does not match its publication")
    band = next(
        (
            band
            for band in content.bands
            if band.material == material
            and Decimal(band.thickness_min_in) <= Decimal(thickness) < Decimal(band.thickness_max_in)
        ),
        None,
    )
    if band is None:
        return {
            "schema_version": 1,
            "status": "unmatched",
            "policy": None,
            "explanation": "The current company policy has no band for this material family and normalized thickness.",
        }
    gap, margin = resolve_band(band, thickness)
    snapshot = {
        "schema_version": 1,
        "company_id": company_id,
        "policy_id": current.policy_id,
        "publication_id": current.id,
        "revision_id": revision.id,
        "revision_number": revision.revision_number,
        "content_sha256": revision.content_sha256,
        "band": band.model_dump(mode="json"),
        "thickness_in": thickness,
        "gap_in": gap,
        "margin_in": margin,
        "resolved_at": to_utc_iso(now),
    }
    return {
        "schema_version": 1,
        "status": "resolved",
        "policy": snapshot,
        "explanation": "Explicitly apply this company-approved family-level quoting policy. Quote, geometry and manufacturing eligibility remain unapproved.",
    }


def verify_project_policies(db: Session, company_id: int, project) -> list[dict]:
    """Verify new save/start claims, never reinterpret a queued or historical run."""
    claims = [group for group in project.groups if group.quote.spacingPolicy is not None]
    if claims:
        policy_lock(db, company_id)
    now = datetime.utcnow()
    current = _current(db, company_id, now) if claims else None
    issues = []
    for group in project.groups:
        snapshot: SpacingPolicySnapshot | None = group.quote.spacingPolicy
        if snapshot is None:
            issues.append(
                {
                    "code": "custom_spacing" if group.quote.spacingOverride else "unreviewed_spacing",
                    "group_id": group.id,
                    "message": "Spacing is an unapproved estimator choice; no company policy conformance is asserted.",
                }
            )
            continue
        if snapshot.company_id != company_id:
            raise HTTPException(422, "Spacing policy must belong to the active company")
        publication = _publication(db, company_id, snapshot.publication_id)
        if current is None or current.id != publication.id or _withdrawal(db, company_id, publication.id) is not None:
            raise HTTPException(
                409,
                "Spacing policy is no longer current. Apply the current policy or choose custom spacing with a reason.",
            )
        revision = _revision(db, company_id, snapshot.revision_number)
        if (
            (publication.policy_id, publication.revision_id, publication.revision_number, publication.content_sha256)
            != (snapshot.policy_id, snapshot.revision_id, snapshot.revision_number, snapshot.content_sha256)
            or revision.id != snapshot.revision_id
            or digest(revision.content_json) != snapshot.content_sha256
        ):
            raise HTTPException(422, "Spacing policy snapshot does not match its immutable publication")
        content = SpacingContent.model_validate(revision.content_json)
        band = next((item for item in content.bands if item.id == snapshot.band.id), None)
        resolved_at = naive_utc(snapshot.resolved_at)
        if (
            band is None
            or band.model_dump() != snapshot.band.model_dump()
            or not publication.effective_at <= resolved_at <= now
        ):
            raise HTTPException(422, "Spacing policy band or resolution time does not match the published source")
        issues.append(
            {
                "code": "family_spacing_policy_applied",
                "group_id": group.id,
                "message": "Verified current family-level quoting-policy snapshot; this does not approve the quote or manufacturing parameters.",
            }
        )
    return issues
