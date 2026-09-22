from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_company_id, get_current_user
from app.db.database import get_db
from app.models.user import User
from app.schemas.hank import HankBriefingResponse
from app.services.hank_briefing_service import HankBriefingService

router = APIRouter()


@router.get('/briefing', response_model=HankBriefingResponse)
def get_hank_briefing(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Read live, role-ordered priorities; each source enforces its effective view permission."""
    return HankBriefingService(db, current_user, company_id).briefing()
