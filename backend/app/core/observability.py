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
from typing import Any, Callable, List, Mapping, Optional, Sequence, Union

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


# Sentry caps a tag value at 200 characters; a longer one is dropped, not truncated.
_SENTRY_TAG_MAX_LENGTH = 200


def _sentry_sdk_if_configured() -> Any:
    """The ``sentry_sdk`` module when a DSN is configured and the SDK is importable, else None.

    Gated on the DSN, not merely on the import: with no DSN the SDK is never initialized,
    and tagging its process-global default scope would only accumulate stale values.
    """
    if not settings.SENTRY_DSN:
        return None
    try:
        import sentry_sdk
    except ImportError:
        return None
    return sentry_sdk


def _tag_value(value: Any) -> str:
    return str(value)[:_SENTRY_TAG_MAX_LENGTH]


def set_sentry_tags(**tags: Any) -> None:
    """Tag the current Sentry scope with request context. ``None`` values are skipped.

    Under the FastAPI integration each request runs in its own isolation scope, so a tag
    set while serving a request lands on that request's events only. No-op when Sentry is
    not configured; never raises -- tagging is diagnostics and must not fail a request.
    """
    sentry_sdk = _sentry_sdk_if_configured()
    if sentry_sdk is None:
        return
    try:
        for key, value in tags.items():
            if value is not None:
                sentry_sdk.set_tag(key, _tag_value(value))
    except Exception:  # pragma: no cover - tagging must never be fatal
        logger.debug("Failed to set Sentry tags", exc_info=True)


def capture_exception_to_sentry(exc: BaseException, *, context: Optional[Mapping[str, Any]] = None) -> None:
    """Report ``exc`` to Sentry with ``context`` attached. No-op without a DSN; never raises.

    Scalar context values (str/int/bool) become tags, so events can be filtered by them;
    every value is also attached as an extra, so list-valued context (a batch of ids) is
    still on the event without becoming a high-cardinality tag.
    """
    sentry_sdk = _sentry_sdk_if_configured()
    if sentry_sdk is None:
        return
    context = dict(context or {})
    tags = {
        key: _tag_value(value)
        for key, value in context.items()
        if value is not None and isinstance(value, (str, int, bool))
    }
    try:
        sentry_sdk.capture_exception(exc, tags=tags, extras=context)
    except Exception:  # pragma: no cover - reporting must never be fatal
        logger.debug("Failed to report an exception to Sentry", exc_info=True)


def report_absorbed_exception(
    log: Union[logging.Logger, logging.LoggerAdapter],
    message: str,
    exc: BaseException,
    *,
    context: Mapping[str, Any],
) -> None:
    """Make an exception that a best-effort path deliberately absorbs VISIBLE.

    Some paths must not fail on a secondary error -- a reconcile-on-read side effect may
    never turn a GET into a 500 -- but "must not fail" is not "must not be seen". This
    records the failure without changing anything about the caller's control flow:

    * an ERROR log line with the traceback, with ``context`` rendered into the message
      (so the plain-text development formatter shows it) and attached as ``extra_data``
      (which the production JSON formatter emits as ``data``);
    * a Sentry event carrying the same context (see ``capture_exception_to_sentry``).

    Sentry is reported FIRST: Sentry's logging integration also turns the ERROR line into
    an event for the same exception object, and its dedupe keeps whichever arrives first,
    so this order keeps the tagged one. Never raises -- the caller is already handling one
    failure, and reporting it must not become a second.
    """
    capture_exception_to_sentry(exc, context=context)
    rendered = " ".join(f"{key}={value}" for key, value in context.items())
    try:
        log.error("%s [%s]", message, rendered, exc_info=exc, extra={"extra_data": dict(context)})
    except Exception:  # pragma: no cover - logging must never be fatal
        pass
