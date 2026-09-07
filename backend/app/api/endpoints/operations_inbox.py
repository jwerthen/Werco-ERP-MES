from fastapi import APIRouter, Depends, Path
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_company_id, get_current_user
from app.db.database import get_db
from app.models.user import User
from app.schemas.operations_inbox import (
    OperationalInboxItem,
    OperationalInboxResponse,
    OperationalInboxUpdate,
    SourceKind,
)
from app.services.audit_service import AuditService
from app.services.operations_inbox_service import OperationalInboxService

router = APIRouter()


@router.get('/', response_model=OperationalInboxResponse)
def get_operational_inbox(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    return OperationalInboxService(db, current_user, company_id).list()


@router.patch('/{source_kind}/{source_id}', response_model=OperationalInboxItem)
def update_operational_inbox(
    source_kind: SourceKind,
    data: OperationalInboxUpdate,
    source_id: int = Path(gt=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    try:
        result = OperationalInboxService(db, current_user, company_id).update(source_kind, source_id, data, audit)
        db.commit()
        return result
    except IntegrityError as exc:
        db.rollback()
        from fastapi import HTTPException

        raise HTTPException(409, 'Issue changed. Refresh before updating its action.') from exc
    except Exception:
        db.rollback()
        raise
