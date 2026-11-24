"""Admin > System Health: a read-only view of the background worker and the notification backlog."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_company_id, require_role
from app.db.database import get_db
from app.models.user import User, UserRole
from app.schemas.system_health import SystemHealthResponse
from app.services.api_token_service import is_platform_principal
from app.services.system_health_service import build_system_health, read_worker_state

router = APIRouter()


@router.get("/system-health", response_model=SystemHealthResponse)
def get_system_health(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN])),
    company_id: int = Depends(get_current_company_id),
):
    """Show whether the background worker is alive, every scheduled (cron) job with whether it is
    armed, its schedule in shop time, its next run and how its last run went, plus the active
    company's notification backlog: events still waiting to be notified, queued digest items and
    failed email deliveries in the last 7 days.

    Admin only. A pure read: writes nothing and switches nothing on. When Redis cannot be read the
    worker status is "unknown" and every job's armed state is null -- never an error.
    Scheduled jobs are switched on by the worker's WORKER_CRON_JOBS setting, not here.
    A job's last-run outcome and timing are install-wide; its counts (what the job returned, summed
    over every company) are shown to a platform administrator only and are empty for a tenant Admin.
    """
    return build_system_health(
        db, company_id, read_worker_state(), include_run_counts=is_platform_principal(current_user)
    )
