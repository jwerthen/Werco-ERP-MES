from datetime import datetime, timedelta
from typing import Optional
import re
from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.core.security import (
    verify_password, get_password_hash, create_access_token, 
    create_refresh_token, verify_refresh_token
)
from app.core.config import settings
from app.models.user import User, UserRole
from app.schemas.user import UserCreate, UserResponse, UserLogin, Token, TokenRefresh, RefreshTokenRequest, EmployeeLoginRequest
from app.api.deps import get_current_user, require_role
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
    user = db.query(User).filter(User.email == form_data.username).first()
    
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
    
    # Create access token (short-lived)
    access_token = create_access_token(subject=user.id)
    
    # Create refresh token (longer-lived, with rotation)
    refresh_token, session_id, _ = create_refresh_token(subject=user.id)
    
    log_auth_event(db, "LOGIN_SUCCESS", user=user, success=True, request=request)
    
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


def _find_user_by_employee_id(db: Session, employee_id: str) -> Optional[User]:
    """Find user by normalized 4-digit employee_id."""
    candidates = db.query(User).all()
    matches = [
        u for u in candidates
        if _normalize_employee_id(u.employee_id) == employee_id
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
    Authenticate a user by 4-digit employee ID and receive JWT tokens.
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

    access_token = create_access_token(subject=user.id)
    refresh_token, _, _ = create_refresh_token(subject=user.id)

    log_auth_event(db, "EMPLOYEE_LOGIN_SUCCESS", user=user, success=True, request=request)

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
    
    # Create new access token
    new_access_token = create_access_token(subject=user.id)
    
    # Token rotation: create NEW refresh token (invalidates the old one implicitly)
    # Use same session_id to maintain session continuity
    new_refresh_token, _, _ = create_refresh_token(subject=user.id, session_id=session_id)
    
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
    current_user: User = Depends(require_role([UserRole.ADMIN]))
):
    """Register a new user (admin only)"""
    # Check if email already exists
    if db.query(User).filter(User.email == user_in.email).first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )
    
    # Check if employee_id already exists
    if db.query(User).filter(User.employee_id == user_in.employee_id).first():
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
    )
    
    db.add(user)
    db.commit()
    db.refresh(user)
    
    log_auth_event(db, "USER_REGISTERED", user=user, success=True, request=request)
    
    return user
