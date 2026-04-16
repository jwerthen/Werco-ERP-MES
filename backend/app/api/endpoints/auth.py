from datetime import datetime, timedelta
from typing import Optional
import re
from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.db.database import get_db
from app.core.security import (
    verify_password, get_password_hash, create_access_token, 
    create_refresh_token, verify_refresh_token
)
from app.core.config import settings
from app.models.user import User, UserRole
from app.schemas.user import UserCreate, UserResponse, Token, TokenRefresh, RefreshTokenRequest, EmployeeLoginRequest, PublicRegister
from app.models.company import Company
from app.api.deps import get_current_user, get_current_company_id, require_role, require_platform_admin
from app.services.audit_service import AuditService

router = APIRouter()


def log_auth_event(db: Session, action: str, user: User = None, email: str = None,
                   success: bool = True, request: Request = None, error: str = None):
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
def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db)
):
    """
    Authenticate a user and receive JWT access and refresh tokens.
    
    **Rate limited**: 5 attempts per minute
    
    **Account lockout**: After 5 failed attempts, account is locked for 30 minutes (CMMC compliance).
    
    **Request body** (form data):
    - username: User's email address
    - password: User's password
    
    **Returns**:
    - access_token: JWT token for API authorization (valid for 30 minutes)
    - refresh_token: Token to obtain new access tokens (valid for 7 days)
    - token_type: Always "bearer"
    - expires_in: Token expiration time in seconds
    
    **Raises**:
    - 401: Invalid credentials
    - 403: Account locked or inactive
    """
    user = _find_user_by_auth_email(db, form_data.username)
    
    if not user:
        log_auth_event(db, "LOGIN_FAILED", email=form_data.username, 
                      success=False, request=request, error="User not found")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password"
        )
    
    # Check if account is locked (CMMC requirement)
    if user.locked_until and user.locked_until > datetime.utcnow():
        log_auth_event(db, "LOGIN_BLOCKED", user=user, success=False, 
                      request=request, error="Account locked")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is locked. Please contact administrator."
        )
    
    if not verify_password(form_data.password, user.hashed_password):
        # Increment failed attempts
        user.failed_login_attempts += 1
        
        # Lock account after 5 failed attempts (CMMC requirement)
        if user.failed_login_attempts >= 5:
            user.locked_until = datetime.utcnow() + timedelta(minutes=30)
        
        db.commit()
        log_auth_event(db, "LOGIN_FAILED", user=user, success=False, 
                      request=request, error="Invalid password")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password"
        )
    
    if not user.is_active:
        log_auth_event(db, "LOGIN_FAILED", user=user, success=False, 
                      request=request, error="Account disabled")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is disabled"
        )
    
    # Reset failed attempts on successful login
    user.failed_login_attempts = 0
    user.locked_until = None
    db.commit()
    
    # Create access token (short-lived) with company context
    access_token = create_access_token(subject=user.id, company_id=user.company_id)

    # Create refresh token (longer-lived, with rotation)
    refresh_token, session_id, _ = create_refresh_token(subject=user.id, company_id=user.company_id)

    log_auth_event(db, "LOGIN_SUCCESS", user=user, success=True, request=request)
    _ensure_valid_auth_email(user, db)

    return Token(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=UserResponse.model_validate(user)
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
    """
    email = (user.email or "").strip()
    if email.lower().endswith("@werco.local"):
        user.email = _build_repaired_email(user, db)
        db.commit()
        db.refresh(user)


def _find_user_by_employee_id(db: Session, employee_id: str) -> Optional[User]:
    """Find user by exact employee ID, then fallback to 4-digit badge normalization."""
    raw_id = (employee_id or "").strip()
    if not raw_id:
        return None

    exact_matches = db.query(User).filter(func.lower(User.employee_id) == raw_id.lower()).all()
    if len(exact_matches) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Employee ID is not unique. Please contact an administrator."
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
    matches = [
        u for u in candidates
        if _normalize_employee_id(u.employee_id) == normalized_input
    ]
    if not matches:
        return None
    if len(matches) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Employee ID is not unique. Please contact an administrator."
        )
    return matches[0]


@router.post("/employee-login", response_model=Token, summary="Employee ID login")
def employee_login(
    request: Request,
    payload: EmployeeLoginRequest,
    db: Session = Depends(get_db)
):
    """
    Authenticate a user by employee ID or 4-digit badge ID and receive JWT tokens.
    Intended for shop floor job stations and kiosks.
    """
    user = _find_user_by_employee_id(db, payload.employee_id)

    if not user:
        log_auth_event(db, "EMPLOYEE_LOGIN_FAILED", email=None,
                      success=False, request=request, error="Employee ID not found")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid employee ID"
        )

    if user.locked_until and user.locked_until > datetime.utcnow():
        log_auth_event(db, "EMPLOYEE_LOGIN_BLOCKED", user=user, success=False,
                      request=request, error="Account locked")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is locked. Please contact administrator."
        )

    if not user.is_active:
        log_auth_event(db, "EMPLOYEE_LOGIN_FAILED", user=user, success=False,
                      request=request, error="Account disabled")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is disabled"
        )

    # Reset failed attempts on successful login
    user.failed_login_attempts = 0
    user.locked_until = None
    db.commit()

    access_token = create_access_token(subject=user.id, company_id=user.company_id)
    refresh_token, _, _ = create_refresh_token(subject=user.id, company_id=user.company_id)

    log_auth_event(db, "EMPLOYEE_LOGIN_SUCCESS", user=user, success=True, request=request)
    _ensure_valid_auth_email(user, db)

    return Token(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=UserResponse.model_validate(user)
    )


@router.post("/employee-logout", summary="Employee ID logout")
def employee_logout(
    request: Request,
    payload: EmployeeLoginRequest,
    db: Session = Depends(get_db)
):
    """
    Log a logout event for the given employee ID.
    Note: JWT invalidation is handled client-side.
    """
    user = _find_user_by_employee_id(db, payload.employee_id)
    if not user:
        raise HTTPException(status_code=404, detail="Employee ID not found")

    log_auth_event(db, "EMPLOYEE_LOGOUT", user=user, success=True, request=request)
    return {"message": "Logged out successfully"}


@router.post("/refresh", response_model=TokenRefresh)
def refresh_token(
    request: Request,
    token_request: RefreshTokenRequest,
    db: Session = Depends(get_db)
):
    """
    Refresh an access token using a refresh token.
    Implements token rotation: returns new refresh token each time.
    """
    # Verify the refresh token
    payload = verify_refresh_token(token_request.refresh_token)
    
    if not payload:
        log_auth_event(db, "TOKEN_REFRESH_FAILED", email="unknown", 
                      success=False, request=request, error="Invalid or expired refresh token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token"
        )
    
    # Get the user
    user_id = payload.get("user_id")
    session_id = payload.get("session_id")
    
    user = db.query(User).filter(User.id == int(user_id)).first()
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found"
        )
    
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is disabled"
        )
    
    # Check if account got locked since last token
    if user.locked_until and user.locked_until > datetime.utcnow():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is locked"
        )
    
    # Preserve company context from the refresh token
    token_company_id = payload.get("company_id") or user.company_id

    # Create new access token
    new_access_token = create_access_token(subject=user.id, company_id=token_company_id)

    # Token rotation: create NEW refresh token (invalidates the old one implicitly)
    # Use same session_id to maintain session continuity
    new_refresh_token, _, _ = create_refresh_token(subject=user.id, session_id=session_id, company_id=token_company_id)
    
    log_auth_event(db, "TOKEN_REFRESHED", user=user, success=True, request=request)
    
    return TokenRefresh(
        access_token=new_access_token,
        refresh_token=new_refresh_token,
        token_type="bearer",
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    )


@router.post("/logout")
def logout(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Logout endpoint - logs the event.
    Note: With JWTs, true server-side invalidation requires a token blacklist (Redis).
    Client should discard tokens on logout.
    """
    log_auth_event(db, "LOGOUT", user=current_user, success=True, request=request)
    return {"message": "Logged out successfully"}


@router.post("/register", response_model=UserResponse)
def register(
    request: Request,
    user_in: UserCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN])),
    company_id: int = Depends(get_current_company_id)
):
    """Register a new user within the current company (admin only)"""
    # Check if email already exists within this company
    if db.query(User).filter(User.email == user_in.email, User.company_id == company_id).first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )

    # Check if employee_id already exists within this company
    if db.query(User).filter(User.employee_id == user_in.employee_id, User.company_id == company_id).first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Employee ID already exists"
        )

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
    db.commit()
    db.refresh(user)

    log_auth_event(db, "USER_REGISTERED", user=user, success=True, request=request)

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
    db.commit()
    db.refresh(user)

    action = "FIRST_USER_REGISTERED" if is_first_user else "PUBLIC_REGISTRATION"
    log_auth_event(db, action, user=user, success=True, request=request)

    if is_first_user:
        return {"message": "Admin account created successfully", "is_first_user": True}
    else:
        return {"message": "Account submitted for approval", "is_first_user": False}


@router.post("/switch-company/{target_company_id}", response_model=Token)
def switch_company(
    target_company_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_platform_admin)
):
    """
    Switch the active company context (platform admin only).
    Issues new tokens scoped to the target company for read-only browsing.
    """
    company = db.query(Company).filter(Company.id == target_company_id, Company.is_active == True).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found or inactive")

    access_token = create_access_token(subject=current_user.id, company_id=company.id)
    refresh_token, _, _ = create_refresh_token(subject=current_user.id, company_id=company.id)

    log_auth_event(db, "COMPANY_SWITCH", user=current_user, success=True, request=request)

    return Token(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=UserResponse.model_validate(current_user)
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

    tables_result = db.execute(text(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
        "AND tablename != 'alembic_version'"
    ))
    tables = [row[0] for row in tables_result]

    db.execute(text("SET session_replication_role = 'replica'"))
    for table in tables:
        db.execute(text(f'TRUNCATE TABLE "{table}" CASCADE'))
    db.execute(text("SET session_replication_role = 'origin'"))
    db.commit()

    return {"message": f"All {len(tables)} tables cleared. Visit /register to create admin account."}
