import re
import secrets
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.core.security import get_password_hash, verify_password
from app.db.database import get_db
from app.models.user import User, UserRole
from app.schemas.user import validate_password_strength
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

    @field_validator("password")
    @classmethod
    def _validate_password(cls, v: str) -> str:
        # Reuse the canonical AS9100D/CMMC strength policy (schemas.user) so the
        # admin create path can't accept a weaker password than /auth/register.
        return validate_password_strength(v)


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

    @field_validator("new_password")
    @classmethod
    def _validate_password(cls, v: str) -> str:
        # Admin-driven reset must meet the same strength policy as registration;
        # a weak password here would otherwise bypass /auth/register enforcement.
        return validate_password_strength(v)


class PasswordChange(BaseModel):
    current_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _validate_password(cls, v: str) -> str:
        # Self-service password change must meet the same strength policy as the
        # admin create/reset and registration paths; a weak new password here
        # would otherwise bypass the enforced /auth/register policy.
        return validate_password_strength(v)


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


def _reject_platform_admin_assignment(role: Optional[UserRole]) -> None:
    """Reject assigning ``platform_admin`` from a tenant-scoped user endpoint.

    ``platform_admin`` is Werco's cross-company oversight role; it must never be
    mintable from a tenant path (create/update). Mirrors the inline guards in
    approve/import (which keep their own distinct wording).
    """
    if role == UserRole.PLATFORM_ADMIN:
        raise HTTPException(status_code=400, detail="Platform admin role cannot be assigned")


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
    audit: AuditService = Depends(get_audit_service),
):
    """Create a new user (Admin only).

    ``platform_admin`` is the cross-company Werco oversight role and can never be
    assigned from this tenant-scoped path. A company admin assigning ``admin``
    stays allowed per the RBAC matrix. The creation is recorded in the
    tamper-evident audit log.
    """
    # platform_admin is the cross-company oversight role; a tenant admin must not
    # be able to mint one here (mirrors the approve/import guards).
    _reject_platform_admin_assignment(user_in.role)

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
    db.flush()
    audit.log_create(
        "user",
        user.id,
        user.employee_id,
        # Deliberately not passing new_values: the model carries hashed_password
        # and secrets must never land in the audit log.
        description=f"Created user {user.employee_id}",
        extra_data={"source": "admin", "role": user.role.value, "email": user.email},
    )
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
    # Parse + import are CPU/DB-bound sync work; run them in the threadpool so a
    # large upload can't stall the event loop (the request-scoped Session/audit
    # are used sequentially from one worker thread — same as a sync endpoint).
    try:
        table = await run_in_threadpool(
            parse_import_file, file.filename, content, required_columns={"employee_id", "first_name", "last_name"}
        )
    except ImportFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _run_import() -> UserCsvImportResponse:
        existing_employee_ids = {
            (value or "").strip().lower()
            for (value,) in db.query(User.employee_id).filter(User.company_id == company_id).all()
        }
        existing_emails = {
            (value or "").strip().lower()
            for (value,) in db.query(User.email).filter(User.company_id == company_id).all()
        }

        audit = AuditService(db, current_user, request)
        errors: List[UserCsvImportError] = []
        created_ids: List[int] = []
        total_rows = 0
        accepted_count = 0
        # New name (not a rebind): assigning to `default_password` here would make
        # the captured Form parameter an unbound local inside this closure.
        fallback_password = (default_password or "").strip()
        # platform_admin is the cross-company Werco oversight role; it must never be
        # mintable from a tenant spreadsheet, so don't advertise it as valid either.
        valid_roles = sorted(role.value for role in UserRole if role != UserRole.PLATFORM_ADMIN)

        for row_number, row in table.iter_rows():
            total_rows += 1
            employee_id = row.get("employee_id", "")
            first_name = row.get("first_name", "")
            last_name = row.get("last_name", "")
            email = row.get("email", "")
            password = row.get("password", "") or fallback_password
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

            if role == UserRole.PLATFORM_ADMIN:
                # A company admin must not be able to mint a cross-company platform
                # admin from a spreadsheet row.
                errors.append(
                    UserCsvImportError(
                        row=row_number,
                        employee_id=employee_id,
                        email=email or None,
                        reason="role 'platform_admin' cannot be assigned via import",
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
            else:
                # A user-supplied password (CSV column or default_password) must meet
                # the same strength policy as the admin create/reset paths. The
                # operator auto-generated password above is policy-compliant by
                # construction and is intentionally not re-validated here.
                try:
                    validate_password_strength(password)
                except ValueError as exc:
                    errors.append(
                        UserCsvImportError(
                            row=row_number,
                            employee_id=employee_id,
                            email=email or None,
                            reason=f"Weak password: {exc}",
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

    return await run_in_threadpool(_run_import)


@router.put("/{user_id}", response_model=UserResponse)
def update_user(
    user_id: int,
    user_in: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Update a user (Admin only).

    ``platform_admin`` can never be assigned from this tenant-scoped path, and an
    admin cannot change their OWN role (self role-escalation guard) — editing
    one's own name/email/other fields stays allowed. The change (including any
    role escalation) is recorded in the tamper-evident audit log.
    """
    user = db.query(User).filter(User.id == user_id, User.company_id == company_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    update_data = user_in.model_dump(exclude_unset=True)

    # platform_admin is the cross-company oversight role; a tenant admin must not
    # be able to promote anyone (incl. themselves) to it (mirrors approve/import).
    _reject_platform_admin_assignment(update_data.get("role"))

    # Self role-escalation guard (mirrors deactivate's self-guard): an admin must
    # not change their OWN role. Editing one's own other fields stays allowed.
    if user_id == current_user.id and "role" in update_data and update_data["role"] != user.role:
        raise HTTPException(status_code=400, detail="You cannot change your own role")

    # Check email uniqueness if changing
    if "email" in update_data and update_data["email"] != user.email:
        if db.query(User).filter(User.email == update_data["email"], User.company_id == company_id).first():
            raise HTTPException(status_code=400, detail="Email already registered")

    # Snapshot before mutating so the audit diff (e.g. a role change) is visible.
    # log_update runs both sides through _model_to_dict, which drops
    # hashed_password/password, so no secret reaches the audit log.
    old_values = {c.key: getattr(user, c.key) for c in user.__table__.columns}

    for field, value in update_data.items():
        setattr(user, field, value)

    audit.log_update("user", user.id, user.employee_id, old_values=old_values, new_values=user)
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
    audit: AuditService = Depends(get_audit_service),
):
    """Approve a self-registered user and assign their operational role.

    Grants a role and activates the account, so the role + is_active transition is
    recorded in the tamper-evident audit log.
    """
    if approval.role == UserRole.PLATFORM_ADMIN:
        raise HTTPException(status_code=400, detail="Platform admin role cannot be assigned through approval")

    user = db.query(User).filter(User.id == user_id, User.company_id == company_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.is_active:
        raise HTTPException(status_code=400, detail="User is already active")
    if user.role != UserRole.VIEWER:
        raise HTTPException(status_code=400, detail="Only pending self-registered users can be approved")

    # Snapshot before mutating so the audit diff captures the role grant +
    # activation. _model_to_dict drops hashed_password/password from the diff.
    old_values = {c.key: getattr(user, c.key) for c in user.__table__.columns}

    user.role = approval.role
    if approval.department is not None:
        user.department = approval.department
    user.is_active = True

    audit.log_update(
        "user",
        user.id,
        user.employee_id,
        old_values=old_values,
        new_values=user,
        action="approve",
        description=f"Approved user {user.employee_id} as {user.role.value}",
    )
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
    audit: AuditService = Depends(get_audit_service),
):
    """Reset a user's password (Admin only).

    CMMC AU-family event: the reset is recorded in the tamper-evident audit log.
    The new password/hash is deliberately NEVER included in the record.
    """
    user = db.query(User).filter(User.id == user_id, User.company_id == company_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.hashed_password = get_password_hash(password_data.new_password)
    # No old/new values: the password hash must never enter the audit log.
    audit.log(
        action=AuditService.ACTIONS["PASSWORD_CHANGE"],
        resource_type="user",
        resource_id=user.id,
        resource_identifier=user.employee_id,
        description=f"Reset password for user {user.employee_id}",
        extra_data={"source": "admin_reset"},
    )
    db.commit()

    return {"message": "Password reset successfully"}


@router.post("/change-password")
def change_own_password(
    password_data: PasswordChange,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    audit: AuditService = Depends(get_audit_service),
):
    """Change own password.

    CMMC AU-family event: the self-service change is recorded in the tamper-evident
    audit log, mirroring the admin reset path. The new password/hash is deliberately
    NEVER included in the record.
    """
    if not verify_password(password_data.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    current_user.hashed_password = get_password_hash(password_data.new_password)
    # No old/new values: the password hash must never enter the audit log.
    audit.log(
        action=AuditService.ACTIONS["PASSWORD_CHANGE"],
        resource_type="user",
        resource_id=current_user.id,
        resource_identifier=current_user.employee_id,
        description=f"Changed own password for user {current_user.employee_id}",
        extra_data={"source": "self_service"},
    )
    db.commit()

    return {"message": "Password changed successfully"}


@router.delete("/{user_id}")
def deactivate_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Deactivate a user (Admin only). The is_active change is audit-logged."""
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself")

    user = db.query(User).filter(User.id == user_id, User.company_id == company_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.is_active = False
    audit.log_status_change(
        "user",
        user.id,
        user.employee_id,
        "active",
        "inactive",
        description=f"Deactivated user {user.employee_id}",
    )
    db.commit()

    return {"message": "User deactivated"}


@router.post("/{user_id}/activate")
def activate_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Reactivate a user (Admin only). The is_active change is audit-logged."""
    user = db.query(User).filter(User.id == user_id, User.company_id == company_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.is_active = True
    audit.log_status_change(
        "user",
        user.id,
        user.employee_id,
        "inactive",
        "active",
        description=f"Activated user {user.employee_id}",
    )
    db.commit()

    return {"message": "User activated"}
