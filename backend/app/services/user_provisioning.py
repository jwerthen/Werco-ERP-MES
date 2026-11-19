"""Shared tenant-account policy and atomic security mutation helpers.

Tenant APIs never grant platform authority, even when called by a platform user.
The empty-install bootstrap is a separate, server-selected provisioning flow.
"""

from contextlib import contextmanager
from typing import Any, Iterator, Optional

from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.security import get_password_hash
from app.models.user import User, UserRole
from app.services.audit_service import AuditService, AuditWriteError

TENANT_ROLES = frozenset(role for role in UserRole if role != UserRole.PLATFORM_ADMIN)

# Floor PIN authority (AS9100D 8.5.2). Whoever chooses a person's floor PIN can sign in as that
# person with their badge, so a MANAGER may set, reset, clear or unlock PINs only for these
# roles (``users._floor_pin_target_or_404``); every other tenant role signs judgments
# (approvals, dispositions, quality sign-off) and its PIN needs an Admin.
MANAGER_FLOOR_PIN_TARGET_ROLES = frozenset({UserRole.OPERATOR, UserRole.SUPERVISOR, UserRole.SHIPPING, UserRole.VIEWER})
FLOOR_PIN_JUDGMENT_ROLES = TENANT_ROLES - MANAGER_FLOOR_PIN_TARGET_ROLES  # admin, manager, quality

# ``extra_data`` markers on the role-change audit row when a promotion cleared the floor PIN, and
# when it also left the user needing a new one (they had a PIN).
FLOOR_PIN_CLEARED_ON_PROMOTION_KEY = "floor_pin_cleared_on_promotion"
FLOOR_PIN_RESET_REQUIRED_KEY = "floor_pin_reset_required"


def clear_floor_pin_on_promotion(user: User, previous_role: Optional[UserRole]) -> bool:
    """Clear ``user``'s floor PIN when a role change takes them INTO a judgment role. True if cleared.

    The Manager-scope rule is checked when a PIN is written; this closes the other door. A PIN
    a Manager set for an Operator would otherwise survive that person's promotion to Admin,
    Manager or Quality, and the Manager would still know the PIN of someone who now signs
    judgments. So a change from a role outside ``FLOOR_PIN_JUDGMENT_ROLES`` to one inside it
    clears the hash, the set date, the wrong-PIN count and the locks -- staged on ``user`` for
    the caller's transaction, which records it on the role change's audit row
    (``promotion_pin_markers``, never a value).

    A user who HAD a PIN is NOT downgraded to badge-only: ``floor_pin_reset_required`` is set, so
    badge sign-on answers 403 ``floor_pin_reset_required`` (whatever the company policy) until an
    Admin sets a new PIN -- or clears it, an explicit decision to allow badge-only. Their existing
    badge+PIN sessions end at the next request (the hash they verified is gone). A change within
    the judgment roles (Manager to Admin), a demotion, and a user with no PIN state are left
    alone.
    """
    if user.role not in FLOOR_PIN_JUDGMENT_ROLES or previous_role in FLOOR_PIN_JUDGMENT_ROLES:
        return False
    had_pin = bool(user.floor_pin_hash)
    has_pin_state = (
        had_pin
        or user.floor_pin_set_at is not None
        or bool(user.floor_pin_failed_attempts)
        or user.floor_pin_locked_until is not None
    )
    if not has_pin_state:
        return False
    user.floor_pin_hash = None
    user.floor_pin_set_at = None
    user.floor_pin_failed_attempts = 0
    user.floor_pin_locked_until = None
    if had_pin:
        user.floor_pin_reset_required = True
    return True


def promotion_pin_markers(user: User) -> dict[str, Any]:
    """The ``extra_data`` markers for a role change whose promotion cleared the floor PIN."""
    markers: dict[str, Any] = {FLOOR_PIN_CLEARED_ON_PROMOTION_KEY: True}
    if user.floor_pin_reset_required:
        markers[FLOOR_PIN_RESET_REQUIRED_KEY] = True
    return markers


class RequiredSecurityAudit(AuditService):
    """Opt legacy helper calls into required evidence inside a security transaction.

    The API-token revocation service calls log_status_change/log, so requiring
    only the enclosing user-status row would leave its companion rows optional.
    Keep the same chain, savepoints and credential attribution in AuditService.
    """

    def log(self, *args, **kwargs):
        # ``AuditService.log`` re-raises a database error that aborted the transaction.
        # Report it as ``AuditWriteError`` too, as ``log_required`` does, so the
        # security write answers its documented 503 rather than a generic 409/500.
        try:
            entry = super().log(*args, **kwargs)
        except DBAPIError as exc:
            raise AuditWriteError("The required audit record could not be saved") from exc
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
    extra_data: Optional[dict[str, Any]] = None,
) -> None:
    """Audit only changed public account fields; passwords/hashes are never serialized.

    ``extra_data`` is merged into the row's ``extra_data`` beside ``changes``: markers such as
    ``floor_pin_cleared_on_promotion``, or a reason. Never a credential value.
    """
    fields = ("email", "first_name", "last_name", "role", "department", "phone", "is_active", "export_authorized")
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
        extra_data={
            "changes": {key: {"old": old_values[key], "new": getattr(user, key)} for key in changed},
            **(extra_data or {}),
        },
    )


EXPORT_AUTHORIZATION_DEACTIVATION_REASON = "user deactivated"


def revoke_export_authorization_for_deactivation(audit: AuditService, user: User) -> bool:
    """Clear a deactivated user's export authorization, with its companion audit row; True if one was held.

    Called by both user-deactivation verbs inside their security transaction, like the API-token
    sweep: deactivation RETIRES the grant, it never pauses it, so a later reactivation restores
    the account and no export authorization. The row is required evidence (``audit_user_update``
    -> ``log_required``): if it cannot be written the deactivation rolls back too.
    """
    if not user.export_authorized and user.export_authorized_at is None and user.export_authorized_by is None:
        return False
    old_values = {"export_authorized": bool(user.export_authorized)}
    revoked_grant = {
        "export_authorized_at": user.export_authorized_at.isoformat() + "Z" if user.export_authorized_at else None,
        "export_authorized_by": user.export_authorized_by,
    }
    user.export_authorized = False
    user.export_authorized_at = None
    user.export_authorized_by = None
    audit_user_update(
        audit,
        user,
        old_values,
        action="EXPORT_AUTHORIZATION_REVOKE",
        description=f"Revoked export authorization for user {user.employee_id} on deactivation",
        extra_data={
            "reason": EXPORT_AUTHORIZATION_DEACTIVATION_REASON,
            "source": "deactivation",
            "revoked_grant": revoked_grant,
        },
    )
    return True
