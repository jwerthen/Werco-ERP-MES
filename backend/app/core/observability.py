"""Process-agnostic observability wiring, shared by the API and the ARQ worker.

Sentry init used to live only in ``app/main.py``. The worker process never imports
``app.main``, so it ran completely un-instrumented: a cron that raised produced a line in
the container log and nothing else -- no issue, no alert, no release tag. Since the whole
point of the worker is that nobody is watching when it runs at 02:30, that is exactly the
process that most needs error tracking.

This module holds the ONE implementation both entry points call, so error tracking cannot
be wired for one process and forgotten for the other. ``component`` is set as a Sentry tag
so API and worker events are separable in the same project.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, List, Optional, Sequence

from app.core.config import settings

logger = logging.getLogger(__name__)


def init_sentry(
    component: str = "api",
    integrations_factory: Optional[Callable[[], Sequence[Any]]] = None,
) -> None:
    """Initialize Sentry error tracking when a DSN is configured.

    Never raises: with no DSN, a missing SDK, a missing integration, or a DSN the SDK
    refuses, the process boots without error tracking rather than not at all.

    ``integrations_factory`` is a callable (not a list) so a framework integration is only
    imported in the process that actually needs it -- the worker must not pull in the
    FastAPI integration.
    """
    if not settings.SENTRY_DSN:
        return
    try:
        import sentry_sdk
    except ImportError:
        logger.warning("Sentry DSN provided but sentry-sdk not installed")
        return

    integrations: List[Any] = []
    if integrations_factory is not None:
        try:
            integrations = list(integrations_factory())
        except ImportError:
            logger.warning(
                "Sentry integration unavailable for component %s; continuing with the SDK defaults",
                component,
            )

    try:
        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            integrations=integrations,
            # PERFORMANCE TRANSACTIONS ONLY -- this is not an error-capture switch. Errors
            # are governed by `sample_rate` (left at its 1.0 default), so every exception is
            # still reported at any value of this. Defaults to 0.1; see the comment on
            # SENTRY_TRACES_SAMPLE_RATE in core/config.py for why 1.0 was actively harmful
            # here (two 30-second pollers ingesting ~5,800 useless transactions/day, burning
            # the quota that real errors are billed against).
            traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
            # Ties every event to the deployed commit. None when unset (local dev), which
            # Sentry accepts -- events are simply recorded without a release.
            release=settings.APP_RELEASE,
            environment=settings.ENVIRONMENT,
        )
    except Exception:
        # Monitoring must never be able to stop a process from doing its job. sentry_sdk.init()
        # validates the DSN synchronously and raises BadDsn on a malformed one (a typo or a
        # trailing space in the Railway variable is enough) -- and this runs at import, so an
        # uncaught raise here means the process never starts and the deploy is dead in the
        # water rather than merely un-instrumented. Log it and run without Sentry.
        logger.exception("Sentry initialization failed; continuing without error tracking")
        return

    try:
        sentry_sdk.set_tag("component", component)
    except Exception:  # pragma: no cover - tagging must never be fatal
        pass

    logger.info(
        "Sentry initialized successfully (component=%s, traces_sample_rate=%s, release=%s)",
        component,
        settings.SENTRY_TRACES_SAMPLE_RATE,
        settings.APP_RELEASE or "unset",
    )
