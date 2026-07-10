"""Nightly OEE auto-calculation job (Lean Phase 1 / issue #88).

Fans out over every active company and computes YESTERDAY's OEERecord per
active work center via the shared ``services/oee_service`` math (the exact code
behind ``POST /oee/calculate/{work_center_id}``), stamped
``calculation_source='auto'``.

Skip policy:
  * A ``calculation_source='manual'`` record for the same (WC, date) is
    authoritative -- the cron never overwrites it (ANY shift: a hand-entered
    per-shift record means a human owns that WC/day, and an auto shift=None
    whole-day row alongside it would double-count the day).
  * ``'auto'`` records ARE recomputed by re-runs (idempotent refresh).
  * Idle work centers (no closed clocked entry and no unplanned downtime that
    day) are skipped -- no staffed time is genuinely uncomputable (OEE-4), not
    an all-zero measurement.

Audit context: like the MRP/outcome-capture crons, there is no request/user, so
writes are audited through ``AuditService(db, user=None, company_id=cid)`` --
the tenant tag is explicit, the actor is the system.
"""

import logging
from datetime import date, timedelta
from typing import Optional

from app.db.session import SessionLocal
from app.models.company import Company
from app.models.oee import CalculationSource, OEERecord
from app.models.work_center import WorkCenter
from app.services.audit_service import AuditService
from app.services.oee_service import (
    OEERecordConflictError,
    compute_oee_for_work_center,
    work_center_has_activity,
)

logger = logging.getLogger(__name__)


async def run_oee_auto_calc_task(company_id: Optional[int] = None, record_date: Optional[date] = None) -> dict:
    """Compute yesterday's OEE per active company + active work center.

    ``company_id`` confines the run to one tenant (``None`` = every active
    company, one isolated pass per tenant); ``record_date`` overrides the
    default "yesterday" for re-runs/backfilling a specific day.
    """
    target_date = record_date or (date.today() - timedelta(days=1))
    db = SessionLocal()
    try:
        if company_id is not None:
            company_ids = [company_id]
        else:
            company_ids = [
                row_id for (row_id,) in db.query(Company.id).filter(Company.is_active == True).all()
            ]  # noqa: E712

        totals = {"companies": 0, "computed": 0, "skipped_manual": 0, "skipped_idle": 0, "errors": 0}
        for cid in company_ids:
            try:
                result = _run_for_company(db, cid, target_date)
                totals["companies"] += 1
                for key in ("computed", "skipped_manual", "skipped_idle", "errors"):
                    totals[key] += result[key]
            except Exception:
                # One tenant's failure must not abort the others' nightly OEE.
                logger.exception("OEE auto-calc failed for company %s", cid)
                db.rollback()
                totals["errors"] += 1

        logger.info("OEE auto-calc for %s: %s", target_date, totals)
        return {"record_date": target_date.isoformat(), **totals}
    finally:
        db.close()


def _run_for_company(db, company_id: int, target_date: date) -> dict:
    """One tenant-scoped OEE pass: every active WC, yesterday, source='auto'."""
    audit = AuditService(db, user=None, company_id=company_id)
    work_centers = (
        db.query(WorkCenter)
        .filter(WorkCenter.company_id == company_id, WorkCenter.is_active == True)  # noqa: E712
        .all()
    )

    result = {"computed": 0, "skipped_manual": 0, "skipped_idle": 0, "errors": 0}
    for wc in work_centers:
        try:
            # A manual record for this WC/day (any shift) is authoritative -- skip.
            manual_exists = (
                db.query(OEERecord.id)
                .filter(
                    OEERecord.company_id == company_id,
                    OEERecord.work_center_id == wc.id,
                    OEERecord.record_date == target_date,
                    OEERecord.calculation_source == CalculationSource.MANUAL.value,
                )
                .first()
            )
            if manual_exists:
                result["skipped_manual"] += 1
                continue

            if not work_center_has_activity(db, company_id, wc.id, target_date):
                result["skipped_idle"] += 1
                continue

            compute_oee_for_work_center(
                db,
                company_id,
                wc,
                target_date,
                None,  # shift: the cron writes the whole-day (no-shift) record
                calculation_source=CalculationSource.AUTO,
                created_by_user_id=None,
                audit=audit,
            )
            result["computed"] += 1
        except OEERecordConflictError:
            # A concurrent writer created the record between lookup and insert;
            # their row stands -- this is a benign skip, not an error.
            logger.info("OEE auto-calc skipped WC %s (%s): record already exists", wc.id, target_date)
            result["skipped_manual"] += 1
        except Exception:
            logger.exception("OEE auto-calc failed for WC %s (company %s, %s)", wc.id, company_id, target_date)
            db.rollback()
            result["errors"] += 1

    return result
