from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List
from app.api.deps import get_db, get_current_user
from app.models.user import User
from app.models.job import Job, JobStatus
from app.schemas.job import JobResponse, JobStatsResponse
from app.core.queue import get_redis_pool
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/status", response_model=JobStatsResponse)
async def get_job_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get overall job queue status"""

    # Get counts by status
    stats = db.query(
        func.count(Job.id).label("total"),
        func.sum(func.cast(Job.status == JobStatus.PENDING, Integer)).label("pending"),
        func.sum(func.cast(Job.status == JobStatus.IN_PROGRESS, Integer)).label("in_progress"),
        func.sum(func.cast(Job.status == JobStatus.COMPLETED, Integer)).label("completed"),
        func.sum(func.cast(Job.status == JobStatus.FAILED, Integer)).label("failed"),
    ).filter(
        Job.enqueued_at >= datetime.utcnow() - timedelta(days=1)  # Last 24 hours
    ).first()

    # Get queue depth from Redis
    try:
        pool = await get_redis_pool()
        queue_depth = await pool.llen("arq:queue")
        worker_status = "healthy"
    except Exception as e:
        logger.error(f"Failed to connect to Redis: {e}")
        queue_depth = 0
        worker_status = "unknown"

    return JobStatsResponse(
        total_jobs=stats.total or 0,
        pending=stats.pending or 0,
        in_progress=stats.in_progress or 0,
        completed=stats.completed or 0,
        failed=stats.failed or 0,
        queue_depth=queue_depth,
        worker_status=worker_status
    )


@router.get("/failed", response_model=List[JobResponse])
def get_failed_jobs(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get failed jobs for debugging"""

    jobs = db.query(Job).filter(
        Job.status == JobStatus.FAILED
    ).order_by(
        Job.completed_at.desc()
    ).offset(skip).limit(limit).all()

    return jobs


@router.get("/{job_id}", response_model=JobResponse)
def get_job(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get job by ID"""

    job = db.query(Job).filter(Job.job_id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return job


@router.post("/{job_id}/retry")
async def retry_job(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Retry a failed job"""

    job = db.query(Job).filter(Job.job_id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status != JobStatus.FAILED:
        raise HTTPException(status_code=400, detail="Only failed jobs can be retried")

    # Re-enqueue the job
    from app.core.queue import enqueue_job

    try:
        new_job = await enqueue_job(
            job.job_type,
            **(job.args or {})
        )

        # Update old job status
        job.status = JobStatus.RETRYING
        db.commit()

        return {"message": "Job retried successfully", "new_job_id": new_job.job_id}

    except Exception as e:
        logger.error(f"Failed to retry job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to retry job: {str(e)}")


@router.get("", response_model=List[JobResponse])
def list_jobs(
    status: JobStatus = None,
    job_type: str = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List jobs with optional filtering"""

    query = db.query(Job)

    if status:
        query = query.filter(Job.status == status)

    if job_type:
        query = query.filter(Job.job_type == job_type)

    jobs = query.order_by(
        Job.enqueued_at.desc()
    ).offset(skip).limit(limit).all()

    return jobs
