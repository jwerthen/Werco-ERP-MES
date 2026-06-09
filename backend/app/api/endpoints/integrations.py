"""Admin endpoints for carrier-integration credentials + shipping profile.

These are the per-company admin-console controls for the multi-carrier shipping
integration: manage aggregator credentials (EasyPost / Zenkraft), run a
credential-only connection test, and manage the company shipping profile that
holds the ship-from origin and the customer-data egress kill switch.

COMPLIANCE / SECURITY invariants enforced here:

* **Admin-only + tenant-scoped.** Every route is gated to ``UserRole.ADMIN``
  (``admin_only``) and scoped to the ACTIVE company (``get_current_company_id``).
* **Secrets are write-only.** ``api_key`` / ``webhook_secret`` are accepted on
  write, Fernet-encrypted via ``carriers.crypto`` before storage, and NEVER
  returned (the response exposes only ``api_key_last4`` + ``has_webhook_secret``)
  or placed in audit / event payloads.
* **Soft delete.** Deleting a carrier account uses ``.soft_delete()`` (the model
  carries ``SoftDeleteMixin``) -- a hard delete would orphan purchased
  labels/BOLs that reference it.
* **Audit.** Create / update / delete / the egress-toggle are recorded through
  the tamper-evident ``AuditService`` with only non-secret fields in the payload.
* **Egress exemption.** ``test-connection`` is the ONLY carrier round-trip not
  gated by ``allow_carrier_egress`` -- it transmits no customer data, only the
  stored credential.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_company_id, require_role
from app.db.database import get_db
from app.models.carrier_account import CarrierAccount, CompanyShippingProfile
from app.models.user import User, UserRole
from app.schemas.shipping import (
    CarrierAccountCreate,
    CarrierAccountResponse,
    CarrierAccountUpdate,
    CarrierConnectionTestResponse,
    CompanyShippingProfileResponse,
    CompanyShippingProfileUpdate,
)
from app.services.audit_service import AuditService
from app.services.carriers.crypto import encrypt_secret
from app.services.shipping_service import ShippingService

router = APIRouter()

# Admin-only dependency (mirrors admin_settings.admin_only).
admin_only = require_role([UserRole.ADMIN])


def _account_to_response(account: CarrierAccount) -> CarrierAccountResponse:
    """Build the safe read shape for a carrier account.

    SECURITY: the plaintext api key is decrypted in-memory ONLY to derive the
    last-4 mask, then discarded; it is never returned or logged. A decrypt
    failure (e.g. a rotated/missing encryption key) is swallowed so the account
    still lists -- last4 simply becomes ``None``.
    """
    api_key_last4: Optional[str] = None
    if account.encrypted_api_key:
        try:
            from app.services.carriers.crypto import decrypt_secret

            api_key_last4 = ShippingService.last4(decrypt_secret(account.encrypted_api_key))
        except Exception:  # noqa: BLE001 - never leak the secret or fail the read
            api_key_last4 = None

    carrier_refs = list((account.carrier_refs or {}).keys())
    return CarrierAccountResponse(
        id=account.id,
        name=account.name,
        provider=account.provider,
        environment=account.environment,
        is_active=account.is_active,
        is_default=account.is_default,
        carrier_refs=carrier_refs,
        api_key_last4=api_key_last4,
        has_webhook_secret=bool(account.webhook_secret_encrypted),
        created_at=account.created_at,
    )


def _account_audit_snapshot(account: CarrierAccount) -> dict:
    """Non-secret snapshot of an account for audit payloads (NEVER the key)."""
    return {
        "id": account.id,
        "name": account.name,
        "provider": account.provider,
        "environment": account.environment,
        "is_active": account.is_active,
        "is_default": account.is_default,
        "carrier_refs": list((account.carrier_refs or {}).keys()),
        "has_webhook_secret": bool(account.webhook_secret_encrypted),
    }


def _get_account(db: Session, account_id: int, company_id: int) -> CarrierAccount:
    account = (
        db.query(CarrierAccount)
        .filter(
            CarrierAccount.id == account_id,
            CarrierAccount.company_id == company_id,
            CarrierAccount.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Carrier account not found")
    return account


def _clear_other_defaults(db: Session, company_id: int, keep_id: Optional[int] = None) -> None:
    """Ensure at most one default account per company (tenant-scoped)."""
    query = db.query(CarrierAccount).filter(
        CarrierAccount.company_id == company_id,
        CarrierAccount.is_default == True,  # noqa: E712
        CarrierAccount.is_deleted == False,  # noqa: E712
    )
    if keep_id is not None:
        query = query.filter(CarrierAccount.id != keep_id)
    for other in query.all():
        other.is_default = False


# ===========================================================================
# Carrier accounts (credentials) CRUD.
# ===========================================================================


@router.get("/carrier-accounts", response_model=List[CarrierAccountResponse])
def list_carrier_accounts(
    include_inactive: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only),
    company_id: int = Depends(get_current_company_id),
):
    """List the company's carrier accounts (secrets masked)."""
    query = db.query(CarrierAccount).filter(
        CarrierAccount.company_id == company_id,
        CarrierAccount.is_deleted == False,  # noqa: E712
    )
    if not include_inactive:
        query = query.filter(CarrierAccount.is_active == True)  # noqa: E712
    accounts = query.order_by(CarrierAccount.name).all()
    return [_account_to_response(a) for a in accounts]


@router.get("/carrier-accounts/{account_id}", response_model=CarrierAccountResponse)
def get_carrier_account(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only),
    company_id: int = Depends(get_current_company_id),
):
    """Fetch a single carrier account (secrets masked)."""
    return _account_to_response(_get_account(db, account_id, company_id))


@router.post("/carrier-accounts", response_model=CarrierAccountResponse, status_code=status.HTTP_201_CREATED)
def create_carrier_account(
    data: CarrierAccountCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create a per-company carrier account. The api key / webhook secret are
    Fernet-encrypted before storage and NEVER returned."""
    account = CarrierAccount(
        name=data.name,
        provider=data.provider.strip().lower(),
        environment=data.environment,
        encrypted_api_key=encrypt_secret(data.api_key),
        webhook_secret_encrypted=encrypt_secret(data.webhook_secret) if data.webhook_secret else None,
        carrier_refs=data.carrier_refs or {},
        is_active=data.is_active,
        is_default=data.is_default,
        created_by=current_user.id,
    )
    account.company_id = company_id
    if data.is_default:
        _clear_other_defaults(db, company_id)
    db.add(account)
    db.flush()

    audit.log_create(
        resource_type="carrier_account",
        resource_id=account.id,
        resource_identifier=account.name,
        new_values=_account_audit_snapshot(account),
        description=f"Carrier account created: {account.name} ({account.provider})",
    )
    db.commit()
    db.refresh(account)
    return _account_to_response(account)


@router.put("/carrier-accounts/{account_id}", response_model=CarrierAccountResponse)
def update_carrier_account(
    account_id: int,
    data: CarrierAccountUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Patch a carrier account. Sending ``api_key`` / ``webhook_secret`` rotates
    the stored secret; omitting them leaves it unchanged. Secrets never appear in
    the audit payload (only a ``api_key_rotated`` / ``webhook_secret_rotated`` flag)."""
    account = _get_account(db, account_id, company_id)
    before = _account_audit_snapshot(account)

    update = data.model_dump(exclude_unset=True)
    api_key_rotated = False
    webhook_secret_rotated = False

    if "name" in update and update["name"] is not None:
        account.name = update["name"]
    if "environment" in update and update["environment"] is not None:
        account.environment = update["environment"]
    if "carrier_refs" in update and update["carrier_refs"] is not None:
        account.carrier_refs = update["carrier_refs"]
    if "is_active" in update and update["is_active"] is not None:
        account.is_active = update["is_active"]
    if "is_default" in update and update["is_default"] is not None:
        account.is_default = update["is_default"]
        if update["is_default"]:
            _clear_other_defaults(db, company_id, keep_id=account.id)
    # Secrets: rotate only when a non-empty value is supplied.
    if update.get("api_key"):
        account.encrypted_api_key = encrypt_secret(update["api_key"])
        api_key_rotated = True
    if update.get("webhook_secret"):
        account.webhook_secret_encrypted = encrypt_secret(update["webhook_secret"])
        webhook_secret_rotated = True

    db.flush()
    after = _account_audit_snapshot(account)
    audit.log_update(
        resource_type="carrier_account",
        resource_id=account.id,
        resource_identifier=account.name,
        old_values=before,
        new_values=after,
        description=f"Carrier account updated: {account.name}",
        extra_data={"api_key_rotated": api_key_rotated, "webhook_secret_rotated": webhook_secret_rotated},
    )
    db.commit()
    db.refresh(account)
    return _account_to_response(account)


@router.delete("/carrier-accounts/{account_id}")
def delete_carrier_account(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Soft-delete a carrier account (never a physical delete -- purchased
    labels/BOLs may reference it)."""
    account = _get_account(db, account_id, company_id)
    snapshot = _account_audit_snapshot(account)
    account.soft_delete(current_user.id)
    account.is_default = False

    audit.log_delete(
        resource_type="carrier_account",
        resource_id=account.id,
        resource_identifier=account.name,
        old_values=snapshot,
        description=f"Carrier account deleted: {account.name}",
        soft_delete=True,
    )
    db.commit()
    return {"status": "ok", "message": f"Carrier account '{account.name}' deleted"}


@router.post("/carrier-accounts/{account_id}/test-connection", response_model=CarrierConnectionTestResponse)
async def test_carrier_connection(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only),
    company_id: int = Depends(get_current_company_id),
):
    """Validate the stored credential against the provider.

    SAFETY: this is the ONLY carrier round-trip NOT gated by
    ``allow_carrier_egress`` -- it transmits no customer data, only authenticates
    the stored key. Tenant-scoped (the service loads the company's own account)."""
    # Verify the account exists for this tenant (clean 404 vs the service's
    # generic CarrierError) before the credential round-trip.
    account = _get_account(db, account_id, company_id)
    service = ShippingService(db)
    ok, message = await service.test_connection(company_id, account_id)
    return CarrierConnectionTestResponse(ok=ok, provider=account.provider, message=message)


# ===========================================================================
# Company shipping profile (ship-from + egress kill switch).
# ===========================================================================


@router.get("/shipping-profile", response_model=CompanyShippingProfileResponse)
def get_shipping_profile(
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only),
    company_id: int = Depends(get_current_company_id),
):
    """Return the company shipping profile (404 until one is created via PUT)."""
    profile = db.query(CompanyShippingProfile).filter(CompanyShippingProfile.company_id == company_id).first()
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shipping profile not configured")
    return CompanyShippingProfileResponse.model_validate(profile)


@router.put("/shipping-profile", response_model=CompanyShippingProfileResponse)
def upsert_shipping_profile(
    data: CompanyShippingProfileUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create or update the company shipping profile.

    SAFETY: ``allow_carrier_egress`` is the customer-data egress kill switch. It
    is created OFF (the model + create schema default False) and only flips when
    an admin explicitly sets it here; the egress toggle is audited as a status
    change so enabling/disabling egress is on the tamper-evident trail."""
    profile = db.query(CompanyShippingProfile).filter(CompanyShippingProfile.company_id == company_id).first()
    update = data.model_dump(exclude_unset=True)
    is_create = profile is None
    previous_egress: Optional[bool] = None

    if profile is None:
        # New profile: egress defaults OFF unless explicitly enabled in this call.
        profile = CompanyShippingProfile(allow_carrier_egress=False)
        profile.company_id = company_id
        db.add(profile)
    else:
        previous_egress = profile.allow_carrier_egress

    for field, value in update.items():
        if field == "allow_carrier_egress" and value is None:
            continue
        setattr(profile, field, value)

    db.flush()

    if is_create:
        audit.log_create(
            resource_type="company_shipping_profile",
            resource_id=profile.id,
            resource_identifier=f"company:{company_id}",
            new_values={"allow_carrier_egress": profile.allow_carrier_egress},
            description="Company shipping profile created",
        )
    else:
        audit.log_update(
            resource_type="company_shipping_profile",
            resource_id=profile.id,
            resource_identifier=f"company:{company_id}",
            old_values={"allow_carrier_egress": previous_egress},
            new_values={"allow_carrier_egress": profile.allow_carrier_egress},
            description="Company shipping profile updated",
        )

    # The egress kill switch flipping is a security-relevant status change --
    # record it explicitly on the tamper-evident trail (separate from the field
    # diff above) whenever the value actually changed.
    if previous_egress is not None and previous_egress != profile.allow_carrier_egress:
        audit.log_status_change(
            "company_shipping_profile",
            profile.id,
            f"company:{company_id}",
            "egress_enabled" if previous_egress else "egress_disabled",
            "egress_enabled" if profile.allow_carrier_egress else "egress_disabled",
            description=(
                "Carrier customer-data egress "
                f"{'ENABLED' if profile.allow_carrier_egress else 'DISABLED'} for company {company_id}"
            ),
        )

    db.commit()
    db.refresh(profile)
    return CompanyShippingProfileResponse.model_validate(profile)
