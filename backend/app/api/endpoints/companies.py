"""
Company self-registration and self-management endpoints.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.audit_guard import required_audit
from app.api.deps import (
    get_admin_user,
    get_audit_service,
    get_current_company_id,
    get_current_user,
    require_role,
)
from app.core.config import settings
from app.core.credentials import API_TOKEN_EGRESS_SWITCH, is_api_token_credential
from app.core.security import create_access_token, create_refresh_token
from app.db.database import get_db
from app.models.company import Company
from app.models.user import User, UserRole
from app.schemas.company import (
    CompanyAIEgressUpdate,
    CompanyRegister,
    CompanyResponse,
    CompanySMSEgressUpdate,
    CompanyUpdate,
)
from app.schemas.user import Token, UserResponse
from app.services.audit_service import AuditService
from app.services.company_onboarding import onboard_company

router = APIRouter()

# What the AI-egress flip answers when its REQUIRED audit rows cannot be written (nothing saved).
AI_EGRESS_AUDIT_UNAVAILABLE_DETAIL = (
    "The audit record for this AI egress change could not be saved; nothing was changed. Retry."
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


@router.post("/register", response_model=Token, summary="Register a new company")
def register_company(request: Request, payload: CompanyRegister, db: Session = Depends(get_db)):
    """
    Self-registration: creates a new company and its initial admin user.
    Returns JWT tokens for immediate login.
    """
    # Check if email is already used
    if db.query(User).filter(User.email == payload.admin_email).first():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email already registered")

    company, admin_user = onboard_company(
        db=db,
        name=payload.company_name,
        admin_email=payload.admin_email,
        admin_first_name=payload.admin_first_name,
        admin_last_name=payload.admin_last_name,
        admin_password=payload.admin_password,
        request=request,
    )

    access_token = create_access_token(subject=admin_user.id, company_id=company.id)
    refresh_token, _, _ = create_refresh_token(subject=admin_user.id, company_id=company.id)

    return Token(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=UserResponse.model_validate(admin_user),
    )


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
