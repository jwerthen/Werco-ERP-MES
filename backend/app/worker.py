"""
ARQ Worker Configuration

Run with: arq app.worker.WorkerSettings
"""
from arq import cron
from arq.connections import RedisSettings
from app.core.queue import get_redis_settings
import logging

logger = logging.getLogger(__name__)


# ============================================================================
# JOB FUNCTIONS (imported from job modules)
# ============================================================================

async def send_email_job(ctx, to: str, subject: str, body: str, template: str = None, context: dict = None):
    """Send email job"""
    from app.jobs.email_jobs import send_email_task
    return await send_email_task(to, subject, body, template, context)


async def send_webhook_job(ctx, webhook_id: int, event: str, payload: dict):
    """Send webhook job"""
    from app.jobs.webhook_jobs import send_webhook_task
    return await send_webhook_task(webhook_id, event, payload)


async def run_mrp_job(ctx, mode: str = "REVIEW"):
    """Run MRP calculation job"""
    from app.jobs.mrp_jobs import run_mrp_task
    return await run_mrp_task(mode)


async def generate_report_job(ctx, report_type: str, filters: dict = None):
    """Generate report job"""
    from app.jobs.report_jobs import generate_report_task
    return await generate_report_task(report_type, filters)


async def run_scheduling_job(ctx):
    """Run constraint-based scheduling job"""
    from app.jobs.scheduling_jobs import run_scheduling_task
    return await run_scheduling_task()


async def send_daily_digest_job(ctx):
    """Send daily digest emails job"""
    from app.jobs.email_jobs import send_daily_digest_task
    return await send_daily_digest_task()


async def check_calibrations_job(ctx):
    """Check calibration due dates job"""
    from app.jobs.notification_jobs import check_calibrations_task
    return await check_calibrations_task()


async def cleanup_old_logs_job(ctx):
    """Cleanup old logs job"""
    from app.jobs.maintenance_jobs import cleanup_old_logs_task
    return await cleanup_old_logs_task()


async def check_late_work_orders_job(ctx):
    """Check for late work orders job"""
    from app.jobs.notification_jobs import check_late_work_orders_task
    return await check_late_work_orders_task()


async def check_low_stock_job(ctx):
    """Check for low stock items job"""
    from app.jobs.notification_jobs import check_low_stock_task
    return await check_low_stock_task()


async def check_quote_expiring_job(ctx):
    """Check for expiring quotes job"""
    from app.jobs.notification_jobs import check_quote_expiring_task
    return await check_quote_expiring_task()


# ============================================================================
# STARTUP/SHUTDOWN
# ============================================================================

async def startup(ctx):
    """Worker startup - initialize connections"""
    logger.info("ARQ worker starting up...")
    # Database connection will be created per-job
    logger.info("ARQ worker ready")


async def shutdown(ctx):
    """Worker shutdown - cleanup"""
    logger.info("ARQ worker shutting down...")


# ============================================================================
# WORKER SETTINGS
# ============================================================================

class WorkerSettings:
    """ARQ Worker configuration"""

    # Redis connection
    redis_settings = get_redis_settings()

    # Job functions
    functions = [
        send_email_job,
        send_webhook_job,
        run_mrp_job,
        generate_report_job,
        run_scheduling_job,
        send_daily_digest_job,
        check_calibrations_job,
        cleanup_old_logs_job,
        check_late_work_orders_job,
        check_low_stock_job,
        check_quote_expiring_job,
    ]

    # Cron jobs (scheduled tasks)
    cron_jobs = [
        cron(run_mrp_job, hour=6, minute=0, kwargs={"mode": "AUTO_DRAFT"}),  # 6 AM daily
        cron(send_daily_digest_job, hour=8, minute=0),  # 8 AM daily
        cron(check_calibrations_job, hour=7, minute=0),  # 7 AM daily
        cron(check_late_work_orders_job, hour=8, minute=0),  # 8 AM daily
        cron(check_low_stock_job, hour=7, minute=30),  # 7:30 AM daily
        cron(check_quote_expiring_job, hour=9, minute=0),  # 9 AM daily
        cron(cleanup_old_logs_job, weekday=0, hour=2, minute=0),  # Sunday 2 AM
    ]

    # Lifecycle
    on_startup = startup
    on_shutdown = shutdown

    # Worker settings
    max_jobs = 10
    job_timeout = 600  # 10 minutes default
    keep_result = 3600  # Keep results for 1 hour

    # Queue settings
    queue_name = "arq:queue"
