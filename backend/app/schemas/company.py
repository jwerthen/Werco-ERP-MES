from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.schemas.base import UTCModel
from app.schemas.user import validate_password_strength


class CompanyBase(UTCModel):
    name: str = Field(..., min_length=1, max_length=255, description="Company name")
    slug: Optional[str] = Field(
        None, max_length=100, description="URL-safe identifier (auto-generated if not provided)"
    )
    logo_url: Optional[str] = Field(None, max_length=500)
    timezone: str = Field(default="America/Chicago", max_length=50)
    address: Optional[str] = None
    phone: Optional[str] = Field(None, max_length=50)
    website: Optional[str] = Field(None, max_length=255)


class CompanyRegister(BaseModel):
    """Self-registration: creates a new company + admin user"""

    company_name: str = Field(..., min_length=1, max_length=255)
    admin_email: EmailStr
    admin_first_name: str = Field(..., min_length=1, max_length=50)
    admin_last_name: str = Field(..., min_length=1, max_length=50)
    admin_password: str = Field(..., min_length=12, max_length=128)

    @field_validator('admin_password')
    @classmethod
    def validate_password(cls, v: str) -> str:
        # Reuse the canonical AS9100D/CMMC strength policy (schemas.user) so the
        # UNAUTHENTICATED self-registration path can't accept a weaker first-admin
        # password than /auth/register. Previously omitted the common-substring
        # check, so "Password1234!" was accepted here.
        return validate_password_strength(v)


class CompanyCreate(CompanyBase):
    """Platform admin creates a company + initial admin"""

    admin_email: EmailStr
    admin_first_name: str = Field(..., min_length=1, max_length=50)
    admin_last_name: str = Field(..., min_length=1, max_length=50)
    admin_password: str = Field(..., min_length=12, max_length=128)
    parent_company_id: Optional[int] = None

    @field_validator('admin_password')
    @classmethod
    def validate_password(cls, v: str) -> str:
        # Platform-admin company creation must meet the same first-admin strength
        # policy as self-registration; without this the POST /platform/companies
        # path accepted any 12-char password (no complexity check at all).
        return validate_password_strength(v)


class CompanyUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=255)
    logo_url: Optional[str] = Field(None, max_length=500)
    timezone: Optional[str] = Field(None, max_length=50)
    address: Optional[str] = None
    phone: Optional[str] = Field(None, max_length=50)
    website: Optional[str] = Field(None, max_length=255)
    is_active: Optional[bool] = None


class CompanyResponse(CompanyBase):
    id: int
    is_active: bool
    allow_ai_egress: bool
    allow_sms_egress: bool = False
    # Badge sign-on policy (AS9100D 8.5.2): a badge user with no floor PIN is refused.
    require_floor_pin: bool = False
    parent_company_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    user_count: Optional[int] = None

    class Config:
        from_attributes = True


class CompanyAIEgressUpdate(BaseModel):
    """Request body for the dedicated AI-egress kill-switch toggle."""

    allow_ai_egress: bool = Field(..., description="Allow outbound AI document-extraction egress to the Anthropic API")


class CompanySMSEgressUpdate(BaseModel):
    """Request body for the dedicated SMS-egress kill-switch toggle."""

    allow_sms_egress: bool = Field(..., description="Allow outbound notification SMS egress to Twilio")


class CompanyFloorPinPolicyUpdate(BaseModel):
    """Request body for the badge-sign-on PIN policy toggle."""

    require_floor_pin: bool = Field(
        ...,
        description="Require a floor PIN at badge / employee-ID sign-on; a badge user with no PIN is then refused",
    )


class CompanyFloorPinPolicyResponse(CompanyResponse):
    """The company record after the policy change, plus how many badge users have no PIN yet."""

    users_without_pin: int = Field(
        ...,
        description=(
            "Active users of the company with an employee ID and no floor PIN, platform accounts excluded "
            "(refused at badge sign-on while the policy is on)"
        ),
    )


class CompanyListResponse(BaseModel):
    id: int
    name: str
    slug: str
    logo_url: Optional[str] = None
    is_active: bool
    user_count: int = 0
    active_work_orders: int = 0

    class Config:
        from_attributes = True
