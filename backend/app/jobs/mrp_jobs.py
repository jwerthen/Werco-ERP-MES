import logging
from typing import List, Optional

from app.db.session import SessionLocal
from app.models.company import Company
from app.services.mrp_auto_service import MRPAutoMode, MRPAutoService
from app.services.mrp_service import MRPService
from app.services.notification_dispatch import dispatch_direct
from app.services.notification_service import get_notification_recipients

logger = logging.getLogger(__name__)


async def run_mrp_task(
    mode: str = MRPAutoMode.REVIEW,
    planning_horizon_days: int = 90,
    company_id: Optional[int] = None,
):
    """
    Background job to run MRP calculation.

    Tenant isolation: MRP is per-company. When ``company_id`` is provided the run
    is confined to that tenant; otherwise the job fans out over every active
    company and runs one isolated MRP pass per tenant (so a cron run never nets
    one company's demand against another's inventory).

    Args:
        mode: Auto-processing mode (REVIEW, AUTO_DRAFT, AUTO_SUBMIT)
        planning_horizon_days: Planning horizon in days
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
                    await _run_mrp_for_company(
                        db=db, company_id=cid, mode=mode, planning_horizon_days=planning_horizon_days
                    )
                )
            except Exception as e:
                logger.error(f"MRP job failed for company {cid}: {e}")
                # Continue with the remaining tenants; one tenant's failure must
                # not abort planning for the others.

        if company_id is not None:
            return runs[0] if runs else {"company_id": company_id, "status": "error"}
        return {"companies_processed": len(runs), "runs": runs}

    finally:
        db.close()


async def _run_mrp_for_company(db, company_id: int, mode: str, planning_horizon_days: int) -> dict:
    """Run a single tenant-scoped MRP pass and notify that tenant's planners."""
    mrp_service = MRPService(db, company_id)
    auto_service = MRPAutoService(db, company_id)

    logger.info(f"Starting MRP run for company {company_id} with mode={mode}, horizon={planning_horizon_days} days")

    mrp_run = mrp_service.run_mrp(
        user_id=None,  # System user
        planning_horizon_days=planning_horizon_days,
        include_safety_stock=True,
        include_allocated=True,
    )

    logger.info(
        f"MRP run {mrp_run.run_number} completed: "
        f"{mrp_run.total_actions} actions, "
        f"{mrp_run.total_requirements} requirements"
    )

    # Auto-process actions based on mode
    if mode != MRPAutoMode.REVIEW and mrp_run.actions:
        results = auto_service.process_actions(actions=mrp_run.actions, mode=mode, user_id=None)  # System user

        logger.info(
            f"Auto-processed MRP actions: "
            f"{results['pos_created']} POs, "
            f"{results['wos_created']} WOs, "
            f"{results['errors']} errors"
        )

        # Notify planners (scoped to this tenant) via the notification dispatcher.
        planners = get_notification_recipients(db, role="manager", company_id=company_id)

        if planners:
            await dispatch_direct(
                db,
                event_key="mrp.completed",
                company_id=company_id,
                recipients=planners,
                related_type="MRPRun",
                related_id=mrp_run.id,
                title=f"MRP Run {mrp_run.run_number} Completed",
                body=f"MRP run {mrp_run.run_number} completed with {mrp_run.total_actions} action(s).",
                link="/mrp",
                template="mrp_complete",
                context={
                    "run_number": mrp_run.run_number,
                    "total_actions": mrp_run.total_actions,
                    "mode": mode,
                    "link_path": "/mrp",
                },
            )

        return {
            "company_id": company_id,
            "mrp_run_id": mrp_run.id,
            "mrp_run_number": mrp_run.run_number,
            "total_actions": mrp_run.total_actions,
            "auto_processing": results,
        }

    # Review mode - just notify about actions
    if mrp_run.total_actions > 0:
        planners = get_notification_recipients(db, role="manager", company_id=company_id)

        if planners:
            await dispatch_direct(
                db,
                event_key="mrp.review_needed",
                company_id=company_id,
                recipients=planners,
                related_type="MRPRun",
                related_id=mrp_run.id,
                title=f"MRP Run {mrp_run.run_number}: {mrp_run.total_actions} Actions Need Review",
                body=f"MRP run {mrp_run.run_number} produced {mrp_run.total_actions} action(s) needing review.",
                link="/mrp",
                template="mrp_review_needed",
                context={
                    "run_number": mrp_run.run_number,
                    "total_actions": mrp_run.total_actions,
                    "link_path": "/mrp",
                },
            )

    return {
        "company_id": company_id,
        "mrp_run_id": mrp_run.id,
        "mrp_run_number": mrp_run.run_number,
        "total_actions": mrp_run.total_actions,
        "mode": "REVIEW",
    }
