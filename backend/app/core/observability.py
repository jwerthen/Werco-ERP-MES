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
# The scrubber above matches dict KEYS. A credential also travels inside VALUES, where it never
# looks:
#   1. raw request-body locals -- ``fastapi.routing``'s ``body_bytes`` and ``app.main``'s
#      ``limit_json_body_size`` ``body`` hold ``b'{"employee_id": "0042", "pin": "2580"}'`` as a
#      repr string while the route runs, in every traceback;
#   2. the repr of a request model in a frame variable -- ``UserCreate(email=..., password='...')``
#      on ``POST /users/``, ``CompanyCreate(..., admin_password='...')`` on ``POST /platform/companies``;
#   3. ``key=value`` / ``"key": "value"`` text inside any string: an exception message, a log
#      message or its params, a breadcrumb, an extra;
#   4. a bcrypt hash anywhere (a hash of a 4-6 digit PIN IS the PIN to anyone who can run bcrypt);
#   5. SQLAlchemy's ``[parameters: {...}]`` and Postgres's ``Failing row contains (...)`` (which
#      prints the whole row, ``hashed_password`` and ``floor_pin_hash`` included) in an error text.
# And on a CREDENTIAL route nothing in the frames or the request body is worth the risk, so every
# frame variable and the request data are dropped there entirely.
SENTRY_FILTERED = "[Filtered]"
_BODY_FRAME_VARS = frozenset({"body_bytes", "body", "raw_body", "json_body", "request_body", "content"})
_BCRYPT_HASH = re.compile(r"\$2[aby]\$\d\d\$[./A-Za-z0-9]*")
_SQL_PARAMETERS = re.compile(r"\[parameters: [^\n]*")
_FAILING_ROW = re.compile(r"(Failing row contains )\([^\n]*")
# Credential field names. A name ENDING in one of them counts too (``access_token``,
# ``admin_password``, ``station_pin``); one that merely contains it does not (``spin``,
# ``pinned_inventory_item_id``, ``token_type``).
_CREDENTIAL_KEYWORDS = (
    "password|new_password|current_password|plain_password|default_password|hashed_password|"
    "password_hash|pin|new_pin|floor_pin|floor_pin_hash|secret|token|api_key"
)
_CREDENTIAL_KEY = r"(?<![A-Za-z0-9_])(?:[A-Za-z0-9]+_)*(?:" + _CREDENTIAL_KEYWORDS + r")"
# ``key=value``, ``key: value``, ``'key': 'value'``, ``"key": "value"`` -- the value quoted (to its
# closing quote, escapes included) or bare (to the next quote, comma, paren, brace or space).
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?P<key>" + _CREDENTIAL_KEY + r")(?P<kq>['\"]?)(?P<sep>\s*[=:]\s*)"
    r"(?P<value>b?'(?:[^'\\]|\\.)*'|b?\"(?:[^\"\\]|\\.)*\"|[^'\",)\s}\]]+)",
    re.IGNORECASE,
)
# A frame variable whose repr is a model / dataclass instance carrying such a field
# (``UserCreate(..., password='...')``): the whole variable goes.
_OBJECT_REPR = re.compile(r"^<?[A-Za-z_][\w.]*\(")
_CREDENTIAL_FIELD_IN_REPR = re.compile(r"[(,\s]" + _CREDENTIAL_KEY + r"\s*=", re.IGNORECASE)
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


def describe_exception(exc: BaseException) -> str:
    """An exception for a log line: its class and, for a database error, the driver error's class
    and SQLSTATE -- NEVER its text.

    A database error's text is not safe to log: Postgres's ``DETAIL: Failing row contains (...)``
    prints the whole row (on ``users``, ``hashed_password`` and ``floor_pin_hash``), and a driver
    without ``hide_parameters`` adds the bound values. Logged text reaches Sentry as a breadcrumb
    or an event, so the log line carries only what identifies the failure.
    """
    name = type(exc).__name__
    orig = getattr(exc, "orig", None)
    if orig is None:
        return name
    code = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
    detail = f"{name} [{type(orig).__name__}]"
    return f"{detail} (SQLSTATE {code})" if code else detail


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


def _mask_value(match: "re.Match[str]") -> str:
    value = match.group("value")
    prefix = "b" if value[:1] in ("b", "B") and value[1:2] in ("'", '"') else ""
    quote = value[len(prefix)] if value[len(prefix) : len(prefix) + 1] in ("'", '"') else ""
    return f"{match.group('key')}{match.group('kq')}{match.group('sep')}{prefix}{quote}{SENTRY_FILTERED}{quote}"


def scrub_text(text: str) -> str:
    """One string with every credential VALUE cut out: SQL parameters, a ``Failing row contains``
    row, bcrypt hashes, and ``key=value`` / ``"key": "value"`` for every credential key (the key
    is kept, so the text still says what was there)."""
    text = _SQL_PARAMETERS.sub("[parameters: " + SENTRY_FILTERED + "]", text)
    text = _FAILING_ROW.sub(r"\1(" + SENTRY_FILTERED + ")", text)
    text = _BCRYPT_HASH.sub(SENTRY_FILTERED, text)
    return _CREDENTIAL_ASSIGNMENT.sub(_mask_value, text)


def _scrub_strings(value: Any, whole_on_hash: bool = False) -> Any:
    """``value`` with every string run through ``scrub_text``, at any depth (iterative).

    ``whole_on_hash`` (frame variables) replaces a string holding a bcrypt hash entirely rather
    than cutting just the hash out of it.
    """

    def clean(text: str) -> str:
        if whole_on_hash and _BCRYPT_HASH.search(text):
            return SENTRY_FILTERED
        return scrub_text(text)

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


def _is_credential_bearing_repr(value: Any) -> bool:
    return isinstance(value, str) and bool(_OBJECT_REPR.match(value)) and bool(_CREDENTIAL_FIELD_IN_REPR.search(value))


def scrub_sentry_event(event: Dict[str, Any], hint: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """``before_send``: remove what the key-based ``EventScrubber`` cannot see (see above).

    For EVERY event:
      * frame variables -- drop the raw-body ones (``_BODY_FRAME_VARS``) and any whose repr is an
        object carrying a credential field; replace one holding a bcrypt hash with
        ``"[Filtered]"``; run ``scrub_text`` over every other string, at any depth;
      * exception values, the top-level message, ``logentry.message`` / ``formatted`` /
        ``params``, every breadcrumb's ``message`` and ``data``, the extras and the request
        data -- ``scrub_text`` over every string.
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
        for name in list(frame_vars):
            if name in _BODY_FRAME_VARS or _is_credential_bearing_repr(frame_vars[name]):
                del frame_vars[name]
        _scrub_strings(frame_vars, whole_on_hash=True)

    exception = event.get("exception")
    for value in (exception.get("values") if isinstance(exception, dict) else None) or ():
        if isinstance(value, dict) and isinstance(value.get("value"), str):
            value["value"] = scrub_text(value["value"])
    if isinstance(event.get("message"), str):
        event["message"] = scrub_text(event["message"])
    logentry = event.get("logentry")
    if isinstance(logentry, dict):
        for key in ("message", "formatted", "params"):
            if key in logentry:
                logentry[key] = _scrub_strings(logentry[key])
    breadcrumbs = event.get("breadcrumbs")
    crumbs = breadcrumbs.get("values") if isinstance(breadcrumbs, dict) else breadcrumbs
    for crumb in crumbs if isinstance(crumbs, list) else ():
        if isinstance(crumb, dict):
            for key in ("message", "data"):
                if key in crumb:
                    crumb[key] = _scrub_strings(crumb[key])

    if isinstance(request, dict):
        if credential_route:
            request.pop("data", None)
        elif "data" in request:
            request["data"] = _scrub_strings(request["data"])
    if "extra" in event:
        event["extra"] = _scrub_strings(event["extra"])
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
