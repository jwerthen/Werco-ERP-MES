"""Read or explicitly save your own dashboard layout (which widgets show on ``/``, in what order)."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.audit_guard import required_audit
from app.api.deps import get_audit_service, get_current_company_id, get_current_user
from app.db.database import get_db
from app.models.user import User
from app.schemas.dashboard_layout import DashboardLayoutCommand, DashboardLayoutResponse, DashboardLayoutSave
from app.services.audit_service import AuditService
from app.services.dashboard_layout_service import STALE_VERSION_DETAIL, DashboardLayoutService

router = APIRouter()

AUDIT_FAILED_DETAIL = (
    'Your dashboard layout could not be saved because its audit record failed; nothing was changed. Retry.'
)


def _write(db: Session, service: DashboardLayoutService, command, audit: AuditService, *, reset: bool = False):
    try:
        with required_audit(db, AUDIT_FAILED_DETAIL):
            response = service.save(command, audit, reset=reset)
            db.commit()
        return response
    except IntegrityError as exc:
        # Two first saves raced past the version check; the unique (company, user) key refused one.
        db.rollback()
        raise HTTPException(409, STALE_VERSION_DETAIL) from exc
    except Exception:
        db.rollback()
        raise


@router.get('/layout', response_model=DashboardLayoutResponse)
def get_my_dashboard_layout(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Return your saved dashboard layout for the active company, or null for the stock default.

    Never creates a row. The response says whether this session may edit the layout: an API
    token, a read-only company view or an inactive company can read it but not save it.
    """
    return DashboardLayoutService(db, user, company_id).get()


@router.put('/layout', response_model=DashboardLayoutResponse)
def save_my_dashboard_layout(
    payload: DashboardLayoutSave,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Replace your saved dashboard layout after a current-version check; presentation only.

    An empty widget list saves an empty dashboard; use the reset verb to return to the stock
    default. Only an interactive person in a writable company may save (403 otherwise); a wrong
    company or a stale version is 409. An identical layout is a no-op with no version bump.
    """
    return _write(db, DashboardLayoutService(db, user, company_id), payload, audit)


@router.post('/layout/reset', response_model=DashboardLayoutResponse)
def reset_my_dashboard_layout(
    payload: DashboardLayoutCommand,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Return your saved dashboard layout to the stock default, keeping its audit history.

    A no-op when nothing is saved or it is already the default. Same credential, company and
    version rules as saving.
    """
    return _write(db, DashboardLayoutService(db, user, company_id), payload, audit, reset=True)
