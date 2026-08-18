import hmac
import re
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import (
    get_audit_service,
    get_current_company_id,
    get_current_user,
    oauth2_scheme,
    require_platform_admin,
    require_role,
)
from app.core.config import settings
from app.core.login_throttle import client_ip_from_request, employee_login_throttle, password_login_throttle
from app.core.security import (
    create_access_token,
    create_refresh_token,
    get_password_hash,
    verify_kiosk_token,
    verify_password,
    verify_refresh_token,
)
from app.db.database import get_db
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.kiosk_station import KioskStation
from app.models.user import User, UserRole
from app.schemas.display_token import (
    DisplayTokenClaimRequest,
    DisplayTokenClaimResponse,
    DisplayTokenCreate,
    DisplayTokenIssueResponse,
    DisplayTokenListResponse,
    DisplayTokenResponse,
    SetupCodeReissueResponse,
)
from app.schemas.kiosk_station import (
    KioskBadgeTokenRequest,
    KioskBadgeTokenResponse,
    KioskBadgeUser,
)
from app.schemas.user import (
    EmployeeLoginRequest,
    PublicRegister,
    RefreshTokenRequest,
    Token,
    TokenRefresh,
    UserCreate,
    UserResponse,
)
from app.services.audit_service import AuditService
from app.services.display_token_service import (
    claim_display_token,
    issue_display_token,
    list_display_tokens,
    reissue_setup_code,
    revoke_display_token,
)
from app.services.user_identity import (
    LEGACY_RESERVED_EMAIL_DOMAIN,
    SYNTHETIC_EMAIL_DOMAIN,
    IdentifierDerivationExhausted,
    employee_id_from_email,
    is_synthetic_email,
    synthetic_email_for_employee_id,
)

router = APIRouter()


# What ``audit_logs.resource_identifier`` can actually hold, READ OFF THE MODEL rather than
# repeated as a literal. Widening the column must widen this guard with it: a hard-coded
# 255 left behind by a migration would keep truncating values the column had grown to hold,
# and nothing would fail -- the rows would just quietly stop naming what was tried.
# ``or 255`` is a floor for a dialect that reports no length, never the source of truth.
_AUDIT_IDENTIFIER_MAX_LENGTH = AuditLog.__table__.c.resource_identifier.type.length or 255

# Appended to anything this module shortens, and it is kept INSIDE the limit so the marker
# itself can never be what overruns the column. Same marker /auth/login's over-length shape
# refusal already writes, so the two truncations read alike to whoever queries the table.
_AUDIT_TRUNCATION_MARKER = "…[truncated]"


def _bounded_audit_value(value: str) -> str:
    """Shorten ``value`` to what ``audit_logs.resource_identifier`` holds, marked as cut.

    A value that already fits comes back BYTE-IDENTICAL -- every existing audit row keeps
    the identifier it had, and only a genuinely over-long one changes shape.

    Why this is central rather than at the ~15 call sites: a call site knows what it is
    logging, never how wide the column is, and it cannot see the ``employee-id:`` prefix
    :func:`log_auth_event` will add. So the bound has to be applied to the FINAL composed
    value, here. On Postgres the failure mode this prevents is not cosmetic: an over-long
    INSERT raises ``StringDataRightTruncation`` -> ``DataError``, which both
    ``AuditService.log`` and this module's ``except Exception`` swallow -- and the poisoned
    session then fails the caller's ``db.commit()``, so the attempt loses its audit row AND
    the request 500s. SQLite ignores VARCHAR widths, so no test on this suite can see it.
    """
    if len(value) <= _AUDIT_IDENTIFIER_MAX_LENGTH:
        return value
    keep = _AUDIT_IDENTIFIER_MAX_LENGTH - len(_AUDIT_TRUNCATION_MARKER)
    if keep <= 0:  # pragma: no cover - only reachable if the column is narrowed below 12
        return value[:_AUDIT_IDENTIFIER_MAX_LENGTH]
    return value[:keep] + _AUDIT_TRUNCATION_MARKER


def log_auth_event(
    db: Session,
    action: str,
    user: User = None,
    email: str = None,
    success: bool = True,
    request: Request = None,
    error: str = None,
    employee_id: str = None,
):
    """Log authentication events for CMMC compliance using AuditService.

    ``employee_id`` exists because a badge-only registrant has no address to key on, and
    the badge must NOT be smuggled through ``email=``: that lands it in ``extra_data``
    under a key named "email", and an audit row can never be corrected afterwards -- the
    008/060 triggers refuse UPDATE and DELETE, and invariant 2 forbids backfilling. So a
    consumer filtering ``extra_data->>'email'`` for addresses would silently drop every
    badge-only row, which are exactly the rows the anti-enumeration design relies on
    being visible. Pass identifiers under their own names; the ``employee-id:`` prefix on
    ``resource_identifier`` keeps a badge from being read back as an address.

    EVERY value this writes is bounded here (``_bounded_audit_value``), on the COMPOSED
    string rather than at the caller: the ``employee-id:`` prefix is 12 characters a call
    site cannot account for, so a badge that /auth/login legitimately accepts at its own
    255-character bound still overruns ``audit_logs.resource_identifier`` once prefixed --
    and the 429 throttle branch logs the submitted badge BEFORE that bound has even run.
    The ``extra_data`` copies are bounded too, so the row can never be a storage-
    amplification vector by the back door.
    """
    try:
        raw_identifier = (
            email or (f"employee-id:{employee_id}" if employee_id else None) or (user.email if user else None)
        )
        resource_identifier = _bounded_audit_value(raw_identifier) if raw_identifier else None
        identifiers = {}
        if email and not user:
            identifiers["email"] = _bounded_audit_value(email)
        if employee_id:
            identifiers["employee_id"] = _bounded_audit_value(employee_id)
        audit_service = AuditService(db, user, request)
        audit_service.log(
            action=action,
            resource_type="authentication",
            resource_id=user.id if user else None,
            resource_identifier=resource_identifier,
            description=f"{action} attempt for {resource_identifier or 'unknown'}",
            success=success,
            error_message=error,
            extra_data=identifiers or None,
        )
    except Exception as e:
        # Don't let audit logging failures break authentication
        import logging

        logging.warning(f"Failed to log auth event: {e}")


# Longest ``form_data.username`` ``/auth/login`` will look at. This is the ``users.email``
# column width (``String(255)``, models/user.py) and is chosen as the bound BECAUSE it is:
# email is the widest identifier column an account can hold (``employee_id`` is
# ``String(50)``), so nothing longer can match a stored row on either resolver. The check
# therefore costs no legitimate sign-in while keeping an unauthenticated, form-encoded
# route -- the one endpoint ``limit_json_body_size`` does not cover -- from choosing how
# many bytes the app lowercases, regex-scans and binds into a query. See the call site.
MAX_LOGIN_IDENTIFIER_LENGTH = 255


@router.post("/login", response_model=Token, summary="User login")
def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """
    Authenticate a user and receive JWT access and refresh tokens.

    **Rate limited**: 5 attempts per minute

    **Account lockout**: After 5 failed attempts, account is locked for 30 minutes (CMMC compliance).

    **Request body** (form data):
    - username: the user's email address, OR their employee ID / badge number
    - password: User's password

    **Which identifier was submitted is decided by "@"** -- an email address always
    contains one and an employee ID (letters, digits, hyphen, underscore) never can, so
    the split is deterministic and there is no fallback between the two lookups. Both
    lookups are install-wide and both refuse a 409 rather than guess when the identifier
    resolves to accounts in more than one company. The password is verified either way.
    ``POST /auth/employee-login`` remains the separate passwordless badge route and is
    unchanged by this -- nobody loses a way in.

    **Returns**:
    - access_token: JWT token for API authorization (valid for 15 minutes — ACCESS_TOKEN_EXPIRE_MINUTES)
    - refresh_token: Token to obtain new access tokens (valid for 7 days)
    - token_type: Always "bearer"
    - expires_in: Token expiration time in seconds

    **Raises**:
    - 401: Invalid credentials
    - 403: Account locked or inactive
    - 409: The identifier exists in more than one company (see ``_find_user_by_auth_email``
      and ``_find_user_by_employee_id``)
    - 429: Too many failed attempts from this IP, for ENUMERABLE identifiers only — a
      badge, or a synthetic ``emp-…@users.werco.com`` / ``@werco.local`` address (which is
      derived from a badge and therefore just as enumerable)
    """
    submitted_identifier = form_data.username or ""

    # DETERMINISTIC SPLIT, NO FALLBACK. An email address always contains "@", and the
    # employee_id pattern every write path enforces (``^[A-Za-z0-9\-_]+$`` on
    # PublicRegister / UserCreate, schemas/user.py) can never contain one -- so the two
    # identifier spaces do not overlap and one input has exactly one resolver.
    #
    # Do NOT add a fallback from one resolver to the other. Two lookups per attempt is
    # two ways to fail, which rebuilds the account-existence oracle both resolvers are
    # written to avoid; and both are deliberately install-wide (an unauthenticated caller
    # has no company yet), so a fallback could resolve a badge onto a stranger's row in
    # another tenant. Both keep their one-or-refuse 409 for the same reason.
    is_email_login = "@" in submitted_identifier

    # Which identifier this attempt is RECORDED under. A badge travels under
    # ``employee_id=`` and never through ``email=``, so it is keyed as
    # ``employee-id:<badge>`` and can never be read back as an address (see
    # log_auth_event). On the email path both values are exactly what they were before
    # this route learned about badges -- the submitted address, and None -- which is what
    # keeps every audit row on that path byte-identical.
    submitted_email: Optional[str] = submitted_identifier if is_email_login else None
    submitted_badge: Optional[str] = None if is_email_login else submitted_identifier

    # ONE generic 401 for the whole path, used for "no such account" AND "wrong password"
    # alike. The badge wording is a PARALLEL of the email one, never a more specific one:
    # distinguishing the two would answer "does this badge exist?" for a caller who
    # cannot supply the password, and the badge keyspace is small enough to sweep.
    invalid_credentials_detail = "Invalid email or password" if is_email_login else "Invalid employee ID or password"

    # THROTTLED WHEN THE SUBMITTED IDENTIFIER LIES IN AN ENUMERABLE SPACE. Same mechanism
    # /auth/employee-login runs (app/core/login_throttle.py), on this route's OWN counter
    # and OWN budget: ``password_login_throttle``, key prefix ``auth:login:failed``,
    # 60 failures / 6 h -> 429. It is needed here even though this route verifies a
    # password, because this route DRIVES THE ACCOUNT LOCKOUT (5 failures -> 30 minutes),
    # a lock that also blocks /auth/employee-login -- so sweeping identifiers here can
    # take people off the kiosk. The 5/min slowapi limit alone does not bound that.
    #
    # A SEPARATE COUNTER FROM THE KIOSK'S, and that separation is load-bearing rather
    # than tidy. This route counts WRONG-PASSWORD failures, an outcome the passwordless
    # badge route cannot produce, and the kiosk budget of 8 is sized on the premise that
    # a failure is an UNKNOWN id rather than a slow scan. Sharing one counter meant
    # ordinary password typos drained the kiosk's budget, and an empty budget 429s BADGE
    # SIGN-IN for every operator behind that egress IP for the cooldown, with no admin
    # reset -- and the login screen actively steers badge-only operators onto this path,
    # so both routes see the same people from the same IP all day. The budgets differ for
    # the same reason: one legitimate user can spend 5 failures here before their own
    # lockout stops them, so 8 cannot absorb two of them. See login_throttle.py for the
    # derivation of the 60 / 6 h figures from both directions.
    #
    # THE LINE IS "ENUMERABLE OR NOT", NOT "BADGE OR EMAIL" -- and getting that wrong is
    # how the control was bypassable. This route's earlier rationale said the email path
    # needs no throttle because "an address space is not enumerable the way a four-digit
    # badge is". That is false for exactly the population the throttle protects: a
    # badge-only or CSV-imported user HAS no real address, so the system minted one FROM
    # the badge -- ``emp-<sanitized-badge>@users.werco.com`` (services/user_identity) --
    # and ``_find_user_by_auth_email`` resolves it exactly. So emp-0000@… through
    # emp-9999@… reaches the same accounts, drives the same lockout, and under the old
    # rule was not throttled at all: the attacker just types an "@".
    #
    # Two enumerable spaces, therefore, and both are throttled:
    #   * a badge -- ``_normalize_employee_id`` collapses input to 4 trailing digits, a
    #     ~10^4 keyspace;
    #   * an address on a domain THIS SYSTEM MINTS (``is_synthetic_email``: @users.werco.com
    #     and the legacy @werco.local, which the same local part maps onto), because its
    #     local part is a function of the badge.
    #
    # An ordinary address at a real domain stays UNTHROTTLED, deliberately: that space
    # genuinely is not enumerable, and counting it would let one person mistyping their
    # password on a shared office NAT take their whole floor's login offline for fifteen
    # minutes -- a self-inflicted outage that buys nothing.
    #
    # Decided from the SUBMITTED STRING ALONE (no DB read), which is what keeps the check
    # ABOVE the user lookup: a throttled IP must do zero account probing, so the throttle
    # cannot be allowed to depend on what the lookup would have found.
    #
    # This BOUNDS the lockout DoS, it does not eliminate it (noted, not fixed here):
    # the throttle keys on the client IP, and start.sh runs uvicorn with
    # ``--forwarded-allow-ips=*``, so a caller can supply the forwarded IP the limiter
    # keys on and rotate it. Every rate limit in the app shares that weakness -- it is an
    # owner fix on the deployment flags. Do NOT work around it with home-grown
    # X-Forwarded-For parsing here; that would only make this one control key differently
    # from every other limit (see client_ip_from_request).
    is_enumerable_identifier = (not is_email_login) or is_synthetic_email(submitted_identifier)

    client_ip = client_ip_from_request(request)
    if is_enumerable_identifier:
        retry_after = password_login_throttle.blocked_retry_after(client_ip)
        if retry_after is not None:
            log_auth_event(
                db,
                "LOGIN_BLOCKED",
                email=submitted_email,
                employee_id=submitted_badge,
                success=False,
                request=request,
                error="Throttled: too many failed attempts from this address",
            )
            db.commit()
            # The wait is stated from the ACTUAL remaining cooldown, not as "a few minutes".
            # Login.tsx renders this ``detail`` verbatim, and this route's block runs for an
            # hour (PASSWORD_LOGIN_COOLDOWN_SECONDS) -- telling an operator "a few minutes"
            # sends them back to the form every few minutes for an hour and reads as the app
            # being broken. The kiosk twin keeps its own wording; its cooldown really is 15
            # minutes. Derived from retry_after so the copy cannot drift from the constant.
            wait_minutes = max(1, round(retry_after / 60))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    "Too many failed sign-in attempts from this network — "
                    f"try again in about {wait_minutes} minute{'s' if wait_minutes != 1 else ''}. "
                    "Badge sign-in at the kiosk is unaffected."
                ),
                headers={"Retry-After": str(retry_after)},
            )

    def _count_enumerable_failure() -> None:
        """Record one FAILED attempt on an enumerable identifier against this IP.

        Counted on THIS route's throttle only (``password_login_throttle``). A failure
        here must never be spendable against /auth/employee-login's budget -- that is
        the kiosk outage described above.

        Successful logins are never counted -- mirroring employee_login, so a shift
        change that cycles badges correctly stays fast -- and an ordinary address at a
        real domain never counts at all, on either outcome.
        """
        if is_enumerable_identifier:
            password_login_throttle.register_failure(client_ip)

    # REFUSED ON SHAPE TOO, and it is the SAME argument as the over-length refusal below.
    # A blank ``username`` contains no "@", so it takes the badge branch and
    # ``is_enumerable_identifier`` is True -- which meant a blank submission walked all the
    # way to "user not found" and SPENT one unit of the per-IP password-login budget. Both
    # resolvers return ``None`` on a blank string before touching the database, so the
    # attempt establishes nothing about any account; letting it drain the budget shared
    # with everyone behind the same NAT would hand an attacker a cheaper route to the
    # lockout that budget exists to bound than the sweep itself.
    #
    # WHITESPACE-ONLY is the reachable half and the reason this tests ``.strip()`` rather
    # than truthiness. A genuinely EMPTY value never arrives: FastAPI treats ``""`` on a
    # required ``Form`` field as missing and answers 422 above the handler, so it cannot
    # charge the throttle either. ``"  "`` is a present, non-empty form value that reaches
    # this line. Both are covered here so the behavior does not silently depend on that
    # framework detail.
    #
    # Refused BELOW the 429 check and ABOVE ``_count_enumerable_failure``: a throttled IP
    # is still refused first, and this outcome never charges the counter. Same generic
    # 401 as every other failure here -- a distinct "identifier required" would be a new
    # distinguishable outcome on an unauthenticated route, and a trivially cheap one to
    # probe. No identifier is passed to the audit row because there is none: the row
    # records that a blank submission was refused, not a value.
    if not submitted_identifier.strip():
        log_auth_event(
            db,
            "LOGIN_FAILED",
            success=False,
            request=request,
            error="Empty identifier; refused before lookup",
        )
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=invalid_credentials_detail)

    # REFUSED ON SHAPE, BEFORE EITHER LOOKUP. ``limit_json_body_size`` (app/main.py)
    # gates ``application/json`` only, and this route is form-encoded -- so the 256 KB
    # body cap does not apply to the one unauthenticated FORM endpoint, and an arbitrarily
    # long username reaches the handler. Unbounded it is lowercased, regex-scanned by
    # ``_normalize_employee_id``, and handed to Postgres as a bind parameter compared
    # against ``users.employee_id`` (String(50)) or ``users.email`` (String(255)).
    #
    # THE BOUND IS THE ``users.email`` COLUMN WIDTH, 255. It is the widest identifier
    # column any account can hold, so a longer submission provably cannot match a stored
    # row on either resolver -- rejecting it discards nothing a lookup could have found,
    # which is what makes refusing here safe rather than merely convenient. (It is NOT what
    # keeps the audit write safe, despite matching ``audit_logs.resource_identifier``'s
    # width: the ``employee-id:`` prefix pushes an identifier at this bound 12 characters
    # PAST the column, and the 429 branch above logs before this check runs at all. The
    # audit column is bounded where the row is composed -- ``_bounded_audit_value``.)
    #
    # Same generic 401 as every other failure on this route, deliberately: an explicit
    # "too long" error would add a new distinguishable outcome on an unauthenticated
    # route, and this one is cheap for an attacker to probe.
    #
    # NOT counted against the throttle. It establishes nothing about any account, so
    # letting malformed input drain the budget shared with legitimate users behind the
    # same NAT would hand an attacker a cheaper lockout than the one being bounded. The
    # throttle's 429 still runs FIRST (above), so a throttled IP is refused before this.
    if len(submitted_identifier) > MAX_LOGIN_IDENTIFIER_LENGTH:
        log_auth_event(
            db,
            "LOGIN_FAILED",
            # Truncated so the audit row cannot be a storage-amplification vector, and
            # marked as truncated so the row never reads as the whole submitted value.
            # ``_AUDIT_TRUNCATION_MARKER``, not a re-spelling of it. That constant's own
            # comment claims this site writes the same string log_auth_event writes, and
            # two literals are not one source -- an edit to the constant that missed these
            # lines would silently split one truncation convention across two spellings on
            # rows 008/060 refuse to UPDATE or DELETE.
            email=(submitted_identifier[:64] + _AUDIT_TRUNCATION_MARKER) if is_email_login else None,
            employee_id=None if is_email_login else (submitted_identifier[:64] + _AUDIT_TRUNCATION_MARKER),
            success=False,
            request=request,
            error=f"Identifier exceeds {MAX_LOGIN_IDENTIFIER_LENGTH} characters; refused before lookup",
        )
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=invalid_credentials_detail)

    try:
        if is_email_login:
            user = _find_user_by_auth_email(db, form_data.username)
        else:
            # Reused AS IS, normalization fallback included, so "0339" means the same
            # person here as it does on /auth/employee-login.
            user = _find_user_by_employee_id(db, form_data.username)
    except HTTPException as exc:
        # The lookup refused rather than answered. Refuse instead of authenticating an
        # arbitrary account, and leave a trail an admin can act on — nothing else in the
        # system reports either cause, and the affected users only see a login that
        # stopped working.
        #
        # The status is checked, not assumed. The 409 is the only HTTPException
        # the lookup can raise today, but writing a specific cause onto the
        # tamper-evident chain for whatever a future edit happens to raise
        # would make the audit row a lie.
        #
        # AND WHICH 409 IT WAS COMES FROM THE EXCEPTION, NOT THE STATUS CODE. The badge
        # resolver raises 409 for two different facts — an established duplicate, and a
        # candidate window that TRUNCATED so uniqueness was never established — and this
        # handler used to hard-code "resolves to more than one account" for both. On a
        # truncation that sentence states something no query checked, permanently
        # (008/060 refuse UPDATE/DELETE), and sends an admin hunting a duplicate that may
        # not exist. ``_conflict_audit_error`` reads the cause structurally; it must not
        # be "simplified" into a test on ``exc.detail``, which is UI copy.
        #
        # No throttle failure is registered here: an ambiguous identifier is an admin
        # data problem, not a wrong guess, and the account provably EXISTS -- counting it
        # would let one duplicate row lock an IP out of a login that is failing through
        # no fault of the person typing. /auth/employee-login treats its own 409 the
        # same way.
        if exc.status_code == status.HTTP_409_CONFLICT:
            log_auth_event(
                db,
                "LOGIN_BLOCKED",
                email=submitted_email,
                employee_id=submitted_badge,
                success=False,
                request=request,
                error=_conflict_audit_error(exc),
            )
            db.commit()
        raise

    if not user:
        # Log the audit row, then commit so it persists before raising
        # (the audit row is only flushed by AuditService; get_db never commits).
        _count_enumerable_failure()
        log_auth_event(
            db,
            "LOGIN_FAILED",
            email=submitted_email,
            employee_id=submitted_badge,
            success=False,
            request=request,
            error="User not found",
        )
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=invalid_credentials_detail)

    # Check if account is locked (CMMC requirement). ``employee_id`` is passed on every
    # row below for the same reason it is passed above: on the badge path the resolved
    # user's stored address may be a MINTED emp-...@users.werco.com, so keying these rows
    # on user.email alone would leave a badge's failed attempts and its lockout sharing no
    # identifier an operator could query. On the email path it is None -- unchanged.
    if user.locked_until and user.locked_until > datetime.utcnow():
        # DELIBERATELY NOT COUNTED against the per-IP budget, unlike every other failure
        # branch on this route. The account is ALREADY LOCKED: a further attempt against it
        # establishes nothing new (the password is not even checked) and gains an attacker
        # nothing they did not already have from the five attempts that caused the lock. So
        # the only population it can charge is the legitimate one -- the person retrying
        # their own locked account during the 30-minute window, who blows straight past the
        # 5-failures-per-user figure the budget is sized on and can take password sign-in
        # down for everyone behind the same NAT. That is a self-inflicted outage bought
        # with no security. (The 429 check above still runs first, so an already-throttled
        # IP is refused before reaching here.)
        log_auth_event(
            db,
            "LOGIN_BLOCKED",
            user=user,
            employee_id=submitted_badge,
            success=False,
            request=request,
            error="Account locked",
        )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Account is locked. Please contact administrator."
        )

    if not verify_password(form_data.password, user.hashed_password):
        # Increment failed attempts
        user.failed_login_attempts += 1

        # Lock account after 5 failed attempts (CMMC requirement)
        if user.failed_login_attempts >= 5:
            user.locked_until = datetime.utcnow() + timedelta(minutes=30)

        # Log BEFORE the terminal commit so the audit row commits atomically
        # with the failed-attempt increment.
        _count_enumerable_failure()
        log_auth_event(
            db,
            "LOGIN_FAILED",
            user=user,
            employee_id=submitted_badge,
            success=False,
            request=request,
            error="Invalid password",
        )
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=invalid_credentials_detail)

    if not user.is_active:
        _count_enumerable_failure()
        log_auth_event(
            db,
            "LOGIN_FAILED",
            user=user,
            employee_id=submitted_badge,
            success=False,
            request=request,
            error="Account disabled",
        )
        db.commit()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account is disabled")

    # Reset failed attempts on successful login
    user.failed_login_attempts = 0
    user.locked_until = None

    # Repair any legacy reserved-domain email BEFORE the terminal commit so the
    # email change, the reset of failed attempts, and the audit row all commit
    # atomically (AuditService only flushes; get_db never commits).
    _ensure_valid_auth_email(user, db)

    # Log BEFORE the terminal commit so the LOGIN_SUCCESS audit row is persisted.
    # A success never touches the throttle (only failures are counted).
    log_auth_event(db, "LOGIN_SUCCESS", user=user, employee_id=submitted_badge, success=True, request=request)
    db.commit()
    db.refresh(user)

    # Create access token (short-lived) with company context
    access_token = create_access_token(subject=user.id, company_id=user.company_id)

    # Create refresh token (longer-lived, with rotation)
    refresh_token, session_id, _ = create_refresh_token(subject=user.id, company_id=user.company_id)

    return Token(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=UserResponse.model_validate(user),
    )


def _normalize_employee_id(value: str) -> Optional[str]:
    """Normalize employee_id to a 4-digit numeric string."""
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if not digits:
        return None
    if len(digits) < 4:
        return digits.zfill(4)
    return digits[-4:]


_AMBIGUOUS_EMAIL_DETAIL = "Email is not unique. Please contact an administrator."

# The badge equivalent, hoisted out of the four raise sites that used to spell it inline so
# the two resolvers below cannot drift apart on wording.
_AMBIGUOUS_EMPLOYEE_ID_DETAIL = "Employee ID is not unique. Please contact an administrator."

# Raised instead when the candidate set was TRUNCATED, so "how many rows normalize to this
# badge" has no answer. Deliberately its own wording: "not unique" would be a claim the
# query never established, and the admin's remediation differs (a duplicate to merge vs. a
# user table this lookup can no longer scan). Same 409 status, and it discloses nothing
# about whether any account exists -- neither message ever does.
_UNRESOLVABLE_EMPLOYEE_ID_DETAIL = "Employee ID could not be resolved. Please contact an administrator."

# The ``error`` each 409 CAUSE earns on its audit row. Deliberately separate strings from
# the ``detail`` constants above: ``detail`` is human-readable UI copy that anyone may
# reword, while these land on rows migrations 008/060 refuse to UPDATE or DELETE and
# invariant 2 forbids backfilling. Coupling the permanent fact to the mutable copy -- by
# sniffing ``exc.detail`` to decide which one to write -- would make a wording edit
# silently start recording the wrong cause forever.
_AUDIT_ERROR_EMAIL_AMBIGUOUS = "Email resolves to more than one account"
_AUDIT_ERROR_EMPLOYEE_ID_AMBIGUOUS = "Employee ID resolves to more than one account"

# NOT "resolves to more than one account". A truncated candidate window means the query
# REFUSED TO ANSWER, so nothing at all was established about uniqueness -- and the
# admin's remediation differs (a user table this lookup can no longer scan, versus a
# duplicate to merge). Same line the registration path already draws with
# ``_REJECTED_EMPLOYEE_ID_UNRESOLVABLE``.
_AUDIT_ERROR_EMPLOYEE_ID_UNRESOLVABLE = (
    "Employee ID could not be resolved: the candidate window truncated, so uniqueness was "
    "never established. Sign-in was refused rather than guessed at."
)

# For a 409 that is not one of the two classified causes below. Unreachable today; it
# exists so a future edit that raises a bare 409 from a resolver records "we do not know
# why" instead of inheriting whichever specific claim happened to be written first.
_AUDIT_ERROR_CONFLICT_UNCLASSIFIED = "Identifier could not be resolved: unclassified conflict from the resolver"


class _IdentifierConflict(HTTPException):
    """A 409 from an identifier resolver, carrying the audit sentence its cause earns.

    The cause travels as a TYPE plus a structural attribute, never as the ``detail``
    string. Both resolvers now raise 409 for two genuinely different reasons -- an
    established duplicate, and a candidate window that truncated -- and the handlers have
    to tell them apart to write an honest audit row. The status code cannot do it (both
    are 409) and the detail string must not (see the constants above).
    """

    def __init__(self, detail: str, audit_error: str) -> None:
        super().__init__(status_code=status.HTTP_409_CONFLICT, detail=detail)
        self.audit_error = audit_error


class _AmbiguousIdentifier(_IdentifierConflict):
    """More than one account matched: uniqueness was established, and violated."""


class _UnresolvableIdentifier(_IdentifierConflict):
    """The candidate window truncated: uniqueness was never established at all."""


def _conflict_audit_error(exc: HTTPException) -> str:
    """The permanent audit sentence for a 409 raised by an identifier resolver.

    Structural on purpose (``isinstance``), not a string match on ``exc.detail``.
    """
    if isinstance(exc, _IdentifierConflict):
        return exc.audit_error
    return _AUDIT_ERROR_CONFLICT_UNCLASSIFIED


# How many rows the normalized-badge fallback will look at before it refuses to answer.
#
# TWO SEPARATE JOBS, and they are easy to conflate into a bug:
#   * the LIMIT bounds a runaway query -- ``core_digits`` can be a single character, and
#     ``ilike('%1%')`` matches a large fraction of a real user table;
#   * the 409 above exists so truncation can never masquerade as a unique answer. Without
#     it, a genuine duplicate sitting outside the window returns an arbitrary single match
#     instead of refusing, and a genuine match outside the window reads as "not found" --
#     on a password-verifying, lockout-DRIVING path, where resolving onto the wrong row
#     increments a STRANGER'S failed-attempt counter and can lock a stranger's account in
#     another tenant.
#
# The NUMBER is chosen so the second is unreachable in normal operation. It cannot be small:
# with a single-digit core a real shop's table plausibly exceeds 50 matches, so a cap of 50
# plus a 409 would turn ordinary badge logins into a floor-wide outage on /auth/login and
# /auth/employee-login -- strictly worse than the bug being fixed. 500 cannot realistically
# truncate a single-tenant shop's user table (the global resolver is install-wide, so it is
# every tenant's users together, and this is still an order of magnitude clear), which
# leaves the 409 firing only on a genuinely pathological set. Raise it before shrinking it.
_EMPLOYEE_ID_CANDIDATE_CAP = 500


def _one_user_for_email_or_refuse(db: Session, normalized_email: str) -> Optional[User]:
    """Resolve an already-lowercased address to exactly ONE user, or refuse.

    ``limit(2)`` is all it takes to tell "one" from "more than one" without
    loading a whole duplicate set; ``order_by(User.id)`` makes the single-match
    case deterministic too, so the query plan can never change which row a
    given address resolves to.

    Refusal is a 409 with the same shape and wording as the badge equivalent
    (``_find_user_by_employee_id``): an admin data problem, stated plainly, not
    an auth probe.
    """
    matches = db.query(User).filter(func.lower(User.email) == normalized_email).order_by(User.id).limit(2).all()
    if len(matches) > 1:
        raise _AmbiguousIdentifier(_AMBIGUOUS_EMAIL_DETAIL, _AUDIT_ERROR_EMAIL_AMBIGUOUS)
    return matches[0] if matches else None


def _find_user_by_auth_email(db: Session, email: str) -> Optional[User]:
    """
    Find a user for email login using a case-insensitive lookup.

    Email is unique PER COMPANY, never globally: the constraint is
    ``uq_users_company_email`` (app/models/user.py), and every creation and
    rename path scopes its duplicate check to the company — admin ``register``
    below, ``POST /users/``, the user CSV importer, and ``PUT /users/{id}``.
    Two tenants may therefore legitimately hold the same address. This lookup,
    by contrast, has no company to scope to: the caller is unauthenticated and
    the company context is derived FROM the row this returns.

    That mismatch used to end in ``.first()`` — an unordered, unscoped "pick
    one". Which tenant's account an ambiguous address authenticated as was
    decided by row order, so the same credentials could resolve differently
    across restarts or plan changes, and a legitimate user whose row was not
    the one picked failed their own password against a stranger's hash and
    incremented THAT stranger's lockout counter.

    Resolution is now all-or-nothing: exactly one match logs in, two or more
    refuse with a 409. Refusing beats guessing because no guess can be made
    correct without a tenant discriminator on the login form (a company
    slug/subdomain), and adding one is a product change, not a bug fix. See
    the caller for the audit trail this leaves.

    Legacy imports may still have `@werco.local` stored until first successful
    login repair. Allow the repaired `@users.werco.com` address to find the
    legacy record so the repair path can complete — the legacy probe is held
    to the same one-or-refuse rule.
    """
    normalized_email = (email or "").strip().lower()
    if not normalized_email:
        return None

    user = _one_user_for_email_or_refuse(db, normalized_email)
    if user:
        return user

    # READ FROM services/user_identity, NEVER SPELLED INLINE. The /auth/login throttle
    # decides whether an identifier is ENUMERABLE by asking ``is_synthetic_email``, which
    # derives its answer from these same two constants. That control is correct only while
    # the set of addresses THIS RESOLVER CAN REACH and the set of placeholder domains agree
    # exactly: an address shape the resolver resolves but ``is_synthetic_email`` does not
    # recognise is a reachable, badge-derived, fully enumerable identifier that the
    # throttle would not count -- i.e. the bypass this pairing exists to close. Two string
    # literals that happen to be spelled the same are not agreement; one source is.
    if normalized_email.endswith(f"@{SYNTHETIC_EMAIL_DOMAIN}"):
        local_part = normalized_email.removesuffix(f"@{SYNTHETIC_EMAIL_DOMAIN}")
        return _one_user_for_email_or_refuse(db, f"{local_part}@{LEGACY_RESERVED_EMAIL_DOMAIN}")

    return None


def _build_repaired_email(user: User, db: Session) -> str:
    """Generate a valid non-reserved email for legacy .local imports.

    The shape lives in ``services/user_identity`` (one shape, three seams); what this
    seam owns is the dedup SCOPE. The probe is install-wide AND treats the user's OWN
    row as free -- a repair that has already run once must be able to converge on the
    address the row is holding instead of walking the suffix forever.
    """

    def _taken_by_someone_else(candidate: str) -> bool:
        existing = db.query(User).filter(func.lower(User.email) == candidate.lower()).first()
        return existing is not None and existing.id != user.id

    return synthetic_email_for_employee_id(user.employee_id or "", _taken_by_someone_else)


def _ensure_valid_auth_email(user: User, db: Session) -> None:
    """
    Patch legacy reserved-domain addresses so token response validation does not crash.
    This keeps logins working for users imported before the email-domain fix.

    **THE REPAIR IS OPPORTUNISTIC, NOT REQUIRED, AND IT MUST NEVER FAIL A SIGN-IN.**
    ``synthetic_email_for_employee_id`` is a bounded walk that raises
    ``IdentifierDerivationExhausted`` when no candidate is free, and this is the worst of
    the three seams that call one: it runs on the LOGIN path, after the password has
    already been verified, so an uncaught exception here is a 500 on sign-in for a legacy
    ``@werco.local`` user -- and it is raised BEFORE the terminal commit, so the
    failed-attempt reset and the LOGIN_SUCCESS audit row are lost with it. Caught, the
    login proceeds on the address the row already holds: nothing is worse than it was
    before the repair was attempted, the counters reset, and the audit row is written.

    RESIDUAL, stated rather than papered over: ``UserResponse.email`` is an ``EmailStr``
    and pydantic rejects ``@werco.local`` as a reserved special-use domain, so an
    unrepaired row will still fail response validation. That is a property of the response
    schema, not of this function, and it is not this seam's to fix by inventing an address
    the probe just refused -- which would land a real operator on someone else's address.
    What this catch buys is that the attempt is recorded and the account state is correct;
    the fix for a genuinely unrepairable row is an admin editing it.

    Mutates ``user.email`` in place but does NOT commit. The caller is responsible
    for committing so the email repair commits atomically with the rest of the
    login transaction (including the audit row).

    The domain is read from ``services/user_identity``, not spelled here, for the reason
    given on ``_find_user_by_auth_email``: the /auth/login throttle's enumerability test
    derives the same two domains from those constants via ``is_synthetic_email``, and its
    coverage is correct only while the resolver's reachable address set and the
    placeholder-domain set agree. They must read from one place, not agree by coincidence
    of spelling -- an edit to one literal that missed the other would reopen the bypass
    silently.
    """
    email = (user.email or "").strip()
    if not email.lower().endswith(f"@{LEGACY_RESERVED_EMAIL_DOMAIN}"):
        return

    try:
        user.email = _build_repaired_email(user, db)
    except IdentifierDerivationExhausted:
        # Leave the address alone and let the login continue (see the docstring). Logged
        # at WARNING because it needs an admin: every spelling the mint can reach is held
        # by another account, which is a data problem no retry resolves. No identifier is
        # put in the message -- it can reach an exception log, and these are credentials-
        # adjacent values.
        import logging

        logging.getLogger(__name__).warning(
            "Legacy reserved-domain email left unrepaired for user id=%s: no free synthetic address", user.id
        )


def _normalized_employee_id_matches(db: Session, normalized_input: str, company_id: Optional[int] = None) -> List[User]:
    """Rows whose ``employee_id`` normalizes to ``normalized_input`` — or refuse 409.

    THE ONE implementation of the normalized-badge fallback, shared by the install-wide
    resolver and its company-scoped kiosk twin (and by public registration's collision
    probe) so the cap, the ordering and the truncation refusal cannot drift between them.
    ``company_id`` is the only difference: ``None`` searches every tenant, an int fences
    the query to one.

    Narrows in SQL first so a login never loads the whole user table. ``normalized_input``
    is always four zero-padded trailing digits; a stored ``"339"`` / ``"0339"`` /
    ``"EMP-00339"`` can all normalize to the same value, so the probe uses the digit-core
    with leading zeros stripped (for the degenerate ``"0000"`` it matches any row whose
    badge contains a zero) and the exact comparison is redone in Python.

    ``order_by(User.id)`` is not cosmetic. Without it the window this query returns is
    whatever the plan happens to produce, so WHICH rows a badge is resolved against could
    change between two identical requests — and on this path that decides whose lockout
    counter moves. Ordered, the window is stable and the truncation check below means an
    incomplete window is refused rather than answered from.

    READ IN TWO PHASES, and the split is what keeps the cost off the badge path: the
    window is up to ``_EMPLOYEE_ID_CANDIDATE_CAP + 1`` rows wide and every row in it is
    discarded except the handful that normalize to the submitted badge, so the wide read
    selects TWO COLUMNS rather than hydrating ~501 mapped ``User`` entities (plus their
    identity-map bookkeeping) to compare one string — on the query the crew station runs
    on every badge scan. The entities are then loaded only for the ids that actually
    matched: normally one, never more than the caller is about to refuse as ambiguous.
    """
    core_digits = normalized_input.lstrip("0") or normalized_input[-1:]
    query = db.query(User.id, User.employee_id).filter(
        User.employee_id.isnot(None),
        User.employee_id.ilike(f"%{core_digits}%"),
    )
    if company_id is not None:
        query = query.filter(User.company_id == company_id)

    # cap + 1: one row past the cap is how truncation is DETECTED rather than assumed.
    candidates = query.order_by(User.id).limit(_EMPLOYEE_ID_CANDIDATE_CAP + 1).all()
    if len(candidates) > _EMPLOYEE_ID_CANDIDATE_CAP:
        raise _UnresolvableIdentifier(_UNRESOLVABLE_EMPLOYEE_ID_DETAIL, _AUDIT_ERROR_EMPLOYEE_ID_UNRESOLVABLE)

    matched_ids = [row.id for row in candidates if _normalize_employee_id(row.employee_id) == normalized_input]
    if not matched_ids:
        return []
    # Re-ordered by id so the returned list keeps the determinism the window above
    # establishes — an ``IN`` clause promises no order of its own.
    return db.query(User).filter(User.id.in_(matched_ids)).order_by(User.id).all()


def _find_user_by_employee_id(db: Session, employee_id: str) -> Optional[User]:
    """Find user by exact employee ID, then fallback to 4-digit badge normalization."""
    raw_id = (employee_id or "").strip()
    if not raw_id:
        return None

    exact_matches = db.query(User).filter(func.lower(User.employee_id) == raw_id.lower()).all()
    if len(exact_matches) > 1:
        raise _AmbiguousIdentifier(_AMBIGUOUS_EMPLOYEE_ID_DETAIL, _AUDIT_ERROR_EMPLOYEE_ID_AMBIGUOUS)
    if len(exact_matches) == 1:
        return exact_matches[0]

    normalized_input = _normalize_employee_id(raw_id)
    if not normalized_input:
        return None

    matches = _normalized_employee_id_matches(db, normalized_input)
    if not matches:
        return None
    if len(matches) > 1:
        raise _AmbiguousIdentifier(_AMBIGUOUS_EMPLOYEE_ID_DETAIL, _AUDIT_ERROR_EMPLOYEE_ID_AMBIGUOUS)
    return matches[0]


@router.post("/employee-login", response_model=Token, summary="Employee ID login")
def employee_login(request: Request, payload: EmployeeLoginRequest, db: Session = Depends(get_db)):
    """
    Authenticate a user by employee ID or 4-digit badge ID and receive JWT tokens.
    Intended for shop floor job stations and kiosks.

    **Rate limited**: 10 requests/minute per IP (slowapi), PLUS a per-IP
    failed-attempt throttle — 8 failures within 15 minutes locks the IP out for
    15 minutes (429). Successful logins never count toward the throttle.
    """
    # Compensating control for the 10/min slowapi limit (see
    # app/core/login_throttle.py): checked BEFORE the user lookup so a
    # throttled IP does zero account probing.
    client_ip = client_ip_from_request(request)
    retry_after = employee_login_throttle.blocked_retry_after(client_ip)
    if retry_after is not None:
        log_auth_event(
            db,
            "EMPLOYEE_LOGIN_BLOCKED",
            email=None,
            success=False,
            request=request,
            error="Throttled: too many failed attempts from this address",
        )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed sign-in attempts — wait a few minutes",
            headers={"Retry-After": str(retry_after)},
        )

    try:
        user = _find_user_by_employee_id(db, payload.employee_id)
    except HTTPException as exc:
        # MIRRORS /auth/login's handling of the same refusal, which this route was missing
        # entirely: a 409 used to return with no audit row at all, so the ONE thing that
        # reports a duplicate badge -- or a candidate window the resolver can no longer
        # scan -- was silent on the route the floor actually uses. The affected operator
        # just sees a badge that stopped working.
        #
        # Status checked, not assumed, and the CAUSE read structurally (see
        # ``_conflict_audit_error``): this resolver raises 409 both for an established
        # duplicate and for a truncated window, and the two earn different permanent
        # sentences.
        #
        # NO throttle failure is registered, exactly as on /auth/login: the account
        # provably exists (or the table could not be scanned) -- neither is a wrong guess,
        # and counting it would let one duplicated row lock a whole shop's egress IP out
        # of badge sign-in for the cooldown, with no admin reset.
        #
        # The badge IS recorded here, unlike the failure rows below which deliberately
        # log none: a 409 means the submitted value MATCHED real rows, so it is a
        # known-good badge rather than a possibly-mistyped credential fragment, and it is
        # the only thing that tells an admin which rows to merge.
        if exc.status_code == status.HTTP_409_CONFLICT:
            log_auth_event(
                db,
                "EMPLOYEE_LOGIN_BLOCKED",
                employee_id=payload.employee_id,
                success=False,
                request=request,
                error=_conflict_audit_error(exc),
            )
            db.commit()
        raise

    if not user:
        employee_login_throttle.register_failure(client_ip)
        # Log the audit row, then commit so it persists before raising
        # (AuditService only flushes; get_db never commits).
        log_auth_event(
            db, "EMPLOYEE_LOGIN_FAILED", email=None, success=False, request=request, error="Employee ID not found"
        )
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid employee ID")

    if user.locked_until and user.locked_until > datetime.utcnow():
        employee_login_throttle.register_failure(client_ip)
        log_auth_event(db, "EMPLOYEE_LOGIN_BLOCKED", user=user, success=False, request=request, error="Account locked")
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Account is locked. Please contact administrator."
        )

    if not user.is_active:
        employee_login_throttle.register_failure(client_ip)
        log_auth_event(db, "EMPLOYEE_LOGIN_FAILED", user=user, success=False, request=request, error="Account disabled")
        db.commit()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account is disabled")

    # Reset failed attempts on successful login
    user.failed_login_attempts = 0
    user.locked_until = None

    # Repair any legacy reserved-domain email and log BEFORE the terminal commit
    # so the email change and the audit row commit atomically.
    _ensure_valid_auth_email(user, db)
    log_auth_event(db, "EMPLOYEE_LOGIN_SUCCESS", user=user, success=True, request=request)
    db.commit()
    db.refresh(user)

    access_token = create_access_token(subject=user.id, company_id=user.company_id)
    refresh_token, _, _ = create_refresh_token(subject=user.id, company_id=user.company_id)

    return Token(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=UserResponse.model_validate(user),
    )


@router.post("/employee-logout", summary="Employee ID logout")
def employee_logout(
    request: Request,
    payload: EmployeeLoginRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Record a logout audit event for the CALLING user.

    Identity comes from the bearer token, never from the request body. This
    endpoint used to take no auth at all and resolve ``payload.employee_id``
    through the GLOBALLY UNSCOPED ``_find_user_by_employee_id``, then write a
    chain-linked, tenant-tagged ``EMPLOYEE_LOGOUT`` row and commit. That made it
    two things at once: an unauthenticated audit-forgery surface (anyone could
    produce ``EMPLOYEE_LOGOUT / admin@werco.com / company_id=1 / success=true``,
    visible to any Admin at ``GET /api/v1/audit/?resource_type=authentication``),
    and — because badges normalize to 4 digits and a miss returned 404 while a
    hit returned 200 — an unauthenticated CROSS-TENANT badge-enumeration oracle
    at the global default rate (no per-path limit, and ``employee_login_throttle``
    is wired only to /employee-login).

    Deleting the route was the preferred fix but is not available: the office
    "confirm your badge to sign out" flow calls it
    (``frontend/src/context/AuthContext.tsx`` -> ``api.logoutWithEmployeeId``),
    and it is one of the two paths the kiosk scope fence allows
    (``KIOSK_TOKEN_EXACT_PATHS`` in ``app/api/deps.py``). So it is authenticated
    instead, via ``get_current_user`` rather than a hand-rolled optional bearer —
    that is what keeps the kiosk path fence and the read-only-context write guard
    in force, since both are enforced INSIDE ``get_current_user``.

    The body is retained for wire compatibility (the client still sends it) but
    is NOT consulted for identity, and there is no 404/200 distinction left to
    probe: an authenticated caller always gets 200. The frontend already verifies
    the typed badge against the active user before calling, and the token is
    cleared only after this returns.
    """
    # AuditService only flushes; commit so the audit row persists before the
    # request session closes.
    log_auth_event(db, "EMPLOYEE_LOGOUT", user=current_user, success=True, request=request)
    db.commit()
    return {"message": "Logged out successfully"}


# ---------------------------------------------------------------------------
# Crew-station kiosk: badge → 5-minute kiosk-scoped operator token.
#
# The shared crew tablet holds a scoped type="kiosk" STATION token (minted by
# POST /shop-floor/kiosk-stations/station-login). Each badge scan exchanges
# (station token + badge) for a short-lived type="access" OPERATOR token with a
# scope="kiosk" claim, path-fenced in get_current_user to the shop-floor
# endpoints (+ employee-logout). NO refresh token is ever minted here — a
# shared terminal must never hold a long-lived credential for an individual
# operator. Rate-limited (30/min per IP, see main.py AUTH_RATE_LIMITS) — safe
# because the endpoint is station-token-gated, not public.
# ---------------------------------------------------------------------------

# Lifetime of a badge-minted kiosk operator token (minutes). Long enough for
# one join/leave/report action window, short enough that a stolen token dies
# before it matters.
KIOSK_BADGE_TOKEN_TTL_MINUTES = 5

_KIOSK_INVALID_BADGE = "Invalid badge"


def _find_user_by_employee_id_in_company(db: Session, employee_id: str, company_id: int) -> Optional[User]:
    """Company-scoped variant of ``_find_user_by_employee_id`` (kiosk badge mint).

    Identical exact-then-normalized matching, but every query is fenced to the
    station's company so a foreign tenant's badge can never resolve — it reads
    as "unknown badge" (uniform 401 upstream). Ambiguity within the company is
    still a 409 (an admin data problem, not an auth probe).

    "Identical" is now literal for the normalized half: it calls the same
    ``_normalized_employee_id_matches`` the global resolver does, passing ``company_id``,
    so the ordering, the candidate cap and the truncation refusal are one implementation
    rather than two copies that agreed on the day they were written.
    """
    raw_id = (employee_id or "").strip()
    if not raw_id:
        return None

    exact_matches = (
        db.query(User).filter(func.lower(User.employee_id) == raw_id.lower(), User.company_id == company_id).all()
    )
    if len(exact_matches) > 1:
        raise _AmbiguousIdentifier(_AMBIGUOUS_EMPLOYEE_ID_DETAIL, _AUDIT_ERROR_EMPLOYEE_ID_AMBIGUOUS)
    if len(exact_matches) == 1:
        return exact_matches[0]

    normalized_input = _normalize_employee_id(raw_id)
    if not normalized_input:
        return None

    matches = _normalized_employee_id_matches(db, normalized_input, company_id=company_id)
    if not matches:
        return None
    if len(matches) > 1:
        raise _AmbiguousIdentifier(_AMBIGUOUS_EMPLOYEE_ID_DETAIL, _AUDIT_ERROR_EMPLOYEE_ID_AMBIGUOUS)
    return matches[0]


def _audit_kiosk_badge_event(
    db: Session,
    *,
    request: Request,
    station: KioskStation,
    action: str,
    user: Optional[User] = None,
    error: Optional[str] = None,
) -> None:
    """Write + commit the KIOSK_BADGE_TOKEN_ISSUED / _FAILED audit row.

    Attributed to the station's company (the authoritative DB row); the actor
    is the badge-identified operator on success, the station (user=None) on
    failure. Follows the visitor station-login failed-PIN pattern: AuditService
    only flushes, so we commit here to persist the row before raising/returning.
    The scanned badge value is deliberately NOT logged (a failed scan may be a
    mistyped credential fragment).
    """
    try:
        audit = AuditService(db, user=user, request=request, company_id=station.company_id)
        audit.log(
            action=action,
            resource_type="kiosk_station",
            resource_id=station.id,
            resource_identifier=station.label,
            description=(f"{action} at crew-station kiosk '{station.label}'" + (f": {error}" if error else "")),
            success=error is None,
            error_message=error,
        )
        db.commit()
    except Exception:  # pragma: no cover - defensive: audit failure must not mask the auth result
        import logging

        logging.getLogger(__name__).exception("Failed to audit kiosk badge-token event")


@router.post("/kiosk-badge-token", response_model=KioskBadgeTokenResponse, summary="Kiosk badge token mint")
def kiosk_badge_token(
    request: Request,
    payload: KioskBadgeTokenRequest,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
):
    """Exchange (station token + badge scan) for a 5-minute kiosk-scoped operator token.

    **Auth**: ``Authorization: Bearer <kiosk station token>`` — validated
    against the ``kiosk_stations`` row (exists, not revoked, ``cid`` matches).
    **Rate limited**: 30/minute per IP.

    Badge lookup is fenced to the station's company; unknown, inactive, locked,
    and foreign-tenant badges are all a uniform 401 "Invalid badge" so the
    response can't be used to probe accounts. Returns a ``scope="kiosk"``
    access token (path-fenced to ``/api/v1/shop-floor`` + employee-logout) and
    the operator's display identity. **Never** returns a refresh token.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    claims = verify_kiosk_token(token)
    if claims is None or not claims.get("station_id"):
        raise credentials_exception

    station = db.query(KioskStation).filter(KioskStation.id == claims["station_id"]).first()
    if station is None or station.revoked:
        raise credentials_exception
    if claims.get("company_id") != station.company_id:
        raise credentials_exception

    try:
        user = _find_user_by_employee_id_in_company(db, payload.employee_id, station.company_id)
    except HTTPException as exc:
        # MIRRORS /auth/login and /auth/employee-login, which this route was the last one
        # missing: BOTH 409 causes -- an established duplicate badge inside the station's
        # company, and a candidate window that TRUNCATED -- returned with no audit row at
        # all, so the only surface that reports either fact was silent on the door the
        # floor scans into. The operator just sees a badge that stopped working.
        #
        # Status checked, not assumed, and the CAUSE read structurally
        # (``_conflict_audit_error``) rather than off ``exc.detail``, which is UI copy:
        # the two causes earn different permanent sentences and 008/060 refuse to correct
        # a row afterwards.
        #
        # Written under the route's existing ``KIOSK_BADGE_TOKEN_FAILED`` action rather
        # than a new one: the cause is carried by ``error``, and inventing a third action
        # string would leave every consumer (and both runbooks) describing a vocabulary
        # the code no longer uses.
        #
        # The badge is still NOT logged, unlike /auth/employee-login's 409 row: this
        # audit helper is station-keyed by design (``resource_identifier`` is the station
        # label) and a scanned value may be a mistyped credential fragment. The station,
        # the company and the cause are what an admin needs to find the duplicate rows.
        if exc.status_code == status.HTTP_409_CONFLICT:
            _audit_kiosk_badge_event(
                db,
                request=request,
                station=station,
                action="KIOSK_BADGE_TOKEN_FAILED",
                error=_conflict_audit_error(exc),
            )
        raise

    if not user:
        _audit_kiosk_badge_event(
            db, request=request, station=station, action="KIOSK_BADGE_TOKEN_FAILED", error="Badge not recognized"
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_KIOSK_INVALID_BADGE)

    if user.locked_until and user.locked_until > datetime.utcnow():
        _audit_kiosk_badge_event(
            db, request=request, station=station, action="KIOSK_BADGE_TOKEN_FAILED", error="Account locked"
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_KIOSK_INVALID_BADGE)

    if not user.is_active:
        _audit_kiosk_badge_event(
            db, request=request, station=station, action="KIOSK_BADGE_TOKEN_FAILED", error="Account disabled"
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_KIOSK_INVALID_BADGE)

    _audit_kiosk_badge_event(db, request=request, station=station, action="KIOSK_BADGE_TOKEN_ISSUED", user=user)

    access_token = create_access_token(
        subject=user.id,
        company_id=station.company_id,
        expires_delta=timedelta(minutes=KIOSK_BADGE_TOKEN_TTL_MINUTES),
        scope="kiosk",
    )

    return KioskBadgeTokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=KIOSK_BADGE_TOKEN_TTL_MINUTES * 60,
        user=KioskBadgeUser(id=user.id, full_name=user.full_name, employee_id=user.employee_id),
    )


@router.post("/refresh", response_model=TokenRefresh)
def refresh_token(request: Request, token_request: RefreshTokenRequest, db: Session = Depends(get_db)):
    """
    Refresh an access token using a refresh token.
    Implements token rotation: returns new refresh token each time.
    """
    # Verify the refresh token
    payload = verify_refresh_token(token_request.refresh_token)

    if not payload:
        # Log the audit row, then commit so it persists before raising
        # (AuditService only flushes; get_db never commits).
        log_auth_event(
            db,
            "TOKEN_REFRESH_FAILED",
            email="unknown",
            success=False,
            request=request,
            error="Invalid or expired refresh token",
        )
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token")

    # Get the user
    user_id = payload.get("user_id")
    session_id = payload.get("session_id")

    user = db.query(User).filter(User.id == int(user_id)).first()

    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account is disabled")

    # Check if account got locked since last token
    if user.locked_until and user.locked_until > datetime.utcnow():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is locked")

    # Preserve company context from the refresh token
    token_company_id = payload.get("company_id") or user.company_id
    token_read_only = bool(payload.get("read_only", False))

    # Create new access token
    new_access_token = create_access_token(subject=user.id, company_id=token_company_id, read_only=token_read_only)

    # Token rotation: create NEW refresh token (invalidates the old one implicitly)
    # Use same session_id to maintain session continuity
    new_refresh_token, _, _ = create_refresh_token(
        subject=user.id,
        session_id=session_id,
        company_id=token_company_id,
        read_only=token_read_only,
    )

    # AuditService only flushes; commit so the audit row persists before the
    # request session closes.
    log_auth_event(db, "TOKEN_REFRESHED", user=user, success=True, request=request)
    db.commit()

    return TokenRefresh(
        access_token=new_access_token,
        refresh_token=new_refresh_token,
        token_type="bearer",
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """
    Logout endpoint - logs the event.
    Note: With JWTs, true server-side invalidation requires a token blacklist (Redis).
    Client should discard tokens on logout.
    """
    # AuditService only flushes; commit so the audit row persists before the
    # request session closes.
    log_auth_event(db, "LOGOUT", user=current_user, success=True, request=request)
    db.commit()
    return {"message": "Logged out successfully"}


@router.post("/register", response_model=UserResponse)
def register(
    request: Request,
    user_in: UserCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN])),
    company_id: int = Depends(get_current_company_id),
):
    """Register a new user within the current company (admin only)"""
    # Check if email already exists within this company
    if db.query(User).filter(User.email == user_in.email, User.company_id == company_id).first():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email already registered")

    # Check if employee_id already exists within this company
    if db.query(User).filter(User.employee_id == user_in.employee_id, User.company_id == company_id).first():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Employee ID already exists")

    user = User(
        email=user_in.email,
        employee_id=user_in.employee_id,
        first_name=user_in.first_name,
        last_name=user_in.last_name,
        role=user_in.role,
        department=user_in.department,
        hashed_password=get_password_hash(user_in.password),
        company_id=company_id,
    )

    db.add(user)
    db.flush()  # assign the PK without committing so the audit row carries a real resource_id

    # Log BEFORE the terminal commit so the audit row commits atomically with the new user.
    log_auth_event(db, "USER_REGISTERED", user=user, success=True, request=request)
    db.commit()
    db.refresh(user)

    return user


@router.get("/setup-status")
def setup_status(db: Session = Depends(get_db)):
    """Check whether the system has been set up (i.e., at least one user exists)."""
    user_count = db.query(User).count()
    return {"has_users": user_count > 0, "is_setup_required": user_count == 0}


# The ONE body every non-bootstrap outcome returns. A duplicate email or
# employee ID has to be indistinguishable from a fresh submission, so the
# refusal path returns exactly this too. Handed out as a copy so a handler can
# never mutate the shared literal.
_PUBLIC_REGISTRATION_PENDING = {"message": "Account submitted for approval", "is_first_user": False}

# Why a candidate identifier could not be used, as reported by ``_employee_id_taken_reason``
# inside ``register_public``. Two values, because they are two different facts and one of
# them is a fact nobody established -- see the rejection audit row.
_TAKEN_COLLISION = "collision"
_TAKEN_UNRESOLVABLE = "unresolvable"

# The ``error`` recorded on a PUBLIC_REGISTRATION_REJECTED audit row. NEITHER is ever shown
# to the caller -- the response is the uniform pending body in both cases, so no oracle is
# added -- and both are permanent: migrations 008/060 refuse UPDATE and DELETE on
# ``audit_logs`` and invariant 2 forbids backfilling, so whichever sentence is written is
# the one an admin reads forever.
_REJECTED_IDENTIFIER_TAKEN = "Email or employee ID already in use"

# Deliberately NOT the sentence above. Reaching this means the badge collision probe
# TRUNCATED its candidate window and refused to answer, so no duplicate was found and none
# may be claimed; the remediation is a user table the resolver can no longer scan, not a
# duplicate to merge. Same reasoning the HTTP path already uses for
# ``_UNRESOLVABLE_EMPLOYEE_ID_DETAIL``, carried onto the record that cannot be corrected.
_REJECTED_EMPLOYEE_ID_UNRESOLVABLE = (
    "Employee ID could not be resolved: the collision probe truncated its candidate "
    "window, so no duplicate was established. Registration was refused rather than "
    "guessed at."
)

# A THIRD distinct cause, and again not either sentence above. The suffix walk in
# ``services/user_identity`` offered its full ``MAX_IDENTIFIER_CANDIDATES`` and every one
# came back unusable, so no free identifier could be derived at all. That is neither an
# established duplicate ("already in use" claims a specific row) nor a single truncated
# probe -- and it is what the walk raises when the predicate is pinned True, which is the
# state a truncated window puts it in for every candidate alike.
_REJECTED_EMPLOYEE_ID_UNDERIVABLE = (
    "Employee ID could not be derived: every candidate offered to the collision probe came "
    "back unusable, up to the iteration cap. Registration was refused rather than looping "
    "or reusing a candidate."
)

_REJECTED_EMAIL_UNDERIVABLE = (
    "Synthetic email could not be derived: every candidate offered to the duplicate probe "
    "came back in use, up to the iteration cap. Registration was refused rather than "
    "looping or reusing a candidate."
)


@router.post("/register-public")
def register_public(
    request: Request,
    user_in: PublicRegister,
    db: Session = Depends(get_db),
):
    """
    Public registration endpoint.

    **Rate limited**: 3/minute per IP (``AUTH_RATE_LIMITS`` in app/main.py).

    - If no users exist yet this is the initial system setup: the first user
      is created as an active admin with superuser privileges.
    - Otherwise the account is created with the VIEWER role, inactive
      (pending admin approval).

    **Either identifier alone is enough** (plus the password): an email, an employee
    ID, or both. ``PublicRegister`` refuses only the request that carries neither.
    Whichever one is missing is derived here (``services/user_identity``) because both
    columns are NOT NULL -- a badge-only signup is stored under a synthetic
    ``emp-...@users.werco.com`` address. That derivation is a storage detail, not an
    outcome: a badge-only signup returns the same body as every other one, and the
    minting runs only after the refusal check so a refused attempt does not pay for it.

    This route is unauthenticated AND install-wide — its uniqueness checks span
    every company, not just the one it registers into. It used to answer
    "Email already registered" (400) or, distinctly, "Employee ID already
    exists" (400), which made it two account-existence oracles over every
    tenant's user list and badge numbers, on a route that also inserts
    attacker-controlled rows into ``users``. Both 400s are gone: outside the
    first-user bootstrap EVERY outcome returns the same body, and a duplicate
    simply does not insert.

    The checks stay install-wide on purpose. Scoping them to the target
    company would let this path mint the cross-tenant duplicate emails and
    badge numbers that make ``_find_user_by_auth_email`` (409) and
    ``_find_user_by_employee_id`` (409) refuse — i.e. it would create the
    lockout it is now non-committal about.

    The password is hashed BEFORE the duplicate check so the accepted and
    refused paths do the same ~100 ms of bcrypt work. Skipping the hash on a
    refusal would rebuild the oracle in the response time.
    """
    # Hash first: same work on both paths (see docstring).
    hashed_password = get_password_hash(user_in.password)

    user_count = db.query(User).count()
    is_first_user = user_count == 0

    def _email_taken(candidate: str) -> bool:
        return db.query(User).filter(func.lower(User.email) == candidate.lower()).first() is not None

    def _employee_id_taken_reason(candidate: str) -> Optional[str]:
        """WHY this badge cannot be used, or ``None`` when it is free.

        Returns the reason rather than a bare bool because the two "cannot be used"
        outcomes are different facts and one of them ends up on a permanent audit row:
        ``_TAKEN_COLLISION`` means a colliding row was FOUND, ``_TAKEN_UNRESOLVABLE``
        means the query REFUSED TO ANSWER. See the rejection audit row below.

        The exact probe alone was a hole an unauthenticated caller could drive through.
        Both resolvers compare ``_normalize_employee_id`` values (4 trailing digits), so
        registering ``00339`` while a real operator holds ``EMP-0339`` passes an exact
        comparison, inserts, and from then on every scan of badge ``0339`` finds TWO
        normalized matches and 409s that operator off the kiosk, the crew station and both
        login routes. The write path has to test collisions the way the read path resolves
        them, or the read path is what discovers the difference.

        It runs for the DERIVED candidate too, not just a submitted badge: an email-only
        signup at ``0339@anything.com`` derives the badge ``0339``, which is the same
        attack with the same result. Sharing one predicate is what closes both, and the
        suffix walk in ``employee_id_from_email`` then simply steps past the collision.

        A TRUNCATED candidate set is unusable too, and answers ``_TAKEN_UNRESOLVABLE``.
        The probe reuses the resolvers' own query shape, cap and all, so on a pathological
        set it refuses to answer — and the safe answer on an unauthenticated write path is
        "do not insert": a registration that silently does nothing costs a stranger one
        retry (and an admin can still create the account through ``POST /users/``), while
        a wrong "free" here takes a real operator off the floor. Uniform 200 either way,
        so it adds no oracle. What it must NOT do is call that outcome a collision — see
        the constants and the rejection audit row.
        """
        if db.query(User).filter(func.lower(User.employee_id) == candidate.lower()).first() is not None:
            return _TAKEN_COLLISION

        normalized = _normalize_employee_id(candidate)
        if not normalized:
            # No digits at all ("jmw") — outside the normalized keyspace entirely, so the
            # resolvers' fallback can never match it and there is nothing to collide with.
            return None

        try:
            return _TAKEN_COLLISION if _normalized_employee_id_matches(db, normalized) else None
        except _UnresolvableIdentifier:
            # Caught by TYPE, not by status code: the truncation refusal is the one
            # outcome that means "the probe did not answer", and it is now its own class.
            # Any other 409 a future edit adds propagates instead of being silently read
            # as a collision — which was the point of the status check this replaces, held
            # more tightly (an *ambiguity* 409 added here would have slipped through it).
            return _TAKEN_UNRESOLVABLE

    def _employee_id_taken(candidate: str) -> bool:
        """The ``is_taken`` predicate shape ``employee_id_from_email``'s suffix walk needs.

        Both reasons mean "do not use this candidate", so the walk treats them alike and
        simply steps to the next suffix; only the REJECTION row cares which one it was,
        and that call site reads the reason directly.

        NOT RELIABLY A FUNCTION OF THE CANDIDATE, which is why the walk it feeds is
        capped: ``_TAKEN_UNRESOLVABLE`` describes the user TABLE, not the candidate, so
        for any candidate that reaches the normalized probe a truncated window answers
        True alike. ``employee_id_from_email`` raises ``IdentifierDerivationExhausted``
        rather than looping; the call site below turns that into a refusal.

        WHAT THE LETTER SUFFIXES CHANGED, because the two facts are easy to conflate. The
        walk now offers only DIGIT-FREE candidates after the base, and this function
        returns ``None`` for those on the ``if not normalized`` line above -- before
        ``_normalized_employee_id_matches`` is called at all. So a digit-free candidate is
        neither a normalized collision nor pinnable by a truncated window, and the walk
        converges on exact-match collisions alone. The cap stays because the guarantee is
        the WALK's, not this predicate's, and because the mint's walk has no such property.
        """
        return _employee_id_taken_reason(candidate) is not None

    def _refuse(error: str) -> dict:
        """Refuse without saying so: one audit row naming ``error``, then the uniform body.

        THE one refusal shape for this handler, so every cause below is guaranteed to
        produce the same 200 + ``_PUBLIC_REGISTRATION_PENDING`` and differ only on the
        audit row. Nothing is inserted. Both identifiers go in, each under its OWN name
        (see log_auth_event on why a badge must never travel through email=): a badge-only
        registrant has no address, and a rejection row reading "unknown" would defeat the
        one thing this handler is deliberately loud about -- the response says nothing, the
        audit log says who tried. These are the SUBMITTED values, not the derived ones;
        nothing is minted on a refusal, and recording a value the caller never sent would
        make the row a claim about the system rather than about the attempt.
        """
        log_auth_event(
            db,
            "PUBLIC_REGISTRATION_REJECTED",
            email=user_in.email,
            employee_id=user_in.employee_id,
            success=False,
            request=request,
            error=error,
        )
        db.commit()
        return dict(_PUBLIC_REGISTRATION_PENDING)

    # Email is optional now (badge-only signups), so the duplicate probe only runs when
    # an address was actually supplied. Unguarded it would compare the empty string
    # against every row -- harmless today, but it makes "" a value that can start
    # matching, and the answer it produces is the oracle this handler exists to avoid.
    normalized_email = (user_in.email or "").strip().lower()

    # Tracked as THREE flags rather than one ``already_taken`` because they answer
    # different questions and only one of them can be written onto a permanent audit row
    # as a fact:
    #   * ``collision_established`` -- a duplicate row was actually FOUND (an address, or a
    #     badge matching exactly / under normalization);
    #   * ``employee_id_unresolvable`` -- the badge probe REFUSED TO ANSWER because its
    #     candidate window truncated, so nothing at all was established about uniqueness;
    #   * ``employee_id_underivable`` -- the suffix walk ran out of candidates, so no
    #     usable badge could be derived at all (see ``IdentifierDerivationExhausted``).
    # All three refuse the insert; see the audit row below for why the distinction
    # survives all the way into ``error``.
    collision_established = bool(normalized_email) and _email_taken(normalized_email)
    employee_id_unresolvable = False
    employee_id_underivable = False

    # Auto-generate employee_id from email if not provided. Only reachable WITH an
    # email: PublicRegister refuses a request carrying neither identifier, so "no
    # badge" implies "has address" and there is nothing to dereference blindly.
    employee_id = user_in.employee_id
    if not employee_id and user_in.email:
        # Use email local part as base, e.g. "jmw@wercomfg.com" -> "jmw".
        # The local part can legally be all-punctuation, which used to yield an
        # empty employee_id; the shared helper falls back so the column always
        # gets a real value.
        #
        # The walk is CAPPED and the cap is caught here. It is a backstop now rather than
        # the working limit it was: the walk suffixes with LETTERS, so every candidate
        # after the base is digit-free, ``_employee_id_taken_reason`` answers "free" on
        # those without running the normalized probe, and neither a badge-keyspace
        # collision nor a truncated window can pin it True. Numeric suffixes could not
        # converge at all on a shop with contiguous badge numbers -- ``jmw-2`` IS badge
        # 0002 -- and dropped legitimate signups behind the uniform 200. Still caught:
        # ``is_taken`` is this handler's to define, and refusing beats answering with the
        # last candidate, which is a value the probe just said not to use.
        try:
            employee_id = employee_id_from_email(user_in.email, _employee_id_taken)
        except IdentifierDerivationExhausted:
            # With zero users nothing is ever taken, so the walk cannot exhaust during the
            # bootstrap; if a later edit makes it possible, surface it rather than fall
            # through with no badge (``users.employee_id`` is NOT NULL). Same posture the
            # IntegrityError race below takes for the bootstrap.
            if is_first_user:
                raise
            employee_id_underivable = True
    elif employee_id:
        # Explicitly supplied. Recorded, not reported: the response body and status are
        # the uniform pending one either way, so widening what counts as unusable grows
        # only the set of inputs that silently do nothing. It adds no oracle, because the
        # caller cannot tell this outcome from a successful signup.
        #
        # The REASON is read here rather than through the bool predicate, and only from
        # THIS call -- the decisive one. The suffix walk on the derived path above also
        # runs the probe, but a candidate it rejects is one it steps past, not a
        # rejection of the registration, so letting those calls set these flags would put
        # a cause on the audit row that explains nothing about why the request was
        # refused.
        badge_reason = _employee_id_taken_reason(employee_id)
        if badge_reason == _TAKEN_COLLISION:
            collision_established = True
        elif badge_reason == _TAKEN_UNRESOLVABLE:
            employee_id_unresolvable = True

    # With zero users nothing can be taken, so the bootstrap can never reach
    # this branch; the guard is explicit anyway so no later edit can turn the
    # one-time setup path into a silent no-op.
    if (collision_established or employee_id_unresolvable or employee_id_underivable) and not is_first_user:
        # THE CAUSE IS THE CAUSE. A truncated candidate window establishes NO collision --
        # the query refused to answer -- so writing "already in use" for it states a fact
        # nobody checked, and an exhausted suffix walk establishes even less. The route
        # already draws exactly this line for the HTTP response
        # (``_UNRESOLVABLE_EMPLOYEE_ID_DETAIL``, on the grounds that "not unique" would be
        # a claim the query never established); the audit row is where it matters more,
        # because 008/060 refuse UPDATE and DELETE so the sentence is permanent, and the
        # wrong one sends an admin hunting a duplicate that does not exist instead of at a
        # user table the resolver can no longer scan. An ESTABLISHED collision wins the
        # wording when more than one is true: "already in use" is then a fact, and it is
        # the one that explains the refusal. The other two are mutually exclusive by
        # construction (one comes from the submitted-badge branch, the other from the
        # derived one), so the order between them decides nothing.
        if collision_established:
            rejection = _REJECTED_IDENTIFIER_TAKEN
        elif employee_id_underivable:
            rejection = _REJECTED_EMPLOYEE_ID_UNDERIVABLE
        else:
            rejection = _REJECTED_EMPLOYEE_ID_UNRESOLVABLE
        return _refuse(rejection)

    # A badge-only registrant still has to satisfy ``User.email`` (NOT NULL). For a
    # shop-floor user the BADGE is the real credential and an address may simply not
    # exist, so mint the synthetic ``emp-...@users.werco.com`` the rest of the system
    # already recognises rather than widening the column. Deliberately AFTER the
    # refusal above, so a registration that is not going to insert never pays for it.
    #
    # The mint dedups install-wide, exactly like the duplicate checks: a company-scoped
    # probe could mint an address that already exists in another tenant, which is the
    # cross-tenant ambiguity that makes ``_find_user_by_auth_email`` refuse 409.
    resolved_email = user_in.email
    if not normalized_email:
        try:
            resolved_email = synthetic_email_for_employee_id(employee_id or "", _email_taken)
        except IdentifierDerivationExhausted:
            # Same cap, same reasoning, and refused the SAME way as every cause above --
            # uniform pending body, no insert, one audit row naming what actually
            # happened. Inserting the last candidate instead would race the address into
            # ``uq_users_company_email`` (a 500, i.e. a new distinguishable outcome) or,
            # worse, land a badge-only operator on an address another account owns.
            if is_first_user:
                raise
            return _refuse(_REJECTED_EMAIL_UNDERIVABLE)

    if is_first_user:
        role = UserRole.PLATFORM_ADMIN
        is_superuser = True
        is_active = True
        # Create the initial Werco company
        werco = db.query(Company).filter(Company.slug == "werco").first()
        if not werco:
            werco = Company(name="Werco Manufacturing", slug="werco", is_active=True)
            db.add(werco)
            db.flush()
        initial_company_id = werco.id
    else:
        role = UserRole.VIEWER
        is_superuser = False
        is_active = False
        # Assign to the first (Werco) company by default
        werco = db.query(Company).filter(Company.slug == "werco").first()
        initial_company_id = werco.id if werco else 1

    user = User(
        email=resolved_email,
        employee_id=employee_id,
        first_name=user_in.first_name,
        last_name=user_in.last_name,
        role=role,
        is_superuser=is_superuser,
        is_active=is_active,
        hashed_password=hashed_password,
        company_id=initial_company_id,
    )

    db.add(user)
    db.flush()  # assign the PK without committing so the audit row carries a real resource_id

    # Log BEFORE the terminal commit so the audit row commits atomically with the new
    # user (and the initial company, if this is the first-user bootstrap).
    action = "FIRST_USER_REGISTERED" if is_first_user else "PUBLIC_REGISTRATION"
    # The badge is passed explicitly so the accepted row and the REJECTED rows for the
    # same badge join on extra_data.employee_id. Without it the accepted row keys on
    # user.email -- a MINTED address for a badge-only signup -- and the two halves of one
    # badge's registration history share no key an operator could query on.
    #
    # ``email`` is passed too, and it is load-bearing rather than redundant: log_auth_event
    # resolves resource_identifier as `email or employee-id:<badge> or user.email`, so
    # omitting it would make an EMAIL registrant's accepted row key on the badge that
    # employee_id_from_email *derived* -- a value the registrant never submitted -- instead
    # of the address they did. Passing both lets the submitted address win when there is
    # one and `employee-id:<badge>` win when there is not. Do NOT "simplify" this by
    # reordering the chain to put user.email second: that puts the minted
    # emp-...@users.werco.com address on the badge-only row, which is the same defect
    # facing the other way. Audit rows cannot be corrected afterwards (008/060 refuse
    # UPDATE/DELETE), so getting this wrong is permanent for every row written after it.
    log_auth_event(db, action, user=user, success=True, request=request, email=user_in.email, employee_id=employee_id)
    try:
        db.commit()
    except IntegrityError:
        # Lost the race to a concurrent registration of the same address or
        # badge (uq_users_company_email / uq_users_company_employee_id). Answer
        # exactly as the duplicate path does: a 500 here would be the same
        # existence oracle wearing a different status code. The bootstrap has
        # no uniform response to fall back to, so it still surfaces.
        db.rollback()
        if is_first_user:
            raise
        # The CONSTANT, not a re-spelling of it: ``_REJECTED_IDENTIFIER_TAKEN`` exists for
        # exactly this row, and an inline literal drifting from it would split one cause
        # across two sentences on a table nobody can correct afterwards.
        return _refuse(_REJECTED_IDENTIFIER_TAKEN)
    db.refresh(user)

    if is_first_user:
        return {"message": "Admin account created successfully", "is_first_user": True}
    return dict(_PUBLIC_REGISTRATION_PENDING)


@router.post("/switch-company/{target_company_id}", response_model=Token)
def switch_company(
    target_company_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_platform_admin),
):
    """
    Switch the active company context (platform admin only).
    Issues new tokens scoped to the target company for read-only browsing.
    """
    company = db.query(Company).filter(Company.id == target_company_id, Company.is_active == True).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found or inactive")

    read_only = company.id != current_user.company_id
    access_token = create_access_token(subject=current_user.id, company_id=company.id, read_only=read_only)
    refresh_token, _, _ = create_refresh_token(subject=current_user.id, company_id=company.id, read_only=read_only)

    # AuditService only flushes; commit so the audit row persists before the
    # request session closes.
    log_auth_event(db, "COMPANY_SWITCH", user=current_user, success=True, request=request)
    db.commit()

    return Token(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=UserResponse.model_validate(current_user),
    )


# ---------------------------------------------------------------------------
# Scoped display tokens for unattended TV wallboards (A0.5).
#
# A display token is a long-lived JWT with type="display" that authenticates
# ONLY GET /shop-floor/wallboard (via the get_display_or_user dependency).
# verify_token rejects it everywhere else, so it can never act as a user
# session. Issuance/revocation are ADMIN/MANAGER-gated and audit-logged; the
# raw JWT is shown exactly once at creation and never stored.
# ---------------------------------------------------------------------------

_DISPLAY_TOKEN_MANAGER_ROLES = [UserRole.ADMIN, UserRole.MANAGER]


@router.post("/display-token", response_model=DisplayTokenIssueResponse, summary="Issue a wallboard display token")
def create_display_token_endpoint(
    payload: DisplayTokenCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_DISPLAY_TOKEN_MANAGER_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Mint a scoped, revocable display token for a shop TV (ADMIN/MANAGER).

    The returned ``token`` AND ``setup_code`` are shown ONCE — neither is
    stored and neither can be retrieved again (the code can be *reissued*
    via POST /display-token/{id}/setup-code). Default lifetime 90 days,
    capped at 365; the setup code itself expires in 15 minutes.
    """
    record, token, setup_code = issue_display_token(
        db,
        company_id=company_id,
        label=payload.label,
        expires_days=payload.expires_days,
        created_by=current_user.id,
        audit=audit,
        dept=payload.dept,
        show_customer_names=payload.show_customer_names,
    )
    return DisplayTokenIssueResponse(
        **DisplayTokenResponse.model_validate(record).model_dump(),
        token=token,
        setup_code=setup_code,
        setup_code_expires_at=record.setup_code_expires_at,
    )


@router.get("/display-token", response_model=DisplayTokenListResponse, summary="List wallboard display tokens")
def list_display_tokens_endpoint(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_DISPLAY_TOKEN_MANAGER_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """List this company's display tokens (no JWTs — metadata only)."""
    records = list_display_tokens(db, company_id=company_id)
    return DisplayTokenListResponse(display_tokens=[DisplayTokenResponse.model_validate(record) for record in records])


@router.delete("/display-token/{token_id}", response_model=DisplayTokenResponse, summary="Revoke a display token")
def revoke_display_token_endpoint(
    token_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_DISPLAY_TOKEN_MANAGER_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Revoke a display token (ADMIN/MANAGER, tenant-scoped, audited, idempotent).

    The wallboard dependency re-checks the DB row on every request, so the
    TV loses access on its next poll (within ~30s).
    """
    record = revoke_display_token(
        db,
        company_id=company_id,
        token_id=token_id,
        revoked_by=current_user.id,
        audit=audit,
    )
    return DisplayTokenResponse.model_validate(record)


@router.post(
    "/display-token/{token_id}/setup-code",
    response_model=SetupCodeReissueResponse,
    summary="Reissue a one-time TV setup code",
)
def reissue_setup_code_endpoint(
    token_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_DISPLAY_TOKEN_MANAGER_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Rotate the pairing code for an existing display token (ADMIN/MANAGER).

    The previous code — used or not — stops working immediately; the new code
    is shown ONCE and expires in 15 minutes. 400 for revoked/expired tokens
    (issue a fresh token instead), 404 if the token isn't this company's.
    """
    record, setup_code = reissue_setup_code(
        db,
        company_id=company_id,
        token_id=token_id,
        audit=audit,
    )
    return SetupCodeReissueResponse(
        id=record.id,
        label=record.label,
        dept=record.dept,
        setup_code=setup_code,
        setup_code_expires_at=record.setup_code_expires_at,
    )


@router.post(
    "/display-token/claim",
    response_model=DisplayTokenClaimResponse,
    summary="Claim a TV setup code (public)",
)
def claim_display_token_endpoint(
    payload: DisplayTokenClaimRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Exchange a one-time setup code for the wallboard display JWT.

    PUBLIC + rate-limited (10/minute per IP, see main.py AUTH_RATE_LIMITS) —
    the TV has no credentials yet; the high-entropy single-use code IS the
    credential and the matched row is the company-binding authority. Every
    failure mode (unknown / used / expired code, revoked / expired display)
    returns the SAME generic 404 so the endpoint can't be used as an oracle.
    The minted JWT is re-minted from the row, so revoking the display token
    still kills the TV on its next poll.
    """
    record, token = claim_display_token(db, raw_code=payload.code, request=request)
    return DisplayTokenClaimResponse(
        token=token,
        label=record.label,
        dept=record.dept,
        expires_at=record.expires_at,
    )


def db_reset_route_enabled(environment: str) -> bool:
    """Whether the destructive /reset-database route should be MOUNTED at all.

    Never in production. Gating route *registration* (rather than checking the
    environment inside the handler) means the route is not even enumerable on a
    production host: it 404s like any unknown path, and it cannot appear in the
    OpenAPI schema. Pure function so the decision is testable without booting the
    app — same pattern as ``_host_validation_log_record`` in app/main.py.

    ENVIRONMENT is a free-form string typed into a deploy dashboard, so a bare
    ``!= "production"`` FAILS OPEN on "Production", "PRODUCTION", or a
    pasted-in trailing space — each of which would mount a no-auth
    TRUNCATE-every-table route on a production host. Normalize first, matching
    the existing idiom in ``app/services/carriers/crypto.py``.
    """
    return (environment or "").strip().lower() != "production"


def reset_database(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Reset all data in the database. Protected by SECRET_KEY header.

    DANGEROUS: TRUNCATEs every table in the public schema with FK triggers
    disabled. Registered only outside production (see ``db_reset_route_enabled``)
    and additionally gated on ALLOW_DB_RESET=true.

    The header is compared with ``hmac.compare_digest``: the credential is the
    SECRET_KEY, which is ALSO the JWT signing key, so a byte-by-byte ``!=`` made
    this endpoint a timing oracle for total token forgery as well as for total
    data destruction. Deleting this endpoint outright remains the recommended
    end state — that is an owner decision, not one this hardening makes.
    """
    import os

    from sqlalchemy import text

    # Arm check FIRST, deliberately. If this ran after the key comparison, a
    # disarmed host would answer "Invalid reset key" for a wrong key but
    # "Database reset is disabled" for a right one — confirming a candidate
    # SECRET_KEY (which is also the JWT signing key) without needing the reset to
    # be armed. Checking the arm state first makes every request to a disarmed
    # host indistinguishable.
    if os.environ.get("ALLOW_DB_RESET", "false").lower() != "true":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Database reset is disabled")

    # Must provide the SECRET_KEY as authorization. Compared in constant time,
    # on bytes (compare_digest rejects non-ASCII str, and header values are not
    # guaranteed ASCII).
    provided_key = request.headers.get("X-Reset-Key", "")
    actual_key = os.environ.get("SECRET_KEY", "")
    if not provided_key or not actual_key:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid reset key")
    if not hmac.compare_digest(provided_key.encode("utf-8"), actual_key.encode("utf-8")):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid reset key")

    tables_result = db.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' " "AND tablename != 'alembic_version'")
    )
    tables = [row[0] for row in tables_result]

    db.execute(text("SET session_replication_role = 'replica'"))
    for table in tables:
        db.execute(text(f'TRUNCATE TABLE "{table}" CASCADE'))
    db.execute(text("SET session_replication_role = 'origin'"))
    db.commit()

    return {"message": f"All {len(tables)} tables cleared. Visit /register to create admin account."}


# Mount the reset route only outside production. Keep this immediately after the
# handler so the two are read together.
if db_reset_route_enabled(settings.ENVIRONMENT):
    router.post("/reset-database")(reset_database)
