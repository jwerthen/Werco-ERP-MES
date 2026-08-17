"""
ARQ Worker Configuration

Run with: arq app.worker.WorkerSettings

OBSERVABILITY CONTRACT (see docs/WORKER_SERVICE.md)
--------------------------------------------------
A background worker fails in silence by construction: nobody is watching at 02:30, and a
worker that "started successfully" while pointed at the wrong Redis looks identical to a
healthy one. Three things counter that, all wired below:

1. **Fail fast at import.** ``assert_redis_configured`` raises before arq builds anything
   if this process would fall back to ``localhost:6379`` in production/staging.
2. **Say what you connected to.** The startup log prints the resolved Redis target (host /
   port / db / TLS / whether a password is in play -- never the password itself), the queue
   name, the release, and every registered cron with its next run time.
3. **Report crashes.** Sentry is initialized here, tagged ``component=worker``. It was not,
   before: the worker never imports ``app.main``, where the only ``sentry_sdk.init`` lived.

CRON EXPOSURE
-------------
Every cron in ``WorkerSettings.cron_jobs`` fires the moment a worker process runs -- there
is no separate enable step. Several of them WRITE (``run_mrp_auto_draft_job`` creates draft
POs and work orders; ``check_late_work_orders_job`` emails one message per late WO to every
supervisor and manager, with no age cap). ``WORKER_CRON_JOBS`` exists so a first boot can
drain the enqueue-driven queue with no scheduled work at all; it defaults to "everything",
i.e. exactly today's behavior. To switch ONE cron off without freezing the rest into an
allowlist, exclude it: ``WORKER_CRON_JOBS=all,-run_mrp_auto_draft_job``. See
``select_cron_jobs`` for every accepted shape.
"""

import logging
import os
from datetime import datetime
from typing import Dict, List, Sequence, Set

from arq import cron
from arq.cron import CronJob

from app.core.config import settings
from app.core.observability import init_sentry
from app.core.queue import RedisProfile, assert_redis_configured, get_redis_settings, redis_config_warnings

# Import for side effects: attaches the transactional-outbox SQLAlchemy Session listeners
# in the worker process so operational events committed here (e.g. cron writes) also tee
# into the notification pipeline.
from app.services import notification_outbox  # noqa: F401

logger = logging.getLogger(__name__)

# Resolved once, at import, BEFORE arq constructs a Worker or opens a connection. In
# production/staging a Redis that resolved to the localhost default raises here, so the
# container dies loudly instead of idling against an empty queue while the API enqueues
# somewhere else. Outside those environments it is only described, so dev and pytest are
# unaffected. See app/core/queue.py for the root cause this guards.
REDIS_TARGET = assert_redis_configured("arq worker")


# ============================================================================
# JOB FUNCTIONS (imported from job modules)
# ============================================================================


async def send_email_job(ctx, to: str, subject: str, body: str, template: str = None, context: dict = None):
    """Send email job"""
    from app.jobs.email_jobs import send_email_task

    return await send_email_task(to, subject, body, template, context)


async def send_sms_job(
    ctx,
    *,
    company_id: int,
    user_id: int,
    body: str,
    notification_log_id: int = None,
    event_type: str = "sms",
):
    """Send one notification SMS via Twilio (gated by ``Company.allow_sms_egress``).

    Enqueued by the notification dispatcher's SMS leg. Takes ``user_id`` rather than a
    phone number so PII stays out of the Redis payload and the recipient is re-resolved
    tenant-scoped + active at send time. Re-raises on transport failure so ARQ retries.
    """
    from app.jobs.sms_jobs import send_sms_task

    return await send_sms_task(
        company_id=company_id,
        user_id=user_id,
        body=body,
        notification_log_id=notification_log_id,
        event_type=event_type,
    )


async def send_sms_overflow_job(ctx, *, company_id: int, user_id: int):
    """Storm-control collapse: one "…and N more" SMS standing in for suppressed alerts.

    Enqueued deferred (``SMS_COLLAPSE_DELAY_SECONDS``) by the dispatcher when a user's
    per-hour SMS cap is first exceeded.
    """
    from app.jobs.sms_jobs import send_sms_overflow_task

    return await send_sms_overflow_task(company_id=company_id, user_id=user_id)


async def send_webhook_job(ctx, webhook_id: int, event: str, payload: dict, company_id: int = None):
    """Send webhook job"""
    from app.jobs.webhook_jobs import send_webhook_task

    return await send_webhook_task(webhook_id, event, payload, company_id=company_id)


async def run_mrp_job(ctx, mode: str = "REVIEW", company_id: int = None):
    """Run MRP calculation job.

    ``company_id`` confines the run to one tenant; ``None`` (the cron default)
    fans out over every active company, one isolated MRP pass per tenant.
    """
    from app.jobs.mrp_jobs import run_mrp_task

    return await run_mrp_task(mode, company_id=company_id)


async def run_mrp_auto_draft_job(ctx):
    """Cron entrypoint for the daily MRP AUTO_DRAFT pass.

    ARQ fires cron coroutines with only ``ctx``, so the schedule can't pass
    ``mode`` through ``cron()``. This thin wrapper pins ``mode="AUTO_DRAFT"``
    (the request default is "REVIEW") and fans out over every active company.
    """
    return await run_mrp_job(ctx, mode="AUTO_DRAFT")


async def generate_report_job(ctx, report_type: str, filters: dict = None):
    """Generate report job"""
    from app.jobs.report_jobs import generate_report_task

    return await generate_report_task(report_type, filters)


async def run_scheduling_job(
    ctx, work_center_ids: list = None, horizon_days: int = 90, optimize_setup: bool = False, company_id: int = None
):
    """Run constraint-based scheduling job.

    ``company_id`` confines the run to one tenant (the API ``/run-background``
    entry point passes the caller's company); ``None`` fans out over every
    active company, one isolated scheduling pass per tenant.
    """
    from app.jobs.scheduling_jobs import run_scheduling_task

    return await run_scheduling_task(
        work_center_ids=work_center_ids,
        horizon_days=horizon_days,
        optimize_setup=optimize_setup,
        company_id=company_id,
    )


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


async def archive_aged_audit_logs_job(ctx):
    """Export audit rows past their retention window to cold storage (CMMC AU-3.3.8)."""
    from app.jobs.maintenance_jobs import archive_aged_audit_logs_task

    return await archive_aged_audit_logs_task()


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


async def aggregate_ai_learning_job(ctx):
    """Aggregate AI feedback into improvement recommendations"""
    from app.jobs.ai_learning_jobs import aggregate_ai_learning_task

    return await aggregate_ai_learning_task()


async def dispatch_work_order_completion_signals_job(ctx, work_order_id: int, company_id: int, status: str):
    """Send outbound completion signals (notification + webhook) for a finished WO.

    Batch 5 / rank 8 (EVT-3): enqueued from the completion request handlers so the
    email/webhook path runs off the request thread, tenant-scoped to ``company_id``.
    """
    from app.jobs.completion_signal_jobs import dispatch_work_order_completion_signals_task

    return await dispatch_work_order_completion_signals_task(work_order_id, company_id, status)


async def process_tracking_webhook_job(
    ctx, *, company_id: int, shipment_id: int, provider: str = None, events: list = None
):
    """Apply inbound carrier-webhook tracking events to a shipment.

    Enqueued by the inbound carrier-webhook endpoint with the tenant ALREADY
    resolved from stored shipment data (``aggregator_shipment_id``); this wrapper
    forwards the kwargs to the task, which persists the events tenant-scoped.
    """
    from app.jobs.shipping_jobs import process_tracking_webhook_task

    return await process_tracking_webhook_task(
        company_id=company_id, shipment_id=shipment_id, provider=provider, events=events
    )


async def print_receiving_label_job(ctx, *, company_id: int, receipt_id: int, user_id: int):
    """Auto-print the 4x6 receiving label for a committed PO receipt.

    Enqueued best-effort from ``receive_material`` with the tenant passed explicitly
    as ``company_id``. The task is the SOLE decider of whether to print (gated on the
    per-company ``auto_print_on_receipt`` + ``allow_print_egress`` toggles) and is
    best-effort -- it never raises out of the worker.
    """
    from app.jobs.label_jobs import print_receiving_label_task

    return await print_receiving_label_task(company_id=company_id, receipt_id=receipt_id, user_id=user_id)


async def run_oee_auto_calc_job(ctx, company_id: int = None, record_date: str = None):
    """Nightly OEE auto-calculation (Lean Phase 1).

    Computes YESTERDAY's OEERecord per active company + active work center with
    ``calculation_source='auto'``; never overwrites a 'manual' record. ARQ fires
    cron coroutines with only ``ctx``, so the fan-out defaults live in the task.
    ``record_date`` (ISO date string, manual enqueues only) re-runs a specific day.
    """
    from datetime import date as _date

    from app.jobs.oee_jobs import run_oee_auto_calc_task

    parsed_date = _date.fromisoformat(record_date) if record_date else None
    return await run_oee_auto_calc_task(company_id=company_id, record_date=parsed_date)


async def poll_tracking_job(ctx):
    """Cron fallback: refresh tracking for in-flight shipments, fanned out per tenant.

    Only polls tenants whose ``allow_carrier_egress`` kill switch is ON (the call
    to ``provider.get_tracking`` is outbound carrier traffic). Best-effort -- never
    raises out of the cron.
    """
    from app.jobs.shipping_jobs import poll_tracking_task

    return await poll_tracking_task()


async def dispatch_notification_job(ctx, event_id: int):
    """Fan out notifications for one committed OperationalEvent (transactional outbox).

    Enqueued by the after_commit tee (and the relay sweeper). Idempotent + crash-safe via
    the event's ``notified_at`` marker.
    """
    from app.jobs.notification_jobs import dispatch_notification_task

    return await dispatch_notification_task(event_id)


async def relay_pending_notifications_job(ctx):
    """5-min sweeper: re-enqueue catalog-mapped events whose notifications never dispatched
    (covers a Redis outage at after_commit-enqueue time)."""
    from app.jobs.notification_jobs import relay_pending_notifications_task

    return await relay_pending_notifications_task()


async def dispatch_notification_direct_job(
    ctx,
    *,
    event_key: str,
    company_id: int,
    recipient_ids: list,
    related_type: str = None,
    related_id: int = None,
    title: str,
    body: str = None,
    link: str = None,
    template: str = None,
    context: dict = None,
):
    """Run ``dispatch_direct`` in the worker for a sync request-path caller (e.g. visitor
    check-in), which cannot await the async dispatcher itself."""
    from app.jobs.notification_jobs import dispatch_notification_direct_task

    return await dispatch_notification_direct_task(
        event_key=event_key,
        company_id=company_id,
        recipient_ids=recipient_ids,
        related_type=related_type,
        related_id=related_id,
        title=title,
        body=body,
        link=link,
        template=template,
        context=context,
    )


# ============================================================================
# CRON SCHEDULE
# ============================================================================

# Every entry here fires automatically once a worker process is running. The comment on each
# line is the schedule in the CONTAINER's local timezone -- arq defaults to
# `datetime.now().astimezone().tzinfo`, which on Railway is UTC unless TZ is set, so "6 AM"
# means 06:00 UTC (01:00 Central) until somebody sets TZ. See docs/WORKER_SERVICE.md.
ALL_CRON_JOBS: List[CronJob] = [
    cron(run_mrp_auto_draft_job, hour=6, minute=0),  # 6 AM daily (MRP AUTO_DRAFT) -- WRITES draft POs/WOs
    cron(send_daily_digest_job, hour=8, minute=0),  # 8 AM daily -- sends email
    cron(check_calibrations_job, hour=7, minute=0),  # 7 AM daily -- sends email
    cron(check_late_work_orders_job, hour=8, minute=0),  # 8 AM daily -- sends email, no age cap
    cron(check_low_stock_job, hour=7, minute=30),  # 7:30 AM daily -- sends email (aggregated)
    cron(check_quote_expiring_job, hour=9, minute=0),  # 9 AM daily -- sends email
    cron(aggregate_ai_learning_job, hour=5, minute=30),  # 5:30 AM daily -- WRITES recommendations/events
    cron(run_oee_auto_calc_job, hour=2, minute=30),  # 2:30 AM daily (yesterday's OEE, Lean Phase 1)
    cron(cleanup_old_logs_job, weekday=0, hour=2, minute=0),  # Sunday 2 AM -- physical DELETEs (not audit)
    cron(archive_aged_audit_logs_job, day=1, hour=3, minute=0),  # 1st of month, 3 AM -- needs a durable volume
    cron(poll_tracking_job, minute={0, 30}),  # every 30 min (tracking poll fallback) -- carrier egress
    # Notification relay sweeper: every 5 min re-enqueue catalog-mapped events whose
    # after_commit enqueue was lost (e.g. Redis outage). See notification_jobs.
    cron(relay_pending_notifications_job, minute=set(range(0, 60, 5))),
]


def select_cron_jobs(spec: str = None, available: Sequence[CronJob] = None) -> List[CronJob]:
    """Pick which crons this worker registers, from ``WORKER_CRON_JOBS``.

    There are exactly two shapes. Either you name the crons you WANT (an allowlist), or you
    start from everything and subtract the ones you DON'T (a denylist, ``-`` prefix). You
    may not mix them -- see "Hard errors" below for why that is refused rather than guessed.

    ALLOWLIST -- unchanged, byte for byte, from before exclusions existed:
      * unset / empty / ``"all"``   -> every cron in ``ALL_CRON_JOBS``. TODAY'S BEHAVIOR:
        this function is a no-op by default and enables nothing that was previously off.
      * ``"none"``                  -> no crons at all. The worker still drains the
        enqueue-driven queue (notifications, webhooks, labels, completion signals), which is
        the safe shape for a first-ever boot: those jobs correspond to something a user
        actually did, whereas the crons enumerate accumulated state and fire in bulk.
      * ``"check_low_stock_job,poll_tracking_job"`` -> ONLY those two, in the order listed.

    DENYLIST -- a ``-`` prefix EXCLUDES that cron from the full set:
      * ``"all,-run_mrp_auto_draft_job"``  -> every cron EXCEPT the MRP auto-draft pass.
      * ``"-run_mrp_auto_draft_job"``      -> identical. A spec made only of exclusions
        implies ``all`` as its base; there is nothing else it could mean.
      * ``"-cron:run_mrp_auto_draft_job"`` -> identical again. arq names crons
        ``cron:<coroutine name>`` and the startup log prints that form, so BOTH spellings
        are accepted for exclusions exactly as they already are for inclusions -- whichever
        one the operator copied has to work.
      * ``"-a,-b"`` -> subtracts both. Excluding every cron is legal and simply means
        ``"none"``; it is not treated as a mistake.

    The denylist exists so that switching ONE cron off does not require freezing the other
    eleven into an allowlist. An allowlist silently drops any cron added to ``ALL_CRON_JOBS``
    later -- "I enabled the cron and nothing happened", the exact failure mode this module
    exists to eliminate, just delayed until the next release.

    Order: the allowlist form preserves the order YOU listed; the denylist form is a
    subtraction from the full set, so it follows ``ALL_CRON_JOBS`` order. Registration order
    does not affect scheduling either way. Duplicates collapse instead of erroring, in both
    forms -- ``"-x,-x"``, or ``"-x"`` together with ``"-cron:x"``, excludes x once.

    Hard errors, all raised at import so the container dies loudly instead of quietly running
    the wrong schedule against production data overnight:
      * An unknown name, INCLUDING an unknown exclusion. Excluding a cron that does not exist
        means the exclusion is not doing what you think and the job you meant to silence is
        still armed -- so it is refused for the same reason an unknown inclusion is.
      * Mixing inclusions with exclusions, e.g. ``"poll_tracking_job,-run_mrp_auto_draft_job"``.
        That reads either as "only the tracking poll" or as "everything except the MRP pass",
        and those differ by most of the schedule -- including crons that write draft POs and
        work orders and crons that email every supervisor. Nothing in the string says which
        was meant, so it is refused. ``all`` is the ONE positive token allowed alongside
        exclusions, because there it is the explicit base rather than an inclusion.
      * ``"none"`` together with an exclusion: there is nothing to subtract from.
      * ``all`` or ``none`` used as one token among others with NOTHING excluded, e.g.
        ``"all,poll_tracking_job"`` or the trailing-comma slip ``"all,"``. Both are whole-spec
        keywords: each has to be the entire value. Long-standing behavior, called out here
        because the denylist form teaches ``all`` as a base token and an operator will
        reasonably reach for it.
      * A value that is punctuation only (``","``), which names no cron at all. Until this
        was refused it was the single input in the grammar that produced a wrong SET instead
        of an error -- it armed ZERO crons and logged exactly what a deliberate ``"none"``
        logs, so nothing distinguished a mangled value from an intentional one.

    Case: ``all`` and ``none`` match case-insensitively (as they always have); job names match
    case-SENSITIVELY. Surrounding whitespace is ignored everywhere -- around commas, around
    each token, and between a ``-`` and the name it negates -- so both ``" all , -x "`` and
    ``"all, - x"`` work.
    """
    jobs = list(ALL_CRON_JOBS if available is None else available)
    raw = (os.getenv("WORKER_CRON_JOBS") if spec is None else spec) or ""
    wanted = raw.strip()
    if not wanted or wanted.lower() == "all":
        return jobs
    if wanted.lower() == "none":
        return []
    tokens = [t.strip() for t in wanted.split(",") if t.strip()]
    if not tokens:
        # Separators only: ",", ",,", " , ". The whole-spec keywords above have already
        # returned, so the operator typed SOMETHING and none of it survived the split. This
        # is the one malformed shape that used to produce a SET instead of an error: every
        # check below was vacuously satisfied, the allowlist path looped over an empty list,
        # and the worker armed ZERO crons while logging the same "none armed" line as a
        # deliberate WORKER_CRON_JOBS=none. MRP, the late-WO emails and the 5-minute
        # notification relay sweeper would all stop with no receipt anywhere distinguishing
        # "asked for nothing" from "value got mangled". Refused rather than mapped onto
        # 'all' or 'none': picking one is the guess this function declines to make everywhere
        # else, and the two guesses differ by the entire schedule.
        raise ValueError(
            f"WORKER_CRON_JOBS={wanted!r} is punctuation with no job names in it. Use 'all', "
            f"'none', a comma-separated subset ('a,b'), or 'all,-<job>' to arm everything but one."
        )

    # arq names cron jobs "cron:<coroutine name>". Accept either spelling so the value you
    # copy out of the startup log and the value you copy out of worker.py both work. Both
    # spellings map to the SAME CronJob object, which is what makes the id()-keyed de-dup
    # below collapse "x" and "cron:x" instead of treating them as two crons.
    by_name: Dict[str, CronJob] = {}
    for job in jobs:
        by_name[job.name] = job
        by_name[job.name.removeprefix("cron:")] = job

    excluded_tokens = [t for t in tokens if t.startswith("-")]
    positive_tokens = [t for t in tokens if not t.startswith("-")]

    def excluded_job_name(token: str) -> str:
        """The job name inside an exclusion token: peel the '-' THEN strip.

        Tokens are already stripped at their edges, but that happens BEFORE the '-' is
        peeled, so a space the operator left after the dash for legibility -- ``"all, -
        run_mrp_auto_draft_job"`` -- stays glued to the front of the name and turns a
        correct exclusion into a bogus "unknown cron job" crash loop. The docstring promises
        whitespace is ignored everywhere; this is what makes that true rather than
        almost-true. Used by BOTH the validation below and the subtraction at the end -- they
        must peel identically, or the validator would bless a token the resolver then cannot
        find (a KeyError at import instead of a readable ValueError).
        """
        return token.removeprefix("-").strip()

    # "all"/"none" are keywords among a list of tokens ONLY when something is being
    # subtracted. In a plain positive list they are not crons and never were:
    # "all,poll_tracking_job" has always been an error and still is, because that spec is
    # just as ambiguous as the mixed one and nobody is relying on it working. They get their
    # OWN error rather than being reported as unknown job names -- see below.
    stop_tokens: List[str] = []
    keyword_tokens: List[str] = []
    included_tokens: List[str] = []
    for token in positive_tokens:
        lowered = token.lower()
        if lowered in ("all", "none"):
            if not excluded_tokens:
                keyword_tokens.append(token)  # a keyword used where only names are legal
            elif lowered == "none":
                stop_tokens.append(token)  # contradicts the subtraction; reported below
            # else: 'all' alongside exclusions is the explicit base, not a cron to arm.
            continue
        included_tokens.append(token)

    # Names are validated FIRST and identically in both shapes -- a typo is a typo whether it
    # is being armed or silenced, and it is the most actionable thing to report. Deliberately
    # ahead of the keyword error below: in "all,<mistyped>" the mistyped name is the defect,
    # so it must be what the message leads with. The realistic case is an en-dash paste --
    # "all,–run_mrp_auto_draft_job" copied out of a doc that auto-substituted the hyphen --
    # where the en-dash token is not an exclusion at all and 'all' is merely collateral.
    unknown = [t for t in included_tokens if t not in by_name]
    unknown += [t for t in excluded_tokens if excluded_job_name(t) not in by_name]
    if unknown:
        known = sorted({job.name.removeprefix("cron:") for job in jobs})
        raise ValueError(
            f"WORKER_CRON_JOBS names unknown cron job(s): {', '.join(sorted(set(unknown)))}. "
            f"Known jobs: {', '.join(known)}. Use 'all', 'none', or a comma-separated subset "
            f"to arm exactly those; prefix a name with '-' to exclude it from the full set "
            f"(e.g. 'all,-run_mrp_auto_draft_job'). An excluded name must be a known job too: "
            f"excluding one that does not exist means the cron you meant to silence is still armed."
        )

    if keyword_tokens:
        # Reached by "all,", ",all", "none," and "all,poll_tracking_job". Behavior is
        # unchanged -- all four have always been refused -- but the message used to call
        # 'all' an unknown CRON JOB and then, in the same sentence, tell the operator to use
        # 'all'. Staring at WORKER_CRON_JOBS=all, that reads as "'all' is unknown; use 'all'".
        # A trailing comma left behind while deleting an exclusion is the likeliest way to
        # land here, so the comma is named explicitly.
        raise ValueError(
            f"WORKER_CRON_JOBS={wanted!r} uses {', '.join(sorted(set(keyword_tokens)))} as a cron "
            f"NAME, but 'all' and 'none' are whole-spec keywords: each one has to be the entire "
            f"value. A stray leading or trailing comma is the usual cause -- 'all,' splits into "
            f"the token 'all' and nothing else -- so delete the comma. To arm a subset, list only "
            f"the names you want ('a,b'); to arm everything except one, use 'all,-<job>'."
        )

    if stop_tokens:
        # Any inclusions are named too: fixing the 'none' only to be told on the NEXT deploy
        # that the spec also mixes shapes is two crash-loop round trips for one bad value.
        also_mixed = f" (it also names cron job(s) to include: {', '.join(included_tokens)})" if included_tokens else ""
        raise ValueError(
            f"WORKER_CRON_JOBS={wanted!r} combines 'none' with exclusion(s) "
            f"({', '.join(excluded_tokens)}){also_mixed}. 'none' already arms no crons, so there is "
            f"nothing to subtract from. Use 'none' on its own to arm nothing, or "
            f"'all,-<job>' to arm everything except <job>."
        )

    if excluded_tokens and included_tokens:
        raise ValueError(
            f"WORKER_CRON_JOBS={wanted!r} mixes cron job(s) to INCLUDE "
            f"({', '.join(included_tokens)}) with exclusion(s) ({', '.join(excluded_tokens)}). "
            f"That could mean 'arm only {included_tokens[0]}' or 'arm everything except "
            f"{excluded_tokens[0].removeprefix('-')}' -- those differ by most of the schedule, "
            f"and several crons write to production data, so it is refused rather than guessed. "
            f"Use only names ('a,b') to arm exactly those, or only exclusions ('all,-a' or "
            f"'-a') to arm everything else."
        )

    if excluded_tokens:
        # A subtraction from the full set, so the result keeps ALL_CRON_JOBS order rather than
        # anything implied by the operator's string.
        #
        # Keyed by resolved NAME, not by id(). arq derives a cron's name from its coroutine
        # ("cron:" + __qualname__), so scheduling one coroutine twice -- a morning and an
        # evening MRP pass, a second tracking poll, an ordinary future edit -- produces two
        # CronJob objects carrying the IDENTICAL name, and by_name keeps only the last of
        # them. An id()-keyed subtraction would then drop that one and leave the OTHER
        # instance armed, while startup()'s "SUPPRESSED by WORKER_CRON_JOBS" line still
        # announced the cron was off: two contradictory lines in one startup log, with the
        # reassuring one wrong and a writing cron still firing. Subtracting by name removes
        # every instance, so "off" means off. Names also collapse the "-x" / "-cron:x"
        # spellings and a repeated exclusion for free, which is why no id()-keyed de-dup is
        # needed here the way it is on the allowlist path below.
        excluded_names = {by_name[excluded_job_name(t)].name for t in excluded_tokens}
        return [job for job in jobs if job.name not in excluded_names]

    # De-duplicate by identity, preserving order: a name listed twice -- or once in each
    # spelling -- must not register the same cron twice.
    selected: List[CronJob] = []
    seen: Set[int] = set()
    for name in included_tokens:
        job = by_name[name]
        if id(job) not in seen:
            seen.add(id(job))
            selected.append(job)
    return selected


def describe_cron_schedule(jobs: Sequence[CronJob], now: datetime = None) -> List[str]:
    """One ``name -> next run`` line per registered cron, for the startup log.

    Computed with arq's own ``next_cron`` against COPIES of each job's schedule fields; the
    ``CronJob`` objects are never mutated, so arq's own scheduling is untouched. ``now`` is
    timezone-aware in the container's local zone, matching how arq itself resolves its
    default timezone -- so the printed times are the times that will actually happen.
    """
    from arq.cron import next_cron

    reference = now or datetime.now().astimezone()
    lines: List[str] = []
    for job in jobs:
        try:
            nxt = next_cron(
                reference,
                month=job.month,
                day=job.day,
                weekday=job.weekday,
                hour=job.hour,
                minute=job.minute,
                second=job.second,
                microsecond=job.microsecond,
            )
            when = nxt.isoformat()
        except Exception:  # pragma: no cover - never let logging break startup
            when = "unknown"
        lines.append(f"{job.name} -> next run {when}")
    return lines


# ============================================================================
# STARTUP/SHUTDOWN
# ============================================================================


async def startup(ctx):
    """Worker startup - announce exactly what this process is and what it will do.

    Everything logged here answers a question that previously required guessing: which Redis
    did it actually connect to, which commit is running, which crons are armed, and when
    each of them next fires.
    """
    logger.info(
        "ARQ worker starting up (environment=%s, release=%s)",
        settings.ENVIRONMENT,
        settings.APP_RELEASE or "unset",
    )
    logger.info(
        "ARQ worker Redis: %s | queue=%s",
        REDIS_TARGET.describe(),
        WorkerSettings.queue_name,
    )
    for warning in redis_config_warnings():
        logger.warning("ARQ worker Redis config: %s", warning)

    registered = list(WorkerSettings.cron_jobs)
    registered_ids = {id(job) for job in registered}
    suppressed = [job.name for job in ALL_CRON_JOBS if id(job) not in registered_ids]
    if suppressed:
        logger.warning(
            "ARQ worker cron: %d of %d cron jobs SUPPRESSED by WORKER_CRON_JOBS=%r: %s",
            len(suppressed),
            len(ALL_CRON_JOBS),
            os.getenv("WORKER_CRON_JOBS"),
            ", ".join(sorted(suppressed)),
        )
    if registered:
        local_zone = datetime.now().astimezone().tzname()
        logger.info("ARQ worker cron: %d job(s) armed, times in %s", len(registered), local_zone)
        for line in describe_cron_schedule(registered):
            logger.info("ARQ worker cron:   %s", line)
    else:
        logger.info("ARQ worker cron: none armed; draining enqueue-driven jobs only")

    logger.info("ARQ worker ready (%d job functions registered)", len(WorkerSettings.functions))


async def shutdown(ctx):
    """Worker shutdown - cleanup"""
    logger.info("ARQ worker shutting down...")


# ============================================================================
# WORKER SETTINGS
# ============================================================================


# Sentry for the WORKER process. The API initializes it at import of app.main, which the
# worker never imports -- so until now every cron traceback died in the container log.
# Tagged component=worker so worker and API events are separable in one Sentry project.
init_sentry(component="worker")


class WorkerSettings:
    """ARQ Worker configuration"""

    # Redis connection -- the SAME resolver the enqueue side uses (app/core/queue.py), which
    # is what makes "the API enqueues where the worker listens" true by construction rather
    # than by two configs happening to agree. Guarded by tests/test_worker_redis_parity.py.
    #
    # The WORKER profile differs from the enqueue side ONLY in transport tuning (how long it
    # waits for Redis), never in which Redis it reaches -- the parity guard enforces exactly
    # that split. It exists because arq re-raises a Redis blip during its own job bookkeeping
    # straight out of _poll_iteration and kills the process; this worker's first production
    # boot died that way on 2026-08-05, 22 seconds in. See RedisProfile in app/core/queue.py
    # for the traceback, the measurements, and why retry_on_timeout is load-bearing.
    redis_settings = get_redis_settings(profile=RedisProfile.WORKER)

    # Job functions
    functions = [
        send_email_job,
        send_sms_job,
        send_sms_overflow_job,
        send_webhook_job,
        run_mrp_job,
        run_mrp_auto_draft_job,
        generate_report_job,
        run_scheduling_job,
        send_daily_digest_job,
        check_calibrations_job,
        cleanup_old_logs_job,
        archive_aged_audit_logs_job,
        check_late_work_orders_job,
        check_low_stock_job,
        check_quote_expiring_job,
        aggregate_ai_learning_job,
        dispatch_work_order_completion_signals_job,
        process_tracking_webhook_job,
        print_receiving_label_job,
        run_oee_auto_calc_job,
        dispatch_notification_job,
        relay_pending_notifications_job,
        dispatch_notification_direct_job,
    ]

    # Cron jobs (scheduled tasks). Defined in ALL_CRON_JOBS above; WORKER_CRON_JOBS narrows
    # the set for a controlled first boot and defaults to all of them (unchanged behavior).
    cron_jobs = select_cron_jobs()

    # Lifecycle
    on_startup = startup
    on_shutdown = shutdown

    # Worker settings
    max_jobs = 10
    job_timeout = 600  # 10 minutes default
    keep_result = 3600  # Keep results for 1 hour

    # Queue settings
    queue_name = "arq:queue"
