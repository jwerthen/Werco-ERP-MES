import json
import secrets
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_company_id, require_role
from app.db.database import get_db
from app.models.company import Company
from app.models.quote_config import (
    QuoteSettings,
    SettingsAuditLog,
)
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.schemas.admin_settings import (
    AuditLogWithUser,
    EmailRecipientsResponse,
    EmailRecipientsUpdate,
    WorkCenterRateResponse,
    WorkCenterRateUpdate,
    WorkCenterTypesResponse,
    WorkCenterTypesUpdate,
)
from app.services.notification_email_recipients import (
    EMAIL_EVENTS,
    deliverable,
    email_recipient_ids,
    email_settings_response,
    setting_key,
)
from app.services.work_center_type_service import (
    get_in_use_work_center_types,
    get_work_center_types,
    set_work_center_types,
)

router = APIRouter()

# Admin-only dependency
admin_only = require_role([UserRole.ADMIN])


def log_change(
    db: Session,
    entity_type: str,
    entity_id: int,
    entity_name: str,
    action: str,
    current_user: User,
    field_changed: str = None,
    old_value: any = None,
    new_value: any = None,
    ip_address: str = None,
):
    """Log a settings change for audit purposes.

    SettingsAuditLog has a NOT NULL company_id (TenantMixin). The row is tagged
    with the *active* company — the one resolved by get_current_company_id, i.e.
    the company a platform admin has switched into (``current_user._active_company_id``)
    — falling back to the user's home company on non-request paths. This mirrors
    ``AuditService._resolve_company_id`` so settings audits attribute to the same
    tenant as every other write; using ``current_user.company_id`` here would
    mis-attribute a platform admin's cross-company change to their home company.
    """
    active_company_id = getattr(current_user, "_active_company_id", None)
    company_id = active_company_id if active_company_id is not None else current_user.company_id
    audit = SettingsAuditLog(
        entity_type=entity_type,
        entity_id=entity_id,
        entity_name=entity_name,
        action=action,
        field_changed=field_changed,
        old_value=json.dumps(old_value) if old_value is not None else None,
        new_value=json.dumps(new_value) if new_value is not None else None,
        changed_by=current_user.id,
        ip_address=ip_address,
        company_id=company_id,
    )
    db.add(audit)


def get_client_ip(request: Request) -> str:
    """Get client IP from request"""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ============ EMAIL RECIPIENTS ============


@router.get("/email-recipients", response_model=EmailRecipientsResponse)
def get_email_recipients(
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only),
    company_id: int = Depends(get_current_company_id),
):
    return email_settings_response(db, company_id)


@router.put("/email-recipients/{event_key}", response_model=EmailRecipientsResponse)
def update_email_recipients(
    event_key: str,
    data: EmailRecipientsUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only),
    company_id: int = Depends(get_current_company_id),
):
    if event_key not in EMAIL_EVENTS:
        raise HTTPException(status_code=400, detail="This email type does not have configurable recipients.")
    ids = sorted(set(data.user_ids)) if data.user_ids is not None else None
    if ids:
        users = db.query(User).filter(User.company_id == company_id, User.id.in_(ids), User.is_active.is_(True)).all()
        if {user.id for user in users if deliverable(user)} != set(ids):
            raise HTTPException(
                status_code=400, detail="Select active users in this company with deliverable email addresses."
            )

    # Serialize first saves as well as updates for this company.
    db.query(Company).filter(Company.id == company_id).with_for_update().one()
    key = setting_key(event_key)
    setting = (
        db.query(QuoteSettings).filter(QuoteSettings.company_id == company_id, QuoteSettings.setting_key == key).first()
    )
    previous_ids = email_recipient_ids(db, company_id, event_key)
    old_value = sorted(previous_ids) if previous_ids is not None else None
    if ids is None:
        if setting is not None:
            db.delete(setting)
    elif setting is not None:
        setting.setting_value = json.dumps(ids)
    else:
        db.add(
            QuoteSettings(
                company_id=company_id,
                setting_key=key,
                setting_value=json.dumps(ids),
                setting_type="json",
                description=f"Email recipients: {EMAIL_EVENTS[event_key].label}",
            )
        )
    log_change(
        db,
        "email_recipients",
        None,
        EMAIL_EVENTS[event_key].label,
        "update",
        current_user,
        "user_ids",
        old_value,
        ids,
        get_client_ip(request),
    )
    db.commit()
    return email_settings_response(db, company_id)


# ============ WORK CENTER RATES ============


@router.get("/work-center-rates", response_model=List[WorkCenterRateResponse])
def list_work_center_rates(
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only),
    company_id: int = Depends(get_current_company_id),
):
    """List all work centers with their rates"""
    query = db.query(WorkCenter).filter(WorkCenter.company_id == company_id)
    if not include_inactive:
        query = query.filter(WorkCenter.is_active == True)
    return query.order_by(WorkCenter.name).all()


@router.put("/work-center-rates/{work_center_id}", response_model=WorkCenterRateResponse)
def update_work_center_rate(
    work_center_id: int,
    data: WorkCenterRateUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only),
    company_id: int = Depends(get_current_company_id),
):
    """Update a work center's hourly rate"""
    wc = db.query(WorkCenter).filter(WorkCenter.id == work_center_id, WorkCenter.company_id == company_id).first()
    if not wc:
        raise HTTPException(status_code=404, detail="Work center not found")

    old_rate = wc.hourly_rate
    wc.hourly_rate = data.hourly_rate

    log_change(
        db,
        "work_center_rate",
        wc.id,
        wc.name,
        "update",
        current_user,
        "hourly_rate",
        old_rate,
        data.hourly_rate,
        get_client_ip(request),
    )

    db.commit()
    db.refresh(wc)
    return wc


# ============ WORK CENTER TYPES ============


@router.get("/work-center-types", response_model=WorkCenterTypesResponse)
def list_work_center_types_admin(
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only),
    company_id: int = Depends(get_current_company_id),
):
    """List allowed work center types"""
    # Include in-use types so the admin UI always shows the real types
    # currently referenced by work_centers, even if the saved JSON is stale
    # or got corrupted by a prior bad write. Without this, the UI can omit
    # in-use types, and every save then 400s on the "Cannot remove types in
    # use" guard in the PUT below.
    types = get_work_center_types(db, include_in_use=True, company_id=company_id)
    in_use = get_in_use_work_center_types(db, company_id=company_id)
    return {"types": types, "in_use": in_use}


@router.put("/work-center-types", response_model=WorkCenterTypesResponse)
def update_work_center_types_admin(
    data: WorkCenterTypesUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only),
    company_id: int = Depends(get_current_company_id),
):
    """Update allowed work center types"""
    old_types = get_work_center_types(db, include_in_use=False, company_id=company_id)
    in_use = get_in_use_work_center_types(db, company_id=company_id)
    missing_in_use = [t for t in in_use if t not in (data.types or [])]
    if missing_in_use:
        raise HTTPException(status_code=400, detail=f"Cannot remove types in use: {', '.join(missing_in_use)}")
    types = set_work_center_types(db, data.types, company_id=company_id)
    log_change(
        db,
        "work_center_types",
        0,
        "work_center_types",
        "update",
        current_user,
        "types",
        old_types,
        types,
        get_client_ip(request),
    )
    db.commit()
    return {"types": types, "in_use": in_use}


# ============ AUDIT LOG ============


@router.get("/audit-log", response_model=List[AuditLogWithUser])
def get_audit_log(
    entity_type: Optional[str] = None,
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(100, ge=1, le=5000),
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only),
    company_id: int = Depends(get_current_company_id),
):
    """Get audit log entries (up to 1 year history)"""
    cutoff_date = datetime.utcnow() - timedelta(days=min(days, 365))

    query = db.query(SettingsAuditLog, User.first_name, User.last_name).outerjoin(
        User, SettingsAuditLog.changed_by == User.id
    )

    query = query.filter(SettingsAuditLog.changed_at >= cutoff_date)

    if hasattr(SettingsAuditLog, 'company_id'):
        query = query.filter(SettingsAuditLog.company_id == company_id)

    if entity_type:
        query = query.filter(SettingsAuditLog.entity_type == entity_type)

    results = query.order_by(SettingsAuditLog.changed_at.desc()).limit(limit).all()

    response = []
    for audit, first_name, last_name in results:
        item = AuditLogWithUser(
            id=audit.id,
            entity_type=audit.entity_type,
            entity_id=audit.entity_id,
            entity_name=audit.entity_name,
            action=audit.action,
            field_changed=audit.field_changed,
            old_value=audit.old_value,
            new_value=audit.new_value,
            changed_by=audit.changed_by,
            changed_at=audit.changed_at,
            user_name=f"{first_name} {last_name}" if first_name else None,
        )
        response.append(item)

    return response


# ============ SEED DEFAULT DATA ============


def _generate_bootstrap_password() -> str:
    """Generate a strong, policy-compliant one-time bootstrap password.

    Unique per call — never a hardcoded/well-known value. Mirrors the operator
    auto-password convention in the users router.

    Validated rather than assumed compliant: the strength policy is length plus a
    substring blocklist (see ``validate_password_strength``), and a random token can
    incidentally contain a blocklisted substring. That is rare, but it is a real
    source of nondeterminism, so regenerate until the value actually passes instead
    of trusting the shape of the f-string.
    """
    from app.schemas.user import validate_password_strength

    for _ in range(10):
        candidate = f"Seed!{secrets.token_urlsafe(18)}1aZ"
        try:
            return validate_password_strength(candidate)
        except ValueError:
            continue
    # Unreachable in practice (each attempt fails with probability ~1e-5).
    raise RuntimeError("Could not generate a policy-compliant bootstrap password")


@router.post("/seed-database")
async def seed_database(db: Session = Depends(get_db), current_user: User = Depends(admin_only)):
    """Seed database with initial data. Requires admin authentication.

    Bootstrap credentials are GENERATED at runtime (never hardcoded) and returned
    exactly once in the response so the calling admin can distribute and rotate
    them. They satisfy the AS9100D/CMMC password-strength policy by construction.
    """

    from app.core.security import get_password_hash

    # Check if already seeded
    if db.query(User).first():
        return {"status": "already_seeded", "message": "Database already has users"}

    # One-time credentials surfaced to the calling admin. Each user gets a distinct
    # generated password; the plaintext exists only in this response, never in source.
    generated_credentials: dict = {}

    # Create admin user
    admin_password = _generate_bootstrap_password()
    admin = User(
        employee_id="EMP001",
        email="admin@werco.com",
        hashed_password=get_password_hash(admin_password),
        first_name="System",
        last_name="Administrator",
        role=UserRole.ADMIN,
        department="IT",
        is_superuser=True,
    )
    db.add(admin)
    generated_credentials["admin@werco.com"] = admin_password

    # Create sample users
    users_data = [
        ("EMP002", "jsmith@werco.com", "John", "Smith", UserRole.MANAGER, "Production"),
        ("EMP003", "mjohnson@werco.com", "Mary", "Johnson", UserRole.SUPERVISOR, "Fabrication"),
        ("EMP004", "bwilliams@werco.com", "Bob", "Williams", UserRole.OPERATOR, "CNC"),
        ("EMP005", "sjones@werco.com", "Sarah", "Jones", UserRole.QUALITY, "Quality"),
    ]

    for emp_id, email, first, last, role, dept in users_data:
        user_password = _generate_bootstrap_password()
        user = User(
            employee_id=emp_id,
            email=email,
            hashed_password=get_password_hash(user_password),
            first_name=first,
            last_name=last,
            role=role,
            department=dept,
        )
        db.add(user)
        generated_credentials[email] = user_password

    db.commit()
    return {
        "status": "success",
        "message": (
            "Database seeded with admin and sample users. Store these one-time "
            "credentials securely and rotate them immediately after first login."
        ),
        "credentials": generated_credentials,
    }


# ============ ROLE PERMISSIONS ============

from app.models.role_permission import ALL_PERMISSIONS, DEFAULT_ROLE_PERMISSIONS, PERMISSION_CATEGORIES, RolePermission


@router.get("/role-permissions")
def get_all_role_permissions(
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only),
    company_id: int = Depends(get_current_company_id),
):
    """
    Get permissions for all roles.
    Returns stored custom permissions or defaults if not customized.
    """
    stored = db.query(RolePermission).filter(RolePermission.company_id == company_id).all()
    stored_map = {rp.role: rp.permissions for rp in stored}

    result = {}
    for role in UserRole:
        if role in stored_map:
            result[role.value] = stored_map[role]
        else:
            result[role.value] = DEFAULT_ROLE_PERMISSIONS.get(role, [])

    return {
        "role_permissions": result,
        "all_permissions": ALL_PERMISSIONS,
        "permission_categories": PERMISSION_CATEGORIES,
        "roles": [{"value": r.value, "label": r.value.title()} for r in UserRole],
    }


@router.get("/role-permissions/{role}")
def get_role_permissions(
    role: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only),
    company_id: int = Depends(get_current_company_id),
):
    """Get permissions for a specific role"""
    try:
        user_role = UserRole(role)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid role: {role}")

    stored = (
        db.query(RolePermission)
        .filter(RolePermission.role == user_role, RolePermission.company_id == company_id)
        .first()
    )

    if stored:
        return {"role": role, "permissions": stored.permissions, "is_customized": True}
    else:
        return {"role": role, "permissions": DEFAULT_ROLE_PERMISSIONS.get(user_role, []), "is_customized": False}


@router.put("/role-permissions/{role}")
def update_role_permissions(
    role: str,
    request: Request,
    permissions: list[str],
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only),
    company_id: int = Depends(get_current_company_id),
):
    """Update permissions for a specific role"""
    try:
        user_role = UserRole(role)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid role: {role}")

    # Validate permissions
    invalid = [p for p in permissions if p not in ALL_PERMISSIONS]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Invalid permissions: {invalid}")

    # Get or create role permission record
    stored = (
        db.query(RolePermission)
        .filter(RolePermission.role == user_role, RolePermission.company_id == company_id)
        .first()
    )

    old_permissions = stored.permissions if stored else DEFAULT_ROLE_PERMISSIONS.get(user_role, [])

    if stored:
        stored.permissions = permissions
        stored.updated_by = current_user.id
    else:
        stored = RolePermission(
            role=user_role,
            permissions=permissions,
            updated_by=current_user.id,
            company_id=company_id,
        )
        db.add(stored)

    # Log the change
    log_change(
        db,
        "role_permission",
        stored.id if stored.id else 0,
        role,
        "update",
        current_user,
        field_changed="permissions",
        old_value=old_permissions,
        new_value=permissions,
        ip_address=get_client_ip(request),
    )

    db.commit()
    db.refresh(stored)

    return {"role": role, "permissions": stored.permissions, "is_customized": True}


@router.post("/role-permissions/{role}/reset")
def reset_role_permissions(
    role: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only),
    company_id: int = Depends(get_current_company_id),
):
    """Reset a role's permissions to defaults"""
    try:
        user_role = UserRole(role)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid role: {role}")

    stored = (
        db.query(RolePermission)
        .filter(RolePermission.role == user_role, RolePermission.company_id == company_id)
        .first()
    )

    if stored:
        old_permissions = stored.permissions
        db.delete(stored)

        log_change(
            db,
            "role_permission",
            stored.id,
            role,
            "reset",
            current_user,
            field_changed="permissions",
            old_value=old_permissions,
            new_value=DEFAULT_ROLE_PERMISSIONS.get(user_role, []),
            ip_address=get_client_ip(request),
        )

        db.commit()

    return {"role": role, "permissions": DEFAULT_ROLE_PERMISSIONS.get(user_role, []), "is_customized": False}
