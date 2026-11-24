"""
Company self-management endpoints (the caller's own active company).

There is deliberately no public company self-registration route: Werco is the only
company on this system (owner decision 2026-11-23). A company is created only by a
platform admin (``POST /platform/companies``) or by the empty-install first-user
bootstrap in ``POST /auth/register-public`` (zero users install-wide only; reuses the
'werco' company if present).
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.api.audit_guard import required_audit
from app.api.deps import (
    get_admin_user,
    get_audit_service,
    get_current_company_id,
    get_current_user,
    require_role,
)
from app.core.credentials import API_TOKEN_EGRESS_SWITCH, API_TOKEN_FLOOR_PIN_ADMIN, is_api_token_credential
from app.db.database import get_db
from app.models.company import Company
from app.models.user import User, UserRole
from app.schemas.company import (
    CompanyAIEgressUpdate,
    CompanyFloorPinPolicyResponse,
    CompanyFloorPinPolicyUpdate,
    CompanyResponse,
    CompanySMSEgressUpdate,
    CompanyUpdate,
)
from app.services.audit_service import AuditService

router = APIRouter()

# What the AI-egress flip answers when its REQUIRED audit rows cannot be written (nothing saved).
AI_EGRESS_AUDIT_UNAVAILABLE_DETAIL = (
    "The audit record for this AI egress change could not be saved; nothing was changed. Retry."
)
# What the floor-PIN policy flip answers when its REQUIRED audit rows cannot be written.
FLOOR_PIN_POLICY_AUDIT_UNAVAILABLE_DETAIL = (
    "The audit record for this badge sign-in PIN policy change could not be saved; nothing was changed. Retry."
)


def _refuse_api_token_egress_switch(current_user: User) -> None:
    """403 for ANY API token on an egress kill-switch route, before anything is read or written.

    Opening or closing data egress is a person's judgment (lead decision, Claude-plugin brief
    build 2026-11-15): an Admin signed in to the app, never a standing machine credential,
    whatever its role or scope. A crew-station badge token never reaches these routes (its
    path fence keeps it on the shop floor).
    """
    if is_api_token_credential(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=API_TOKEN_EGRESS_SWITCH)


@router.get("/me", response_model=CompanyResponse, summary="Get current company info")
def get_my_company(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get details of the currently active company."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    user_count = db.query(User).filter(User.company_id == company.id, User.is_active == True).count()

    response = CompanyResponse.model_validate(company)
    response.user_count = user_count
    return response


@router.put("/me", response_model=CompanyResponse, summary="Update company settings")
def update_my_company(
    payload: CompanyUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_admin_user),
    company_id: int = Depends(get_current_company_id),
):
    """Update the current company's settings (admin only)."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    update_data = payload.model_dump(exclude_unset=True)
    # Don't allow non-platform-admins to deactivate their own company
    update_data.pop("is_active", None)

    for field, value in update_data.items():
        setattr(company, field, value)

    db.commit()
    db.refresh(company)
    return CompanyResponse.model_validate(company)


@router.put(
    "/me/ai-egress",
    response_model=CompanyResponse,
    summary="Toggle the company's AI document-extraction egress kill switch",
)
def update_my_company_ai_egress(
    payload: CompanyAIEgressUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Flip the active company's ``allow_ai_egress`` kill switch.

    SAFETY: ``allow_ai_egress`` gates all outbound AI document-extraction egress to
    the Anthropic API -- AND (Claude-plugin brief P0.3) every use of an AI-external API
    token, the credential an outside AI connector such as Claude holds: while it is off,
    such a token is refused 403 on every API route and at the MCP door (``initialize`` and
    ``tools/list`` included). Turning it on therefore also opens the connector -- do it
    only once the empowered official has cleared that (docs/EXPORT_CONTROL_AI_EGRESS.md).
    ADMIN-only, matching the sibling CUI egress kill switches
    ``allow_carrier_egress`` / ``allow_print_egress`` (a CUI-boundary decision is
    reserved to Admins). It is created OFF and only flips when an admin sets it here.

    A person's judgment: ANY API token is refused 403 (``API_TOKEN_EGRESS_SWITCH``) before
    anything is read, and the route is not an MCP tool (``catalog.EXCLUDED_OPERATIONS``).

    A real flip is REQUIRED evidence: a field-level UPDATE row and a security-relevant
    STATUS_CHANGE row, written in the same transaction as the flip. If either cannot be
    written the flip is rolled back and the answer is 503 -- nothing changed. Setting the
    value the company already has writes nothing.

    Tenant-safe: only ever mutates the caller's OWN active company -- the company
    is loaded by ``get_current_company_id`` and never taken from the request body.
    """
    _refuse_api_token_egress_switch(current_user)
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    previous_egress = bool(company.allow_ai_egress)
    if previous_egress == payload.allow_ai_egress:
        # No change: nothing to write, nothing to audit (the STATUS_CHANGE fires only on a flip).
        return CompanyResponse.model_validate(company)

    identifier = company.name or f"company:{company_id}"
    with required_audit(db, AI_EGRESS_AUDIT_UNAVAILABLE_DETAIL):
        company.allow_ai_egress = payload.allow_ai_egress
        db.flush()
        audit.log_required(
            action=AuditService.ACTIONS["UPDATE"],
            resource_type="company",
            resource_id=company.id,
            resource_identifier=identifier,
            description="Company AI egress setting updated",
            old_values={"allow_ai_egress": previous_egress},
            new_values={"allow_ai_egress": company.allow_ai_egress},
            extra_data={"changes": {"allow_ai_egress": {"old": previous_egress, "new": company.allow_ai_egress}}},
        )
        # The egress kill switch flipping is a security-relevant status change -- recorded
        # explicitly on the tamper-evident trail, as required evidence too.
        audit.log_required(
            action=AuditService.ACTIONS["STATUS_CHANGE"],
            resource_type="company",
            resource_id=company.id,
            resource_identifier=identifier,
            description=(
                "AI document-extraction egress "
                f"{'ENABLED' if company.allow_ai_egress else 'DISABLED'} for company {company_id}"
            ),
            old_values={"status": "ai_egress_enabled" if previous_egress else "ai_egress_disabled"},
            new_values={"status": "ai_egress_enabled" if company.allow_ai_egress else "ai_egress_disabled"},
        )
        db.commit()
    db.refresh(company)
    return CompanyResponse.model_validate(company)


@router.put(
    "/me/sms-egress",
    response_model=CompanyResponse,
    summary="Toggle the company's notification-SMS egress kill switch",
)
def update_my_company_sms_egress(
    payload: CompanySMSEgressUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Flip the active company's ``allow_sms_egress`` kill switch.

    SAFETY: ``allow_sms_egress`` gates ALL outbound notification SMS to Twilio, which
    sits outside the CUI boundary. ADMIN-only, matching the sibling CUI egress kill
    switches ``allow_ai_egress`` / ``allow_carrier_egress`` / ``allow_print_egress``
    (a CUI-boundary decision is reserved to Admins). It is created OFF and only flips
    when an admin sets it here; ``sms_service`` re-checks it fail-closed before every
    send, so turning it off stops in-flight queued messages too.

    The flip is audited both as a field-level update AND as a security-relevant status
    change so enabling/disabling egress lands on the tamper-evident trail.

    A person's judgment: ANY API token is refused 403 (``API_TOKEN_EGRESS_SWITCH``) before
    anything is read, and the route is not an MCP tool (``catalog.EXCLUDED_OPERATIONS``).

    Tenant-safe: only ever mutates the caller's OWN active company -- the company is
    loaded by ``get_current_company_id`` and never taken from the request body.
    """
    _refuse_api_token_egress_switch(current_user)
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    previous_egress = company.allow_sms_egress
    company.allow_sms_egress = payload.allow_sms_egress
    db.flush()

    identifier = company.name or f"company:{company_id}"

    audit.log_update(
        resource_type="company",
        resource_id=company.id,
        resource_identifier=identifier,
        old_values={"allow_sms_egress": previous_egress},
        new_values={"allow_sms_egress": company.allow_sms_egress},
        description="Company SMS egress setting updated",
    )

    if previous_egress != company.allow_sms_egress:
        audit.log_status_change(
            "company",
            company.id,
            identifier,
            "sms_egress_enabled" if previous_egress else "sms_egress_disabled",
            "sms_egress_enabled" if company.allow_sms_egress else "sms_egress_disabled",
            description=(
                "Notification SMS egress "
                f"{'ENABLED' if company.allow_sms_egress else 'DISABLED'} for company {company_id}"
            ),
        )

    db.commit()
    db.refresh(company)
    return CompanyResponse.model_validate(company)


def _users_without_floor_pin(db: Session, company_id: int) -> int:
    """Badge users of ``company_id`` with no floor PIN -- refused at badge sign-on while the policy is on.

    Counts active accounts that carry an employee ID (a badge to sign in with) and are not
    platform principals (a superuser / ``platform_admin`` is Werco oversight, not a floor badge
    this company sets a PIN for).
    """
    return (
        db.query(User)
        .filter(
            User.company_id == company_id,
            User.is_active == True,  # noqa: E712
            User.employee_id.isnot(None),
            User.employee_id != "",
            User.floor_pin_hash.is_(None),
            or_(User.is_superuser == False, User.is_superuser.is_(None)),  # noqa: E712
            User.role != UserRole.PLATFORM_ADMIN,
        )
        .count()
    )


@router.put(
    "/me/floor-pin-policy",
    response_model=CompanyFloorPinPolicyResponse,
    summary="Require (or stop requiring) a floor PIN at badge sign-in",
)
def update_my_company_floor_pin_policy(
    payload: CompanyFloorPinPolicyUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Turn the active company's "require a PIN for badge sign-in" policy on or off (Admin).

    With ``require_floor_pin`` on, a badge / employee-ID sign-on (``POST /auth/employee-login``
    and the crew-station badge mint) by a user who has no floor PIN is refused **403**
    ``floor_pin_not_set``; users who have a PIN are challenged for it either way. Turning it
    on while some active users still have no PIN is allowed -- the answer's
    ``users_without_pin`` (active badge users, platform accounts excluded) says how many
    would now be refused, so set their PINs first
    (``PUT /users/{user_id}/floor-pin``). Off by default.

    A person's act: ANY API token is refused **403** before anything is read, and the route is
    not an MCP tool. A real flip writes two REQUIRED audit rows -- the field UPDATE and a
    security STATUS_CHANGE -- in the same transaction (**503**, nothing changed, if either
    cannot be written). Setting the value the company already has writes nothing.

    Tenant-safe: only ever changes the caller's OWN active company, never one named in the
    request.
    """
    if is_api_token_credential(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=API_TOKEN_FLOOR_PIN_ADMIN)
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    previous = bool(company.require_floor_pin)
    if previous != payload.require_floor_pin:
        identifier = company.name or f"company:{company_id}"
        with required_audit(db, FLOOR_PIN_POLICY_AUDIT_UNAVAILABLE_DETAIL):
            company.require_floor_pin = payload.require_floor_pin
            db.flush()
            audit.log_required(
                action=AuditService.ACTIONS["UPDATE"],
                resource_type="company",
                resource_id=company.id,
                resource_identifier=identifier,
                description="Company badge sign-in PIN policy updated",
                old_values={"require_floor_pin": previous},
                new_values={"require_floor_pin": company.require_floor_pin},
                extra_data={"changes": {"require_floor_pin": {"old": previous, "new": company.require_floor_pin}}},
            )
            audit.log_required(
                action=AuditService.ACTIONS["STATUS_CHANGE"],
                resource_type="company",
                resource_id=company.id,
                resource_identifier=identifier,
                description=(
                    "Floor PIN at badge sign-in "
                    f"{'REQUIRED' if company.require_floor_pin else 'NOT REQUIRED'} for company {company_id}"
                ),
                old_values={"status": "floor_pin_required" if previous else "floor_pin_optional"},
                new_values={"status": "floor_pin_required" if company.require_floor_pin else "floor_pin_optional"},
            )
            db.commit()
        db.refresh(company)

    response = CompanyFloorPinPolicyResponse.model_validate(
        {
            **CompanyResponse.model_validate(company).model_dump(),
            "users_without_pin": _users_without_floor_pin(db, company_id),
        }
    )
    return response
