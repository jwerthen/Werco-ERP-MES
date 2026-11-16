"""Nightly OEE auto-calculation job (Lean Phase 1 / issue #88).

Fans out over every active company and computes the PREVIOUS PRODUCTION DAY's
whole-day OEERecord per active work center via the shared ``services/oee_service``
math (the exact code behind ``POST /oee/calculate/{work_center_id}``), stamped
``calculation_source='auto'``.

Target day (migration 120, P1.2): ``current_production_day(calendar) - 1`` per company,
on that company's production calendar (the company-local calendar day when none is
configured) -- never the worker container's ``date.today()``. The schedule is unchanged
(02:30 container time = UTC on Railway = the previous Central evening), so the record
for a production day is written the evening after it closes: with a 05:00 day start,
Monday's production day ends Tuesday 05:00 and is computed at Tuesday ~20:30 Central.
The cron writes whole-day records only (``shift_id`` NULL); per-shift records come from
``POST /oee/calculate/{work_center_id}?shift_id=``.

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
from typing import Dict, Optional

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
from app.services.production_calendar import current_production_day
from app.services.production_calendar_service import load_production_calendar

logger = logging.getLogger(__name__)


async def run_oee_auto_calc_task(company_id: Optional[int] = None, record_date: Optional[date] = None) -> dict:
    """Compute each company's previous production day of OEE, per active work center.

    ``company_id`` confines the run to one tenant (``None`` = every active
    company, one isolated pass per tenant); ``record_date`` overrides the
    default (each company's ``current_production_day - 1``) for re-runs or
    backfilling a specific production day.
    """
    db = SessionLocal()
    try:
        if company_id is not None:
            company_ids = [company_id]
        else:
            company_ids = [
                row_id for (row_id,) in db.query(Company.id).filter(Company.is_active == True).all()
            ]  # noqa: E712

        totals = {"companies": 0, "computed": 0, "skipped_manual": 0, "skipped_idle": 0, "errors": 0}
        dates_by_company: Dict[int, str] = {}
        for cid in company_ids:
            try:
                calendar = load_production_calendar(db, cid)
                target_date = record_date or (current_production_day(calendar) - timedelta(days=1))
                dates_by_company[cid] = target_date.isoformat()
                result = _run_for_company(db, cid, target_date, calendar)
                totals["companies"] += 1
                for key in ("computed", "skipped_manual", "skipped_idle", "errors"):
                    totals[key] += result[key]
            except Exception:
                # One tenant's failure must not abort the others' nightly OEE.
                logger.exception("OEE auto-calc failed for company %s", cid)
                db.rollback()
                totals["errors"] += 1

        distinct_dates = set(dates_by_company.values())
        summary_date = (
            record_date.isoformat() if record_date else (distinct_dates.pop() if len(distinct_dates) == 1 else None)
        )
        logger.info("OEE auto-calc for %s: %s", summary_date or dates_by_company, totals)
        return {"record_date": summary_date, "record_dates_by_company": dates_by_company, **totals}
    finally:
        db.close()


def _run_for_company(db, company_id: int, target_date: date, calendar=None) -> dict:
    """One tenant-scoped OEE pass: every active WC, one production day, source='auto'."""
    calendar = calendar or load_production_calendar(db, company_id)
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

            if not work_center_has_activity(db, company_id, wc.id, target_date, calendar):
                result["skipped_idle"] += 1
                continue

            compute_oee_for_work_center(
                db,
                company_id,
                wc,
                target_date,
                None,  # shift: the cron writes the whole-production-day (no-shift) record
                calculation_source=CalculationSource.AUTO,
                created_by_user_id=None,
                audit=audit,
                calendar=calendar,
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
