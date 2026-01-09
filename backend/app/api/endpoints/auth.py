from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.core.security import verify_password, get_password_hash, create_access_token
from app.models.user import User, UserRole
from app.models.audit_log import AuditLog
from app.schemas.user import UserCreate, UserResponse, UserLogin, Token
from app.api.deps import get_current_user, require_role

router = APIRouter()


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


@router.post("/login", response_model=Token)
def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db)
):
    """Authenticate user and return JWT token"""
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
    
    # Create access token
    access_token = create_access_token(subject=user.id)
    
    log_auth_event(db, "LOGIN_SUCCESS", user=user, success=True, request=request)
    
    return Token(
        access_token=access_token,
        token_type="bearer",
        user=UserResponse.model_validate(user)
    )


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
