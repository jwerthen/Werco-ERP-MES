"""Shared tenant-account policy and atomic security mutation helpers.

Tenant APIs never grant platform authority, even when called by a platform user.
The empty-install bootstrap is a separate, server-selected provisioning flow.
"""

from contextlib import contextmanager
from typing import Any, Iterator, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.security import get_password_hash
from app.models.user import User, UserRole
from app.services.audit_service import AuditService, AuditWriteError

TENANT_ROLES = frozenset(role for role in UserRole if role != UserRole.PLATFORM_ADMIN)


class RequiredSecurityAudit(AuditService):
    """Opt legacy helper calls into required evidence inside a security transaction.

    The API-token revocation service calls log_status_change/log, so requiring
    only the enclosing user-status row would leave its companion rows optional.
    Keep the same chain, savepoints and credential attribution in AuditService.
    """

    def log(self, *args, **kwargs):
        entry = super().log(*args, **kwargs)
        if entry is None:
            raise AuditWriteError("The required audit record could not be saved")
        return entry


def required_security_audit(audit: AuditService) -> AuditService:
    return RequiredSecurityAudit(audit.db, audit.user, audit.request, company_id=audit.company_id)


def require_tenant_role(role: Optional[UserRole], detail: str = "Platform admin role cannot be assigned") -> None:
    if role is not None and role not in TENANT_ROLES:
        raise HTTPException(status_code=400, detail=detail)


def require_manageable_user(actor: User, target: User) -> None:
    """Password, email and activation control over a platform user is platform authority."""
    target_platform = target.is_superuser or target.role == UserRole.PLATFORM_ADMIN
    actor_platform = actor.is_superuser or actor.role == UserRole.PLATFORM_ADMIN
    if target_platform and not actor_platform:
        raise HTTPException(status_code=403, detail="Only platform administrators can manage platform accounts")


@contextmanager
def atomic_security_write(db: Session) -> Iterator[None]:
    """Commit the security mutation and its required evidence as one transaction."""
    try:
        yield
        db.commit()
    except AuditWriteError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Unable to save audit record") from exc
    except Exception:
        db.rollback()
        raise


def create_tenant_user(
    db: Session,
    *,
    company_id: int,
    email: str,
    employee_id: str,
    first_name: str,
    last_name: str,
    password: str,
    role: UserRole = UserRole.OPERATOR,
    department: Optional[str] = None,
    phone: Optional[str] = None,
    is_active: bool = True,
    created_by: Optional[int] = None,
) -> User:
    """Stage a non-platform account; the caller owns audit evidence and commit."""
    require_tenant_role(role)
    if db.query(User).filter_by(company_id=company_id, email=email).first():
        raise HTTPException(status_code=400, detail="Email already registered")
    if db.query(User).filter_by(company_id=company_id, employee_id=employee_id).first():
        raise HTTPException(status_code=400, detail="Employee ID already exists")
    user = User(
        company_id=company_id,
        email=email,
        employee_id=employee_id,
        first_name=first_name,
        last_name=last_name,
        hashed_password=get_password_hash(password),
        role=role,
        is_superuser=False,
        is_active=is_active,
        department=department,
        phone=phone,
        created_by=created_by,
    )
    db.add(user)
    db.flush()
    return user


def audit_user_created(audit: AuditService, user: User, *, source: str, authentication: bool = False) -> None:
    audit.log_required(
        action="USER_REGISTERED" if authentication else "CREATE",
        resource_type="authentication" if authentication else "user",
        resource_id=user.id,
        resource_identifier=user.email if authentication else user.employee_id,
        company_id=user.company_id,
        description=f"Created user {user.employee_id}",
        extra_data={"source": source, "role": user.role.value, "email": user.email},
    )


def audit_user_update(
    audit: AuditService,
    user: User,
    old_values: dict[str, Any],
    *,
    action: str = "UPDATE",
    description: Optional[str] = None,
) -> None:
    """Audit only changed public account fields; passwords/hashes are never serialized."""
    fields = ("email", "first_name", "last_name", "role", "department", "phone", "is_active")
    changed = [key for key in fields if key in old_values and old_values[key] != getattr(user, key)]
    if not changed:
        return
    audit.log_required(
        action=action.upper(),
        resource_type="user",
        resource_id=user.id,
        resource_identifier=user.employee_id,
        company_id=user.company_id,
        description=description or f"Updated user {user.employee_id}",
        old_values={key: old_values[key] for key in changed},
        new_values={key: getattr(user, key) for key in changed},
        extra_data={"changes": {key: {"old": old_values[key], "new": getattr(user, key)} for key in changed}},
    )
