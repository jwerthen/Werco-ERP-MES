from typing import List, Optional
import csv
import io
import re
import secrets
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.api.deps import get_current_user, require_role
from app.models.user import User, UserRole
from app.core.security import get_password_hash, verify_password
from pydantic import BaseModel, EmailStr
from datetime import datetime

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


class PasswordReset(BaseModel):
    new_password: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class UserResponse(BaseModel):
    id: int
    email: str
    employee_id: str
    first_name: str
    last_name: str
    role: UserRole
    department: Optional[str] = None
    phone: Optional[str] = None
    is_active: bool
    created_at: datetime
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


def _normalize_csv_header(header: str) -> str:
    normalized = (header or "").strip().lower()
    normalized = normalized.replace("-", "_").replace(" ", "_")
    return re.sub(r"[^a-z0-9_]", "", normalized)


def _generated_email(employee_id: str, existing_emails: set[str]) -> str:
    local_part = re.sub(r"[^a-z0-9._-]", "", employee_id.lower())
    if not local_part:
        local_part = "employee"

    base = f"emp-{local_part}"
    candidate = f"{base}@werco.local"
    suffix = 2
    while candidate in existing_emails:
        candidate = f"{base}-{suffix}@werco.local"
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
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """List all users"""
    query = db.query(User)
    if not include_inactive:
        query = query.filter(User.is_active == True)
    users = query.order_by(User.last_name, User.first_name).all()
    return users


@router.get("/me", response_model=UserResponse)
def get_current_user_info(current_user: User = Depends(get_current_user)):
    """Get current user info"""
    return current_user


@router.get("/{user_id}", response_model=UserResponse)
def get_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Get user by ID"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.post("/", response_model=UserResponse)
def create_user(
    user_in: UserCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN]))
):
    """Create a new user (Admin only)"""
    # Check if email exists
    if db.query(User).filter(User.email == user_in.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")
    
    # Check if employee_id exists
    if db.query(User).filter(User.employee_id == user_in.employee_id).first():
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
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/import-csv", response_model=UserCsvImportResponse)
async def import_users_csv(
    file: UploadFile = File(...),
    default_password: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN]))
):
    """Import users from CSV (Admin only)."""
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Please upload a CSV file")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded CSV file is empty")

    try:
        decoded_content = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="CSV must be UTF-8 encoded")

    reader = csv.DictReader(io.StringIO(decoded_content))
    if not reader.fieldnames:
        raise HTTPException(status_code=400, detail="CSV must include a header row")

    header_map = {raw: _normalize_csv_header(raw) for raw in reader.fieldnames if raw}
    required_headers = {"employee_id", "first_name", "last_name"}
    missing_headers = sorted(required_headers - set(header_map.values()))
    if missing_headers:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required CSV columns: {', '.join(missing_headers)}"
        )

    existing_employee_ids = {
        (value or "").strip().lower()
        for (value,) in db.query(User.employee_id).all()
    }
    existing_emails = {
        (value or "").strip().lower()
        for (value,) in db.query(User.email).all()
    }

    errors: List[UserCsvImportError] = []
    created_ids: List[int] = []
    total_rows = 0
    default_password = (default_password or "").strip()
    valid_roles = sorted([role.value for role in UserRole])

    for row_number, raw_row in enumerate(reader, start=2):
        row = {}
        for raw_key, raw_value in raw_row.items():
            if not raw_key:
                continue
            row[header_map.get(raw_key, _normalize_csv_header(raw_key))] = (raw_value or "").strip()

        if not any(row.values()):
            continue

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
            db.add(user)
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
        existing_employee_ids.add(employee_key)
        existing_emails.add(email_key)

    return UserCsvImportResponse(
        total_rows=total_rows,
        created_count=len(created_ids),
        skipped_count=total_rows - len(created_ids),
        created_ids=created_ids,
        errors=errors,
    )


@router.put("/{user_id}", response_model=UserResponse)
def update_user(
    user_id: int,
    user_in: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN]))
):
    """Update a user (Admin only)"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    update_data = user_in.model_dump(exclude_unset=True)
    
    # Check email uniqueness if changing
    if "email" in update_data and update_data["email"] != user.email:
        if db.query(User).filter(User.email == update_data["email"]).first():
            raise HTTPException(status_code=400, detail="Email already registered")
    
    for field, value in update_data.items():
        setattr(user, field, value)
    
    db.commit()
    db.refresh(user)
    return user


@router.post("/{user_id}/reset-password")
def reset_user_password(
    user_id: int,
    password_data: PasswordReset,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN]))
):
    """Reset a user's password (Admin only)"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    user.hashed_password = get_password_hash(password_data.new_password)
    db.commit()
    
    return {"message": "Password reset successfully"}


@router.post("/change-password")
def change_own_password(
    password_data: PasswordChange,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
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
    current_user: User = Depends(require_role([UserRole.ADMIN]))
):
    """Deactivate a user (Admin only)"""
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself")
    
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    user.is_active = False
    db.commit()
    
    return {"message": "User deactivated"}


@router.post("/{user_id}/activate")
def activate_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN]))
):
    """Reactivate a user (Admin only)"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    user.is_active = True
    db.commit()
    
    return {"message": "User activated"}
