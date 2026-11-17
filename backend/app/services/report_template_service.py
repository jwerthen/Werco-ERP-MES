"""Saved custom-report templates: access rules, stored-definition judgment, audited writes.

Access (deliberate, enforced here and documented on each route):

* **Read or run** a template -- ADMIN/MANAGER (the route's role gate), and only the
  author's own templates or shared ones (``require_readable``, 403 otherwise).
* **Change or delete** -- ADMIN/MANAGER, and only the author or an ADMIN
  (``require_modifiable``, 403 otherwise). A platform principal counts as an ADMIN here,
  as ``require_role`` already treats it everywhere else.
* A template of another company, or no template at all, is the same 404.

Stored definitions are judged, never trusted. ``report_templates`` rows were written with
type checks only (the API and the MCP tool ``create_report_template``), so a template
saved before a rule existed may break it. Such a template still lists and opens (its
``definition_errors`` say why it cannot run); running or exporting it is a 409 naming the
template. Both judgments go through ONE path -- building the request model from the
stored JSON (``_judge_stored``, behind ``request_from_template`` / ``stored_definition_errors``)
-- so the list and the run can never disagree. A stored row is data, never a request: no
value in one may 500 the list, so anything unexpected the rules raise on it is logged and
reported as a definition error like any other.

Template writes are audited best-effort (``AuditService.log_create`` / ``log_update`` /
``log_delete``, resource type ``report_template``): a template is a saved query, not a
quality record. ``ReportTemplate`` is not a ``SoftDeleteMixin`` model, so delete stays a
physical delete; the audit row keeps a snapshot of the definition it removed. Every write
takes the template's row lock BEFORE the audit call (which takes the global audit-chain
lock), so two writers can never wait on each other's lock in opposite order.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple, Type, TypeVar

from fastapi import HTTPException, status
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.db.tenant_filter import tenant_query
from app.models.analytics import ReportTemplate
from app.models.user import User, UserRole
from app.schemas.analytics import (
    REPORT_DEFAULT_LIMIT,
    REPORT_DEFINITION_ERROR_TYPE,
    CustomReportRequest,
    ReportDefinitionInput,
    ReportTemplateCreate,
    ReportTemplateResponse,
    ReportTemplateUpdate,
)
from app.services.audit_service import AuditService

logger = logging.getLogger(__name__)

AUDIT_RESOURCE_TYPE = "report_template"
NOT_FOUND_DETAIL = "Report template not found"
READ_DENIED_DETAIL = "Access denied to this report"
MODIFY_DENIED_DETAIL = "Only the template's author or an admin can change or delete it"
# The one problem a stored definition carries when judging it raised something unexpected.
UNCHECKABLE_DEFINITION_PROBLEM = "The saved definition could not be checked; edit the report and save it again"

_Judged = TypeVar("_Judged", bound=BaseModel)


def validation_problems(exc: ValidationError) -> List[str]:
    """User-facing problems from a ``ValidationError`` raised by a report request model.

    A ``definition_problems`` failure carries its list in ``ctx["problems"]``; any other
    error (a stored row whose JSON no longer has the right shape) reads ``<location>: <msg>``.
    """
    problems: List[str] = []
    for error in exc.errors():
        if error.get("type") == REPORT_DEFINITION_ERROR_TYPE:
            listed = (error.get("ctx") or {}).get("problems")
            problems.extend(listed if listed else [error["msg"]])
            continue
        location = ".".join(str(part) for part in error.get("loc", ()))
        problems.append(f"{location}: {error['msg']}" if location else error["msg"])
    return problems


def _stored_definition(template: ReportTemplate) -> Dict[str, Any]:
    return {
        "data_source": template.data_source,
        "columns": template.columns or [],
        "filters": template.filters or [],
        "group_by": template.group_by or [],
        "sort": template.sort or [],
    }


def _judge_stored(
    template: ReportTemplate, model: Type[_Judged], extra: Dict[str, Any]
) -> Tuple[Optional[_Judged], List[str]]:
    """Build ``model`` from the template's stored JSON: ``(request, [])`` or ``(None, problems)``.

    The ONE judgment of a stored definition, shared by the list/get (``definition_errors``)
    and the run/export (409). A ``ValidationError`` names its problems. Anything else the
    rules raise on a stored value is a defect in the rules, not in the request -- but the
    row was saved before that defect could be refused, so it is logged and reported as one
    problem instead of escaping: one bad row must never 500 the company's template list.
    """
    try:
        return model.model_validate({**_stored_definition(template), **extra}), []
    except ValidationError as exc:
        return None, validation_problems(exc)
    except Exception:  # deliberately broad -- see the docstring
        logger.exception("Could not judge the stored definition of report template %s", template.id)
        return None, [UNCHECKABLE_DEFINITION_PROBLEM]


def stored_definition_errors(template: ReportTemplate) -> List[str]:
    """Why this stored template can no longer run ([] when it can)."""
    return _judge_stored(template, ReportDefinitionInput, {})[1]


def request_from_template(template: ReportTemplate, *, limit: int = REPORT_DEFAULT_LIMIT) -> CustomReportRequest:
    """The validated request a stored template runs as, or 409 naming the template."""
    request, problems = _judge_stored(template, CustomReportRequest, {"limit": limit})
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f'Saved report "{template.name}" can no longer run: {"; ".join(problems)}. '
            "Edit it and save it again.",
        )
    return request


def template_response(template: ReportTemplate) -> ReportTemplateResponse:
    """Render a stored template, including its ``definition_errors``."""
    return ReportTemplateResponse(
        id=template.id,
        name=template.name,
        description=template.description,
        data_source=template.data_source,
        columns=template.columns or [],
        filters=template.filters or [],
        group_by=template.group_by or [],
        sort=template.sort or [],
        is_shared=bool(template.is_shared),
        created_by=template.created_by,
        created_at=template.created_at,
        updated_at=template.updated_at,
        definition_errors=stored_definition_errors(template),
    )


def _is_admin(user: User) -> bool:
    return bool(user.is_superuser) or user.role in (UserRole.ADMIN, UserRole.PLATFORM_ADMIN)


def get_template(db: Session, template_id: int, company_id: int) -> ReportTemplate:
    template = tenant_query(db, ReportTemplate, company_id).filter(ReportTemplate.id == template_id).first()
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    return template


def require_readable(db: Session, template_id: int, company_id: int, user: User) -> ReportTemplate:
    """The template, if the caller may read or run it: their own, or shared."""
    template = get_template(db, template_id, company_id)
    if not template.is_shared and template.created_by != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=READ_DENIED_DETAIL)
    return template


def require_modifiable(db: Session, template_id: int, company_id: int, user: User) -> ReportTemplate:
    """The template, if the caller may change or delete it: its author, or an admin."""
    template = get_template(db, template_id, company_id)
    if template.created_by != user.id and not _is_admin(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=MODIFY_DENIED_DETAIL)
    return template


def list_templates(db: Session, company_id: int, user: User) -> List[ReportTemplate]:
    """The caller's own templates plus every shared one, by name."""
    return (
        tenant_query(db, ReportTemplate, company_id)
        .filter((ReportTemplate.created_by == user.id) | (ReportTemplate.is_shared == True))  # noqa: E712
        .order_by(ReportTemplate.name, ReportTemplate.id)
        .all()
    )


def _definition_json(definition: ReportDefinitionInput) -> Dict[str, Any]:
    """The normalized definition as stored: plain JSON, sort directions lowercased."""
    return {
        "data_source": definition.data_source.value,
        "columns": [c.model_dump(mode="json") for c in definition.columns],
        "filters": [f.model_dump(mode="json") for f in definition.filters],
        "group_by": [g.model_dump(mode="json") for g in definition.group_by],
        "sort": [s.model_dump(mode="json") for s in definition.sort],
    }


def _snapshot(template: ReportTemplate) -> Dict[str, Any]:
    """What the audit row records: the template's identity, sharing and full definition."""
    return {
        "name": template.name,
        "description": template.description,
        "is_shared": bool(template.is_shared),
        "created_by": template.created_by,
        **_stored_definition(template),
    }


def create_template(
    db: Session, payload: ReportTemplateCreate, company_id: int, user: User, audit: AuditService
) -> ReportTemplate:
    template = ReportTemplate(
        company_id=company_id,
        name=payload.name,
        description=payload.description or None,
        is_shared=payload.is_shared,
        created_by=user.id,
        **_definition_json(payload),
    )
    db.add(template)
    db.flush()
    audit.log_create(AUDIT_RESOURCE_TYPE, template.id, template.name, new_values=_snapshot(template))
    db.commit()
    db.refresh(template)
    return template


def update_template(
    db: Session, template: ReportTemplate, payload: ReportTemplateUpdate, audit: AuditService
) -> ReportTemplate:
    before = _snapshot(template)
    fields = payload.model_fields_set
    if "name" in fields and payload.name is not None:
        template.name = payload.name
    if "description" in fields:
        template.description = payload.description or None
    if "is_shared" in fields and payload.is_shared is not None:
        template.is_shared = payload.is_shared
    if "definition" in fields and payload.definition is not None:
        for key, value in _definition_json(payload.definition).items():
            setattr(template, key, value)
    db.flush()
    audit.log_update(AUDIT_RESOURCE_TYPE, template.id, template.name, old_values=before, new_values=_snapshot(template))
    db.commit()
    db.refresh(template)
    return template


def delete_template(db: Session, template: ReportTemplate, company_id: int, audit: AuditService) -> None:
    """Physically delete ``template`` and audit a snapshot of what it held.

    Lock order matches create and update: the row first (the DELETE below takes its row
    lock), THEN ``log_delete`` (the global audit-chain lock). Auditing first inverted the
    order a concurrent PATCH takes -- row, then audit -- so the two could deadlock.

    The DELETE is an explicit tenant-scoped statement whose rowcount is checked. If a
    concurrent delete removed the row after ``require_modifiable`` read it, this one
    deletes nothing: that is the same 404 as a missing template, and no audit row is
    written for a delete that did not happen.
    """
    template_id = template.id
    name = template.name
    snapshot = _snapshot(template)
    deleted = (
        tenant_query(db, ReportTemplate, company_id)
        .filter(ReportTemplate.id == template_id)
        .delete(synchronize_session="evaluate")
    )
    if deleted != 1:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    audit.log_delete(AUDIT_RESOURCE_TYPE, template_id, name, old_values=snapshot)
    db.commit()
