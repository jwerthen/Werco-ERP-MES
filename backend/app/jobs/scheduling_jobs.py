import logging
from typing import List, Optional

from app.db.session import SessionLocal
from app.models.company import Company
from app.services.notification_dispatch import dispatch_direct
from app.services.notification_service import get_notification_recipients
from app.services.scheduling_service import SchedulingService

logger = logging.getLogger(__name__)


async def run_scheduling_task(
    work_center_ids: list = None,
    horizon_days: int = 90,
    optimize_setup: bool = False,
    company_id: Optional[int] = None,
):
    """
    Background job to run constraint-based scheduling.

    Tenant isolation: scheduling is per-company. When ``company_id`` is provided
    the run is confined to that tenant; otherwise the job fans out over every
    active company and runs one isolated scheduling pass per tenant (so a
    background run never schedules or overwrites another tenant's rows).

    Args:
        work_center_ids: List of work center IDs to schedule (None = all in tenant)
        horizon_days: Scheduling horizon in days
        optimize_setup: Optimize for setup time reduction
        company_id: Run for a single tenant; ``None`` runs every active tenant
    """
    db = SessionLocal()
    try:
        if company_id is not None:
            company_ids: List[int] = [company_id]
        else:
            company_ids = [row_id for (row_id,) in db.query(Company.id).filter(Company.is_active == True).all()]

        runs = []
        for cid in company_ids:
            try:
                runs.append(
                    await _run_scheduling_for_company(
                        db=db,
                        company_id=cid,
                        work_center_ids=work_center_ids,
                        horizon_days=horizon_days,
                        optimize_setup=optimize_setup,
                    )
                )
            except Exception as e:
                logger.error(f"Scheduling job failed for company {cid}: {e}")
                # Continue with remaining tenants; one tenant's failure must not
                # abort scheduling for the others.

        if company_id is not None:
            return runs[0] if runs else {"company_id": company_id, "status": "error"}
        return {"companies_processed": len(runs), "runs": runs}

    finally:
        db.close()


async def _run_scheduling_for_company(
    db,
    company_id: int,
    work_center_ids: list,
    horizon_days: int,
    optimize_setup: bool,
) -> dict:
    """Run a single tenant-scoped scheduling pass and notify that tenant's managers."""
    scheduling_service = SchedulingService(db, company_id)

    logger.info(f"Starting scheduling job for company {company_id}: horizon={horizon_days} days")

    # Run scheduling algorithm
    results = scheduling_service.run_scheduling(
        work_center_ids=work_center_ids, horizon_days=horizon_days, optimize_setup=optimize_setup
    )

    logger.info(
        f"Scheduling complete for company {company_id}: {results['scheduled_count']} operations scheduled, "
        f"{results['conflict_count']} conflicts"
    )

    # Check for capacity conflicts
    conflicts = scheduling_service.detect_conflicts()

    if conflicts:
        logger.warning(f"Detected {len(conflicts)} capacity conflicts for company {company_id}")

        # Notify production managers via the notification dispatcher.
        managers = get_notification_recipients(db, role="manager", company_id=company_id)

        if managers:
            await dispatch_direct(
                db,
                event_key="capacity.overload",
                company_id=company_id,
                recipients=managers,
                related_type="WorkCenter",
                related_id=None,
                title=f"Production Scheduling: {len(conflicts)} Capacity Conflicts Detected",
                body=f"{len(conflicts)} capacity conflict(s) were detected in the latest scheduling run.",
                link="/scheduling",
                template="scheduling_conflicts",
                context={
                    "conflict_count": len(conflicts),
                    "scheduled_count": results["scheduled_count"],
                    "link_path": "/scheduling",
                },
            )

    return {
        "company_id": company_id,
        "scheduled_count": results["scheduled_count"],
        "conflict_count": results["conflict_count"],
        "capacity_conflicts": len(conflicts),
        "conflicts": conflicts[:10],  # Return first 10 conflicts
    }
