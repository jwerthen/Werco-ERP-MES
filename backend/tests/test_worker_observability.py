"""The worker must be legible from its log alone.

Nobody watches a background worker. It has no HTTP surface to curl, no UI, and its most
consequential jobs run at 02:30. Everything below exists so a human reading the first twenty
lines of the container log can answer, without guessing: which Redis did it connect to,
which commit is running, which crons are armed, and when each next fires -- and so that a
crash is reported somewhere other than a log line nobody reads.
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict

import pytest

pytestmark = [pytest.mark.unit]


class TestSentryIsWiredInTheWorkerProcess:
    """The worker never imports ``app.main``, where the only ``sentry_sdk.init`` used to be,
    so until this change every worker traceback died in the container log."""

    def test_worker_module_initializes_sentry(self):
        import inspect

        import app.worker as worker

        source = inspect.getsource(worker)
        assert "init_sentry(component=" in source

    def test_shared_init_tags_the_component(self, monkeypatch: pytest.MonkeyPatch):
        """API and worker events land in one Sentry project; the tag is what separates them."""
        import sentry_sdk

        from app.core.config import settings
        from app.core.observability import init_sentry

        captured: Dict[str, Any] = {}
        tags: Dict[str, Any] = {}
        monkeypatch.setattr(sentry_sdk, "init", lambda **kw: captured.update(kw))
        monkeypatch.setattr(sentry_sdk, "set_tag", lambda k, v: tags.update({k: v}))
        monkeypatch.setattr(settings, "SENTRY_DSN", "https://public@example.ingest.sentry.io/1")

        init_sentry(component="worker")

        assert tags["component"] == "worker"
        assert captured["release"] == settings.APP_RELEASE
        assert captured["environment"] == settings.ENVIRONMENT
        # No FastAPI integration in a process that runs no FastAPI app.
        assert captured["integrations"] == []

    def test_a_bad_dsn_cannot_stop_the_worker_from_starting(self, monkeypatch: pytest.MonkeyPatch):
        import sentry_sdk
        from sentry_sdk.utils import BadDsn

        from app.core.config import settings
        from app.core.observability import init_sentry

        def boom(**kwargs: Any) -> None:
            raise BadDsn("Unsupported scheme ''")

        monkeypatch.setattr(sentry_sdk, "init", boom)
        monkeypatch.setattr(settings, "SENTRY_DSN", "not-a-dsn")

        init_sentry(component="worker")  # must not raise

    def test_the_api_still_gets_its_fastapi_integration(self, monkeypatch: pytest.MonkeyPatch):
        """Refactoring the shared init must not have quietly dropped the API's integration."""
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration

        from app.core.config import settings
        from app.main import init_sentry as api_init_sentry

        captured: Dict[str, Any] = {}
        monkeypatch.setattr(sentry_sdk, "init", lambda **kw: captured.update(kw))
        monkeypatch.setattr(settings, "SENTRY_DSN", "https://public@example.ingest.sentry.io/1")

        api_init_sentry()

        assert any(isinstance(i, FastApiIntegration) for i in captured["integrations"])


class TestStartupSaysWhatItConnectedTo:
    @pytest.mark.asyncio
    async def test_startup_logs_the_redis_target_the_queue_name_and_the_release(self, caplog):
        import app.worker as worker

        with caplog.at_level(logging.INFO):
            await worker.startup({})

        text = caplog.text
        assert "ARQ worker Redis:" in text
        assert worker.WorkerSettings.queue_name in text
        assert "environment=" in text and "release=" in text

    @pytest.mark.asyncio
    async def test_startup_never_logs_the_redis_password(self, monkeypatch: pytest.MonkeyPatch, caplog):
        import importlib

        from app.core.config import settings

        monkeypatch.setattr(settings, "REDIS_URL", "redis://default:sup3rs3cret@host.internal:6379/0")
        import app.worker as worker

        worker = importlib.reload(worker)
        try:
            with caplog.at_level(logging.INFO):
                await worker.startup({})
            assert "sup3rs3cret" not in caplog.text
            assert "host.internal" in caplog.text
        finally:
            monkeypatch.undo()
            importlib.reload(worker)

    @pytest.mark.asyncio
    async def test_startup_logs_every_armed_cron_with_a_next_run_time(self, caplog):
        import app.worker as worker

        with caplog.at_level(logging.INFO):
            await worker.startup({})

        for job in worker.WorkerSettings.cron_jobs:
            assert job.name in caplog.text
        assert "next run" in caplog.text


class TestCronScheduleDescription:
    def test_next_run_is_computed_for_every_cron(self):
        from app.worker import ALL_CRON_JOBS, describe_cron_schedule

        lines = describe_cron_schedule(ALL_CRON_JOBS)
        assert len(lines) == len(ALL_CRON_JOBS)
        assert all("next run" in line and "unknown" not in line for line in lines)

    def test_describing_the_schedule_does_not_mutate_the_cron_jobs(self):
        """arq computes ``next_run`` itself at worker construction; pre-setting it from a
        logging helper would interfere with the real scheduling."""
        from app.worker import ALL_CRON_JOBS, describe_cron_schedule

        before = [job.next_run for job in ALL_CRON_JOBS]
        describe_cron_schedule(ALL_CRON_JOBS)
        assert [job.next_run for job in ALL_CRON_JOBS] == before

    def test_times_are_reported_in_the_containers_local_zone(self):
        """arq defaults its timezone to ``datetime.now().astimezone().tzinfo``. On Railway
        that is UTC unless TZ is set -- so "6 AM" is 06:00 UTC, i.e. 01:00 Central. The log
        must print the times that will actually happen, not naive ones."""
        from app.worker import ALL_CRON_JOBS, describe_cron_schedule

        reference = datetime.now().astimezone()
        lines = describe_cron_schedule(ALL_CRON_JOBS[:1], now=reference)
        stamp = lines[0].split("next run ")[1]
        parsed = datetime.fromisoformat(stamp)
        assert parsed.tzinfo is not None
        assert reference <= parsed <= reference + timedelta(days=32)


class TestCronSelection:
    """``WORKER_CRON_JOBS`` narrows the schedule for a controlled first boot. It must default
    to today's behavior -- this change enables nothing that was previously off."""

    def test_default_registers_every_cron(self):
        from app.worker import ALL_CRON_JOBS, select_cron_jobs

        assert select_cron_jobs("") == ALL_CRON_JOBS
        assert select_cron_jobs("all") == ALL_CRON_JOBS

    def test_worker_settings_defaults_to_the_full_schedule(self, monkeypatch: pytest.MonkeyPatch):
        import importlib

        monkeypatch.delenv("WORKER_CRON_JOBS", raising=False)
        import app.worker as worker

        worker = importlib.reload(worker)
        try:
            assert len(worker.WorkerSettings.cron_jobs) == len(worker.ALL_CRON_JOBS)
        finally:
            importlib.reload(worker)

    def test_none_arms_nothing_but_leaves_the_job_functions_registered(self, monkeypatch: pytest.MonkeyPatch):
        """The safe first boot: drain the enqueue-driven queue (notifications, webhooks,
        labels) without firing any bulk scheduled work."""
        import importlib

        monkeypatch.setenv("WORKER_CRON_JOBS", "none")
        import app.worker as worker

        worker = importlib.reload(worker)
        try:
            assert worker.WorkerSettings.cron_jobs == []
            assert len(worker.WorkerSettings.functions) >= 20
        finally:
            monkeypatch.delenv("WORKER_CRON_JOBS", raising=False)
            importlib.reload(worker)

    def test_a_subset_can_be_armed_by_name_in_either_spelling(self):
        from app.worker import select_cron_jobs

        selected = select_cron_jobs("check_low_stock_job,cron:poll_tracking_job")
        assert [job.name for job in selected] == ["cron:check_low_stock_job", "cron:poll_tracking_job"]

    def test_a_name_repeated_in_both_spellings_arms_the_cron_once(self):
        """Registering the same cron twice would double every run of it."""
        from app.worker import select_cron_jobs

        selected = select_cron_jobs("check_low_stock_job,cron:check_low_stock_job")
        assert len(selected) == 1

    def test_an_unknown_name_is_a_hard_error(self):
        """ "I enabled the cron and nothing happened" is the exact failure mode this whole
        change exists to eliminate; a silent skip would recreate it."""
        from app.worker import select_cron_jobs

        with pytest.raises(ValueError) as exc:
            select_cron_jobs("check_low_stock_job,typo_job")
        assert "typo_job" in str(exc.value)


class TestTheCronInventoryIsStable:
    """These crons fire the moment a worker process runs. If the list changes, the go-live
    runbook's blast-radius counts (docs/WORKER_SERVICE.md) are stale."""

    def test_the_declared_cron_set_is_exactly_what_the_runbook_documents(self):
        from app.worker import ALL_CRON_JOBS

        names = {job.name.removeprefix("cron:") for job in ALL_CRON_JOBS}
        assert names == {
            "run_mrp_auto_draft_job",
            "send_daily_digest_job",
            "check_calibrations_job",
            "check_late_work_orders_job",
            "check_low_stock_job",
            "check_quote_expiring_job",
            "aggregate_ai_learning_job",
            "run_oee_auto_calc_job",
            "cleanup_old_logs_job",
            "archive_aged_audit_logs_job",
            "poll_tracking_job",
            "relay_pending_notifications_job",
        }


# ---------------------------------------------------------------------------
# WORKER_CRON_JOBS exclusions ("-" prefix)
# ---------------------------------------------------------------------------
#
# The owner wants ONE cron off (``run_mrp_auto_draft_job``, which writes draft POs and work
# orders at 06:00). Before exclusions existed the only way to say that was to list the other
# eleven -- an allowlist, which FREEZES the schedule: a thirteenth cron added next release
# would silently never register on that worker. That is "I enabled the cron and nothing
# happened" again, just delayed, which is the exact failure this module exists to eliminate.
#
# The value the owner was handed for Railway before exclusions shipped. Kept verbatim so the
# equivalence test below is a real migration proof, not a paraphrase of one.
OWNER_ALLOWLIST_VALUE = (
    "send_daily_digest_job,check_calibrations_job,check_late_work_orders_job,check_low_stock_job,"
    "check_quote_expiring_job,aggregate_ai_learning_job,run_oee_auto_calc_job,cleanup_old_logs_job,"
    "archive_aged_audit_logs_job,poll_tracking_job,relay_pending_notifications_job"
)

# The value they can write instead.
OWNER_EXCLUSION_VALUE = "all,-run_mrp_auto_draft_job"

MRP_CRON = "cron:run_mrp_auto_draft_job"


def _all_names():
    from app.worker import ALL_CRON_JOBS

    return [job.name for job in ALL_CRON_JOBS]


async def newly_added_cron_job(ctx):  # pragma: no cover - never executed, only registered
    """Stand-in for the thirteenth cron somebody adds next release.

    Deliberately module level: arq derives a CronJob's name from ``__qualname__``, so a
    nested function would register as ``cron:_helper.<locals>.newly_added_cron_job`` and the
    tests below would be matching a name no real cron could ever have.
    """
    return None


def _schedule_with_one_more_cron():
    """``ALL_CRON_JOBS`` plus a cron that does not exist yet -- i.e. next release's schedule.

    ``select_cron_jobs`` takes ``available`` precisely so the future can be tested today
    without touching the real ``ALL_CRON_JOBS`` (which the runbook's blast-radius counts and
    ``TestTheCronInventoryIsStable`` both pin).
    """
    from arq import cron

    from app.worker import ALL_CRON_JOBS

    return list(ALL_CRON_JOBS) + [cron(newly_added_cron_job, hour=4, minute=0)]


class TestCronSelectionRegressionsBeforeExclusions:
    """Every shape that already worked, pinned. Exclusions are additive: an operator whose
    Railway value predates them must get byte-identical behavior, so these assertions are
    the contract, not prose in a docstring."""

    def test_unset_registers_every_cron(self, monkeypatch: pytest.MonkeyPatch):
        """The default. ``select_cron_jobs`` reads the env var at call time, so deleting it
        exercises the real production default without reloading the module."""
        from app.worker import ALL_CRON_JOBS, select_cron_jobs

        monkeypatch.delenv("WORKER_CRON_JOBS", raising=False)

        selected = select_cron_jobs()
        assert [job.name for job in selected] == _all_names()
        assert all(a is b for a, b in zip(selected, ALL_CRON_JOBS))

    def test_empty_and_all_and_padded_all_register_every_cron(self):
        from app.worker import ALL_CRON_JOBS, select_cron_jobs

        for spec in ("", "all", "  all  ", "ALL"):
            assert [job.name for job in select_cron_jobs(spec)] == _all_names(), spec
            assert select_cron_jobs(spec) == ALL_CRON_JOBS, spec

    def test_none_arms_nothing(self):
        from app.worker import select_cron_jobs

        assert select_cron_jobs("none") == []
        assert select_cron_jobs("  NONE  ") == []

    def test_a_positive_list_keeps_the_order_the_operator_listed(self):
        """NOT ``ALL_CRON_JOBS`` order. The allowlist path has always echoed the operator's
        own ordering back; the exclusion path deliberately does not (see below), so this is
        the pin that keeps the two from being "unified" into one behavior."""
        from app.worker import select_cron_jobs

        # In ALL_CRON_JOBS, check_calibrations_job comes well before poll_tracking_job.
        selected = select_cron_jobs("poll_tracking_job,check_calibrations_job")
        assert [job.name for job in selected] == ["cron:poll_tracking_job", "cron:check_calibrations_job"]

    def test_a_positive_list_accepts_both_spellings_and_de_dupes_across_them(self):
        from app.worker import select_cron_jobs

        assert [job.name for job in select_cron_jobs("check_low_stock_job,cron:poll_tracking_job")] == [
            "cron:check_low_stock_job",
            "cron:poll_tracking_job",
        ]
        assert len(select_cron_jobs("check_low_stock_job,check_low_stock_job")) == 1
        assert len(select_cron_jobs("check_low_stock_job,cron:check_low_stock_job")) == 1

    def test_an_unknown_positive_name_is_still_a_hard_error(self):
        from app.worker import select_cron_jobs

        with pytest.raises(ValueError) as exc:
            select_cron_jobs("check_low_stock_job,typo_job")
        message = str(exc.value)
        assert "typo_job" in message
        assert "Known jobs:" in message

    def test_all_inside_a_positive_list_is_still_refused(self):
        """Pre-existing behavior, deliberately unchanged. ``all`` became a legal token ONLY as
        the base of a subtraction; ``"all,poll_tracking_job"`` is as ambiguous as it ever was
        (everything? or just the poll?) and still errors rather than acquiring a meaning."""
        from app.worker import select_cron_jobs

        with pytest.raises(ValueError) as exc:
            select_cron_jobs("all,poll_tracking_job")
        assert "all" in str(exc.value)

    def test_job_names_are_matched_case_sensitively(self):
        """Only ``all``/``none`` are keywords, and only they are lowercased. A cron name is an
        identifier, so a case-mangled one is a typo and gets the typo's hard error."""
        from app.worker import select_cron_jobs

        with pytest.raises(ValueError):
            select_cron_jobs("CHECK_LOW_STOCK_JOB")

    def test_a_trailing_comma_after_all_or_none_is_refused_as_it_always_was(self):
        """A documented wart. Still REFUSED -- pinned because it is pre-existing and accepting
        it would be a behavior change -- but the message is no longer self-contradictory.

        ``all`` / ``none`` are WHOLE-SPEC keywords, tested before the spec is ever split, so
        ``"all,"`` has never matched the keyword and has always fallen through to the name
        parser. It used to error on "unknown cron job(s): all" and then, in the same sentence,
        advise "Use 'all'" -- an operator staring at ``WORKER_CRON_JOBS=all,`` read that as
        "'all' is unknown; use 'all'" and chased the wrong token. It now gets its own message
        naming the comma, which is the likeliest way to land here: deleting the exclusion from
        ``all,-run_mrp_auto_draft_job`` after the MRP cutover and leaving the comma behind.

        The exclusion form splits first, so ``"-x,"`` is accepted -- an asymmetry an operator
        can trip over, and loud either way (nothing silently arms the wrong schedule). Whether
        to also ACCEPT ``"all,"`` is left to the owner; this pins today's answer as no."""
        from app.worker import select_cron_jobs

        for spec in ("all,", ",all", "none,", ",none"):
            with pytest.raises(ValueError) as exc:
                select_cron_jobs(spec)
            message = str(exc.value)
            assert "keyword" in message, spec
            assert "comma" in message, spec
            # It must NOT claim the keyword is an unknown cron job -- that was the old defect.
            assert "unknown cron job" not in message, spec
        # The exclusion form tolerates it.
        assert len(select_cron_jobs("-run_mrp_auto_draft_job,")) == 11

    def test_a_spec_that_is_only_separators_is_refused_rather_than_arming_nothing(self):
        """The one malformed input that used to return a SET instead of raising.

        ``","`` split to an empty token list, every downstream check passed vacuously, and the
        allowlist loop returned ``[]`` -- so the worker armed ZERO crons and ``startup()``
        logged "none armed; draining enqueue-driven jobs only", byte-identical to a deliberate
        ``WORKER_CRON_JOBS=none``. Nothing anywhere distinguished "the operator asked for
        nothing" from "the operator's value was mangled", while MRP, the late-WO emails and
        the 5-minute notification relay sweeper all stopped firing.

        Pinned in BOTH directions on purpose: before this test, ``","`` returning 0 crons was
        unpinned, so a later refactor flipping it to 12 -- silently turning the MRP auto-draft
        pass back on in production -- would have passed the entire suite."""
        from app.worker import select_cron_jobs

        for spec in (",", ",,", " , ", ",,,"):
            with pytest.raises(ValueError) as exc:
                select_cron_jobs(spec)
            assert "no job names" in str(exc.value), spec
        # Not mapped onto 'all' either -- the refusal is a refusal, not a re-interpretation.
        assert select_cron_jobs("none") == []


class TestCronExclusions:
    """A ``-`` prefix subtracts one cron from the full set, so switching one off does not
    freeze the other eleven into a list that silently drops cron thirteen."""

    def test_all_minus_a_job_arms_everything_else(self):
        from app.worker import select_cron_jobs

        selected = select_cron_jobs("all,-run_mrp_auto_draft_job")
        names = [job.name for job in selected]
        assert MRP_CRON not in names
        assert names == [n for n in _all_names() if n != MRP_CRON]

    def test_a_bare_exclusion_implies_all_as_its_base(self):
        """``"-x"`` and ``"all,-x"`` must be the same thing: a spec made only of subtractions
        has nothing else it could be subtracting from."""
        from app.worker import select_cron_jobs

        assert [j.name for j in select_cron_jobs("-run_mrp_auto_draft_job")] == [
            j.name for j in select_cron_jobs("all,-run_mrp_auto_draft_job")
        ]

    def test_the_cron_prefixed_spelling_excludes_identically(self):
        """arq names crons ``cron:<coroutine>`` and the startup log prints that form, so
        whichever spelling the operator copied has to work -- for exclusions exactly as it
        already did for inclusions."""
        from app.worker import select_cron_jobs

        assert [j.name for j in select_cron_jobs("-cron:run_mrp_auto_draft_job")] == [
            j.name for j in select_cron_jobs("-run_mrp_auto_draft_job")
        ]
        assert [j.name for j in select_cron_jobs("all,-cron:run_mrp_auto_draft_job")] == [
            j.name for j in select_cron_jobs("all,-run_mrp_auto_draft_job")
        ]

    def test_the_result_follows_all_cron_jobs_order_not_the_operators_order(self):
        """An exclusion is a subtraction from the full set, not a re-ordering of it. Pinned
        because the allowlist path does the opposite and the two share a function."""
        from app.worker import select_cron_jobs

        # Listed MRP-then-poll; ALL_CRON_JOBS has MRP first and poll second-to-last, and the
        # surviving crons must come back in ALL_CRON_JOBS order regardless.
        selected = select_cron_jobs("-run_mrp_auto_draft_job,-poll_tracking_job")
        expected = [n for n in _all_names() if n not in {MRP_CRON, "cron:poll_tracking_job"}]
        assert [job.name for job in selected] == expected

        reversed_spec = select_cron_jobs("-poll_tracking_job,-run_mrp_auto_draft_job")
        assert [job.name for job in reversed_spec] == expected

    def test_two_exclusions_subtract_both(self):
        from app.worker import ALL_CRON_JOBS, select_cron_jobs

        selected = select_cron_jobs("all,-run_mrp_auto_draft_job,-check_late_work_orders_job")
        names = [job.name for job in selected]
        assert len(names) == len(ALL_CRON_JOBS) - 2
        assert MRP_CRON not in names
        assert "cron:check_late_work_orders_job" not in names

    def test_a_repeated_exclusion_is_not_an_error_and_subtracts_once(self):
        """De-dup mirrors the allowlist path's id()-keyed collapse (CronJob is an unhashable
        dataclass). Both spellings resolve to the SAME object, which is what makes it work."""
        from app.worker import ALL_CRON_JOBS, select_cron_jobs

        for spec in (
            "-run_mrp_auto_draft_job,-run_mrp_auto_draft_job",
            "-run_mrp_auto_draft_job,-cron:run_mrp_auto_draft_job",
            "all,-cron:run_mrp_auto_draft_job,-run_mrp_auto_draft_job",
        ):
            selected = select_cron_jobs(spec)
            assert len(selected) == len(ALL_CRON_JOBS) - 1, spec
            assert MRP_CRON not in [job.name for job in selected], spec

    def test_excluding_every_cron_is_legal_and_means_none(self):
        """Legal, not an error: it is just ``none`` spelled the long way, and special-casing
        it into an error would be the clever-not-loud behavior this module avoids."""
        from app.worker import ALL_CRON_JOBS, select_cron_jobs

        spec = ",".join(f"-{job.name}" for job in ALL_CRON_JOBS)
        assert select_cron_jobs(spec) == []
        assert select_cron_jobs("all," + spec) == []

    def test_whitespace_around_tokens_is_tolerated(self):
        from app.worker import select_cron_jobs

        assert [j.name for j in select_cron_jobs("  all , -run_mrp_auto_draft_job  ")] == [
            j.name for j in select_cron_jobs("all,-run_mrp_auto_draft_job")
        ]
        assert [j.name for j in select_cron_jobs(" -run_mrp_auto_draft_job , -poll_tracking_job ")] == [
            j.name for j in select_cron_jobs("-run_mrp_auto_draft_job,-poll_tracking_job")
        ]

    def test_whitespace_between_the_dash_and_the_name_is_tolerated(self):
        """Tokens are stripped at their edges BEFORE the ``-`` is peeled, so a space left
        after the dash for legibility used to survive into the lookup key and produce
        ``unknown cron job(s): - run_mrp_auto_draft_job`` -- a crash loop whose offending
        token renders with an easily-missed leading space. Both the docstring and
        ENVIRONMENT_VARIABLES.md promise whitespace is ignored; this is the case that made
        that almost-true. The validator and the resolver must peel identically, so this also
        guards against one being fixed without the other (which would trade the readable
        ValueError for a KeyError at import)."""
        from app.worker import select_cron_jobs

        expected = [j.name for j in select_cron_jobs("all,-run_mrp_auto_draft_job")]
        for spec in ("all, - run_mrp_auto_draft_job", "- run_mrp_auto_draft_job", "-  cron:run_mrp_auto_draft_job"):
            assert [j.name for j in select_cron_jobs(spec)] == expected, spec

    def test_excluding_a_coroutine_scheduled_twice_removes_every_instance(self):
        """If one coroutine is ever given two schedules -- a morning and an evening MRP pass,
        a second tracking poll -- both ``CronJob``s carry the IDENTICAL arq name
        (``"cron:" + __qualname__``), so a name-keyed lookup can only hold one of them.

        Subtracting by that single object's id() removed only the LAST instance and left the
        earlier one armed, while ``startup()`` still logged "1 of 13 cron jobs SUPPRESSED ...
        cron:run_mrp_auto_draft_job". Two contradictory lines in one startup log, the
        reassuring one wrong, and the surviving instance was the 06:00 draft-PO/WO pass the
        owner was trying to switch off. Not reachable against today's ``ALL_CRON_JOBS`` (12
        distinct coroutines), which is exactly why it needs a test rather than a comment: the
        edit that makes it reachable is an ordinary one and would land silently."""
        from arq import cron

        from app.worker import ALL_CRON_JOBS, run_mrp_auto_draft_job, select_cron_jobs

        twice = list(ALL_CRON_JOBS) + [cron(run_mrp_auto_draft_job, hour=18, minute=0)]
        assert len(twice) == len(ALL_CRON_JOBS) + 1

        selected = select_cron_jobs("all,-run_mrp_auto_draft_job", available=twice)

        assert not any("run_mrp_auto_draft_job" in job.name for job in selected)
        assert len(selected) == len(ALL_CRON_JOBS) - 1
        # And the same holds for the bare/prefixed spellings of the exclusion.
        for spec in ("-run_mrp_auto_draft_job", "-cron:run_mrp_auto_draft_job"):
            assert not any("run_mrp_auto_draft_job" in j.name for j in select_cron_jobs(spec, available=twice)), spec

    def test_the_all_base_token_is_case_insensitive_like_the_bare_keyword(self):
        from app.worker import select_cron_jobs

        assert [j.name for j in select_cron_jobs("ALL,-run_mrp_auto_draft_job")] == [
            j.name for j in select_cron_jobs("all,-run_mrp_auto_draft_job")
        ]

    def test_the_surviving_jobs_are_the_same_objects_not_copies(self):
        """Load-bearing for the startup log: ``startup`` computes the SUPPRESSED list by
        id()-diffing the registered set against ``ALL_CRON_JOBS``. A rebuilt CronJob would
        make every armed cron read as suppressed."""
        from app.worker import ALL_CRON_JOBS, select_cron_jobs

        selected = select_cron_jobs("all,-run_mrp_auto_draft_job")
        survivors = {id(job) for job in ALL_CRON_JOBS if job.name != MRP_CRON}
        assert {id(job) for job in selected} == survivors

    def test_an_unknown_excluded_name_is_a_hard_error(self):
        """The one most likely to have been written as a silent no-op. Excluding a cron that
        does not exist means the cron you MEANT to silence is still armed and still writing
        -- so it fails exactly as loudly as an unknown inclusion."""
        from app.worker import select_cron_jobs

        with pytest.raises(ValueError) as exc:
            select_cron_jobs("-run_mrp_autodraft_job")  # missing underscore
        message = str(exc.value)
        assert "-run_mrp_autodraft_job" in message
        assert "Known jobs:" in message
        assert "run_mrp_auto_draft_job" in message  # the real name is in the listing

    def test_an_unknown_excluded_name_errors_even_with_an_explicit_all_base(self):
        from app.worker import select_cron_jobs

        with pytest.raises(ValueError) as exc:
            select_cron_jobs("all,-no_such_job")
        assert "-no_such_job" in str(exc.value)

    def test_the_unknown_name_error_explains_the_minus_syntax(self):
        """The error is where an operator lands mid-mistake, so it has to teach the shape
        that would have worked -- otherwise they reach for the frozen allowlist again."""
        from app.worker import select_cron_jobs

        with pytest.raises(ValueError) as exc:
            select_cron_jobs("-no_such_job")
        message = str(exc.value)
        assert "'-'" in message
        assert "all,-run_mrp_auto_draft_job" in message


class TestAmbiguousCronSpecsAreRefused:
    """Guessing here changes what runs against production data overnight -- several crons
    write (draft POs and work orders) or email every supervisor. So an ambiguous spec is a
    hard error at import, never a silently-picked interpretation."""

    def test_mixing_an_inclusion_with_an_exclusion_is_an_error(self):
        from app.worker import select_cron_jobs

        with pytest.raises(ValueError) as exc:
            select_cron_jobs("poll_tracking_job,-run_mrp_auto_draft_job")
        message = str(exc.value)
        # Names BOTH sides: the operator has to see which token was read as which.
        assert "poll_tracking_job" in message
        assert "-run_mrp_auto_draft_job" in message
        # And states the two legal shapes rather than leaving them to guess again.
        assert "'a,b'" in message
        assert "'all,-a'" in message or "'-a'" in message

    def test_mixing_is_refused_in_either_token_order(self):
        from app.worker import select_cron_jobs

        with pytest.raises(ValueError):
            select_cron_jobs("-run_mrp_auto_draft_job,poll_tracking_job")

    def test_mixing_is_refused_with_the_cron_prefixed_spelling_too(self):
        from app.worker import select_cron_jobs

        with pytest.raises(ValueError):
            select_cron_jobs("cron:poll_tracking_job,-cron:run_mrp_auto_draft_job")

    def test_none_combined_with_an_exclusion_is_an_error(self):
        """Contradictory: ``none`` already arms nothing, so there is nothing to subtract."""
        from app.worker import select_cron_jobs

        with pytest.raises(ValueError) as exc:
            select_cron_jobs("none,-run_mrp_auto_draft_job")
        message = str(exc.value)
        assert "none" in message
        assert "-run_mrp_auto_draft_job" in message

    def test_a_bare_minus_is_refused_as_an_unknown_name(self):
        """Not a documented shape; recorded here because the behavior an operator hits has to
        be a real one. ``"-"`` and ``"-   "`` both strip to an empty job name, which is
        unknown, so they take the unknown-name error and show the raw token. Loud, if a
        little odd-looking ("unknown cron job(s): -")."""
        from app.worker import select_cron_jobs

        for spec in ("-", "-   ", "  -  "):
            with pytest.raises(ValueError) as exc:
                select_cron_jobs(spec)
            assert "unknown cron job(s): -" in str(exc.value), spec

    def test_a_double_minus_is_refused(self):
        from app.worker import select_cron_jobs

        with pytest.raises(ValueError) as exc:
            select_cron_jobs("--run_mrp_auto_draft_job")
        assert "-run_mrp_auto_draft_job" in str(exc.value)

    def test_negating_a_keyword_is_refused_rather_than_reinterpreted(self):
        """``"-all"`` could be read as "subtract everything" (i.e. ``none``) and ``"-none"``
        as a double negative. Neither is a documented shape, so both take the unknown-name
        error instead of acquiring a third meaning for a token that already has one."""
        from app.worker import select_cron_jobs

        for spec in ("-all", "-none"):
            with pytest.raises(ValueError) as exc:
                select_cron_jobs(spec)
            assert spec in str(exc.value), spec


class TestTheOwnersMrpCutover:
    """End to end on the actual value going into Railway. This is the test that says the
    migration is safe: the short form and the long form arm the same eleven crons today."""

    def test_the_exclusion_form_arms_exactly_the_other_eleven_crons(self):
        from app.worker import ALL_CRON_JOBS, select_cron_jobs

        selected = select_cron_jobs(OWNER_EXCLUSION_VALUE)
        names = {job.name.removeprefix("cron:") for job in selected}

        assert len(selected) == 11
        assert len(ALL_CRON_JOBS) == 12
        assert "run_mrp_auto_draft_job" not in names
        assert names == {
            "send_daily_digest_job",
            "check_calibrations_job",
            "check_late_work_orders_job",
            "check_low_stock_job",
            "check_quote_expiring_job",
            "aggregate_ai_learning_job",
            "run_oee_auto_calc_job",
            "cleanup_old_logs_job",
            "archive_aged_audit_logs_job",
            "poll_tracking_job",
            "relay_pending_notifications_job",
        }

    def test_the_exclusion_form_and_the_eleven_name_allowlist_arm_the_same_set(self):
        """The migration proof. Same crons, so pasting the short value changes nothing that
        runs tonight -- only what happens when cron thirteen ships."""
        from app.worker import select_cron_jobs

        by_exclusion = select_cron_jobs(OWNER_EXCLUSION_VALUE)
        by_allowlist = select_cron_jobs(OWNER_ALLOWLIST_VALUE)

        assert {job.name for job in by_exclusion} == {job.name for job in by_allowlist}
        assert {id(job) for job in by_exclusion} == {id(job) for job in by_allowlist}

    def test_turning_the_cron_off_does_not_unregister_the_job_function(self, monkeypatch: pytest.MonkeyPatch):
        """MRP stays runnable on demand (the API enqueues ``run_mrp_auto_draft_job``); only
        the 06:00 schedule goes away. Losing the function would turn an operational pause
        into a broken feature."""
        import importlib

        monkeypatch.setenv("WORKER_CRON_JOBS", OWNER_EXCLUSION_VALUE)
        import app.worker as worker

        worker = importlib.reload(worker)
        try:
            assert len(worker.WorkerSettings.cron_jobs) == 11
            assert MRP_CRON not in [job.name for job in worker.WorkerSettings.cron_jobs]
            assert worker.run_mrp_auto_draft_job in worker.WorkerSettings.functions
        finally:
            monkeypatch.delenv("WORKER_CRON_JOBS", raising=False)
            importlib.reload(worker)

    @pytest.mark.asyncio
    async def test_the_startup_log_reads_correctly_for_an_exclusion(self, monkeypatch: pytest.MonkeyPatch, caplog):
        """The SUPPRESSED line is how an operator confirms the change took. It must name the
        excluded cron ONCE (in the suppressed list, never in the armed list) and echo the env
        value that caused it."""
        import importlib

        monkeypatch.setenv("WORKER_CRON_JOBS", OWNER_EXCLUSION_VALUE)
        import app.worker as worker

        worker = importlib.reload(worker)
        try:
            with caplog.at_level(logging.INFO):
                await worker.startup({})
            text = caplog.text

            assert "1 of 12 cron jobs SUPPRESSED" in text
            assert repr(OWNER_EXCLUSION_VALUE) in text
            assert "11 job(s) armed" in text
            # Once, in the SUPPRESSED warning -- never among the armed "next run" lines.
            assert text.count(MRP_CRON) == 1
            assert "cron:poll_tracking_job -> next run" in text
        finally:
            monkeypatch.delenv("WORKER_CRON_JOBS", raising=False)
            importlib.reload(worker)


class TestExclusionsSurviveANewCronBeingAdded:
    """WHY exclusions exist. An allowlist is a frozen snapshot of the schedule at the moment
    it was pasted; the exclusion form is a rule that keeps applying. Both forms agree today
    (proved above) and diverge the moment a cron is added -- which is the whole point."""

    def test_a_newly_added_cron_registers_under_the_exclusion_form(self):
        from app.worker import select_cron_jobs

        extended = _schedule_with_one_more_cron()
        selected = select_cron_jobs(OWNER_EXCLUSION_VALUE, available=extended)
        names = [job.name for job in selected]

        assert "cron:newly_added_cron_job" in names
        assert MRP_CRON not in names
        assert len(selected) == len(extended) - 1

    def test_the_same_newly_added_cron_is_silently_dropped_by_the_allowlist_form(self):
        """No error, no log line, nothing: the failure this change exists to prevent."""
        from app.worker import select_cron_jobs

        extended = _schedule_with_one_more_cron()
        selected = select_cron_jobs(OWNER_ALLOWLIST_VALUE, available=extended)
        names = [job.name for job in selected]

        assert "cron:newly_added_cron_job" not in names
        assert len(selected) == 11

    def test_a_bare_exclusion_also_picks_up_the_new_cron(self):
        from app.worker import select_cron_jobs

        extended = _schedule_with_one_more_cron()
        selected = select_cron_jobs("-run_mrp_auto_draft_job", available=extended)
        assert "cron:newly_added_cron_job" in [job.name for job in selected]

    def test_the_new_cron_can_itself_be_excluded_by_name(self):
        """Sanity: ``available`` really is the universe the parser validates against, so the
        future-proofing test above is not passing on a stale name table."""
        from app.worker import select_cron_jobs

        extended = _schedule_with_one_more_cron()
        selected = select_cron_jobs("-newly_added_cron_job", available=extended)
        assert "cron:newly_added_cron_job" not in [job.name for job in selected]
        assert len(selected) == len(extended) - 1

    def test_an_unknown_name_is_validated_against_the_supplied_schedule(self):
        from app.worker import select_cron_jobs

        with pytest.raises(ValueError) as exc:
            select_cron_jobs("-newly_added_cron_job")  # not in the REAL ALL_CRON_JOBS
        assert "-newly_added_cron_job" in str(exc.value)
