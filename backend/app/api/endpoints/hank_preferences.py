"""Read or explicitly save your own Hank presentation and follow-up choices."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_company_id, get_current_user
from app.db.database import get_db
from app.models.user import User
from app.schemas.hank_preferences import HankPreferenceCommand, HankPreferenceResponse, HankPreferenceSave
from app.services.audit_service import AuditService, AuditWriteError
from app.services.hank_preference_service import HankPreferenceService

router = APIRouter()


def _save(db, service, command, audit, *, reset=False):
    try:
        response = service.save(command, audit, reset=reset)
        db.commit()
        return response
    except AuditWriteError as exc:
        db.rollback()
        raise HTTPException(503, 'Unable to save the required audit record. Preferences were not changed.') from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, 'Your Hank preferences changed. Refresh before saving.') from exc
    except Exception:
        db.rollback()
        raise


@router.get('/preferences', response_model=HankPreferenceResponse)
def get_preferences(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Return personal defaults without creating a row, or your saved company-specific choices."""
    return HankPreferenceService(db, user, company_id).get()


@router.put('/preferences', response_model=HankPreferenceResponse)
def save_preferences(
    payload: HankPreferenceSave,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Replace your typed choices after a current-version check; never change company policy."""
    return _save(db, HankPreferenceService(db, user, company_id), payload, audit)


@router.post('/preferences/reset', response_model=HankPreferenceResponse)
def reset_preferences(
    payload: HankPreferenceCommand,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Restore defaults with required evidence while retaining prior saved-change audit history."""
    return _save(db, HankPreferenceService(db, user, company_id), payload, audit, reset=True)
