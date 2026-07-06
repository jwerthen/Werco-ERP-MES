"""Process Sheets library CRUD + lifecycle (PR 1 of docs/PROCESS_SHEETS_SCOPE.md).

Thin router: validate input, call process_sheet_service, return a Pydantic schema.
All tenant scoping, draft-only guards, per-type config validation, and audit logging
live in the service. Shop-floor execution endpoints are PR 3 (under /shop-floor, so
kiosk-token fencing needs zero changes).

Roles: authoring (create/edit/steps/new-revision) = Admin/Manager/Supervisor/Quality;
release/obsolete = Admin/Manager/Quality (quality owns released inspection documents);
reads = any authenticated user.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.db.database import get_db
from app.models.process_sheet import ProcessSheetStatus
from app.models.user import User, UserRole
from app.schemas.process_sheet import (
    ProcessSheetCreate,
    ProcessSheetListResponse,
    ProcessSheetResponse,
    ProcessSheetStepCreate,
    ProcessSheetStepResponse,
    ProcessSheetStepUpdate,
    ProcessSheetUpdate,
)
from app.services import process_sheet_service
from app.services.audit_service import AuditService

router = APIRouter()

AUTHOR_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.QUALITY]
RELEASE_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY]


@router.get("/", response_model=List[ProcessSheetListResponse])
def list_process_sheets(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    status: Optional[ProcessSheetStatus] = None,
    search: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List process sheets (all revisions), newest sheet number first."""
    sheets = process_sheet_service.list_sheets(
        db, company_id, status=status.value if status else None, search=search, skip=skip, limit=limit
    )
    return [
        ProcessSheetListResponse(
            id=s.id,
            sheet_number=s.sheet_number,
            title=s.title,
            revision=s.revision,
            status=s.status,
            is_active=s.is_active,
            effective_date=s.effective_date,
            step_count=len(s.steps),
            created_at=s.created_at,
            updated_at=s.updated_at,
        )
        for s in sheets
    ]


@router.get("/{sheet_id}", response_model=ProcessSheetResponse)
def get_process_sheet(
    sheet_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get a process sheet with its steps."""
    return process_sheet_service.get_sheet(db, company_id, sheet_id)


@router.post("/", response_model=ProcessSheetResponse)
def create_process_sheet(
    data: ProcessSheetCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(AUTHOR_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create a new process sheet (DRAFT, Rev A, auto-numbered PS-XXXXXX)."""
    return process_sheet_service.create_sheet(db, company_id, data, current_user, audit)


@router.patch("/{sheet_id}", response_model=ProcessSheetResponse)
def update_process_sheet(
    sheet_id: int,
    data: ProcessSheetUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(AUTHOR_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Update sheet header fields. Draft-only — a released/obsolete sheet returns 409."""
    return process_sheet_service.update_sheet(db, company_id, sheet_id, data, current_user, audit)


@router.delete("/{sheet_id}")
def delete_process_sheet(
    sheet_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(AUTHOR_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Soft-delete a DRAFT process sheet (409 for released/obsolete — obsolete those instead)."""
    process_sheet_service.soft_delete_sheet(db, company_id, sheet_id, current_user, audit)
    return {"message": "Process sheet deleted"}


# ---------- lifecycle ----------


@router.post("/{sheet_id}/release", response_model=ProcessSheetResponse)
def release_process_sheet(
    sheet_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(RELEASE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Release a draft sheet (requires at least one step; stamps effective_date)."""
    return process_sheet_service.release_sheet(db, company_id, sheet_id, current_user, audit)


@router.post("/{sheet_id}/obsolete", response_model=ProcessSheetResponse)
def obsolete_process_sheet(
    sheet_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(RELEASE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Obsolete a released sheet (stamps obsolete_date; existing WO snapshots are unaffected)."""
    return process_sheet_service.obsolete_sheet(db, company_id, sheet_id, current_user, audit)


@router.post("/{sheet_id}/new-revision", response_model=ProcessSheetResponse)
def new_process_sheet_revision(
    sheet_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(AUTHOR_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Copy a released/obsolete sheet and its steps to a new DRAFT row with the next revision letter."""
    return process_sheet_service.new_revision(db, company_id, sheet_id, current_user, audit)


# ---------- step CRUD (draft sheets only) ----------


@router.post("/{sheet_id}/steps", response_model=ProcessSheetStepResponse)
def add_process_sheet_step(
    sheet_id: int,
    data: ProcessSheetStepCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(AUTHOR_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Add a typed step to a DRAFT sheet (per-type config validation in the service)."""
    return process_sheet_service.add_step(db, company_id, sheet_id, data, current_user, audit)


@router.patch("/{sheet_id}/steps/{step_id}", response_model=ProcessSheetStepResponse)
def update_process_sheet_step(
    sheet_id: int,
    step_id: int,
    data: ProcessSheetStepUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(AUTHOR_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Update a step on a DRAFT sheet. The merged (effective) definition is re-validated."""
    return process_sheet_service.update_step(db, company_id, sheet_id, step_id, data, current_user, audit)


@router.delete("/{sheet_id}/steps/{step_id}")
def delete_process_sheet_step(
    sheet_id: int,
    step_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(AUTHOR_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Delete a step from a DRAFT sheet (hard delete — steps only exist on drafts)."""
    process_sheet_service.delete_step(db, company_id, sheet_id, step_id, current_user, audit)
    return {"message": "Process sheet step deleted"}
