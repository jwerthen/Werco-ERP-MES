"""Immutable, unapproved client-input revisions; no quote or inventory writes."""

import hashlib
import json
import math
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session, defer

from app.core.time_utils import to_utc_iso
from app.db.locks import acquire_generator_lock
from app.db.tenant_filter import tenant_query
from app.models.quote_config import QuoteMaterial
from app.models.quote_nesting_draft import QuoteNestingDraft, QuoteNestingRevision
from app.models.role_permission import DEFAULT_ROLE_PERMISSIONS, RolePermission
from app.models.user import User, UserRole
from app.schemas.quote_nesting_drafts import MAX_ESTIMATE_BYTES, SavedProject
from app.services.audit_service import AuditService
from app.services.quote_nesting_materials import catalog_material
from app.services.quote_nesting_spacing import verify_project_policies
from app.services.remnant_planning import (
    require_revision_evidence_access,
    require_saved_evidence_access,
    verify_project_selection,
)


def require_access(db: Session, user: User, company_id: int, *, write: bool = False) -> None:
    if write and getattr(user, "_read_only_company_context", False):
        raise HTTPException(403, "This company context is read-only")
    if user.is_superuser or user.role == UserRole.PLATFORM_ADMIN:
        return
    override = tenant_query(db, RolePermission, company_id).filter(RolePermission.role == user.role).first()
    permissions = override.permissions if override is not None else DEFAULT_ROLE_PERMISSIONS.get(user.role, [])
    required = {"purchasing:view", "purchasing:create"} if write else {"purchasing:view"}
    if (
        not isinstance(permissions, list)
        or not all(isinstance(permission, str) for permission in permissions)
        or not required.issubset(permissions)
    ):
        raise HTTPException(403, "Saved nesting drafts require " + " and ".join(sorted(required)))


def canonical_json(value: Any) -> str:
    # This is a SERVER JSON encoding, not the browser geometry canonicalization.
    # The digest identifies the stored client snapshot, never original CAD bytes.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object field")
        result[key] = value
    return result


def parse_estimate(content: bytes) -> tuple[dict, SavedProject, str]:
    if len(content) > MAX_ESTIMATE_BYTES:
        raise HTTPException(413, "Saved estimates are limited to 5 MiB")
    try:
        value = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object)
        stack = [(value, 0)]
        nodes = 0
        while stack:
            item, depth = stack.pop()
            nodes += 1
            if depth > 24 or nodes > 200000:
                raise ValueError("Estimate structure exceeds the parsing budget")
            if isinstance(item, dict):
                if nodes + len(stack) + len(item) > 200000:
                    raise ValueError("Estimate structure exceeds the parsing budget")
                if any(len(key) > 100 for key in item):
                    raise ValueError("Estimate field name is too long")
                stack.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                if nodes + len(stack) + len(item) > 200000:
                    raise ValueError("Estimate structure exceeds the parsing budget")
                stack.extend((child, depth + 1) for child in item)
            elif isinstance(item, str):
                if len(item) > 2000 or "\x00" in item:
                    raise ValueError("Estimate text is too long or contains a null character")
            elif isinstance(item, (int, float)) and not isinstance(item, bool):
                if not math.isfinite(item):
                    raise ValueError("Estimate numbers must be finite")
        parsed = SavedProject.model_validate(value)
        canonical = canonical_json(value)
        if len(canonical.encode("utf-8")) > MAX_ESTIMATE_BYTES:
            raise HTTPException(413, "Canonical saved estimate exceeds 5 MiB")
        return value, parsed, canonical
    except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError, ValidationError) as exc:
        # Pydantic's default error detail can echo an entire private geometry.
        raise HTTPException(422, "Invalid saved estimate structure: " + _validation_message(exc)) from exc


def _validation_message(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        first = exc.errors(include_input=False, include_url=False)[0]
        return ".".join(str(item) for item in first["loc"])[:200] + ": " + first["msg"][:250]
    return str(exc)[:300]


def _review_sources(db: Session, company_id: int, project: SavedProject, raw: dict) -> list[dict]:
    issues = [
        {
            "code": "unapproved_client_snapshot",
            "message": "Saved client inputs only. Geometry, pricing, CAD source hashes and reuse eligibility are not server-approved.",
        }
    ]
    for group, raw_group in zip(project.groups, raw["groups"]):
        if group.quote.geometryProfile is not None:
            issues.append(
                {
                    'code': 'geometry_profile_not_shop_approval',
                    'group_id': group.id,
                    'message': 'The geometry profile identifies software clearance rules only. It does not approve physical cutting allowances, material, pricing or inventory eligibility.',
                }
            )
        if any(stock.exclusions for stock in group.quote.options):
            issues.append(
                {
                    "code": "unverified_stock_exclusions",
                    "group_id": group.id,
                    "message": "Stock exclusions are estimator-reported unavailable areas, not physically verified stock. Topology and guarded clearance are checked only during calculation.",
                }
            )
        binding = group.quote.materialBinding
        if binding is None:
            continue
        if binding.companyId != company_id or (
            binding.resolution is not None and binding.resolution.company_id != company_id
        ):
            raise HTTPException(422, "Every catalog binding must belong to the active company")
        material = tenant_query(db, QuoteMaterial, company_id).filter(QuoteMaterial.id == binding.catalog.id).first()
        if material is None:
            raise HTTPException(422, "A selected catalog material does not exist in the active company")
        if not material.is_active:
            issues.append(
                {
                    "code": "inactive_catalog",
                    "group_id": group.id,
                    "message": "The selected catalog material is inactive; refresh the source before using its prices.",
                }
            )
        current = catalog_material(material).model_dump(mode="json")
        submitted = raw_group["quote"]["materialBinding"]["catalog"]
        if canonical_json(current) != canonical_json(submitted):
            issues.append(
                {
                    "code": "stale_catalog_snapshot",
                    "group_id": group.id,
                    "message": "The client catalog snapshot differs from the current source; saved prices are unconfirmed.",
                }
            )
    return issues


def revision_response(revision: QuoteNestingRevision, *, include_estimate: bool = False) -> dict:
    result = {
        "draft_id": revision.draft_id,
        "company_id": revision.company_id,
        "revision_number": revision.revision_number,
        "draft_version": revision.draft_version,
        "name": revision.name,
        "status": "DRAFT",
        "content_sha256": revision.content_sha256,
        "payload_schema_version": revision.payload_schema_version,
        "payload_bytes": revision.payload_bytes,
        "created_by": revision.created_by,
        "created_at": to_utc_iso(revision.created_at),
        "review_issues": revision.review_issues_json,
    }
    if include_estimate:
        result.update(schema_version=1, estimate=revision.estimate_json)
    return result


def list_drafts(
    db: Session,
    company_id: int,
    *,
    page: int,
    per_page: int,
    draft_id: int | None = None,
    user: User | None = None,
) -> dict:
    query = tenant_query(db, QuoteNestingRevision, company_id).options(defer(QuoteNestingRevision.estimate_json))
    if draft_id is None:
        query = query.join(QuoteNestingDraft, QuoteNestingDraft.id == QuoteNestingRevision.draft_id).filter(
            QuoteNestingDraft.company_id == company_id,
            QuoteNestingRevision.revision_number == QuoteNestingDraft.latest_revision_number,
        )
    else:
        _draft(db, company_id, draft_id)
        query = query.filter(QuoteNestingRevision.draft_id == draft_id)
    total = query.count()
    rows = (
        query.order_by(QuoteNestingRevision.created_at.desc(), QuoteNestingRevision.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    require_revision_evidence_access(db, user, company_id, [row.id for row in rows])
    return {
        "schema_version": 1,
        "items": [revision_response(row) for row in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


def _draft(db: Session, company_id: int, draft_id: int, *, locked: bool = False):
    query = tenant_query(db, QuoteNestingDraft, company_id).filter(QuoteNestingDraft.id == draft_id)
    draft = query.with_for_update().populate_existing().first() if locked else query.first()
    if draft is None:
        raise HTTPException(404, "Nesting draft not found")
    return draft


def get_revision(
    db: Session,
    company_id: int,
    draft_id: int,
    number: int,
    *,
    user: User | None = None,
) -> dict:
    revision = (
        tenant_query(db, QuoteNestingRevision, company_id)
        .filter(QuoteNestingRevision.draft_id == draft_id, QuoteNestingRevision.revision_number == number)
        .first()
    )
    if revision is None:
        raise HTTPException(404, "Nesting draft revision not found")
    require_saved_evidence_access(db, user, company_id, revision.estimate_json)
    return revision_response(revision, include_estimate=True)


def save_revision(
    db: Session,
    user: User,
    company_id: int,
    audit: AuditService,
    *,
    content: bytes,
    request_key: str,
    expected_company_id: int,
    draft_id: int | None = None,
    expected_version: int | None = None,
) -> dict:
    """Flush one revision and its audit; caller owns the sole atomic commit."""
    require_access(db, user, company_id, write=True)
    if type(expected_company_id) is not int or expected_company_id != company_id:
        raise HTTPException(409, "Active company changed. Reopen this company's nesting workspace before saving.")
    try:
        key = str(UUID(request_key))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(422, "request_key must be a UUID") from exc
    if (draft_id is None) != (expected_version is None) or (
        expected_version is not None and (type(expected_version) is not int or expected_version < 1)
    ):
        raise HTTPException(422, "Appending a revision requires a positive expected_version")
    raw, parsed, canonical = parse_estimate(content)
    require_saved_evidence_access(db, user, company_id, raw)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    request_hash = hashlib.sha256(
        canonical_json(
            {
                "version": 1,
                "actor": user.id,
                "company": company_id,
                "draft_id": draft_id,
                "expected_version": expected_version,
                "estimate": raw,
            }
        ).encode("utf-8")
    ).hexdigest()
    acquire_generator_lock(db, "quote_nesting_draft:" + key, company_id)
    prior = tenant_query(db, QuoteNestingRevision, company_id).filter(QuoteNestingRevision.request_key == key).first()
    if prior is not None:
        if prior.created_by != user.id or prior.request_hash != request_hash:
            raise HTTPException(409, "This request key already saved a different nesting revision")
        return revision_response(prior, include_estimate=True)
    draft = _draft(db, company_id, draft_id, locked=True) if draft_id is not None else None
    if draft is not None and draft.version != expected_version:
        raise HTTPException(409, "This draft has a newer revision. Open its latest revision or save a separate draft.")
    issues = _review_sources(db, company_id, parsed, raw)
    issues.extend(verify_project_policies(db, company_id, parsed))
    issues.extend(verify_project_selection(db, user, company_id, parsed, raw))
    now = datetime.utcnow()
    if draft is None:
        draft = QuoteNestingDraft(
            company_id=company_id,
            name=parsed.name,
            status="DRAFT",
            version=1,
            latest_revision_number=1,
            created_by=user.id,
            created_at=now,
            updated_at=now,
        )
        db.add(draft)
        db.flush()
        number = 1
    else:
        number = expected_version + 1
        changed = (
            tenant_query(db, QuoteNestingDraft, company_id)
            .filter(QuoteNestingDraft.id == draft.id, QuoteNestingDraft.version == expected_version)
            .update(
                {
                    QuoteNestingDraft.version: number,
                    QuoteNestingDraft.latest_revision_number: number,
                    QuoteNestingDraft.name: parsed.name,
                    QuoteNestingDraft.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        if changed != 1:
            raise HTTPException(409, "The draft changed during save. Reopen the latest revision.")
    revision = QuoteNestingRevision(
        company_id=company_id,
        draft_id=draft.id,
        revision_number=number,
        draft_version=number,
        name=parsed.name,
        estimate_json=raw,
        content_sha256=digest,
        payload_schema_version=parsed.version,
        payload_bytes=len(canonical.encode("utf-8")),
        created_by=user.id,
        created_at=now,
        request_key=key,
        request_hash=request_hash,
        review_issues_json=issues,
    )
    db.add(revision)
    db.flush()
    audit.log_required(
        action="CREATE",
        resource_type="quote_nesting_revision",
        resource_id=revision.id,
        resource_identifier=f"{draft.id}/revision/{number}",
        company_id=company_id,
        description="Saved an unapproved nesting input revision",
        new_values={
            "draft_id": draft.id,
            "revision_number": number,
            "draft_version": number,
            "content_sha256": digest,
            "payload_schema_version": parsed.version,
            "payload_bytes": revision.payload_bytes,
            "status": "DRAFT",
            "request_key": key,
        },
        extra_data={"created_draft": number == 1, "review_codes": sorted({issue["code"] for issue in issues})},
    )
    return revision_response(revision, include_estimate=True)
