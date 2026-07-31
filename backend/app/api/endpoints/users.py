import re
import secrets
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.core.security import get_password_hash, verify_password
from app.db.database import get_db
from app.models.company import Company
from app.models.notification import NotificationLog, NotificationPreference
from app.models.user import User, UserRole
from app.schemas.notification import (
    NotificationPreferencesResponse,
    NotificationPreferencesUpdate,
    TestSMSResponse,
)
from app.schemas.user import validate_password_strength
from app.services.audit_service import AuditService
from app.services.import_service import ImportFileError, parse_import_file
from app.services.notification_catalog import ALL_CHANNELS, CATALOG, CHANNEL_SMS, get_entry
from app.services.notification_dispatch import channels_from_pref, get_preference_row
from app.services.sms_content import build_test_sms_body
from app.services.sms_service import (
    SMS_TEST_HOURLY_CAP_PER_USER,
    TEST_QUOTA_CAPPED,
    TEST_QUOTA_UNAVAILABLE,
    InvalidPhoneNumberError,
    SMSEgressDisabledError,
    SMSPermanentError,
    normalize_phone,
    reserve_test_sms_quota,
    scrub_phone_numbers,
    send_sms,
    sms_configured,
)

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


class UserPhoneUpdate(BaseModel):
    """Self-service phone number change. ``None``/empty clears the number."""

    phone: Optional[str] = Field(None, max_length=32, description="Phone number; stored normalized to E.164")


class UserResponse(BaseModel):
    """User payload for the SELF profile and ADMIN/MANAGER user-management routes.

    FIELD MINIMIZATION (§8.12): this is the ONLY user schema that carries ``phone``,
    and every route using it is either self-scoped (``GET /users/me``, the self-service
    routes below) or gated to ADMIN/MANAGER user management. General user serialization
    goes through ``app.schemas.user.UserResponse`` (auth/token/platform browse) and the
    per-domain ``UserSummary``-style schemas, none of which expose a phone number.
    """

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
    """Generate a strong password for users authenticating by employee ID.

    Validated rather than assumed compliant: the strength policy is length plus a
    substring blocklist, and a random token can incidentally contain a blocklisted
    substring. Regenerate until the value actually passes.
    """
    for _ in range(10):
        candidate = f"Auto!{secrets.token_urlsafe(18)}1aA"
        try:
            return validate_password_strength(candidate)
        except ValueError:
            continue
    # Unreachable in practice (each attempt fails with probability ~1e-5).
    raise RuntimeError("Could not generate a policy-compliant system password")


def _normalized_phone_or_400(raw: Optional[str]) -> Optional[str]:
    """Validate + normalize a phone number to E.164, or ``None`` when cleared.

    Storage is E.164 only (§3.4) so the SMS transport never has to guess a country
    code. An unparseable number is a 400 rather than a silently-stored string that
    would fail at send time.
    """
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    try:
        return normalize_phone(value)
    except InvalidPhoneNumberError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _effective_preferences(pref: Optional[NotificationPreference]) -> Dict[str, Dict[str, bool]]:
    """Resolve every catalog event's channels exactly as the dispatcher would.

    Uses the dispatcher's own ``channels_from_pref`` so the settings UI and the delivery
    path can never disagree. Read-only: it NEVER creates a preference row (§3.7/§9.8).
    """
    return {
        event_key: {channel: channel in channels_from_pref(pref, entry) for channel in sorted(ALL_CHANNELS)}
        for event_key, entry in CATALOG.items()
    }


def _default_channel_map(entry) -> Dict[str, bool]:
    """Materialize a catalog entry's defaults into the persisted 4-key channel shape.

    The mandatory channel is deliberately NOT forced on here — the stored row records
    the user's own choice, and the dispatcher re-applies mandatory at send time, so a
    later catalog change to the mandatory set takes effect without rewriting rows.
    """
    return {channel: channel in entry.default_channels for channel in sorted(ALL_CHANNELS)}


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
    """Get current user info (self-profile — the one general route that exposes phone)."""
    return current_user


# ---------------------------------------------------------------------------
# Self-service profile + notification settings (My Settings)
#
# All routes here are SELF-scoped: they read/write only ``current_user`` and never
# accept a user id, so no role gate beyond authentication is required and no user can
# reach another user's phone or preferences.
# ---------------------------------------------------------------------------


@router.put("/me/phone", response_model=UserResponse, summary="Set your own phone number")
def update_my_phone(
    payload: UserPhoneUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    audit: AuditService = Depends(get_audit_service),
):
    """Set or clear the current user's phone number (stored E.164, audited).

    The number is the target of every SMS notification, so the change is recorded on
    the tamper-evident trail — a silently redirected alert channel would be an audit
    gap. Sending remains gated by the company's ``allow_sms_egress`` kill switch and by
    per-event SMS opt-in; a phone alone sends nothing.
    """
    new_phone = _normalized_phone_or_400(payload.phone)
    previous = current_user.phone
    if new_phone == previous:
        return current_user

    current_user.phone = new_phone
    audit.log_update(
        "user",
        current_user.id,
        current_user.employee_id,
        old_values={"phone": previous},
        new_values={"phone": new_phone},
        description=f"Updated own phone number for user {current_user.employee_id}",
        extra_data={"source": "self_service"},
    )
    db.commit()
    db.refresh(current_user)
    return current_user


@router.get(
    "/me/notification-preferences",
    response_model=NotificationPreferencesResponse,
    summary="Get your effective notification preferences",
)
def get_my_notification_preferences(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Return the channel matrix the dispatcher would apply for this user right now.

    Read-only and NON-creating: a user who has never saved preferences simply sees the
    catalog defaults (no ``NotificationPreference`` row is written — §3.7/§9.8).
    ``sms_egress_enabled`` / ``sms_configured`` / ``phone`` let the UI explain why an
    SMS toggle would currently be inert.
    """
    pref = get_preference_row(db, current_user.id)
    allow_sms = db.query(Company.allow_sms_egress).filter(Company.id == company_id).scalar()
    return NotificationPreferencesResponse(
        preferences=_effective_preferences(pref),
        has_saved_preferences=pref is not None,
        phone=current_user.phone,
        sms_egress_enabled=bool(allow_sms),
        sms_configured=sms_configured(),
    )


@router.put(
    "/me/notification-preferences",
    response_model=NotificationPreferencesResponse,
    summary="Update your notification preferences (PR 4 scope: the SMS channel)",
)
def update_my_notification_preferences(
    payload: NotificationPreferencesUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Save the current user's SMS opt-ins (audited).

    Scope: this PR owns the **SMS** channel only — no catalog event ships SMS in its
    defaults, so without an explicit opt-in here the SMS leg is unreachable. The row it
    writes keeps the full ``{in_app, email, sms, digest}`` shape per event (seeded from
    catalog defaults for events the user has never touched), so PR 3's complete matrix
    extends the same rows without a migration.

    This is the ONLY place a ``NotificationPreference`` row is created, and it stamps
    ``company_id`` from the active company (the TenantMixin column that today's
    auto-create path omits — §9.8).
    """
    unknown = sorted(key for key in payload.preferences if get_entry(key) is None)
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown notification event(s): {', '.join(unknown)}")

    not_eligible = sorted(
        key for key, change in payload.preferences.items() if change.sms and not get_entry(key).sms_eligible
    )
    if not_eligible:
        raise HTTPException(
            status_code=400,
            detail=f"SMS is not available for event(s): {', '.join(not_eligible)}",
        )

    pref = get_preference_row(db, current_user.id)
    created = pref is None
    if pref is None:
        pref = NotificationPreference(user_id=current_user.id, preferences={})
        # TenantMixin column is non-null: stamp it from the ACTIVE company.
        pref.company_id = company_id
        db.add(pref)

    stored = dict(pref.preferences) if isinstance(pref.preferences, dict) else {}
    previous = {key: dict(value) for key, value in stored.items() if isinstance(value, dict)}

    for event_key, change in payload.preferences.items():
        entry = get_entry(event_key)
        base = stored.get(event_key)
        if not isinstance(base, dict):
            base = _default_channel_map(entry)
        merged = {channel: bool(base.get(channel, False)) for channel in sorted(ALL_CHANNELS)}
        merged[CHANNEL_SMS] = change.sms
        stored[event_key] = merged

    # Reassign (not mutate) so SQLAlchemy detects the JSON change.
    pref.preferences = stored
    db.flush()

    audit.log_update(
        "notification_preference",
        pref.id,
        current_user.employee_id,
        old_values={"preferences": previous},
        new_values={"preferences": stored},
        description=(
            f"{'Created' if created else 'Updated'} notification preferences for user {current_user.employee_id}"
        ),
        extra_data={"source": "self_service", "changed_events": sorted(payload.preferences.keys())},
    )
    db.commit()
    db.refresh(pref)

    allow_sms = db.query(Company.allow_sms_egress).filter(Company.id == company_id).scalar()
    return NotificationPreferencesResponse(
        preferences=_effective_preferences(pref),
        has_saved_preferences=True,
        phone=current_user.phone,
        sms_egress_enabled=bool(allow_sms),
        sms_configured=sms_configured(),
    )


@router.post("/me/test-sms", response_model=TestSMSResponse, summary="Send yourself a test SMS")
async def send_test_sms(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Send a test SMS to the current user's own number.

    Self-only: the destination is ``current_user.phone`` and can never be supplied by
    the caller, so this endpoint cannot be used to message an arbitrary number. It goes
    through the same ``sms_service`` path as real notifications, so the
    ``allow_sms_egress`` kill switch is enforced fail-closed here too.

    Bounded twice: per-IP in ``main.py`` (ENDPOINT_RATE_LIMITS) and — because the per-IP
    limit keys on address alone, so one account can multiply it by rotating egress IPs
    and it is disabled entirely wherever ``RATE_LIMIT_ENABLED=false`` — per-identity via
    :func:`reserve_test_sms_quota`. Every attempt is logged to ``notification_logs``.
    """
    if not current_user.phone:
        raise HTTPException(status_code=400, detail="Add a phone number before sending a test message")

    quota = await reserve_test_sms_quota(current_user.id)
    if quota == TEST_QUOTA_CAPPED:
        raise HTTPException(
            status_code=429,
            detail=f"Test-message limit reached ({SMS_TEST_HOURLY_CAP_PER_USER} per hour). Try again later.",
        )
    if quota == TEST_QUOTA_UNAVAILABLE:
        # Refuse rather than send unmetered, but don't claim a limit the user never hit.
        raise HTTPException(
            status_code=503,
            detail="Test messaging is temporarily unavailable. Try again shortly.",
        )

    body = build_test_sms_body()
    # Persist the attempt BEFORE the outbound call so the delivery log records it even
    # if the provider call (or this process) dies mid-flight.
    log = NotificationLog(
        company_id=company_id,
        user_id=current_user.id,
        event_type="sms.test",
        channel=CHANNEL_SMS,
        subject="Test SMS",
        body=body,
        sent=False,
    )
    db.add(log)
    db.commit()

    def _fail(error: str, status_code: int, detail: str) -> HTTPException:
        log.error = error
        db.commit()
        return HTTPException(status_code=status_code, detail=detail)

    try:
        result = await send_sms(db=db, company_id=company_id, to=current_user.phone, body=body)
    except SMSEgressDisabledError:
        raise _fail(
            "SMS egress is disabled for this company",
            400,
            "SMS is turned off for this company. An admin can enable it in Admin Settings.",
        )
    except InvalidPhoneNumberError as exc:
        # Scrubbed on the way into notification_logs.error (SUPERVISOR can read that
        # field but not `phone`); the caller is the number's owner, so the HTTP detail
        # they get back is unscrubbed.
        raise _fail(f"invalid phone number on file: {scrub_phone_numbers(str(exc))}", 400, str(exc))
    except SMSPermanentError as exc:
        raise _fail(
            f"provider rejected the message: {scrub_phone_numbers(str(exc))}",
            502,
            "The SMS provider rejected the message. Check the number and try again.",
        )
    except Exception:
        raise _fail(
            "transport failure",
            502,
            "Could not reach the SMS provider. Try again shortly.",
        )

    log.sent = result.sent
    log.provider_message_id = result.sid
    log.provider_status = result.provider_status
    log.error = None if result.sent else f"skipped: {result.reason}"
    db.commit()

    if not result.sent:
        return TestSMSResponse(
            status=result.status,
            detail="SMS is not configured on this server. Ask an administrator to finish Twilio setup.",
        )
    return TestSMSResponse(
        status=result.status,
        sid=result.sid,
        provider_status=result.provider_status,
        detail="Test message sent.",
    )


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
        # Previously dropped on the floor (the schema field was a phantom, §9.4).
        phone=_normalized_phone_or_400(user_in.phone),
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

    # Phone now persists (it was a phantom field, §9.4) -- normalize to E.164 so the
    # admin path can't store a number the SMS transport would reject at send time.
    if "phone" in update_data:
        update_data["phone"] = _normalized_phone_or_400(update_data["phone"])

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
