import re
import secrets
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.api.deps import get_current_company_id, get_current_user, require_role
from app.core.security import get_password_hash, verify_password
from app.db.database import get_db
from app.models.user import User, UserRole
from app.services.audit_service import AuditService
from app.services.import_service import ImportFileError, parse_import_file

router = APIRouter()


class UserCreate(BaseModel):
    email: EmailStr
    employee_id: str
    first_name: str
    last_name: str
    password: str
    role: UserRole = UserRole.OPERATOR
    department: Optional[str] = None
    phone: Optional[str] = None


class UserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    role: Optional[UserRole] = None
    department: Optional[str] = None
    phone: Optional[str] = None
    is_active: Optional[bool] = None


class UserApproval(BaseModel):
    role: UserRole = UserRole.OPERATOR
    department: Optional[str] = None


class PendingApprovalSummary(BaseModel):
    count: int


class PasswordReset(BaseModel):
    new_password: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class UserResponse(BaseModel):
    id: int
    version: Optional[int] = 0
    email: str
    employee_id: str
    first_name: str
    last_name: str
    role: UserRole
    department: Optional[str] = None
    phone: Optional[str] = None
    is_active: bool
    is_superuser: bool = False
    company_id: Optional[int] = None
    company_name: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    last_login: Optional[datetime] = None

    class Config:
        from_attributes = True


class UserCsvImportError(BaseModel):
    row: int
    employee_id: Optional[str] = None
    email: Optional[str] = None
    reason: str


class UserCsvImportResponse(BaseModel):
    total_rows: int
    created_count: int
    skipped_count: int
    created_ids: List[int]
    errors: List[UserCsvImportError]
    dry_run: bool = False


def _generated_email(employee_id: str, existing_emails: set[str]) -> str:
    local_part = re.sub(r"[^a-z0-9._-]", "", employee_id.lower())
    if not local_part:
        local_part = "employee"

    base = f"emp-{local_part}"
    candidate = f"{base}@users.werco.com"
    suffix = 2
    while candidate in existing_emails:
        candidate = f"{base}-{suffix}@users.werco.com"
        suffix += 1
    return candidate


def _generate_system_password() -> str:
    """Generate a strong password for users authenticating by employee ID."""
    token = secrets.token_urlsafe(18)
    return f"Auto!{token}1aA"


@router.get("/", response_model=List[UserResponse])
def list_users(
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
):
    """List all users"""
    query = db.query(User).filter(User.company_id == company_id)
    if not include_inactive:
        query = query.filter(User.is_active == True)
    users = query.order_by(User.last_name, User.first_name).all()
    return users


def _pending_approval_query(db: Session, company_id: int):
    return db.query(User).filter(
        User.company_id == company_id,
        User.is_active == False,
        User.role == UserRole.VIEWER,
    )


@router.get("/pending-approvals", response_model=List[UserResponse])
def list_pending_approvals(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN])),
    company_id: int = Depends(get_current_company_id),
):
    """List inactive self-registered accounts awaiting admin approval."""
    return _pending_approval_query(db, company_id).order_by(User.created_at.desc()).all()


@router.get("/pending-approvals/summary", response_model=PendingApprovalSummary)
def pending_approval_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN])),
    company_id: int = Depends(get_current_company_id),
):
    """Return the number of self-registered accounts awaiting approval."""
    return PendingApprovalSummary(count=_pending_approval_query(db, company_id).count())


@router.get("/me", response_model=UserResponse)
def get_current_user_info(current_user: User = Depends(get_current_user)):
    """Get current user info"""
    return current_user


@router.get("/{user_id}", response_model=UserResponse)
def get_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
):
    """Get user by ID"""
    user = db.query(User).filter(User.id == user_id, User.company_id == company_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.post("/", response_model=UserResponse)
def create_user(
    user_in: UserCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN])),
    company_id: int = Depends(get_current_company_id),
):
    """Create a new user (Admin only)"""
    # Check if email exists
    if db.query(User).filter(User.email == user_in.email, User.company_id == company_id).first():
        raise HTTPException(status_code=400, detail="Email already registered")

    # Check if employee_id exists
    if db.query(User).filter(User.employee_id == user_in.employee_id, User.company_id == company_id).first():
        raise HTTPException(status_code=400, detail="Employee ID already exists")

    user = User(
        email=user_in.email,
        employee_id=user_in.employee_id,
        first_name=user_in.first_name,
        last_name=user_in.last_name,
        hashed_password=get_password_hash(user_in.password),
        role=user_in.role,
        department=user_in.department,
    )
    user.company_id = company_id
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/import-csv", response_model=UserCsvImportResponse)
async def import_users_csv(
    request: Request,
    file: UploadFile = File(...),
    default_password: Optional[str] = Form(None),
    dry_run: bool = Query(False, description="Validate only; no rows are written"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN])),
    company_id: int = Depends(get_current_company_id),
):
    """Import users from CSV or XLSX (Admin only)."""
    content = await file.read()
    try:
        table = parse_import_file(file.filename, content, required_columns={"employee_id", "first_name", "last_name"})
    except ImportFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    existing_employee_ids = {
        (value or "").strip().lower()
        for (value,) in db.query(User.employee_id).filter(User.company_id == company_id).all()
    }
    existing_emails = {
        (value or "").strip().lower() for (value,) in db.query(User.email).filter(User.company_id == company_id).all()
    }

    audit = AuditService(db, current_user, request)
    errors: List[UserCsvImportError] = []
    created_ids: List[int] = []
    total_rows = 0
    accepted_count = 0
    default_password = (default_password or "").strip()
    valid_roles = sorted([role.value for role in UserRole])

    for row_number, row in table.iter_rows():
        total_rows += 1
        employee_id = row.get("employee_id", "")
        first_name = row.get("first_name", "")
        last_name = row.get("last_name", "")
        email = row.get("email", "")
        password = row.get("password", "") or default_password
        role_raw = (row.get("role", UserRole.OPERATOR.value) or UserRole.OPERATOR.value).strip().lower()
        department = row.get("department") or None

        if not employee_id:
            errors.append(UserCsvImportError(row=row_number, reason="employee_id is required"))
            continue

        employee_key = employee_id.lower()
        if employee_key in existing_employee_ids:
            errors.append(
                UserCsvImportError(
                    row=row_number,
                    employee_id=employee_id,
                    email=email or None,
                    reason="Employee ID already exists",
                )
            )
            continue

        if not first_name or not last_name:
            errors.append(
                UserCsvImportError(
                    row=row_number,
                    employee_id=employee_id,
                    email=email or None,
                    reason="first_name and last_name are required",
                )
            )
            continue

        try:
            role = UserRole(role_raw)
        except ValueError:
            errors.append(
                UserCsvImportError(
                    row=row_number,
                    employee_id=employee_id,
                    email=email or None,
                    reason=f"Invalid role '{role_raw}'. Valid roles: {', '.join(valid_roles)}",
                )
            )
            continue

        if not password:
            if role == UserRole.OPERATOR:
                password = _generate_system_password()
            else:
                errors.append(
                    UserCsvImportError(
                        row=row_number,
                        employee_id=employee_id,
                        email=email or None,
                        reason="password is required for non-operator roles (CSV column or default_password form value)",
                    )
                )
                continue

        if not password:
            errors.append(
                UserCsvImportError(
                    row=row_number,
                    employee_id=employee_id,
                    email=email or None,
                    reason="password is required",
                )
            )
            continue

        if not email:
            email = _generated_email(employee_id, existing_emails)

        email_key = email.lower()
        if email_key in existing_emails:
            errors.append(
                UserCsvImportError(
                    row=row_number,
                    employee_id=employee_id,
                    email=email,
                    reason="Email already registered",
                )
            )
            continue

        if dry_run:
            accepted_count += 1
            existing_employee_ids.add(employee_key)
            existing_emails.add(email_key)
            continue

        try:
            user = User(
                email=email,
                employee_id=employee_id,
                first_name=first_name,
                last_name=last_name,
                hashed_password=get_password_hash(password),
                role=role,
                department=department,
            )
            user.company_id = company_id
            db.add(user)
            db.flush()
            audit.log_create(
                "user",
                user.id,
                user.employee_id,
                # Deliberately not passing new_values: the model carries
                # hashed_password and secrets must never land in the audit log.
                description=f"Created user {user.employee_id} via import",
                extra_data={"source": "import", "role": role.value, "email": user.email},
            )
            db.commit()
            db.refresh(user)
        except Exception:
            db.rollback()
            errors.append(
                UserCsvImportError(
                    row=row_number,
                    employee_id=employee_id,
                    email=email,
                    reason="Failed to create user due to a database constraint",
                )
            )
            continue

        created_ids.append(user.id)
        accepted_count += 1
        existing_employee_ids.add(employee_key)
        existing_emails.add(email_key)

    return UserCsvImportResponse(
        total_rows=total_rows,
        created_count=accepted_count,
        skipped_count=total_rows - accepted_count,
        created_ids=created_ids,
        errors=errors,
        dry_run=dry_run,
    )


@router.put("/{user_id}", response_model=UserResponse)
def update_user(
    user_id: int,
    user_in: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN])),
    company_id: int = Depends(get_current_company_id),
):
    """Update a user (Admin only)"""
    user = db.query(User).filter(User.id == user_id, User.company_id == company_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    update_data = user_in.model_dump(exclude_unset=True)

    # Check email uniqueness if changing
    if "email" in update_data and update_data["email"] != user.email:
        if db.query(User).filter(User.email == update_data["email"], User.company_id == company_id).first():
            raise HTTPException(status_code=400, detail="Email already registered")

    for field, value in update_data.items():
        setattr(user, field, value)

    db.commit()
    db.refresh(user)
    return user


@router.post("/{user_id}/approve", response_model=UserResponse)
def approve_user(
    user_id: int,
    approval: UserApproval,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN])),
    company_id: int = Depends(get_current_company_id),
):
    """Approve a self-registered user and assign their operational role."""
    if approval.role == UserRole.PLATFORM_ADMIN:
        raise HTTPException(status_code=400, detail="Platform admin role cannot be assigned through approval")

    user = db.query(User).filter(User.id == user_id, User.company_id == company_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.is_active:
        raise HTTPException(status_code=400, detail="User is already active")
    if user.role != UserRole.VIEWER:
        raise HTTPException(status_code=400, detail="Only pending self-registered users can be approved")

    user.role = approval.role
    if approval.department is not None:
        user.department = approval.department
    user.is_active = True
    db.commit()
    db.refresh(user)
    return user


@router.post("/{user_id}/reset-password")
def reset_user_password(
    user_id: int,
    password_data: PasswordReset,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN])),
    company_id: int = Depends(get_current_company_id),
):
    """Reset a user's password (Admin only)"""
    user = db.query(User).filter(User.id == user_id, User.company_id == company_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.hashed_password = get_password_hash(password_data.new_password)
    db.commit()

    return {"message": "Password reset successfully"}


@router.post("/change-password")
def change_own_password(
    password_data: PasswordChange, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    """Change own password"""
    if not verify_password(password_data.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    current_user.hashed_password = get_password_hash(password_data.new_password)
    db.commit()

    return {"message": "Password changed successfully"}


@router.delete("/{user_id}")
def deactivate_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN])),
    company_id: int = Depends(get_current_company_id),
):
    """Deactivate a user (Admin only)"""
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself")

    user = db.query(User).filter(User.id == user_id, User.company_id == company_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.is_active = False
    db.commit()

    return {"message": "User deactivated"}


@router.post("/{user_id}/activate")
def activate_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN])),
    company_id: int = Depends(get_current_company_id),
):
    """Reactivate a user (Admin only)"""
    user = db.query(User).filter(User.id == user_id, User.company_id == company_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.is_active = True
    db.commit()

    return {"message": "User activated"}
