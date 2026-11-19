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
import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Union
from urllib.parse import urlsplit

from app.core.config import settings

logger = logging.getLogger(__name__)

# Keys Sentry must never send, on top of the SDK's ``DEFAULT_DENYLIST`` (which already covers
# ``password``, ``token``, ``authorization``, cookies ...). The SDK matches a key EXACTLY
# (case-insensitive), so every name a credential travels under is listed:
#   * a floor PIN (badge sign-on, the admin set route) and a station / visitor-tablet PIN --
#     ``pin``; ``new_pin`` / ``floor_pin`` for any route that names it so;
#   * the password-bearing names the default misses: ``verify_password``'s ``plain_password``
#     frame variable (a floor PIN passes through it too) and the change / reset / registration
#     bodies' ``new_password`` / ``current_password`` / ``admin_password``.
# FastApiIntegration attaches JSON request bodies to error events and the SDK records frame
# local variables, so without these a PIN reaches Sentry in clear text.
#   * the hash names: a bcrypt hash of a 4-6 digit PIN IS the PIN to anyone who can run bcrypt
#     10^4-10^6 times -- ``stored_hash`` / ``hashed`` / ``hashed_password`` frame variables and the
#     ``floor_pin_hash`` / ``password_hash`` columns;
#   * ``default_password`` (the import bodies).
# FastApiIntegration attaches JSON request bodies to error events and the SDK records frame
# local variables, so without these a PIN reaches Sentry in clear text. The key list cannot see
# INSIDE a value, so ``scrub_sentry_event`` (``before_send``) covers the rest.
SENTRY_EXTRA_DENYLIST = (
    "pin",
    "new_pin",
    "floor_pin",
    "plain_password",
    "new_password",
    "current_password",
    "admin_password",
    "default_password",
    "stored_hash",
    "hashed",
    "hashed_password",
    "floor_pin_hash",
    "password_hash",
)

# --- before_send: what a key-only denylist cannot reach ---------------------------------------
#
# Three things carry a credential in a VALUE, where the scrubber (which matches dict keys) never
# looks:
#   1. raw request-body locals -- ``fastapi.routing``'s ``body_bytes`` and ``app.main``'s
#      ``limit_json_body_size`` ``body`` hold ``b'{"employee_id": "0042", "pin": "2580"}'`` as a
#      repr string while the route runs, in every traceback;
#   2. a bcrypt hash bound to any local under any name;
#   3. SQLAlchemy's ``[parameters: {...}]`` in an exception message (also turned off at the
#      source with the engine's ``hide_parameters=True``, ``app/db/database.py``).
# And on a CREDENTIAL route nothing in the frames or the request body is worth the risk, so every
# frame variable and the request data are dropped there entirely.
SENTRY_FILTERED = "[Filtered]"
_BODY_FRAME_VARS = frozenset({"body_bytes", "body", "raw_body", "json_body", "request_body", "content"})
_BCRYPT_HASH = re.compile(r"\$2[aby]\$\d\d\$[./A-Za-z0-9]*")
_SQL_PARAMETERS = re.compile(r"\[parameters: [^\n]*")
# Paths (after the API prefix) whose request body carries a credential: every ``/auth/`` route,
# floor-PIN administration, the visitor-tablet and crew-station PIN sign-ins and PIN writes, and
# the password writes on the users router.
_CREDENTIAL_ROUTE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"^/auth/",
        r"^/users/[^/]+/floor-pin(/.*)?$",
        r"^/users/[^/]+/reset-password$",
        r"^/users/change-password$",
        r"^/visitor-logs/station-login$",
        r"^/visitor-logs/stations(/[^/]+/reset-pin)?$",
        r"^/shop-floor/kiosk-stations/station-login$",
        r"^/shop-floor/kiosk-stations(/[^/]+/reset-pin)?$",
    )
)


def is_credential_route(path: str) -> bool:
    """Does this request path carry a credential in its body (see ``_CREDENTIAL_ROUTE_PATTERNS``)?"""
    prefix = settings.API_V1_PREFIX.rstrip("/")
    if not path.startswith(prefix + "/"):
        return False
    relative = path[len(prefix) :]
    return any(pattern.search(relative) for pattern in _CREDENTIAL_ROUTE_PATTERNS)


def _iter_frames(event: Dict[str, Any]) -> Any:
    """Every stack frame in the event: exception chains and thread stacks."""
    for key in ("exception", "threads"):
        container = event.get(key)
        values = container.get("values") if isinstance(container, dict) else None
        for value in values or ():
            stacktrace = value.get("stacktrace") if isinstance(value, dict) else None
            frames = stacktrace.get("frames") if isinstance(stacktrace, dict) else None
            for frame in frames or ():
                if isinstance(frame, dict):
                    yield frame


def _filter_hashes(value: Any, whole: bool) -> Any:
    """``value`` with every bcrypt hash removed, at any depth (iterative). ``whole`` replaces a
    string containing one entirely; otherwise only the hash itself is cut out of it."""

    def clean(text: str) -> str:
        if not _BCRYPT_HASH.search(text):
            return text
        return SENTRY_FILTERED if whole else _BCRYPT_HASH.sub(SENTRY_FILTERED, text)

    if isinstance(value, str):
        return clean(value)
    stack = [value]
    while stack:
        node = stack.pop()
        items = node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else ()
        for key, item in list(items):
            if isinstance(item, str):
                node[key] = clean(item)
            elif isinstance(item, (dict, list)):
                stack.append(item)
    return value


def scrub_sentry_event(event: Dict[str, Any], hint: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """``before_send``: remove what the key-based ``EventScrubber`` cannot see (see above).

    For EVERY event: drop the raw-body frame variables (``_BODY_FRAME_VARS``), replace any frame
    variable holding a bcrypt hash with ``"[Filtered]"``, cut ``[parameters: ...]`` and bcrypt
    hashes out of exception messages, and cut bcrypt hashes out of the request data and extras.
    For an event from a CREDENTIAL route (``is_credential_route``): drop every frame variable and
    the request data entirely. Never raises in practice; if it ever did, the SDK drops the event
    (its ``before_send`` wrapper keeps no partial result), which is the safe direction.
    """
    request = event.get("request")
    url = request.get("url") if isinstance(request, dict) else None
    credential_route = isinstance(url, str) and is_credential_route(urlsplit(url).path)

    for frame in _iter_frames(event):
        frame_vars = frame.get("vars")
        if not isinstance(frame_vars, dict):
            continue
        if credential_route:
            frame.pop("vars", None)
            continue
        for name in _BODY_FRAME_VARS & set(frame_vars):
            del frame_vars[name]
        _filter_hashes(frame_vars, whole=True)

    exception = event.get("exception")
    for value in (exception.get("values") if isinstance(exception, dict) else None) or ():
        if isinstance(value, dict) and isinstance(value.get("value"), str):
            message = _SQL_PARAMETERS.sub("[parameters: [Filtered]]", value["value"])
            value["value"] = _filter_hashes(message, whole=False)
    logentry = event.get("logentry")
    if isinstance(logentry, dict):
        for key in ("message", "formatted"):
            if isinstance(logentry.get(key), str):
                logentry[key] = _filter_hashes(_SQL_PARAMETERS.sub("[parameters: [Filtered]]", logentry[key]), False)

    if isinstance(request, dict):
        if credential_route:
            request.pop("data", None)
        elif "data" in request:
            request["data"] = _filter_hashes(request["data"], whole=False)
    if "extra" in event:
        event["extra"] = _filter_hashes(event["extra"], whole=False)
    return event


def build_event_scrubber() -> Any:
    """The ``EventScrubber`` every process hands ``sentry_sdk.init``.

    ``recursive=True`` so a denylisted key is filtered at ANY depth of a request body, an
    ``extra`` or a frame's variables, not only at the top level.
    """
    from sentry_sdk.scrubber import DEFAULT_DENYLIST, EventScrubber

    return EventScrubber(denylist=DEFAULT_DENYLIST + list(SENTRY_EXTRA_DENYLIST), recursive=True)


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
            # Credentials never leave the process: request bodies, extras and frame variables
            # are scrubbed of every key in the SDK default denylist plus SENTRY_EXTRA_DENYLIST
            # (the floor PIN above all), at any depth -- then ``scrub_sentry_event`` removes what a
            # key list cannot see: raw-body locals, bcrypt hashes in any value, SQL parameters,
            # and on a credential route every frame variable and the request body.
            event_scrubber=build_event_scrubber(),
            before_send=scrub_sentry_event,
            before_send_transaction=scrub_sentry_event,
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


def capture_exception_to_sentry(
    exc: BaseException,
    *,
    context: Optional[Mapping[str, Any]] = None,
    fingerprint: Optional[Sequence[str]] = None,
) -> None:
    """Report ``exc`` to Sentry with ``context`` attached. No-op without a DSN; never raises.

    Scalar context values (str/int/bool) become tags, so events can be filtered by them;
    every value is also attached as an extra, so list-valued context (a batch of ids) is
    still on the event without becoming a high-cardinality tag.

    ``fingerprint`` overrides Sentry's stack-based grouping: events with the same
    fingerprint are one issue. Pass it when the stack is not what identifies the problem
    (a lost lock race groups by route and SQLSTATE, wherever in the request it surfaced).
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
    scope_kwargs: dict = {"tags": tags, "extras": context}
    if fingerprint is not None:
        scope_kwargs["fingerprint"] = [str(part) for part in fingerprint]
    try:
        sentry_sdk.capture_exception(exc, **scope_kwargs)
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
