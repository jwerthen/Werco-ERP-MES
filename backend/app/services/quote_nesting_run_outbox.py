"""Durable QUEUED run rows are the outbox; only committed identifiers reach Redis."""

import asyncio
import logging

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.queue import enqueue_job_best_effort, enqueue_job_fire_and_forget_fastfail

logger = logging.getLogger(__name__)
_PENDING = "pending_quote_nesting_run_ids"
_tasks: set[asyncio.Task] = set()


def mark_run_pending(db: Session, company_id: int, run_id: int) -> None:
    db.info.setdefault(_PENDING, set()).add((company_id, run_id))


@event.listens_for(Session, "after_commit")
def _committed(db: Session) -> None:
    if db.in_nested_transaction():
        return
    pending = db.info.pop(_PENDING, set())
    for company_id, run_id in pending:
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is None:
                enqueue_job_best_effort(
                    "run_quote_nesting_job",
                    company_id=company_id,
                    run_id=run_id,
                    _job_id=f"quote-nesting:{company_id}:{run_id}",
                    fast_fail=True,
                )
            else:
                task = loop.create_task(
                    enqueue_job_fire_and_forget_fastfail(
                        "run_quote_nesting_job",
                        company_id=company_id,
                        run_id=run_id,
                        _job_id=f"quote-nesting:{company_id}:{run_id}",
                    )
                )
                _tasks.add(task)
                task.add_done_callback(_tasks.discard)
        except Exception:
            logger.warning("Nesting run dispatch unavailable; committed row remains queued for relay")


@event.listens_for(Session, "after_rollback")
def _rolled_back(db: Session) -> None:
    db.info.pop(_PENDING, None)


@event.listens_for(Session, "after_soft_rollback")
def _soft_rollback(db: Session, previous_transaction) -> None:
    db.info.pop(_PENDING, None)
