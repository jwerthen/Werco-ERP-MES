import re
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import func
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
from app.core.login_throttle import client_ip_from_request, employee_login_throttle
from app.core.security import (
    create_access_token,
    create_refresh_token,
    get_password_hash,
    verify_kiosk_token,
    verify_password,
    verify_refresh_token,
)
from app.db.database import get_db
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

router = APIRouter()


def log_auth_event(
    db: Session,
    action: str,
    user: User = None,
    email: str = None,
    success: bool = True,
    request: Request = None,
    error: str = None,
):
    """Log authentication events for CMMC compliance using AuditService"""
    try:
        resource_identifier = email or (user.email if user else None)
        audit_service = AuditService(db, user, request)
        audit_service.log(
            action=action,
            resource_type="authentication",
            resource_id=user.id if user else None,
            resource_identifier=resource_identifier,
            description=f"{action} attempt for {resource_identifier or 'unknown'}",
            success=success,
            error_message=error,
            extra_data={"email": email} if email and not user else None,
        )
    except Exception as e:
        # Don't let audit logging failures break authentication
        import logging

        logging.warning(f"Failed to log auth event: {e}")


@router.post("/login", response_model=Token, summary="User login")
def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """
    Authenticate a user and receive JWT access and refresh tokens.

    **Rate limited**: 5 attempts per minute

    **Account lockout**: After 5 failed attempts, account is locked for 30 minutes (CMMC compliance).

    **Request body** (form data):
    - username: User's email address
    - password: User's password

    **Returns**:
    - access_token: JWT token for API authorization (valid for 15 minutes — ACCESS_TOKEN_EXPIRE_MINUTES)
    - refresh_token: Token to obtain new access tokens (valid for 7 days)
    - token_type: Always "bearer"
    - expires_in: Token expiration time in seconds

    **Raises**:
    - 401: Invalid credentials
    - 403: Account locked or inactive
    """
    user = _find_user_by_auth_email(db, form_data.username)

    if not user:
        # Log the audit row, then commit so it persists before raising
        # (the audit row is only flushed by AuditService; get_db never commits).
        log_auth_event(
            db, "LOGIN_FAILED", email=form_data.username, success=False, request=request, error="User not found"
        )
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    # Check if account is locked (CMMC requirement)
    if user.locked_until and user.locked_until > datetime.utcnow():
        log_auth_event(db, "LOGIN_BLOCKED", user=user, success=False, request=request, error="Account locked")
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
        log_auth_event(db, "LOGIN_FAILED", user=user, success=False, request=request, error="Invalid password")
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    if not user.is_active:
        log_auth_event(db, "LOGIN_FAILED", user=user, success=False, request=request, error="Account disabled")
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
    log_auth_event(db, "LOGIN_SUCCESS", user=user, success=True, request=request)
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


def _find_user_by_auth_email(db: Session, email: str) -> Optional[User]:
    """
    Find a user for email login using a case-insensitive lookup.

    Legacy imports may still have `@werco.local` stored until first successful
    login repair. Allow the repaired `@users.werco.com` address to find the
    legacy record so the repair path can complete.
    """
    normalized_email = (email or "").strip().lower()
    if not normalized_email:
        return None

    user = db.query(User).filter(func.lower(User.email) == normalized_email).first()
    if user:
        return user

    if normalized_email.endswith("@users.werco.com"):
        local_part = normalized_email.removesuffix("@users.werco.com")
        legacy_email = f"{local_part}@werco.local"
        return db.query(User).filter(func.lower(User.email) == legacy_email).first()

    return None


def _build_repaired_email(user: User, db: Session) -> str:
    """Generate a valid non-reserved email for legacy .local imports."""
    employee_key = re.sub(r"[^a-z0-9._-]", "", (user.employee_id or "").lower()) or "employee"
    local = f"emp-{employee_key}"
    candidate = f"{local}@users.werco.com"
    suffix = 2
    while True:
        existing = db.query(User).filter(func.lower(User.email) == candidate.lower()).first()
        if not existing or existing.id == user.id:
            return candidate
        candidate = f"{local}-{suffix}@users.werco.com"
        suffix += 1


def _ensure_valid_auth_email(user: User, db: Session) -> None:
    """
    Patch legacy reserved-domain addresses so token response validation does not crash.
    This keeps logins working for users imported before the email-domain fix.

    Mutates ``user.email`` in place but does NOT commit. The caller is responsible
    for committing so the email repair commits atomically with the rest of the
    login transaction (including the audit row).
    """
    email = (user.email or "").strip()
    if email.lower().endswith("@werco.local"):
        user.email = _build_repaired_email(user, db)


def _find_user_by_employee_id(db: Session, employee_id: str) -> Optional[User]:
    """Find user by exact employee ID, then fallback to 4-digit badge normalization."""
    raw_id = (employee_id or "").strip()
    if not raw_id:
        return None

    exact_matches = db.query(User).filter(func.lower(User.employee_id) == raw_id.lower()).all()
    if len(exact_matches) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Employee ID is not unique. Please contact an administrator."
        )
    if len(exact_matches) == 1:
        return exact_matches[0]

    normalized_input = _normalize_employee_id(raw_id)
    if not normalized_input:
        return None

    # Fallback path for kiosk badge IDs: narrow in SQL first so we don't
    # load the entire user table on every login.
    # normalized_input is always 4 digits of zero-padded trailing digits.
    # A stored employee_id ("339", "0339", "EMP-00339") can match the same
    # normalized value, so we probe with the digit-core (leading zeros
    # stripped). For the degenerate "0000" case we fall back to matching
    # any row whose employee_id contains a zero. limit(50) bounds worst
    # case; if you have 50+ duplicates the existing 409 conflict path
    # already tells the admin to clean up.
    core_digits = normalized_input.lstrip("0") or normalized_input[-1:]
    candidates = (
        db.query(User)
        .filter(
            User.employee_id.isnot(None),
            User.employee_id.ilike(f"%{core_digits}%"),
        )
        .limit(50)
        .all()
    )
    matches = [u for u in candidates if _normalize_employee_id(u.employee_id) == normalized_input]
    if not matches:
        return None
    if len(matches) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Employee ID is not unique. Please contact an administrator."
        )
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

    user = _find_user_by_employee_id(db, payload.employee_id)

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
def employee_logout(request: Request, payload: EmployeeLoginRequest, db: Session = Depends(get_db)):
    """
    Log a logout event for the given employee ID.
    Note: JWT invalidation is handled client-side.
    """
    user = _find_user_by_employee_id(db, payload.employee_id)
    if not user:
        raise HTTPException(status_code=404, detail="Employee ID not found")

    # AuditService only flushes; commit so the audit row persists before the
    # request session closes.
    log_auth_event(db, "EMPLOYEE_LOGOUT", user=user, success=True, request=request)
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
    """
    raw_id = (employee_id or "").strip()
    if not raw_id:
        return None

    exact_matches = (
        db.query(User).filter(func.lower(User.employee_id) == raw_id.lower(), User.company_id == company_id).all()
    )
    if len(exact_matches) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Employee ID is not unique. Please contact an administrator."
        )
    if len(exact_matches) == 1:
        return exact_matches[0]

    normalized_input = _normalize_employee_id(raw_id)
    if not normalized_input:
        return None

    # Same bounded fallback as the global helper (see its comment), fenced to
    # the station's company.
    core_digits = normalized_input.lstrip("0") or normalized_input[-1:]
    candidates = (
        db.query(User)
        .filter(
            User.company_id == company_id,
            User.employee_id.isnot(None),
            User.employee_id.ilike(f"%{core_digits}%"),
        )
        .limit(50)
        .all()
    )
    matches = [u for u in candidates if _normalize_employee_id(u.employee_id) == normalized_input]
    if not matches:
        return None
    if len(matches) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Employee ID is not unique. Please contact an administrator."
        )
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

    user = _find_user_by_employee_id_in_company(db, payload.employee_id, station.company_id)

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


@router.post("/register-public")
def register_public(
    request: Request,
    user_in: PublicRegister,
    db: Session = Depends(get_db),
):
    """
    Public registration endpoint.

    - If no users exist yet this is the initial system setup: the first user
      is created as an active admin with superuser privileges.
    - Otherwise the account is created with the VIEWER role, inactive
      (pending admin approval).
    """
    # Check for duplicate email
    if db.query(User).filter(func.lower(User.email) == user_in.email.lower()).first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    # Auto-generate employee_id from email if not provided
    employee_id = user_in.employee_id
    if not employee_id:
        # Use email local part as base, e.g. "jmw@wercomfg.com" -> "jmw"
        base = re.sub(r'[^a-zA-Z0-9\-_]', '', user_in.email.split('@')[0])
        candidate = base
        suffix = 2
        while db.query(User).filter(func.lower(User.employee_id) == candidate.lower()).first():
            candidate = f"{base}-{suffix}"
            suffix += 1
        employee_id = candidate
    else:
        # Check for duplicate employee_id only if explicitly provided
        if db.query(User).filter(func.lower(User.employee_id) == employee_id.lower()).first():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Employee ID already exists",
            )

    user_count = db.query(User).count()
    is_first_user = user_count == 0

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
        email=user_in.email,
        employee_id=employee_id,
        first_name=user_in.first_name,
        last_name=user_in.last_name,
        role=role,
        is_superuser=is_superuser,
        is_active=is_active,
        hashed_password=get_password_hash(user_in.password),
        company_id=initial_company_id,
    )

    db.add(user)
    db.flush()  # assign the PK without committing so the audit row carries a real resource_id

    # Log BEFORE the terminal commit so the audit row commits atomically with the new
    # user (and the initial company, if this is the first-user bootstrap).
    action = "FIRST_USER_REGISTERED" if is_first_user else "PUBLIC_REGISTRATION"
    log_auth_event(db, action, user=user, success=True, request=request)
    db.commit()
    db.refresh(user)

    if is_first_user:
        return {"message": "Admin account created successfully", "is_first_user": True}
    else:
        return {"message": "Account submitted for approval", "is_first_user": False}


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


@router.post("/reset-database")
def reset_database(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Reset all data in the database. Protected by SECRET_KEY header.
    Once used, remove this endpoint or set ALLOW_DB_RESET=false.
    """
    import os

    from sqlalchemy import text

    # Must provide the SECRET_KEY as authorization
    provided_key = request.headers.get("X-Reset-Key", "")
    actual_key = os.environ.get("SECRET_KEY", "")
    if not provided_key or provided_key != actual_key:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid reset key")

    # Safety check — can be disabled via env var after go-live
    if os.environ.get("ALLOW_DB_RESET", "false").lower() != "true":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Database reset is disabled")

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
