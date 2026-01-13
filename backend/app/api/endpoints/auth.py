from datetime import datetime, timedelta
from typing import Optional, List, Union
from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from pydantic import BaseModel
from app.db.database import get_db
from app.core.security import (
    verify_password, get_password_hash, create_access_token, 
    create_refresh_token, verify_refresh_token
)
from app.core.config import settings
from app.models.user import User, UserRole
from app.models.audit_log import AuditLog
from app.schemas.user import UserCreate, UserResponse, UserLogin, Token, TokenRefresh, RefreshTokenRequest
from app.api.deps import get_current_user, require_role
from app.services.mfa_service import (
    setup_mfa, verify_totp, verify_backup_code, hash_backup_codes
)
from app.services.audit_service import AuditService

router = APIRouter()


# =============================================================================
# MFA Request/Response Models
# =============================================================================

class MFASetupResponse(BaseModel):
    """Response for MFA setup initiation."""
    secret: str
    qr_code: str  # Base64 encoded PNG
    provisioning_uri: str
    backup_codes: List[str]
    message: str = "Scan the QR code with your authenticator app, then verify with a code"


class MFAVerifyRequest(BaseModel):
    """Request to verify MFA code during setup or login."""
    code: str
    

class MFALoginRequest(BaseModel):
    """Request for MFA verification during login."""
    mfa_token: str  # Temporary token from initial login
    code: str  # TOTP or backup code


class MFARequiredResponse(BaseModel):
    """Response when MFA is required."""
    mfa_required: bool = True
    mfa_token: str  # Temporary token to complete MFA
    message: str = "MFA verification required"


class MFADisableRequest(BaseModel):
    """Request to disable MFA."""
    code: str  # Current TOTP code to confirm
    password: str  # Password for additional verification


def log_auth_event(db: Session, action: str, user: User = None, email: str = None, 
                   success: bool = True, request: Request = None, error: str = None):
    """Log authentication events for CMMC compliance"""
    log = AuditLog(
        user_id=user.id if user else None,
        user_email=user.email if user else email,
        user_name=user.full_name if user else None,
        action=action,
        resource_type="authentication",
        description=f"{action} attempt for {email or (user.email if user else 'unknown')}",
        success="true" if success else "false",
        error_message=error,
        ip_address=request.client.host if request else None,
        user_agent=request.headers.get("user-agent") if request else None,
    )
    db.add(log)
    db.commit()


@router.post("/login", summary="User login")
def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db)
) -> Union[Token, MFARequiredResponse]:
    """
    Authenticate a user and receive JWT access and refresh tokens.
    
    **Rate limited**: 5 attempts per minute
    
    **Account lockout**: After 5 failed attempts, account is locked for 30 minutes (CMMC compliance).
    
    **MFA Support** (CMMC Level 2 AC-3.1.1): If user has MFA enabled, returns mfa_required=true
    with a temporary token. Use /auth/mfa/verify to complete login.
    
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
    
    # Check if MFA is required (CMMC Level 2 AC-3.1.1)
    if user.mfa_enabled:
        # User has MFA enabled - require second factor
        mfa_token = create_mfa_token(user.id)
        log_auth_event(db, "LOGIN_MFA_REQUIRED", user=user, success=True, request=request)
        
        return MFARequiredResponse(
            mfa_required=True,
            mfa_token=mfa_token,
            message="MFA verification required. Use /auth/mfa/verify to complete login."
        )
    
    # No MFA - issue tokens directly (for users who haven't set up MFA yet)
    # Note: CMMC compliance requires all users to have MFA. Prompt them to set it up.
    access_token = create_access_token(subject=user.id)
    refresh_token, session_id, _ = create_refresh_token(subject=user.id)
    
    log_auth_event(db, "LOGIN_SUCCESS", user=user, success=True, request=request)
    
    response = Token(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=UserResponse.model_validate(user)
    )
    
    return response


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
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Register a new user (admin or manager only)"""
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


# =============================================================================
# MFA Endpoints - CMMC Level 2 AC-3.1.1
# =============================================================================

# Temporary storage for MFA setup (in production, use Redis with TTL)
_mfa_setup_pending = {}  # email -> {secret, backup_codes_hashed, expires}
_mfa_login_pending = {}  # mfa_token -> {user_id, expires}


def create_mfa_token(user_id: int) -> str:
    """Create a temporary token for MFA verification during login."""
    import secrets
    token = secrets.token_urlsafe(32)
    _mfa_login_pending[token] = {
        "user_id": user_id,
        "expires": datetime.utcnow() + timedelta(minutes=5)
    }
    return token


def verify_mfa_token(token: str) -> Optional[int]:
    """Verify MFA token and return user_id if valid."""
    data = _mfa_login_pending.get(token)
    if not data:
        return None
    if datetime.utcnow() > data["expires"]:
        del _mfa_login_pending[token]
        return None
    return data["user_id"]


def consume_mfa_token(token: str) -> Optional[int]:
    """Consume (use once) MFA token and return user_id."""
    user_id = verify_mfa_token(token)
    if user_id and token in _mfa_login_pending:
        del _mfa_login_pending[token]
    return user_id


@router.get("/mfa/status", summary="Get MFA status for current user")
def get_mfa_status(
    current_user: User = Depends(get_current_user)
):
    """
    Get the MFA status for the current user.
    
    **CMMC Level 2 Control**: AC-3.1.1 - Multi-factor authentication
    """
    return {
        "mfa_enabled": current_user.mfa_enabled,
        "mfa_required": current_user.mfa_required,
        "mfa_pending_setup": current_user.mfa_pending_setup,
        "mfa_setup_at": current_user.mfa_setup_at.isoformat() if current_user.mfa_setup_at else None
    }


@router.post("/mfa/setup", response_model=MFASetupResponse, summary="Initialize MFA setup")
def initiate_mfa_setup(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Start the MFA setup process. Returns QR code and backup codes.
    
    The user must scan the QR code with an authenticator app (Google Authenticator,
    Authy, Microsoft Authenticator, etc.) and then call /mfa/setup/verify to confirm.
    
    **CMMC Level 2 Control**: AC-3.1.1 - Multi-factor authentication
    
    **Important**: Save the backup codes securely. They can be used if you lose access
    to your authenticator app.
    """
    if current_user.mfa_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA is already enabled. Disable it first to set up again."
        )
    
    # Generate MFA setup data
    mfa_data = setup_mfa(current_user.email)
    
    # Store pending setup (expires in 10 minutes)
    _mfa_setup_pending[current_user.email] = {
        "secret": mfa_data.secret,
        "backup_codes_hashed": mfa_data.backup_codes_hashed,
        "expires": datetime.utcnow() + timedelta(minutes=10)
    }
    
    log_auth_event(db, "MFA_SETUP_INITIATED", user=current_user, success=True, request=request)
    
    return MFASetupResponse(
        secret=mfa_data.secret,
        qr_code=mfa_data.qr_code_base64,
        provisioning_uri=mfa_data.provisioning_uri,
        backup_codes=mfa_data.backup_codes
    )


@router.post("/mfa/setup/verify", summary="Complete MFA setup")
def complete_mfa_setup(
    request: Request,
    verify_request: MFAVerifyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Complete MFA setup by verifying a TOTP code from the authenticator app.
    
    **CMMC Level 2 Control**: AC-3.1.1 - Multi-factor authentication
    """
    # Check for pending setup
    pending = _mfa_setup_pending.get(current_user.email)
    if not pending:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No pending MFA setup. Please call /mfa/setup first."
        )
    
    if datetime.utcnow() > pending["expires"]:
        del _mfa_setup_pending[current_user.email]
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA setup expired. Please start again."
        )
    
    # Verify the code
    if not verify_totp(pending["secret"], verify_request.code):
        log_auth_event(db, "MFA_SETUP_FAILED", user=current_user, success=False, 
                      request=request, error="Invalid verification code")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid verification code. Please try again."
        )
    
    # Save MFA to user
    current_user.mfa_enabled = True
    current_user.mfa_secret = pending["secret"]
    current_user.mfa_backup_codes = pending["backup_codes_hashed"]
    current_user.mfa_setup_at = datetime.utcnow()
    db.commit()
    
    # Clean up pending setup
    del _mfa_setup_pending[current_user.email]
    
    log_auth_event(db, "MFA_ENABLED", user=current_user, success=True, request=request)
    
    return {
        "success": True,
        "message": "MFA has been enabled successfully. You will need to provide a code on each login."
    }


@router.post("/mfa/verify", response_model=Token, summary="Verify MFA during login")
def verify_mfa_login(
    request: Request,
    mfa_request: MFALoginRequest,
    db: Session = Depends(get_db)
):
    """
    Complete login by providing MFA code.
    
    Called after initial login returns mfa_required=true.
    
    **CMMC Level 2 Control**: AC-3.1.1 - Multi-factor authentication
    """
    # Verify the MFA token
    user_id = consume_mfa_token(mfa_request.mfa_token)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired MFA session. Please login again."
        )
    
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found"
        )
    
    # Try TOTP verification first
    code = mfa_request.code.replace("-", "").replace(" ", "")
    
    if len(code) == 6 and code.isdigit():
        # Standard TOTP code
        if not verify_totp(user.mfa_secret, code):
            log_auth_event(db, "MFA_VERIFY_FAILED", user=user, success=False, 
                          request=request, error="Invalid TOTP code")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid verification code"
            )
    else:
        # Try backup code
        is_valid, used_index = verify_backup_code(mfa_request.code, user.mfa_backup_codes or [])
        if not is_valid:
            log_auth_event(db, "MFA_VERIFY_FAILED", user=user, success=False, 
                          request=request, error="Invalid code")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid verification code"
            )
        
        # Invalidate used backup code
        if used_index is not None and user.mfa_backup_codes:
            codes = list(user.mfa_backup_codes)
            codes[used_index] = None  # Mark as used
            user.mfa_backup_codes = codes
            db.commit()
            log_auth_event(db, "MFA_BACKUP_CODE_USED", user=user, success=True, request=request)
    
    # MFA verified - issue tokens
    access_token = create_access_token(subject=user.id)
    refresh_token, session_id, _ = create_refresh_token(subject=user.id)
    
    log_auth_event(db, "MFA_VERIFY_SUCCESS", user=user, success=True, request=request)
    log_auth_event(db, "LOGIN_SUCCESS", user=user, success=True, request=request)
    
    return Token(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=UserResponse.model_validate(user)
    )


@router.post("/mfa/disable", summary="Disable MFA")
def disable_mfa(
    request: Request,
    disable_request: MFADisableRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Disable MFA for the current user.
    
    Requires current TOTP code AND password for security.
    
    **Note**: Disabling MFA may affect CMMC compliance. Consider carefully.
    """
    if not current_user.mfa_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA is not enabled"
        )
    
    # Verify password
    if not verify_password(disable_request.password, current_user.hashed_password):
        log_auth_event(db, "MFA_DISABLE_FAILED", user=current_user, success=False, 
                      request=request, error="Invalid password")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid password"
        )
    
    # Verify TOTP code
    if not verify_totp(current_user.mfa_secret, disable_request.code):
        log_auth_event(db, "MFA_DISABLE_FAILED", user=current_user, success=False, 
                      request=request, error="Invalid TOTP code")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid verification code"
        )
    
    # Disable MFA
    current_user.mfa_enabled = False
    current_user.mfa_secret = None
    current_user.mfa_backup_codes = None
    current_user.mfa_setup_at = None
    db.commit()
    
    log_auth_event(db, "MFA_DISABLED", user=current_user, success=True, request=request)
    
    return {
        "success": True,
        "message": "MFA has been disabled. Your account is now less secure."
    }


@router.post("/mfa/backup-codes/regenerate", summary="Regenerate backup codes")
def regenerate_backup_codes(
    request: Request,
    verify_request: MFAVerifyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Regenerate backup codes. Old codes will be invalidated.
    
    Requires current TOTP code to confirm.
    """
    if not current_user.mfa_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA is not enabled"
        )
    
    # Verify TOTP code
    if not verify_totp(current_user.mfa_secret, verify_request.code):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid verification code"
        )
    
    # Generate new backup codes
    from app.services.mfa_service import generate_backup_codes
    new_codes = generate_backup_codes()
    
    current_user.mfa_backup_codes = hash_backup_codes(new_codes)
    db.commit()
    
    log_auth_event(db, "MFA_BACKUP_CODES_REGENERATED", user=current_user, success=True, request=request)
    
    return {
        "success": True,
        "backup_codes": new_codes,
        "message": "New backup codes generated. Old codes are now invalid. Save these securely."
    }
